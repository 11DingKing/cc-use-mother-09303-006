"""SQLite 持久化层。

设计要点：

- licenses 只保留每项资源当前生效的授权（head）；每次变更都追加进
  license_history，历史永不更新、永不删除，承载撤回/替代/部分授权的谱系。
- package_builds / package_items 一旦以 SUCCEEDED 写入即冻结；FAILED 构建
  不产生任何制品行与下载物，只落一条失败记录。
- lineage_events 是统一的谱系时间线（授权变更与组包事件互相关联）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .clock import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    resource_id    TEXT PRIMARY KEY,
    owner_org_id   TEXT NOT NULL,
    title          TEXT NOT NULL,
    digest         TEXT NOT NULL,
    metadata_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS resource_dependencies (
    resource_id    TEXT NOT NULL REFERENCES resources(resource_id),
    dep_resource_id TEXT NOT NULL,
    dep_version    TEXT,
    required       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (resource_id, dep_resource_id)
);

CREATE TABLE IF NOT EXISTS license_history (
    license_id     TEXT PRIMARY KEY,
    resource_id    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    change_type    TEXT NOT NULL CHECK (change_type IN ('GRANT','PARTIAL_GRANT','REPLACEMENT','REVOCATION')),
    supersedes     TEXT REFERENCES license_history(license_id),
    rights_holder  TEXT NOT NULL,
    territories_json TEXT NOT NULL,
    org_scope_json   TEXT NOT NULL,
    recipient_qual_json TEXT NOT NULL,
    recipient_orgs_json TEXT NOT NULL,
    valid_from     TEXT,
    valid_until    TEXT,
    basis          TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED','SUPERSEDED')),
    superseded_at  TEXT,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    replaced_resource_id TEXT,
    revokes_license_id  TEXT,
    note           TEXT,
    CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from)
);
CREATE INDEX IF NOT EXISTS idx_license_history_resource ON license_history(resource_id, version);

-- 当前 head：每项资源至多一条 ACTIVE 授权
CREATE TABLE IF NOT EXISTS licenses (
    resource_id    TEXT PRIMARY KEY REFERENCES resources(resource_id),
    license_id     TEXT NOT NULL UNIQUE REFERENCES license_history(license_id),
    version        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS package_builds (
    package_id     TEXT PRIMARY KEY,
    owner_org_id   TEXT NOT NULL,
    name           TEXT NOT NULL,
    idempotency_key TEXT,
    status         TEXT NOT NULL CHECK (status IN ('SUCCEEDED','FAILED')),
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    failure_reasons_json TEXT,
    snapshot_digest TEXT,
    item_count     INTEGER,
    artifact_path  TEXT,
    artifact_digest TEXT,
    artifact_size  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_packages_owner ON package_builds(owner_org_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_packages_idempotency
    ON package_builds(owner_org_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND status = 'SUCCEEDED';

CREATE TABLE IF NOT EXISTS package_items (
    package_id     TEXT NOT NULL REFERENCES package_builds(package_id),
    resource_id    TEXT NOT NULL,
    license_id     TEXT NOT NULL,
    license_version INTEGER NOT NULL,
    snapshot_json  TEXT NOT NULL,
    PRIMARY KEY (package_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_package_items_license ON package_items(license_id);
CREATE INDEX IF NOT EXISTS idx_package_items_resource ON package_items(resource_id);

CREATE TABLE IF NOT EXISTS lineage_events (
    event_id       TEXT PRIMARY KEY,
    resource_id    TEXT NOT NULL,
    package_id     TEXT,
    license_id     TEXT,
    event_type     TEXT NOT NULL CHECK (event_type IN (
                       'GRANT','PARTIAL_GRANT','REPLACEMENT','REVOCATION',
                       'PACKAGED','BUILD_REJECTED')),
    occurred_at    TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    linked_license_id TEXT,
    linked_package_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_lineage_resource ON lineage_events(resource_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_lineage_package ON lineage_events(package_id);
CREATE INDEX IF NOT EXISTS idx_lineage_license ON lineage_events(linked_license_id);
"""


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # check_same_thread=False：HTTP 层用全局 service_lock 串行化所有写入，
        # 连接可由工作线程共享。
        self.conn = sqlite3.connect(
            str(path), detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL") if str(path) != ":memory:" else None
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 基础工具 -------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        return cur

    def commit(self) -> None:
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- 资源 -----------------------------------------------------------

    def insert_resource(self, row: dict, dependencies: Iterable[dict]) -> None:
        self.conn.execute(
            """INSERT INTO resources
               (resource_id, owner_org_id, title, digest, metadata_json, created_at, version)
               VALUES (:resource_id, :owner_org_id, :title, :digest, :metadata_json, :created_at, 1)""",
            row,
        )
        for dep in dependencies:
            self.conn.execute(
                """INSERT INTO resource_dependencies
                   (resource_id, dep_resource_id, dep_version, required)
                   VALUES (:resource_id, :dep_resource_id, :dep_version, :required)""",
                dep,
            )

    def get_resource(self, resource_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
        ).fetchone()

    def list_dependencies(self, resource_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM resource_dependencies WHERE resource_id = ? ORDER BY dep_resource_id",
            (resource_id,),
        ).fetchall())

    def list_resources(self, owner_org_id: str | None = None) -> list[sqlite3.Row]:
        if owner_org_id is None:
            return list(self.conn.execute("SELECT * FROM resources ORDER BY resource_id"))
        return list(self.conn.execute(
            "SELECT * FROM resources WHERE owner_org_id = ? ORDER BY resource_id",
            (owner_org_id,),
        ))

    # ---- 授权 -----------------------------------------------------------

    def next_license_version(self, resource_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM license_history WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()
        return int(row["v"]) + 1

    def insert_license_history(self, rec: dict) -> None:
        cols = (
            "license_id", "resource_id", "version", "change_type", "supersedes",
            "rights_holder", "territories_json", "org_scope_json",
            "recipient_qual_json", "recipient_orgs_json",
            "valid_from", "valid_until", "basis", "status",
            "superseded_at", "created_by", "created_at", "snapshot_digest",
            "replaced_resource_id", "revokes_license_id", "note",
        )
        placeholders = ", ".join(f":{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO license_history ({', '.join(cols)}) VALUES ({placeholders})",
            {c: rec.get(c) for c in cols},
        )

    def get_history_row(self, license_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM license_history WHERE license_id = ?", (license_id,)
        ).fetchone()

    def mark_superseded(self, license_id: str, when: str) -> None:
        self.conn.execute(
            "UPDATE license_history SET status='SUPERSEDED', superseded_at=? WHERE license_id=?",
            (when, license_id),
        )

    def mark_revoked(self, license_id: str, when: str) -> None:
        self.conn.execute(
            "UPDATE license_history SET status='REVOKED', superseded_at=? WHERE license_id=?",
            (when, license_id),
        )

    def upsert_head(self, resource_id: str, license_id: str, version: int) -> None:
        self.conn.execute(
            """INSERT INTO licenses(resource_id, license_id, version) VALUES (?,?,?)
               ON CONFLICT(resource_id) DO UPDATE SET license_id=excluded.license_id,
                                                      version=excluded.version""",
            (resource_id, license_id, version),
        )

    def drop_head(self, resource_id: str) -> None:
        self.conn.execute("DELETE FROM licenses WHERE resource_id = ?", (resource_id,))

    def get_head_license(self, resource_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT h.* FROM licenses l JOIN license_history h ON h.license_id = l.license_id
               WHERE l.resource_id = ?""",
            (resource_id,),
        ).fetchone()

    def list_license_history(self, resource_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM license_history WHERE resource_id = ? ORDER BY version",
            (resource_id,),
        ))

    def get_active_license_by_id(self, license_id: str) -> sqlite3.Row | None:
        row = self.get_history_row(license_id)
        return row if row is not None and row["status"] == "ACTIVE" else None

    # ---- 谱系 -----------------------------------------------------------

    def insert_lineage(self, event: dict) -> None:
        self.conn.execute(
            """INSERT INTO lineage_events
               (event_id, resource_id, package_id, license_id, event_type,
                occurred_at, detail_json, linked_license_id, linked_package_id)
               VALUES (:event_id,:resource_id,:package_id,:license_id,:event_type,
                       :occurred_at,:detail_json,:linked_license_id,:linked_package_id)""",
            event,
        )

    def list_lineage(self, resource_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM lineage_events WHERE resource_id = ? ORDER BY occurred_at, event_id",
            (resource_id,),
        ))

    # ---- 包 -------------------------------------------------------------

    def get_build(self, package_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM package_builds WHERE package_id = ?", (package_id,)
        ).fetchone()

    def find_idempotent_build(self, owner_org_id: str, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM package_builds WHERE owner_org_id=? AND idempotency_key=? AND status='SUCCEEDED'",
            (owner_org_id, key),
        ).fetchone()

    def insert_build(self, rec: dict) -> None:
        cols = (
            "package_id", "owner_org_id", "name", "idempotency_key", "status",
            "created_by", "created_at", "failure_reasons_json", "snapshot_digest",
            "item_count", "artifact_path", "artifact_digest", "artifact_size",
        )
        placeholders = ", ".join(f":{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO package_builds ({', '.join(cols)}) VALUES ({placeholders})",
            {c: rec.get(c) for c in cols},
        )

    def attach_artifact(self, package_id: str, path: str, digest: str, size: int) -> None:
        self.conn.execute(
            "UPDATE package_builds SET artifact_path=?, artifact_digest=?, artifact_size=? WHERE package_id=?",
            (path, digest, size, package_id),
        )

    def insert_package_item(self, rec: dict) -> None:
        self.conn.execute(
            """INSERT INTO package_items
               (package_id, resource_id, license_id, license_version, snapshot_json)
               VALUES (:package_id,:resource_id,:license_id,:license_version,:snapshot_json)""",
            rec,
        )

    def list_package_items(self, package_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM package_items WHERE package_id=? ORDER BY resource_id",
            (package_id,),
        ))

    def list_packages(self, owner_org_id: str | None = None) -> list[sqlite3.Row]:
        if owner_org_id is None:
            return list(self.conn.execute(
                "SELECT * FROM package_builds ORDER BY created_at DESC, package_id"))
        return list(self.conn.execute(
            "SELECT * FROM package_builds WHERE owner_org_id=? ORDER BY created_at DESC, package_id",
            (owner_org_id,),
        ))

    def packages_referencing_license(self, license_id: str) -> list[sqlite3.Row]:
        """引用某条授权版本（直接或经由被其替代的旧版本）的所有冻结包。"""
        return list(self.conn.execute(
            """SELECT DISTINCT b.* FROM package_items i
               JOIN package_builds b ON b.package_id = i.package_id
               WHERE i.license_id = ? ORDER BY b.created_at, b.package_id""",
            (license_id,),
        ))

    def packages_referencing_resource(self, resource_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT b.* FROM package_items i JOIN package_builds b ON b.package_id=i.package_id
               WHERE i.resource_id=? ORDER BY b.created_at, b.package_id""",
            (resource_id,),
        ))

    def affected_packages_by_change(self, new_license_id: str, old_license_id: str | None) -> list[sqlite3.Row]:
        """受权利变化影响的已交付包：引用旧版本，或（撤回时）引用被撤版本。"""
        ids: set[str] = set()
        out: list[sqlite3.Row] = []
        for lic in (old_license_id, new_license_id):
            if not lic:
                continue
            for row in self.packages_referencing_license(lic):
                if row["package_id"] not in ids:
                    ids.add(row["package_id"])
                    out.append(row)
        out.sort(key=lambda r: (r["created_at"], r["package_id"]))
        return out

    @staticmethod
    def row_to_resource(row: sqlite3.Row) -> dict:
        return {
            "resource_id": row["resource_id"],
            "owner_org_id": row["owner_org_id"],
            "title": row["title"],
            "digest": row["digest"],
            "metadata": _loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def row_to_license(row: sqlite3.Row) -> dict:
        return {
            "license_id": row["license_id"],
            "resource_id": row["resource_id"],
            "version": row["version"],
            "change_type": row["change_type"],
            "supersedes": row["supersedes"],
            "rights_holder": row["rights_holder"],
            "territories": _loads(row["territories_json"]),
            "org_scope": _loads(row["org_scope_json"]),
            "recipient_qualification": _loads(row["recipient_qual_json"]),
            "recipient_orgs": _loads(row["recipient_orgs_json"]),
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "basis": row["basis"],
            "status": row["status"],
            "superseded_at": row["superseded_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "snapshot_digest": row["snapshot_digest"],
            "replaced_resource_id": row["replaced_resource_id"],
            "revokes_license_id": row["revokes_license_id"],
            "note": row["note"],
        }

    @staticmethod
    def now_iso(when: datetime) -> str:
        return iso(when)
