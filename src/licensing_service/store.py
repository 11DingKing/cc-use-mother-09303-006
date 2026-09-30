"""SQLite 仓储层。

表概览
------
users                      受权人员，归属一个机构，具备角色
institutions               院校机构（权利主体/组包方/接收方）
resources                  资源摘要（不可变描述 + 当前指针）
resource_versions          资源版本与内容哈希、版本依赖（depends_on_json）
licenses                   授权（权利主体→资源），地域/机构/期限/部分授权
                           current_version 随授权更新而追加，旧版本保留谱系
license_grants             部分授权：逐用途/逐要素的许可与禁止项
recipients                 接收方资格（机构、地域、资质、有效期）
packages                   课程包：含固定授权快照
package_items              包内条目，记录快照时的授权/版本/替代链
package_lineage            谱系事件：撤回、替代、部分授权、重复组包
package_attempts           组包尝试；失败尝试保留可审计记录但无产物
deliveries                 交付记录（含交付时再核验结果）
events                     领域事件外发日志（影响分析与审计使用）

访问控制
--------
机构隔离在查询层统一执行：所有按 ID 取资源/授权/包的仓储方法都要求
``viewer_org``，资源/包不属于其可见范围时按不存在处理（404），
使其他机构无权接触的内容既不可读也不可探测。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS institutions (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES institutions(id),
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('provider','copyright','recipient','admin')),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id              TEXT PRIMARY KEY,
    owner_org_id    TEXT NOT NULL REFERENCES institutions(id),
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_versions (
    resource_id     TEXT NOT NULL REFERENCES resources(id),
    version         INTEGER NOT NULL,
    content_digest  TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (resource_id, version)
);

CREATE TABLE IF NOT EXISTS licenses (
    id                  TEXT PRIMARY KEY,
    resource_id         TEXT NOT NULL REFERENCES resources(id),
    licensor_org_id     TEXT NOT NULL REFERENCES institutions(id),
    scope               TEXT NOT NULL CHECK (scope IN ('full','partial')),
    territories_json    TEXT NOT NULL,
    org_ids_json        TEXT NOT NULL,
    valid_from          TEXT NOT NULL,
    valid_until         TEXT,
    current_version     INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('active','superseded','revoked')),
    supersedes_id      TEXT REFERENCES licenses(id),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_licenses_resource ON licenses(resource_id);

CREATE TABLE IF NOT EXISTS license_grants (
    id          TEXT PRIMARY KEY,
    license_id  TEXT NOT NULL REFERENCES licenses(id),
    subject     TEXT NOT NULL,          -- 用途/要素，如 "校内教学"、"实训手册第3章"
    permitted   INTEGER NOT NULL,       -- 1 许可 / 0 禁止
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_license ON license_grants(license_id);

CREATE TABLE IF NOT EXISTS recipients (
    id              TEXT PRIMARY KEY,
    owner_org_id    TEXT NOT NULL REFERENCES institutions(id),
    org_id          TEXT NOT NULL REFERENCES institutions(id),
    territory       TEXT NOT NULL,
    qualifications_json TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_until     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('qualified','suspended','expired')),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipients_org ON recipients(org_id);

CREATE TABLE IF NOT EXISTS packages (
    id                  TEXT PRIMARY KEY,
    owner_org_id        TEXT NOT NULL REFERENCES institutions(id),
    name                TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('verified','failed','delivered')),
    attempt_no          INTEGER NOT NULL,
    supersedes_package_id TEXT REFERENCES packages(id),
    snapshot_digest     TEXT,
    snapshot_json       TEXT,
    artifact_digest     TEXT,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    delivered_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_packages_owner ON packages(owner_org_id);

CREATE TABLE IF NOT EXISTS package_items (
    id              TEXT PRIMARY KEY,
    package_id      TEXT NOT NULL REFERENCES packages(id),
    resource_id     TEXT NOT NULL,
    resource_version INTEGER NOT NULL,
    license_id      TEXT NOT NULL,
    replaced_license_id TEXT,
    item_digest     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_package ON package_items(package_id);
CREATE INDEX IF NOT EXISTS idx_items_resource ON package_items(resource_id);

CREATE TABLE IF NOT EXISTS package_lineage (
    id          TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL REFERENCES packages(id),
    event_type  TEXT NOT NULL CHECK (event_type IN
                ('built','rebuild','license_revoked','material_replaced',
                 'partial_grant','delivery')),
    detail_json TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_package ON package_lineage(package_id);

CREATE TABLE IF NOT EXISTS package_attempts (
    id          TEXT PRIMARY KEY,
    package_id  TEXT,                      -- 失败时尚未建包，可为空
    name        TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    violations_json TEXT NOT NULL,
    artifact_path   TEXT,                 -- 仅成功时存在；失败恒为 NULL
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id          TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL REFERENCES packages(id),
    recipient_id TEXT NOT NULL REFERENCES recipients(id),
    artifact_digest TEXT NOT NULL,
    recheck_digest TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('delivered','blocked')),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_package ON deliveries(package_id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_type, subject_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_to_dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


class Store:
    """线程安全的 SQLite 仓储；每个工作线程持有独立连接。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.memory = self.path == ":memory:"
        if not self.memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        self._lock = threading.RLock()
        if self.memory:
            # 内存库每连接互相隔离，因此全仓储共用单连接，靠 RLock 串行化。
            self._mem_conn = self._connect()
            self._mem_conn.executescript(SCHEMA)
            self._mem_conn.commit()
        else:
            self._master = self._connect()
            self._master.executescript(SCHEMA)
            self._master.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self.memory:
            return self._mem_conn
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._connect()
            self._tls.conn = conn
        return conn

    # ---- 通用辅助 -----------------------------------------------------

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        cur = self.conn.execute(sql, tuple(params))
        rows = rows_to_dicts(cur)
        return rows[0] if rows else None

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return rows_to_dicts(self.conn.execute(sql, tuple(params)))

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, [tuple(p) for p in seq])

    def begin(self) -> "Transaction":
        return Transaction(self)

    def log_event(self, event_type: str, subject_type: str, subject_id: str,
                  payload: dict[str, Any], actor_id: str) -> None:
        self.execute(
            "INSERT INTO events(event_type, subject_type, subject_id, payload_json, actor_id, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (event_type, subject_type, subject_id,
             json.dumps(payload, ensure_ascii=False), actor_id, utcnow()),
        )


class Transaction:
    """显式事务上下文，异常回滚保证“失败不留半成品”。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def __enter__(self) -> Store:
        self.store.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.store.conn.commit()
        else:
            self.store.conn.rollback()
