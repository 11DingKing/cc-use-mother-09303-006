"""领域服务：用例编排、权限边界、快照固定与原子组包。

角色（对应契约 actors）：

- PROVIDER 资源提供院校：登记资源、发起组包、查看本机构谱系；
- COPYRIGHT_ADMIN 版权管理员（隶属资源方机构）：授予/部分授予/撤回许可、登记替代材料；
- RECIPIENT 接收院校：仅出现在交付目标与资格核验中，不能读取他机构数据。

所有读操作都按 actor.org_id 做机构隔离；越权与不存在统一按 NotFound 处理，
避免向其他机构泄露对象是否存在。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import canon
from .clock import Clock, SystemClock, iso, normalize
from .errors import AccessDenied, BuildRejected, Conflict, NotFound, ValidationError
from .policy import Recipient, expand_and_verify
from .store import Store

ROLE_PROVIDER = "PROVIDER"
ROLE_COPYRIGHT_ADMIN = "COPYRIGHT_ADMIN"
ROLE_RECIPIENT = "RECIPIENT"


@dataclass
class Actor:
    subject_id: str
    org_id: str
    roles: set[str] = field(default_factory=set)
    display_name: str = ""

    def has(self, role: str) -> bool:
        return role in self.roles


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class LicensingService:
    def __init__(self, store: Store, artifact_dir: str | Path, clock: Clock | None = None) -> None:
        self.store = store
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()

    # ============================ 资源登记 ============================

    def register_resource(
        self,
        actor: Actor,
        *,
        resource_id: str,
        title: str,
        digest: str,
        resource_type: str = "",
        metadata: dict | None = None,
        dependencies: list[dict] | None = None,
    ) -> dict:
        """登记资源摘要与版本依赖。resource_id 由调用方给出（建议含版本语义）。"""
        self._require(actor, ROLE_PROVIDER)
        if not resource_id or not title or not digest:
            raise ValidationError("resource_id、title、digest 均不能为空")
        if self.store.get_resource(resource_id) is not None:
            raise Conflict(f"资源 {resource_id} 已登记")
        now = self.clock.now()
        meta = {"resource_type": resource_type, **(metadata or {})}
        deps: list[dict] = []
        for dep in dependencies or []:
            dep_id = dep.get("dep_resource_id")
            if not dep_id:
                raise ValidationError("依赖项缺少 dep_resource_id")
            if dep_id == resource_id:
                raise ValidationError("资源不能依赖自身")
            deps.append({
                "resource_id": resource_id,
                "dep_resource_id": dep_id,
                "dep_version": dep.get("dep_version"),
                "required": 1 if dep.get("required", True) else 0,
            })
        self.store.insert_resource(
            {
                "resource_id": resource_id,
                "owner_org_id": actor.org_id,
                "title": title,
                "digest": digest,
                "metadata_json": canon_json(meta),
                "created_at": iso(now),
            },
            deps,
        )
        self.store.commit()
        return self.get_resource(actor, resource_id)

    def list_resources(self, actor: Actor) -> list[dict]:
        return [Store.row_to_resource(r) for r in self.store.list_resources(actor.org_id)]

    def get_resource(self, actor: Actor, resource_id: str) -> dict:
        row = self.store.get_resource(resource_id)
        if row is None or row["owner_org_id"] != actor.org_id:
            raise NotFound(f"资源 {resource_id} 不存在")
        out = Store.row_to_resource(row)
        out["dependencies"] = [
            {
                "dep_resource_id": d["dep_resource_id"],
                "dep_version": d["dep_version"],
                "required": bool(d["required"]),
            }
            for d in self.store.list_dependencies(resource_id)
        ]
        head = self.store.get_head_license(resource_id)
        out["current_license"] = Store.row_to_license(head) if head else None
        return out

    # ============================ 授权管理 ============================

    def grant_license(
        self,
        actor: Actor,
        *,
        resource_id: str,
        rights_holder: str,
        territories: list[str],
        org_scope: dict,
        recipient_qualification: dict | None = None,
        recipient_orgs: list[str] | None = None,
        valid_from: str | datetime | None = None,
        valid_until: str | datetime | None = None,
        basis: str,
        partial: bool = False,
        note: str | None = None,
    ) -> dict:
        """授予或变更许可。partial=True 表示仅覆盖部分地域/机构/用途的部分授权。

        部分授权不会让旧版本消失：旧 head 置为 SUPERSEDED 并在新记录中以
        supersedes 链接，完整谱系可追溯。
        """
        resource = self._require_owner_resource(actor, resource_id, admin=True)
        terms = self._validate_terms(
            rights_holder=rights_holder,
            territories=territories,
            org_scope=org_scope,
            recipient_qualification=recipient_qualification,
            recipient_orgs=recipient_orgs,
            valid_from=valid_from,
            valid_until=valid_until,
            basis=basis,
        )
        head = self.store.get_head_license(resource_id)
        change_type = "PARTIAL_GRANT" if partial else "GRANT"
        if head is not None:
            self.store.mark_superseded(head["license_id"], iso(self.clock.now()))
        rec = self._append_license(
            resource=resource,
            actor=actor,
            change_type=change_type,
            terms=terms,
            supersedes=head["license_id"] if head else None,
            note=note,
        )
        self.store.commit()
        return rec

    def revoke_license(self, actor: Actor, resource_id: str, reason: str) -> dict:
        """撤回许可：当前 head 置 SUPERSEDED，追加 REVOCATION 记录并移除 head。"""
        resource = self._require_owner_resource(actor, resource_id, admin=True)
        head = self.store.get_head_license(resource_id)
        if head is None:
            raise Conflict(f"资源 {resource_id} 无生效中的许可，无需撤回")
        now = self.clock.now()
        terms = {
            "rights_holder": head["rights_holder"],
            "territories": json.loads(head["territories_json"]),
            "org_scope": json.loads(head["org_scope_json"]),
            "recipient_qualification": json.loads(head["recipient_qual_json"]),
            "recipient_orgs": json.loads(head["recipient_orgs_json"]),
            "valid_from": head["valid_from"],
            "valid_until": head["valid_until"],
            "basis": head["basis"],
        }
        self.store.mark_superseded(head["license_id"], iso(now))
        rec = self._append_license(
            resource=resource,
            actor=actor,
            change_type="REVOCATION",
            terms=terms,
            supersedes=head["license_id"],
            revokes_license_id=head["license_id"],
            note=reason,
            active=False,
        )
        self.store.drop_head(resource_id)
        self.store.insert_lineage({
            "event_id": _new_id("evt"),
            "resource_id": resource_id,
            "package_id": None,
            "license_id": rec["license_id"],
            "event_type": "REVOCATION",
            "occurred_at": iso(now),
            "detail_json": canon_json({"reason": reason, "revokes": head["license_id"]}),
            "linked_license_id": head["license_id"],
            "linked_package_id": None,
        })
        self.store.commit()
        return rec

    def register_replacement(
        self,
        actor: Actor,
        *,
        old_resource_id: str,
        new_resource_id: str,
        title: str,
        digest: str,
        resource_type: str = "",
        metadata: dict | None = None,
        rights_holder: str,
        territories: list[str],
        org_scope: dict,
        recipient_qualification: dict | None = None,
        recipient_orgs: list[str] | None = None,
        valid_from: str | datetime | None = None,
        valid_until: str | datetime | None = None,
        basis: str,
        revoke_old: bool = False,
        note: str | None = None,
    ) -> dict:
        """登记替代材料：新材料与被替代材料双向留链。

        旧材料默认保留原许可（可继续用于历史可追溯性），revoke_old=True 时一并撤回；
        无论哪种方式，谱系中都能从旧材料追到替代材料、反之亦然。
        """
        old = self._require_owner_resource(actor, old_resource_id, admin=True)
        if self.store.get_resource(new_resource_id) is not None:
            raise Conflict(f"资源 {new_resource_id} 已存在，不能作为替代材料重复登记")
        now = self.clock.now()
        self.store.insert_resource(
            {
                "resource_id": new_resource_id,
                "owner_org_id": actor.org_id,
                "title": title,
                "digest": digest,
                "metadata_json": canon_json({
                    "resource_type": resource_type,
                    "replaces": old_resource_id,
                    **(metadata or {}),
                }),
                "created_at": iso(now),
            },
            [],
        )
        terms = self._validate_terms(
            rights_holder=rights_holder,
            territories=territories,
            org_scope=org_scope,
            recipient_qualification=recipient_qualification,
            recipient_orgs=recipient_orgs,
            valid_from=valid_from,
            valid_until=valid_until,
            basis=basis,
        )
        new_row = self.store.get_resource(new_resource_id)
        rec = self._append_license(
            resource=new_row,
            actor=actor,
            change_type="REPLACEMENT",
            terms=terms,
            supersedes=None,
            replaced_resource_id=old_resource_id,
            note=note,
        )
        self.store.insert_lineage({
            "event_id": _new_id("evt"),
            "resource_id": old_resource_id,
            "package_id": None,
            "license_id": rec["license_id"],
            "event_type": "REPLACEMENT",
            "occurred_at": iso(now),
            "detail_json": canon_json({
                "replaced_by": new_resource_id,
                "new_license_id": rec["license_id"],
                "old_title": old["title"],
                "note": note,
            }),
            "linked_license_id": rec["license_id"],
            "linked_package_id": None,
        })
        if revoke_old:
            old_head = self.store.get_head_license(old_resource_id)
            if old_head is not None:
                self.store.mark_superseded(old_head["license_id"], iso(now))
                self.store.drop_head(old_resource_id)
                self.store.insert_lineage({
                    "event_id": _new_id("evt"),
                    "resource_id": old_resource_id,
                    "package_id": None,
                    "license_id": rec["license_id"],
                    "event_type": "REVOCATION",
                    "occurred_at": iso(now),
                    "detail_json": canon_json({
                        "reason": "替代材料登记后撤回原许可",
                        "revokes": old_head["license_id"],
                        "replaced_by": new_resource_id,
                    }),
                    "linked_license_id": old_head["license_id"],
                    "linked_package_id": None,
                })
        self.store.commit()
        return rec

    def list_license_history(self, actor: Actor, resource_id: str) -> list[dict]:
        self._require_owner_resource(actor, resource_id)
        return [Store.row_to_license(r) for r in self.store.list_license_history(resource_id)]

    # ============================ 组包与交付 ============================

    def build_package(
        self,
        actor: Actor,
        *,
        name: str,
        resource_ids: list[str],
        recipient: dict,
        idempotency_key: str | None = None,
    ) -> dict:
        """逐项核验 → 固定授权快照 → 原子落盘制品。

        核验失败：只写 FAILED 记录与 BUILD_REJECTED 谱系，不产生任何可下载文件；
        同一 idempotency_key 的重复组包直接返回已冻结成功的包。
        """
        self._require(actor, ROLE_PROVIDER)
        if not name or not resource_ids:
            raise ValidationError("包名与资源清单不能为空")
        if len(resource_ids) != len(set(resource_ids)):
            raise ValidationError("资源清单存在重复条目")
        rcpt = self._build_recipient(recipient)

        if idempotency_key:
            existing = self.store.find_idempotent_build(actor.org_id, idempotency_key)
            if existing is not None:
                return self.get_package(actor, existing["package_id"])

        package_id = _new_id("pkg")
        now = self.clock.now()
        results, missing = expand_and_verify(list(resource_ids), self.store, rcpt, now)

        failures: list[dict] = []
        for rid, deps in missing.items():
            for d in deps:
                label = "资源未登记" if d["dep_resource_id"] == rid else f"必选依赖 {d['dep_resource_id']} 未登记"
                failures.append({"resource_id": rid, "reasons": [label], "checks": []})
        failures.extend(v.failure() for v in results.values() if not v.ok)
        failures = [f for f in failures if f]

        if failures:
            self.store.insert_build({
                "package_id": package_id,
                "owner_org_id": actor.org_id,
                "name": name,
                "idempotency_key": idempotency_key,
                "status": "FAILED",
                "created_by": actor.subject_id,
                "created_at": iso(now),
                "failure_reasons_json": canon_json(failures),
                "snapshot_digest": None,
                "item_count": None,
                "artifact_path": None,
                "artifact_digest": None,
                "artifact_size": None,
            })
            for rid, ver in results.items():
                self.store.insert_lineage({
                    "event_id": _new_id("evt"),
                    "resource_id": rid,
                    "package_id": package_id,
                    "license_id": ver.license_row["license_id"] if ver.license_row else None,
                    "event_type": "BUILD_REJECTED",
                    "occurred_at": iso(now),
                    "detail_json": canon_json({
                        "package_name": name,
                        "recipient": rcpt.to_dict(),
                        "failure": ver.failure(),
                    }),
                    "linked_license_id": ver.license_row["license_id"] if ver.license_row else None,
                    "linked_package_id": package_id,
                })
            self.store.commit()
            raise BuildRejected(failures)

        # 全部通过：固定快照、原子生成制品
        item_snapshots: list[dict] = []
        try:
            for rid in sorted(results):
                ver = results[rid]
                assert ver.license_row is not None
                resource = self.store.get_resource(rid)
                license_dict = Store.row_to_license(ver.license_row)
                snapshot = {
                    "resource_id": rid,
                    "title": resource["title"],
                    "resource_digest": resource["digest"],
                    "owner_org_id": resource["owner_org_id"],
                    "license": license_dict,
                    "verification": {
                        "evaluated_at": iso(now),
                        "recipient": rcpt.to_dict(),
                        "checks": [c.to_dict() for c in ver.checks],
                        "all_passed": True,
                    },
                }
                snapshot["item_digest"] = canon.digest({
                    k: v for k, v in snapshot.items() if k != "item_digest"
                })
                item_snapshots.append(snapshot)

            manifest = {
                "package_id": package_id,
                "name": name,
                "owner_org_id": actor.org_id,
                "created_at": iso(now),
                "created_by": actor.subject_id,
                "recipient": rcpt.to_dict(),
                "items": item_snapshots,
            }
            manifest["snapshot_digest"] = canon.digest(
                {k: v for k, v in manifest.items() if k != "snapshot_digest"}
            )
            data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

            final_path = self.artifact_dir / f"{package_id}.json"
            tmp_path = self.artifact_dir / f".{package_id}.tmp.{uuid.uuid4().hex}"
            with open(tmp_path, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())

            self.store.insert_build({
                "package_id": package_id,
                "owner_org_id": actor.org_id,
                "name": name,
                "idempotency_key": idempotency_key,
                "status": "SUCCEEDED",
                "created_by": actor.subject_id,
                "created_at": iso(now),
                "failure_reasons_json": None,
                "snapshot_digest": manifest["snapshot_digest"],
                "item_count": len(item_snapshots),
                "artifact_path": str(final_path),
                "artifact_digest": canon.digest_bytes(data),
                "artifact_size": len(data),
            })
            for snap in item_snapshots:
                self.store.insert_package_item({
                    "package_id": package_id,
                    "resource_id": snap["resource_id"],
                    "license_id": snap["license"]["license_id"],
                    "license_version": snap["license"]["version"],
                    "snapshot_json": canon_json(snap),
                })
                self.store.insert_lineage({
                    "event_id": _new_id("evt"),
                    "resource_id": snap["resource_id"],
                    "package_id": package_id,
                    "license_id": snap["license"]["license_id"],
                    "event_type": "PACKAGED",
                    "occurred_at": iso(now),
                    "detail_json": canon_json({
                        "package_name": name,
                        "license_version": snap["license"]["version"],
                        "item_digest": snap["item_digest"],
                        "recipient": rcpt.to_dict(),
                    }),
                    "linked_license_id": snap["license"]["license_id"],
                    "linked_package_id": package_id,
                })

            os.replace(tmp_path, final_path)  # 原子改名，制品只以完整形态出现
            self.store.commit()
        except BaseException:
            self.store.conn.rollback()
            for p in (tmp_path if 'tmp_path' in locals() else None,
                      final_path if 'final_path' in locals() else None):
                if p and os.path.exists(p):
                    os.unlink(p)
            raise

        return self.get_package(actor, package_id)

    def list_packages(self, actor: Actor) -> list[dict]:
        return [self._build_summary(r) for r in self.store.list_packages(actor.org_id)]

    def list_failed_builds(self, actor: Actor) -> list[dict]:
        return [
            self._build_summary(r)
            for r in self.store.list_packages(actor.org_id)
            if r["status"] == "FAILED"
        ]

    def get_package(self, actor: Actor, package_id: str) -> dict:
        row = self.store.get_build(package_id)
        if row is None or row["owner_org_id"] != actor.org_id:
            raise NotFound(f"包 {package_id} 不存在")
        out = self._build_summary(row)
        if row["status"] == "SUCCEEDED":
            out["items"] = [
                {
                    "resource_id": i["resource_id"],
                    "license_id": i["license_id"],
                    "license_version": i["license_version"],
                    "snapshot": json.loads(i["snapshot_json"]),
                }
                for i in self.store.list_package_items(package_id)
            ]
        else:
            out["failure_reasons"] = json.loads(row["failure_reasons_json"])
        return out

    def download_package(self, actor: Actor, package_id: str) -> tuple[str, Path, str]:
        """返回 (下载文件名, 制品路径, 摘要)。失败构建无制品可取。"""
        row = self.store.get_build(package_id)
        if row is None or row["owner_org_id"] != actor.org_id:
            raise NotFound(f"包 {package_id} 不存在")
        if row["status"] != "SUCCEEDED" or not row["artifact_path"]:
            raise NotFound(f"包 {package_id} 未成功生成，没有可下载制品")
        path = Path(row["artifact_path"])
        if not path.exists():
            raise NotFound("制品文件缺失，请联系管理员")
        return f"{row['name']}.{package_id}.json", path, row["artifact_digest"]

    # ============================ 谱系与影响分析 ============================

    def get_lineage(self, actor: Actor, resource_id: str) -> dict:
        self._require_owner_resource(actor, resource_id)
        events = []
        for e in self.store.list_lineage(resource_id):
            events.append({
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "occurred_at": e["occurred_at"],
                "package_id": e["package_id"],
                "license_id": e["license_id"],
                "detail": json.loads(e["detail_json"]),
            })
        history = [Store.row_to_license(h) for h in self.store.list_license_history(resource_id)]
        return {"resource_id": resource_id, "license_history": history, "events": events}

    def impact_analysis(self, actor: Actor, resource_id: str) -> dict:
        """某项权利的变化影响了哪些已交付包。

        以当前 head 与其全部历史版本为线索，找出冻结了这些授权版本的包，
        并标注包内固定的版本与当前状态的差异。
        """
        self._require_owner_resource(actor, resource_id)
        history = self.store.list_license_history(resource_id)
        head = self.store.get_head_license(resource_id)
        affected: dict[str, dict] = {}
        for h in history:
            for pkg in self.store.packages_referencing_license(h["license_id"]):
                if pkg["owner_org_id"] != actor.org_id:
                    continue  # 双保险：机构隔离
                entry = affected.setdefault(pkg["package_id"], {
                    "package_id": pkg["package_id"],
                    "name": pkg["name"],
                    "status": pkg["status"],
                    "created_at": pkg["created_at"],
                    "pinned_versions": [],
                })
                entry["pinned_versions"].append({
                    "license_id": h["license_id"],
                    "license_version": h["version"],
                    "change_type": h["change_type"],
                    "license_status_now": h["status"],
                    "is_current_head": head is not None and head["license_id"] == h["license_id"],
                })
        return {
            "resource_id": resource_id,
            "current_license": Store.row_to_license(head) if head else None,
            "affected_package_count": len(affected),
            "affected_packages": sorted(affected.values(), key=lambda x: x["created_at"]),
        }

    # ============================ 内部辅助 ============================

    def _append_license(
        self,
        *,
        resource: Any,
        actor: Actor,
        change_type: str,
        terms: dict,
        supersedes: str | None,
        note: str | None = None,
        active: bool = True,
        replaced_resource_id: str | None = None,
        revokes_license_id: str | None = None,
    ) -> dict:
        now = self.clock.now()
        version = self.store.next_license_version(resource["resource_id"])
        license_id = _new_id("lic")
        record = {
            "resource_id": resource["resource_id"],
            "version": version,
            "change_type": change_type,
            "supersedes": supersedes,
            "rights_holder": terms["rights_holder"],
            "territories": terms["territories"],
            "org_scope": terms["org_scope"],
            "recipient_qualification": terms["recipient_qualification"],
            "recipient_orgs": terms["recipient_orgs"],
            "valid_from": terms["valid_from"],
            "valid_until": terms["valid_until"],
            "basis": terms["basis"],
            "replaced_resource_id": replaced_resource_id,
            "revokes_license_id": revokes_license_id,
        }
        snapshot_digest = canon.digest(record)
        rec = {
            "license_id": license_id,
            "resource_id": resource["resource_id"],
            "version": version,
            "change_type": change_type,
            "supersedes": supersedes,
            "rights_holder": terms["rights_holder"],
            "territories_json": canon_json(terms["territories"]),
            "org_scope_json": canon_json(terms["org_scope"]),
            "recipient_qual_json": canon_json(terms["recipient_qualification"]),
            "recipient_orgs_json": canon_json(terms["recipient_orgs"]),
            "valid_from": terms["valid_from"],
            "valid_until": terms["valid_until"],
            "basis": terms["basis"],
            "status": "ACTIVE" if active else "REVOKED",
            "superseded_at": None,
            "created_by": actor.subject_id,
            "created_at": iso(now),
            "snapshot_digest": snapshot_digest,
            "replaced_resource_id": replaced_resource_id,
            "revokes_license_id": revokes_license_id,
            "note": note,
        }
        self.store.insert_license_history(rec)
        if active:
            self.store.upsert_head(resource["resource_id"], license_id, version)
        self.store.insert_lineage({
            "event_id": _new_id("evt"),
            "resource_id": resource["resource_id"],
            "package_id": None,
            "license_id": license_id,
            "event_type": change_type,
            "occurred_at": iso(now),
            "detail_json": canon_json({"note": note, "supersedes": supersedes,
                                       "snapshot_digest": snapshot_digest}),
            "linked_license_id": supersedes,
            "linked_package_id": None,
        })
        # 不在此处提交：由调用方在补齐谱系/撤回等动作后一次性原子提交
        return Store.row_to_license(self.store.get_history_row(license_id))

    def _validate_terms(self, **t: Any) -> dict:
        territories = t.get("territories") or []
        if not isinstance(territories, list) or not territories:
            raise ValidationError("许可地域不能为空")
        scope = t.get("org_scope") or {}
        if scope.get("type", "WHITELIST") not in ("ALL", "WHITELIST", "EXCLUDE"):
            raise ValidationError("机构范围类型必须是 ALL/WHITELIST/EXCLUDE")
        if not isinstance(scope.get("orgs", []), list):
            raise ValidationError("机构范围 orgs 必须是列表")
        qual = t.get("recipient_qualification") or {"conditions": []}
        for rule in qual.get("conditions", []):
            if "attr" not in rule or rule.get("op", "eq") not in (
                "eq", "ne", "in", "not_in", "truthy"
            ):
                raise ValidationError(f"资格条件不合法：{rule}")
        start = normalize(t["valid_from"]) if t.get("valid_from") else None
        end = normalize(t["valid_until"]) if t.get("valid_until") else None
        if start and end and end <= start:
            raise ValidationError("授权截止时间必须晚于起始时间")
        if not t.get("rights_holder") or not t.get("basis"):
            raise ValidationError("权利主体与授权依据不能为空")
        return {
            "rights_holder": t["rights_holder"],
            "territories": sorted(territories),
            "org_scope": {"type": scope.get("type", "WHITELIST"),
                          "orgs": sorted(scope.get("orgs", []))},
            "recipient_qualification": {"conditions": qual.get("conditions", [])},
            "recipient_orgs": sorted(t.get("recipient_orgs") or []),
            "valid_from": iso(start) if start else None,
            "valid_until": iso(end) if end else None,
            "basis": t["basis"],
        }

    def _build_recipient(self, recipient: dict) -> Recipient:
        for key in ("org_id", "org_name", "country"):
            if not recipient.get(key):
                raise ValidationError(f"接收方信息缺少 {key}")
        return Recipient(
            org_id=recipient["org_id"],
            org_name=recipient["org_name"],
            country=recipient["country"],
            attrs=recipient.get("attrs", {}),
        )

    def _require(self, actor: Actor, role: str) -> None:
        if not actor.has(role):
            raise AccessDenied(f"需要 {role} 角色")

    def _require_owner_resource(self, actor: Actor, resource_id: str, admin: bool = False) -> Any:
        if admin:
            self._require(actor, ROLE_COPYRIGHT_ADMIN)
        row = self.store.get_resource(resource_id)
        if row is None or row["owner_org_id"] != actor.org_id:
            raise NotFound(f"资源 {resource_id} 不存在")
        return row

    def _build_summary(self, row: Any) -> dict:
        return {
            "package_id": row["package_id"],
            "owner_org_id": row["owner_org_id"],
            "name": row["name"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "idempotency_key": row["idempotency_key"],
            "snapshot_digest": row["snapshot_digest"],
            "item_count": row["item_count"],
            "artifact_digest": row["artifact_digest"],
            "artifact_size": row["artifact_size"],
            "downloadable": row["status"] == "SUCCEEDED" and bool(row["artifact_path"]),
        }


def canon_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
