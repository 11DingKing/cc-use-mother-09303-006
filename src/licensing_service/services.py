"""领域服务：登记 / 授权 / 组包 / 交付 / 谱系 / 影响分析。

关键设计
========
1. 固定授权快照：组包核验全部通过后，把每项资源的版本、内容哈希、授权
   要素（地域、机构、期限、部分授权清单）规范化后哈希，写入 ``packages``。
   快照只增不改。
2. 失败不留半成品：组包先在临时文件写产物，数据库事务提交成功后才
   ``os.replace`` 到最终路径；任一步失败回滚并删除文件。失败尝试只留
   ``package_attempts`` 审计记录（``artifact_path`` 恒为 NULL）。
3. 谱系不删除：撤回、替代材料、部分授权、重复组包全部以
   ``package_lineage`` + ``events`` 追加记录；新版本通过
   ``supersedes_id`` / ``supersedes_package_id`` 串接。
4. 交付前再核验：用当前数据库状态重跑核验规则，并与快照（剔除时间戳后）
   比对，权利发生变化即阻断交付。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import access
from .canonical import canonicalize, content_digest, digest
from .errors import (
    AuthorizationError,
    ConflictError,
    DeliveryBlocked,
    NotFoundError,
    PackageVerificationFailed,
    ValidationError,
)
from .store import Store, Transaction, utcnow

TERRITORY_DOMESTIC = "CN"


def _uid() -> str:
    return uuid.uuid4().hex


def _loads(raw: Optional[str]) -> Any:
    return json.loads(raw) if raw else None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


class LicensingService:
    def __init__(self, store: Store, artifact_dir: str | Path,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.store = store
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return self._clock()

    # ===================================================================
    # 主体与人员
    # ===================================================================

    def create_institution(self, institution_id: str, name: str, country: str) -> dict[str, Any]:
        _require(institution_id and name and country, "机构信息不完整")
        try:
            self.store.execute(
                "INSERT INTO institutions(id,name,country,created_at) VALUES (?,?,?,?)",
                (institution_id, name, country, utcnow()),
            )
        except sqlite3.IntegrityError as exc:  # 唯一约束
            raise ConflictError(f"机构 {institution_id} 已存在") from exc
        return self.get_institution(institution_id)

    def get_institution(self, institution_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM institutions WHERE id=?", (institution_id,))
        if not row:
            raise NotFoundError(f"机构 {institution_id} 不存在")
        return row

    def create_user(self, user_id: str, org_id: str, display_name: str, role: str) -> dict[str, Any]:
        self.get_institution(org_id)
        _require(role in ("provider", "copyright", "recipient", "admin"), "角色非法")
        try:
            self.store.execute(
                "INSERT INTO users(id,org_id,display_name,role,created_at) VALUES (?,?,?,?,?)",
                (user_id, org_id, display_name, role, utcnow()),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"用户 {user_id} 已存在") from exc
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM users WHERE id=?", (user_id,))
        if not row:
            raise NotFoundError(f"用户 {user_id} 不存在")
        return row

    # ===================================================================
    # 资源登记与版本依赖
    # ===================================================================

    def register_resource(self, *, owner_org_id: str, title: str, kind: str, summary: str,
                          content: bytes, depends_on: Optional[list[str]] = None,
                          note: str = "", actor: dict[str, Any]) -> dict[str, Any]:
        self._require_role(actor, owner_org_id, ("provider", "admin"))
        _require(title and kind and summary, "资源摘要不完整")
        _require(isinstance(content, bytes) and len(content) > 0, "资源内容不能为空")
        depends_on = depends_on or []
        rid = _uid()
        now = utcnow()
        with Transaction(self.store):
            self.store.execute(
                "INSERT INTO resources(id,owner_org_id,title,kind,summary,current_version,"
                "created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                (rid, owner_org_id, title, kind, summary, now, now),
            )
            self.store.execute(
                "INSERT INTO resource_versions(resource_id,version,content_digest,size_bytes,"
                "depends_on_json,created_at,note) VALUES (?,1,?,?,?,?,?)",
                (rid, content_digest(content), len(content),
                 json.dumps(depends_on, ensure_ascii=False), now, note),
            )
            self.store.log_event("resource_registered", "resource", rid,
                                 {"title": title, "version": 1}, actor["id"])
        return self.get_resource(rid, viewer=actor)

    def new_resource_version(self, resource_id: str, *, content: bytes,
                             depends_on: Optional[list[str]] = None, note: str = "",
                             actor: dict[str, Any]) -> dict[str, Any]:
        """登记新版本：旧版本保留；已签发授权锁定旧版本，需重新授权后方可使用。"""
        resource = self._load_resource_raw(resource_id)
        self._require_role(actor, resource["owner_org_id"], ("provider", "admin"))
        depends_on = depends_on if depends_on is not None else _loads(
            self.store.query_one(
                "SELECT depends_on_json FROM resource_versions WHERE resource_id=? AND version=?",
                (resource_id, resource["current_version"]))["depends_on_json"])
        next_version = resource["current_version"] + 1
        now = utcnow()
        with Transaction(self.store):
            self.store.execute(
                "INSERT INTO resource_versions(resource_id,version,content_digest,size_bytes,"
                "depends_on_json,created_at,note) VALUES (?,?,?,?,?,?,?)",
                (resource_id, next_version, content_digest(content), len(content),
                 json.dumps(depends_on, ensure_ascii=False), now, note),
            )
            self.store.execute(
                "UPDATE resources SET current_version=?, updated_at=? WHERE id=?",
                (next_version, now, resource_id),
            )
            self.store.log_event("resource_versioned", "resource", resource_id,
                                 {"version": next_version}, actor["id"])
        return self.get_resource(resource_id, viewer=actor)

    def _load_resource_raw(self, resource_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM resources WHERE id=?", (resource_id,))
        if not row:
            raise NotFoundError(f"资源 {resource_id} 不存在")
        return row

    def get_resource(self, resource_id: str, *, viewer: dict[str, Any]) -> dict[str, Any]:
        """读取资源摘要；跨机构且无授权关系时按 404 处理（防探测）。"""
        row = self.store.query_one("SELECT * FROM resources WHERE id=?", (resource_id,))
        if not row or not self.can_see_resource(row, viewer):
            raise NotFoundError(f"资源 {resource_id} 不存在")
        return self._decorate_resource(row)

    def list_resources(self, viewer: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.store.query_all("SELECT * FROM resources ORDER BY created_at")
        return [self._decorate_resource(r) for r in rows if self.can_see_resource(r, viewer)]

    def _decorate_resource(self, row: dict[str, Any]) -> dict[str, Any]:
        versions = self.store.query_all(
            "SELECT version,content_digest,size_bytes,depends_on_json,created_at,note "
            "FROM resource_versions WHERE resource_id=? ORDER BY version", (row["id"],))
        for v in versions:
            v["depends_on"] = _loads(v.pop("depends_on_json"))
        return {**row, "versions": versions}

    def can_see_resource(self, resource: dict[str, Any], viewer: dict[str, Any]) -> bool:
        if viewer["role"] == "admin":
            return True
        if resource["owner_org_id"] == viewer["org_id"]:
            return True
        hit = self.store.query_one(
            "SELECT 1 FROM licenses WHERE resource_id=? AND status='active' AND ("
            "org_ids_json='\"*\"' OR EXISTS ("
            "SELECT 1 FROM json_each(org_ids_json) WHERE value=?) ) LIMIT 1",
            (resource["id"], viewer["org_id"]))
        return hit is not None

    # ===================================================================
    # 授权：地域 / 机构 / 期限 / 部分授权
    # ===================================================================

    def grant_license(self, *, resource_id: str, licensor_org_id: str, scope: str,
                      territories: list[str] | str, org_ids: list[str] | str,
                      valid_from: str, valid_until: Optional[str] = None,
                      grants: Optional[list[dict[str, Any]]] = None,
                      supersedes_id: Optional[str] = None,
                      actor: dict[str, Any]) -> dict[str, Any]:
        resource = self._load_resource_raw(resource_id)
        self.get_institution(licensor_org_id)
        self._require_role(actor, licensor_org_id, ("copyright", "admin"))
        _require(scope in ("full", "partial"), "授权范围必须是 full/partial")
        # 通配符 "*" 存为 JSON 字符串 '"*"'；否则存国家码/机构 ID 的非空数组
        if territories != "*":
            _require(isinstance(territories, list) and territories, "地域范围不合法")
        if org_ids != "*":
            _require(isinstance(org_ids, list) and org_ids, "机构范围不合法")
        self._validate_term(valid_from, valid_until)
        grants = grants or []
        if scope == "partial":
            _require(len(grants) > 0, "部分授权必须给出逐项许可清单")
            _require(all({"subject", "permitted"} <= g.keys() for g in grants),
                     "部分授权项需包含 subject/permitted")
        else:
            _require(all(g.get("permitted", True) for g in grants),
                      "完全授权不得包含禁止项")
        if supersedes_id:
            old = self._load_license_raw(supersedes_id)
            _require(old["resource_id"] == resource_id, "被替代授权不属于同一资源")

        lid = _uid()
        now = utcnow()
        version = resource["current_version"]
        with Transaction(self.store):
            self.store.execute(
                "INSERT INTO licenses(id,resource_id,licensor_org_id,scope,territories_json,"
                "org_ids_json,valid_from,valid_until,current_version,status,supersedes_id,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)",
                (lid, resource_id, licensor_org_id, scope,
                 json.dumps(territories, ensure_ascii=False),
                 json.dumps(org_ids, ensure_ascii=False),
                 valid_from, valid_until, version, supersedes_id, now),
            )
            for g in grants:
                self.store.execute(
                    "INSERT INTO license_grants(id,license_id,subject,permitted,created_at)"
                    " VALUES (?,?,?,?,?)",
                    (_uid(), lid, g["subject"], 1 if g["permitted"] else 0, now),
            )
            if supersedes_id:
                self.store.execute(
                    "UPDATE licenses SET status='superseded' WHERE id=? AND status='active'",
                    (supersedes_id,),
                )
            self.store.log_event(
                "partial_grant" if scope == "partial" else "license_granted",
                "license", lid,
                {"resource_id": resource_id, "scope": scope, "territories": territories,
                 "org_ids": org_ids, "valid_from": valid_from, "valid_until": valid_until,
                 "grants": grants, "supersedes_id": supersedes_id, "version": version},
                actor["id"],
            )
        return self.get_license(lid, viewer=actor)

    def revoke_license(self, license_id: str, reason: str, actor: dict[str, Any]) -> dict[str, Any]:
        """许可撤回：授权置 revoked（记录保留），事件驱动影响分析与交付阻断。"""
        lic = self._load_license_raw(license_id)
        self._require_role(actor, lic["licensor_org_id"], ("copyright", "admin"))
        if lic["status"] == "revoked":
            raise ConflictError("授权已处于撤回状态")
        with Transaction(self.store):
            self.store.execute("UPDATE licenses SET status='revoked' WHERE id=?", (license_id,))
            self.store.log_event("license_revoked", "license", license_id,
                                 {"resource_id": lic["resource_id"], "reason": reason},
                                 actor["id"])
            # 固化到所有引用该授权的已核验包谱系中
            affected = self.store.query_all(
                "SELECT package_id FROM package_items WHERE license_id=? OR replaced_license_id=?",
                (license_id, license_id))
            seen = {a["package_id"] for a in affected}
            for pid in seen:
                self.store.execute(
                    "INSERT INTO package_lineage(id,package_id,event_type,detail_json,actor_id,created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (_uid(), pid, "license_revoked",
                     json.dumps({"license_id": license_id, "reason": reason}, ensure_ascii=False),
                     actor["id"], utcnow()),
                )
        return self.get_license(license_id, viewer=actor)

    def _load_license_raw(self, license_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM licenses WHERE id=?", (license_id,))
        if not row:
            raise NotFoundError(f"授权 {license_id} 不存在")
        return row

    def _grants_of(self, license_id: str) -> list[dict[str, Any]]:
        rows = self.store.query_all(
            "SELECT subject, permitted FROM license_grants WHERE license_id=? ORDER BY subject",
            (license_id,))
        return [{"subject": r["subject"], "permitted": bool(r["permitted"])} for r in rows]

    def get_license(self, license_id: str, *, viewer: dict[str, Any]) -> dict[str, Any]:
        lic = self._load_license_raw(license_id)
        if not self.can_see_license(lic, viewer):
            raise NotFoundError(f"授权 {license_id} 不存在")
        return self._decorate_license(lic)

    def can_see_license(self, lic: dict[str, Any], viewer: dict[str, Any]) -> bool:
        if viewer["role"] == "admin":
            return True
        resource = self._load_resource_raw(lic["resource_id"])
        if viewer["org_id"] in (resource["owner_org_id"], lic["licensor_org_id"]):
            return True
        orgs = _loads(lic["org_ids_json"])
        return orgs == "*" or viewer["org_id"] in orgs

    def _decorate_license(self, lic: dict[str, Any]) -> dict[str, Any]:
        return {
            **lic,
            "territories": _loads(lic["territories_json"]),
            "org_ids": _loads(lic["org_ids_json"]),
            "grants": self._grants_of(lic["id"]),
        }

    def _validate_term(self, valid_from: str, valid_until: Optional[str]) -> None:
        start = datetime.strptime(valid_from, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if valid_until:
            end = datetime.strptime(valid_until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            _require(end > start, "授权终止时间必须晚于生效时间")

    # ===================================================================
    # 接收方资格
    # ===================================================================

    def register_recipient(self, *, owner_org_id: str, org_id: str, territory: str,
                           qualifications: list[str], valid_from: str,
                           valid_until: Optional[str], actor: dict[str, Any]) -> dict[str, Any]:
        self._require_role(actor, owner_org_id, ("provider", "admin"))
        self.get_institution(org_id)
        self._validate_term(valid_from, valid_until)
        rid = _uid()
        self.store.execute(
            "INSERT INTO recipients(id,owner_org_id,org_id,territory,qualifications_json,"
            "valid_from,valid_until,status,created_at) VALUES (?,?,?,?,?,?,?,'qualified',?)",
            (rid, owner_org_id, org_id, territory,
             json.dumps(qualifications, ensure_ascii=False), valid_from, valid_until, utcnow()),
        )
        return self.get_recipient(rid, viewer=actor)

    def suspend_recipient(self, recipient_id: str, actor: dict[str, Any]) -> dict[str, Any]:
        rec = self._load_recipient_raw(recipient_id)
        self._require_role(actor, rec["owner_org_id"], ("provider", "admin"))
        self.store.execute("UPDATE recipients SET status='suspended' WHERE id=?", (recipient_id,))
        return self.get_recipient(recipient_id, viewer=actor)

    def _load_recipient_raw(self, recipient_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM recipients WHERE id=?", (recipient_id,))
        if not row:
            raise NotFoundError(f"接收方 {recipient_id} 不存在")
        return row

    def get_recipient(self, recipient_id: str, *, viewer: dict[str, Any]) -> dict[str, Any]:
        rec = self._load_recipient_raw(recipient_id)
        if not (viewer["role"] == "admin" or rec["owner_org_id"] == viewer["org_id"]
                or rec["org_id"] == viewer["org_id"]):
            raise NotFoundError(f"接收方 {recipient_id} 不存在")
        rec = dict(rec)
        rec["qualifications"] = _loads(rec.pop("qualifications_json"))
        return rec

    # ===================================================================
    # 组包：逐项核验 + 固定授权快照 + 原子产物
    # ===================================================================

    def build_package(self, *, name: str, resource_ids: list[str], recipient_id: str,
                      actor: dict[str, Any],
                      license_selection: Optional[dict[str, str]] = None) -> dict[str, Any]:
        _require(name and resource_ids, "包名与资源列表不能为空")
        _require(len(set(resource_ids)) == len(resource_ids), "包内资源不能重复")
        recipient = self._load_recipient_raw(recipient_id)
        self._require_role(actor, recipient["owner_org_id"], ("provider", "admin"))
        license_selection = license_selection or {}
        attempt_id = _uid()
        now = utcnow()

        # 1) 组装核验上下文
        contexts: list[dict[str, Any]] = []
        dependency_map: dict[str, list[str]] = {}
        violations: list[dict[str, Any]] = []
        included = set(resource_ids)
        for rid in resource_ids:
            resource = self.store.query_one("SELECT * FROM resources WHERE id=?", (rid,))
            if not resource:
                violations.append({"rule": "resource_exists", "resource_id": rid,
                                   "message": "资源不存在"})
                continue
            ver = self.store.query_one(
                "SELECT * FROM resource_versions WHERE resource_id=? AND version=?",
                (rid, resource["current_version"]))
            dependency_map[rid] = _loads(ver["depends_on_json"])
            license_id = license_selection.get(rid)
            lic = None
            if license_id:
                lic = self.store.query_one("SELECT * FROM licenses WHERE id=? AND resource_id=?",
                                           (license_id, rid))
                if not lic:
                    violations.append({"rule": "license_exists", "resource_id": rid,
                                       "message": f"指定授权 {license_id} 不存在或不属于该资源"})
            else:
                lic = self.store.query_one(
                    "SELECT * FROM licenses WHERE resource_id=? AND status='active' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1", (rid,))
                if not lic:
                    violations.append({"rule": "license_exists", "resource_id": rid,
                                       "message": "资源不存在有效授权，禁止组包"})
            if lic:
                grants = self.store.query_all(
                    "SELECT subject,permitted FROM license_grants WHERE license_id=?",
                    (lic["id"],))
                grants = [{"subject": g["subject"], "permitted": bool(g["permitted"])} for g in grants]
                violations.extend(access.verify_item(
                    license_row=lic, grants=grants,
                    resource_version=resource["current_version"],
                    recipient=recipient, now=self._now()))
                contexts.append({"resource": resource, "version": ver, "license": lic, "grants": grants})

        # 2) 版本依赖图谱核验（含跨资源依赖）
        for rid in resource_ids:
            if rid in dependency_map:
                violations.extend(access.verify_dependencies(rid, included, dependency_map))

        # 3) 失败：只留审计记录，绝不写产物
        if violations:
            with Transaction(self.store):
                self.store.execute(
                    "INSERT INTO package_attempts(id,package_id,name,ok,violations_json,"
                    "artifact_path,created_by,created_at) VALUES (?,?,?,0,?,NULL,?,?)",
                    (attempt_id, None, name, json.dumps(violations, ensure_ascii=False),
                     actor["id"], now),
                )
            raise PackageVerificationFailed(violations, attempt_id)

        # 4) 通过：构造固定快照
        snapshot = {
            "schema": "license-snapshot/v1",
            "package_name": name,
            "recipient": {
                "id": recipient["id"], "owner_org_id": recipient["owner_org_id"],
                "org_id": recipient["org_id"],
                "territory": recipient["territory"],
                "qualifications": _loads(recipient["qualifications_json"]),
                "valid_from": recipient["valid_from"], "valid_until": recipient["valid_until"],
            },
            "items": [
                {
                    "resource_id": c["resource"]["id"],
                    "title": c["resource"]["title"],
                    "version": c["resource"]["current_version"],
                    "content_digest": c["version"]["content_digest"],
                    "depends_on": _loads(c["version"]["depends_on_json"]),
                    "license": {
                        "id": c["license"]["id"],
                        "licensor_org_id": c["license"]["licensor_org_id"],
                        "scope": c["license"]["scope"],
                        "territories": _loads(c["license"]["territories_json"]),
                        "org_ids": _loads(c["license"]["org_ids_json"]),
                        "valid_from": c["license"]["valid_from"],
                        "valid_until": c["license"]["valid_until"],
                        "current_version": c["license"]["current_version"],
                        "status": c["license"]["status"],
                        "grants": c["grants"],
                    },
                }
                for c in contexts
            ],
        }
        basis_digest = digest(snapshot)  # 剔除时间戳的稳定摘要，交付时用于漂移比对
        snapshot["taken_at"] = now
        snapshot_digest = digest(snapshot)

        # 5) 重复组包：新版本挂接谱系
        prev = self.store.query_one(
            "SELECT * FROM packages WHERE owner_org_id=? AND name=? AND status IN ('verified','delivered')"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (recipient["owner_org_id"], name))
        attempt_no = (self.store.query_one(
            "SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM packages WHERE owner_org_id=? AND name=?",
            (recipient["owner_org_id"], name))["n"])
        pid = _uid()

        manifest = {"package_id": pid, "name": name, "recipient_id": recipient_id,
                    "snapshot_digest": snapshot_digest, "basis_digest": basis_digest,
                    "items": [{
                        "resource_id": s["resource_id"], "version": s["version"],
                        "content_digest": s["content_digest"],
                    } for s in snapshot["items"]]}
        artifact_body = canonicalize(manifest)
        artifact_dgst = content_digest(artifact_body)
        final_path = self.artifact_dir / f"{pid}.pack"
        tmp_path = self.artifact_dir / f"{pid}.tmp.{_uid()}"

        try:
            tmp_path.write_bytes(artifact_body)
            with Transaction(self.store):
                self.store.execute(
                    "INSERT INTO packages(id,owner_org_id,name,status,attempt_no,"
                    "supersedes_package_id,snapshot_digest,snapshot_json,artifact_digest,"
                    "created_by,created_at) VALUES (?,?,?,'verified',?,?,?,?,?,?,?)",
                    (pid, recipient["owner_org_id"], name, attempt_no,
                     prev["id"] if prev else None, snapshot_digest,
                     json.dumps({**snapshot, "basis_digest": basis_digest}, ensure_ascii=False),
                     artifact_dgst, actor["id"], now),
                )
                for c, s in zip(contexts, snapshot["items"]):
                    self.store.execute(
                        "INSERT INTO package_items(id,package_id,resource_id,resource_version,"
                        "license_id,replaced_license_id,item_digest,created_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (_uid(), pid, c["resource"]["id"], c["resource"]["current_version"],
                         c["license"]["id"], None, digest(s), now),
                    )
                self.store.execute(
                    "INSERT INTO package_lineage(id,package_id,event_type,detail_json,actor_id,created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (_uid(), pid, "rebuild" if prev else "built",
                     json.dumps({"attempt_no": attempt_no,
                                 "supersedes_package_id": prev["id"] if prev else None,
                                 "snapshot_digest": snapshot_digest}, ensure_ascii=False),
                     actor["id"], now),
                )
                self.store.execute(
                    "INSERT INTO package_attempts(id,package_id,name,ok,violations_json,"
                    "artifact_path,created_by,created_at) VALUES (?,?,?,1,'[]',?,?,?)",
                    (attempt_id, pid, name, str(final_path), actor["id"], now),
                )
            os.replace(tmp_path, final_path)  # 事务提交后原子落位
        except BaseException:
            for p in (tmp_path, final_path):
                if p.exists():
                    p.unlink()
            raise
        return self.get_package(pid, viewer=actor)

    def replace_material(self, *, package_id: str, old_resource_id: str,
                         new_resource_id: str, recipient_id: Optional[str] = None,
                         actor: dict[str, Any]) -> dict[str, Any]:
        """替代材料：以旧包清单为底替换一项，重新核验并生成新版本包，谱系串接。"""
        old_pkg = self._load_package_visible(package_id, actor)
        items = self.store.query_all("SELECT * FROM package_items WHERE package_id=?", (package_id,))
        ids = [i["resource_id"] for i in items]
        _require(old_resource_id in ids, "被替代资源不在原包中")
        _require(new_resource_id not in ids, "替代材料已在包中")
        new_ids = [new_resource_id if r == old_resource_id else r for r in ids]
        if recipient_id:
            rec_id = recipient_id
        elif old_pkg["snapshot_json"]:
            rec_id = json.loads(old_pkg["snapshot_json"])["recipient"]["id"]
        else:
            rec_id = None
        _require(rec_id, "无法确定接收方，请显式传入 recipient_id")
        new_pkg = self.build_package(name=old_pkg["name"], resource_ids=new_ids,
                                     recipient_id=rec_id, actor=actor)
        old_item = next(i for i in items if i["resource_id"] == old_resource_id)
        with Transaction(self.store):
            # 在新包条目上记录其替代自哪项授权，保证影响分析可沿谱系追踪
            self.store.execute(
                "UPDATE package_items SET replaced_license_id=? "
                "WHERE package_id=? AND resource_id=?",
                (old_item["license_id"], new_pkg["id"], new_resource_id))
            self.store.execute(
                "INSERT INTO package_lineage(id,package_id,event_type,detail_json,actor_id,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (_uid(), new_pkg["id"], "material_replaced",
                 json.dumps({"base_package_id": package_id,
                             "old_resource_id": old_resource_id,
                             "new_resource_id": new_resource_id}, ensure_ascii=False),
                 actor["id"], utcnow()),
            )
            self.store.log_event("material_replaced", "package", new_pkg["id"],
                                 {"base_package_id": package_id,
                                  "old_resource_id": old_resource_id,
                                  "new_resource_id": new_resource_id}, actor["id"])
        return self.get_package(new_pkg["id"], viewer=actor)

    # ===================================================================
    # 交付：再核验 + 快照漂移检测
    # ===================================================================

    def deliver(self, package_id: str, *, actor: dict[str, Any]) -> dict[str, Any]:
        pkg = self._load_package_visible(package_id, actor)
        self._require_role(actor, pkg["owner_org_id"], ("provider", "admin"))
        snapshot = json.loads(pkg["snapshot_json"])
        recipient_id = snapshot["recipient"]["id"]
        recipient = self._load_recipient_raw(recipient_id)

        violations: list[dict[str, Any]] = []
        fresh_items: list[dict[str, Any]] = []
        for item in snapshot["items"]:
            rid = item["resource_id"]
            resource = self._load_resource_raw(rid)
            lic = self.store.query_one("SELECT * FROM licenses WHERE id=?", (item["license"]["id"],))
            if not lic:
                violations.append({"rule": "license_exists", "resource_id": rid,
                                   "message": "快照授权已不存在"})
                continue
            grants = self.store.query_all(
                "SELECT subject,permitted FROM license_grants WHERE license_id=?", (lic["id"],))
            grants = [{"subject": g["subject"], "permitted": bool(g["permitted"])} for g in grants]
            violations.extend(access.verify_item(
                license_row=lic, grants=grants,
                resource_version=resource["current_version"], recipient=recipient,
                now=self._now()))
            fresh_items.append({**item, "license": {
                "id": lic["id"], "licensor_org_id": lic["licensor_org_id"], "scope": lic["scope"],
                "territories": _loads(lic["territories_json"]), "org_ids": _loads(lic["org_ids_json"]),
                "valid_from": lic["valid_from"], "valid_until": lic["valid_until"],
                "current_version": lic["current_version"], "status": lic["status"],
                "grants": grants}})

        fresh_recipient = {
            "id": recipient["id"], "owner_org_id": recipient["owner_org_id"],
            "org_id": recipient["org_id"], "territory": recipient["territory"],
            "qualifications": _loads(recipient["qualifications_json"]),
            "valid_from": recipient["valid_from"], "valid_until": recipient["valid_until"],
        }
        fresh_snapshot = {**{k: v for k, v in snapshot.items() if k not in ("taken_at", "basis_digest")},
                          "recipient": fresh_recipient, "items": fresh_items}
        if digest(fresh_snapshot) != snapshot.get("basis_digest"):
            violations.append({
                "rule": "snapshot_drift", "resource_id": None,
                "message": "授权要素自组包快照后发生变化（撤回/替代/部分授权调整/版本升级）",
            })

        artifact_path = self.artifact_dir / f"{package_id}.pack"
        if not artifact_path.exists():
            raise NotFoundError("产物缺失（不应发生，请联系审计）")
        artifact_bytes = artifact_path.read_bytes()
        did = _uid()
        now = utcnow()
        if violations:
            with Transaction(self.store):
                self.store.execute(
                    "INSERT INTO deliveries(id,package_id,recipient_id,artifact_digest,"
                    "recheck_digest,status,created_by,created_at) VALUES (?,?,?,?,?,'blocked',?,?)",
                    (did, package_id, recipient_id, "", digest(fresh_snapshot), actor["id"], now),
                )
            raise DeliveryBlocked(violations)

        with Transaction(self.store):
            self.store.execute("UPDATE packages SET status='delivered', delivered_at=? WHERE id=?",
                               (now, package_id))
            self.store.execute(
                "INSERT INTO package_lineage(id,package_id,event_type,detail_json,actor_id,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (_uid(), package_id, "delivery",
                 json.dumps({"recipient_id": recipient_id,
                             "recheck_digest": digest(fresh_snapshot)}, ensure_ascii=False),
                 actor["id"], now),
            )
            self.store.execute(
                "INSERT INTO deliveries(id,package_id,recipient_id,artifact_digest,"
                "recheck_digest,status,created_by,created_at) VALUES (?,?,?,?,?,'delivered',?,?)",
                (did, package_id, recipient_id, content_digest(artifact_bytes),
                 digest(fresh_snapshot), actor["id"], now),
            )
        return self.get_package(package_id, viewer=actor)

    def _recipient_id_from_snapshot(self, snapshot: dict[str, Any]) -> str:
        return snapshot["recipient"]["id"]

    # ===================================================================
    # 查询：包 / 谱系 / 产物下载 / 影响分析
    # ===================================================================

    def list_packages(self, viewer: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.store.query_all(
            "SELECT * FROM packages WHERE status!='failed' ORDER BY created_at DESC")
        out = []
        for r in rows:
            if self.can_see_package(r, viewer):
                d = {k: r[k] for k in r.keys() if k != "snapshot_json"}
                out.append(d)
        return out

    def _load_package_visible(self, package_id: str, viewer: dict[str, Any]) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM packages WHERE id=?", (package_id,))
        if not row or not self.can_see_package(row, viewer):
            raise NotFoundError(f"课程包 {package_id} 不存在")
        return row

    def can_see_package(self, pkg: dict[str, Any], viewer: dict[str, Any]) -> bool:
        if viewer["role"] == "admin":
            return True
        if pkg["owner_org_id"] == viewer["org_id"]:
            return True
        # 已成功交付给本机构的包，接收方可见
        hit = self.store.query_one(
            "SELECT 1 FROM deliveries d JOIN recipients r ON r.id=d.recipient_id "
            "WHERE d.package_id=? AND d.status='delivered' AND r.org_id=? LIMIT 1",
            (pkg["id"], viewer["org_id"]))
        return hit is not None

    def get_package(self, package_id: str, *, viewer: dict[str, Any]) -> dict[str, Any]:
        pkg = self._load_package_visible(package_id, viewer)
        items = self.store.query_all(
            "SELECT id,resource_id,resource_version,license_id,replaced_license_id,item_digest "
            "FROM package_items WHERE package_id=? ORDER BY created_at", (package_id,))
        lineage = self.store.query_all(
            "SELECT event_type,detail_json,actor_id,created_at FROM package_lineage "
            "WHERE package_id=? ORDER BY created_at", (package_id,))
        for e in lineage:
            e["detail"] = _loads(e.pop("detail_json"))
        deliveries = self.store.query_all(
            "SELECT id,recipient_id,artifact_digest,recheck_digest,status,created_at "
            "FROM deliveries WHERE package_id=? ORDER BY created_at", (package_id,))
        result = {k: v for k, v in pkg.items() if k != "snapshot_json"}
        result["snapshot"] = _loads(pkg["snapshot_json"])
        result["items"] = items
        result["lineage"] = lineage
        result["deliveries"] = deliveries
        return result

    def get_lineage(self, package_id: str, viewer: dict[str, Any]) -> dict[str, Any]:
        pkg = self.get_package(package_id, viewer=viewer)
        return {"package_id": pkg["id"], "name": pkg["name"], "status": pkg["status"],
                "supersedes_package_id": pkg["supersedes_package_id"],
                "lineage": pkg["lineage"], "items": pkg["items"]}

    def download_artifact(self, package_id: str, viewer: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
        """产物下载：仅已核验/已交付且可见的包可取；失败尝试从无文件可取。"""
        pkg = self._load_package_visible(package_id, viewer)
        if pkg["status"] not in ("verified", "delivered"):
            raise NotFoundError("该包不存在可下载产物")
        path = self.artifact_dir / f"{package_id}.pack"
        if not path.exists():
            raise NotFoundError("产物缺失（不应发生，请联系审计）")
        return path.read_bytes(), pkg

    def rights_change_impact(self, license_id: str, viewer: dict[str, Any]) -> dict[str, Any]:
        """受权人员视角：某项权利变化影响了哪些包（跨机构内容不可见）。"""
        lic = self._load_license_raw(license_id)
        if not self.can_see_license(lic, viewer):
            raise NotFoundError(f"授权 {license_id} 不存在")
        change_events = self.store.query_all(
            "SELECT event_type,payload_json,actor_id,created_at FROM events "
            "WHERE subject_type='license' AND subject_id=? ORDER BY created_at", (license_id,))
        for e in change_events:
            e["payload"] = _loads(e.pop("payload_json"))

        item_rows = self.store.query_all(
            "SELECT pi.package_id, pi.license_id, pi.replaced_license_id, p.name, p.status "
            "FROM package_items pi JOIN packages p ON p.id=pi.package_id "
            "WHERE pi.license_id=? OR pi.replaced_license_id=?", (license_id, license_id))
        affected: list[dict[str, Any]] = []
        revoked = lic["status"] == "revoked"
        for row in item_rows:
            pkg = self.store.query_one("SELECT * FROM packages WHERE id=?", (row["package_id"],))
            if not self.can_see_package(pkg, viewer):
                continue  # 无权接触的机构内容直接剔除，不暴露存在性
            if row["license_id"] != license_id:
                impact = "superseded_by_replacement"  # 已通过替代材料切走该授权
            elif revoked and row["status"] == "delivered":
                impact = "historical_delivery_stands_redelivery_blocked"
            elif revoked:
                impact = "rebuild_required"
            elif lic["status"] == "superseded":
                impact = "rebuild_required_to_adopt_current_authorization"
            else:
                impact = "redelivery_blocked_until_rebuilt"
            affected.append({"package_id": row["package_id"], "name": row["name"],
                             "status": row["status"], "impact": impact})
        return {"license": self._decorate_license(lic), "changes": change_events,
                "affected_packages": affected}

    def list_attempts(self, name: str, viewer: dict[str, Any]) -> list[dict[str, Any]]:
        """组包尝试审计：成功/失败均可追溯，但失败记录没有 artifact_path。"""
        rows = self.store.query_all(
            "SELECT a.id,a.package_id,a.name,a.ok,a.violations_json,a.artifact_path,"
            "a.created_by,a.created_at FROM package_attempts a "
            "WHERE a.name=? ORDER BY a.created_at", (name,))
        out = []
        for r in rows:
            if r["package_id"]:
                pkg = self.store.query_one("SELECT owner_org_id FROM packages WHERE id=?",
                                           (r["package_id"],))
                if pkg and pkg["owner_org_id"] != viewer["org_id"] and viewer["role"] != "admin":
                    continue
            # 失败尝试没有关联包，按创建人机构过滤
            actor = self.get_user(r["created_by"])
            if actor["org_id"] != viewer["org_id"] and viewer["role"] != "admin":
                continue
            d = dict(r)
            d["violations"] = _loads(d.pop("violations_json"))
            out.append(d)
        return out

    # ===================================================================

    def _require_role(self, actor: dict[str, Any], org_id: str, roles: tuple[str, ...]) -> None:
        if actor["role"] == "admin":
            return
        if actor["org_id"] != org_id or actor["role"] not in roles:
            raise AuthorizationError("当前人员无权代表该机构执行此操作")
