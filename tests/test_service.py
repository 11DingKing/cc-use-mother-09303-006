"""后端领域服务端到端测试。

场景对应业务故事：实训手册仅允许本国校内使用，整包交付海外即越权。
覆盖：登记/授权、地域机构期限资格核验、依赖图谱、快照固定、
撤回/替代/部分授权谱系、重复组包幂等、失败无半成品、影响分析、跨机构隔离。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from licensing import LicensingService, Store
from licensing.errors import (
    AccessDenied,
    BuildRejected,
    Conflict,
    NotFound,
)
from licensing.service import (
    ROLE_COPYRIGHT_ADMIN,
    ROLE_PROVIDER,
    Actor,
)


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t


def make_service() -> tuple[LicensingService, tempfile.TemporaryDirectory, FixedClock]:
    tmp = tempfile.TemporaryDirectory()
    clock = FixedClock(datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc))
    svc = LicensingService(Store(":memory:"), Path(tmp.name) / "artifacts", clock=clock)
    return svc, tmp, clock


# 机构与角色
CN_ORG = "ORG_CN_VOC"
OVERSEAS_ORG = "ORG_FRN_PARTNER"
OTHER_ORG = "ORG_OTHER"
provider = Actor("u-prov", CN_ORG, {ROLE_PROVIDER}, "中方教务")
admin = Actor("u-admin", CN_ORG, {ROLE_COPYRIGHT_ADMIN}, "中方版权管理员")
other_admin = Actor("u-admin2", OTHER_ORG, {ROLE_COPYRIGHT_ADMIN}, "外校管理员")
other_provider = Actor("u-prov2", OTHER_ORG, {ROLE_PROVIDER}, "外校教务")

CN_RECIPIENT = {
    "org_id": "ORG_CN_BRANCH", "org_name": "国内分校", "country": "CN",
    "attrs": {"accredited": True, "level": "vocational"},
}
OVERSEAS_RECIPIENT = {
    "org_id": OVERSEAS_ORG, "org_name": "海外伙伴校", "country": "FR",
    "attrs": {"accredited": True, "level": "vocational"},
}

HANDBOOK = {
    "resource_id": "r-handbook-v1",
    "title": "数控实训手册（本国校内限定）",
    "digest": "sha256:handbook0001",
    "resource_type": "manual",
}
SLIDES = {
    "resource_id": "r-slides-v1",
    "title": "课程讲义",
    "digest": "sha256:slides0001",
    "resource_type": "slides",
}
VIDEO = {
    "resource_id": "r-video-v1",
    "title": "操作视频",
    "digest": "sha256:video0001",
    "resource_type": "video",
    "dependencies": [{"dep_resource_id": "r-handbook-v1", "dep_version": "v1", "required": True}],
}


def _reg(svc, actor, spec, holder="本校出版社", *, territories=None, org_scope=None,
         recipient_qualification=None):
    svc.register_resource(
        actor,
        resource_id=spec["resource_id"],
        title=spec["title"],
        digest=spec["digest"],
        resource_type=spec.get("resource_type", ""),
        dependencies=spec.get("dependencies"),
    )
    svc.grant_license(
        admin,
        resource_id=spec["resource_id"],
        rights_holder=holder,
        territories=territories or spec.get("territories", ["CN"]),
        org_scope=org_scope or spec.get("org_scope",
                                        {"type": "WHITELIST", "orgs": ["ORG_CN_BRANCH"]}),
        recipient_qualification=recipient_qualification or spec.get("recipient_qualification", {
            "conditions": [{"attr": "accredited", "op": "truthy", "value": True}]}),
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2027-01-01T00:00:00Z",
        basis="校企合作协议第3条",
        note=spec.get("license_note"),
    )


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp, self.clock = make_service()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_only_provider_can_register_and_summary_is_recorded(self) -> None:
        with self.assertRaises(AccessDenied):
            self.svc.register_resource(admin, resource_id="x", title="t", digest="d")
        out = self.svc.register_resource(
            provider, resource_id=HANDBOOK["resource_id"], title=HANDBOOK["title"],
            digest=HANDBOOK["digest"], resource_type="manual",
            metadata={"language": "zh"},
        )
        self.assertEqual(out["owner_org_id"], CN_ORG)
        self.assertEqual(out["metadata"]["language"], "zh")
        with self.assertRaises(Conflict):
            self.svc.register_resource(
                provider, resource_id=HANDBOOK["resource_id"], title="t", digest="d2")

    def test_only_admin_can_grant(self) -> None:
        self.svc.register_resource(provider, resource_id=HANDBOOK["resource_id"],
                                   title=HANDBOOK["title"], digest=HANDBOOK["digest"])
        with self.assertRaises(AccessDenied):
            self.svc.grant_license(
                provider, resource_id=HANDBOOK["resource_id"], rights_holder="h",
                territories=["CN"], org_scope={"type": "ALL", "orgs": []}, basis="b")

    def test_invalid_terms_rejected(self) -> None:
        self.svc.register_resource(provider, resource_id="r1", title="t", digest="d")
        # 空地域、期限倒挂
        for kwargs in (
            dict(territories=[], org_scope={"type": "ALL", "orgs": []}),
            dict(territories=["CN"], org_scope={"type": "ALL", "orgs": []},
                 valid_from="2027-01-01T00:00:00Z", valid_until="2026-01-01T00:00:00Z"),
        ):
            with self.assertRaises(Exception):
                self.svc.grant_license(
                    admin, resource_id="r1", rights_holder="h", basis="b", **kwargs)


class BuildVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp, self.clock = make_service()
        _reg(self.svc, provider, HANDBOOK)
        _reg(self.svc, provider, SLIDES,
             territories=["*"], org_scope={"type": "ALL", "orgs": []})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_domestic_delivery_passes_and_pins_snapshot(self) -> None:
        pkg = self.svc.build_package(
            provider, name="国内课程包", resource_ids=["r-handbook-v1", "r-slides-v1"],
            recipient=CN_RECIPIENT, idempotency_key="dom-1",
        )
        self.assertEqual(pkg["status"], "SUCCEEDED")
        self.assertEqual(pkg["item_count"], 2)
        self.assertTrue(pkg["downloadable"])
        items = {i["resource_id"]: i for i in pkg["items"]}
        snap = items["r-handbook-v1"]["snapshot"]
        self.assertEqual(snap["license"]["territories"], ["CN"])
        self.assertTrue(all(c["passed"] for c in snap["verification"]["checks"]))
        self.assertTrue(snap["item_digest"].startswith("sha256:"))
        self.assertTrue(pkg["snapshot_digest"].startswith("sha256:"))

        name, path, digest = self.svc.download_package(provider, pkg["package_id"])
        self.assertTrue(Path(path).exists())
        self.assertEqual(digest, pkg["artifact_digest"])
        self.assertIn(pkg["package_id"], name)

    def test_overseas_delivery_is_rejected_with_no_downloadable_artifact(self) -> None:
        artifact_dir = self.svc.artifact_dir
        with self.assertRaises(BuildRejected) as ctx:
            self.svc.build_package(
                provider, name="海外课程包",
                resource_ids=["r-handbook-v1", "r-slides-v1"],
                recipient=OVERSEAS_RECIPIENT,
            )
        reasons = ctx.exception.reasons
        failed = {r["resource_id"]: r for r in reasons}
        self.assertIn("r-handbook-v1", failed)
        self.assertTrue(any("地域" in x for x in failed["r-handbook-v1"]["reasons"]))
        self.assertNotIn("r-slides-v1", failed)  # 全球许可的讲义不受限
        # 磁盘上没有任何半成品文件
        leftovers = list(Path(artifact_dir).glob("*"))
        self.assertEqual(leftovers, [f for f in leftovers if f.name.startswith(".") is False])
        self.assertEqual([p.name for p in Path(artifact_dir).glob("**/*") if p.is_file()], [])
        failed_builds = self.svc.list_failed_builds(provider)
        self.assertEqual(len(failed_builds), 1)
        self.assertFalse(failed_builds[0]["downloadable"])
        with self.assertRaises(NotFound):
            self.svc.download_package(provider, failed_builds[0]["package_id"])

    def test_org_scope_qualification_and_time_window(self) -> None:
        # 机构不在白名单
        with self.assertRaises(BuildRejected) as ctx:
            self.svc.build_package(
                provider, name="p", resource_ids=["r-handbook-v1"],
                recipient={**CN_RECIPIENT, "org_id": "ORG_RANDOM"})
        self.assertTrue(any("机构范围" in r for f in ctx.exception.reasons
                            for r in f["reasons"]))
        # 资格不满足
        with self.assertRaises(BuildRejected) as ctx:
            self.svc.build_package(
                provider, name="p", resource_ids=["r-handbook-v1"],
                recipient={**CN_RECIPIENT, "attrs": {"accredited": False}})
        self.assertTrue(any("accredited" in r for f in ctx.exception.reasons
                            for r in f["reasons"]))
        # 授权已过期
        self.clock.t = datetime(2028, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(BuildRejected) as ctx:
            self.svc.build_package(
                provider, name="p", resource_ids=["r-handbook-v1"],
                recipient=CN_RECIPIENT)
        self.assertTrue(any("授权期限" in r for f in ctx.exception.reasons
                            for r in f["reasons"]))

    def test_required_dependency_is_auto_included_and_verified(self) -> None:
        _reg(self.svc, provider, VIDEO,
             territories=["*"], org_scope={"type": "ALL", "orgs": []})
        # 视频本身全球许可，但它必选依赖本国限定手册 -> 海外组包应连带失败
        with self.assertRaises(BuildRejected) as ctx:
            self.svc.build_package(
                provider, name="海外视频包", resource_ids=["r-video-v1"],
                recipient=OVERSEAS_RECIPIENT)
        failed = {r["resource_id"] for r in ctx.exception.reasons}
        self.assertIn("r-handbook-v1", failed)
        # 国内组包自动把手册带入，item_count=2
        pkg = self.svc.build_package(
            provider, name="国内视频包", resource_ids=["r-video-v1"],
            recipient=CN_RECIPIENT)
        ids = {i["resource_id"] for i in pkg["items"]}
        self.assertEqual(ids, {"r-video-v1", "r-handbook-v1"})

    def test_missing_dependency_reported(self) -> None:
        self.svc.register_resource(
            provider, resource_id="r-broken", title="悬空依赖", digest="sha256:x",
            dependencies=[{"dep_resource_id": "r-ghost", "required": True}])
        self.svc.grant_license(
            admin, resource_id="r-broken", rights_holder="h", territories=["*"],
            org_scope={"type": "ALL", "orgs": []}, basis="b")
        with self.assertRaises(BuildRejected) as ctx:
            self.svc.build_package(
                provider, name="p", resource_ids=["r-broken"], recipient=CN_RECIPIENT)
        self.assertTrue(any("r-ghost" in r for f in ctx.exception.reasons
                            for r in f["reasons"]))


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp, self.clock = make_service()
        _reg(self.svc, provider, HANDBOOK)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repeated_build_with_same_key_returns_frozen_package(self) -> None:
        first = self.svc.build_package(
            provider, name="重复包", resource_ids=["r-handbook-v1"],
            recipient=CN_RECIPIENT, idempotency_key="dup-1")
        second = self.svc.build_package(
            provider, name="重复包", resource_ids=["r-handbook-v1"],
            recipient=CN_RECIPIENT, idempotency_key="dup-1")
        self.assertEqual(first["package_id"], second["package_id"])
        self.assertEqual(first["snapshot_digest"], second["snapshot_digest"])
        # 失败的组包不占用幂等键，允许修正后重试成功
        with self.assertRaises(BuildRejected):
            self.svc.build_package(
                provider, name="海外包", resource_ids=["r-handbook-v1"],
                recipient=OVERSEAS_RECIPIENT, idempotency_key="retry-1")
        with self.assertRaises(BuildRejected):
            self.svc.build_package(
                provider, name="海外包", resource_ids=["r-handbook-v1"],
                recipient=OVERSEAS_RECIPIENT, idempotency_key="retry-1")
        ok = self.svc.build_package(
            provider, name="国内包", resource_ids=["r-handbook-v1"],
            recipient=CN_RECIPIENT, idempotency_key="retry-1")
        self.assertEqual(ok["status"], "SUCCEEDED")


class LineageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp, self.clock = make_service()
        _reg(self.svc, provider, HANDBOOK)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_partial_grant_keeps_full_history_chain(self) -> None:
        self.svc.grant_license(
            admin, resource_id="r-handbook-v1", rights_holder="本校出版社",
            territories=["CN", "FR"],
            org_scope={"type": "WHITELIST", "orgs": ["ORG_CN_BRANCH", OVERSEAS_ORG]},
            recipient_qualification={"conditions": [
                {"attr": "accredited", "op": "truthy", "value": True}]},
            valid_from="2026-06-01T00:00:00Z",
            valid_until="2027-06-01T00:00:00Z",
            basis="补充协议：仅限示范用途", partial=True, note="向法国伙伴部分开放",
        )
        history = self.svc.list_license_history(admin, "r-handbook-v1")
        self.assertEqual([h["version"] for h in history], [1, 2])
        self.assertEqual(history[0]["status"], "SUPERSEDED")
        self.assertEqual(history[1]["change_type"], "PARTIAL_GRANT")
        self.assertEqual(history[1]["supersedes"], history[0]["license_id"])
        # 部分授权后海外交付通过
        pkg = self.svc.build_package(
            provider, name="法国包", resource_ids=["r-handbook-v1"],
            recipient=OVERSEAS_RECIPIENT)
        self.assertEqual(pkg["status"], "SUCCEEDED")
        self.assertEqual(pkg["items"][0]["snapshot"]["license"]["version"], 2)

    def test_revocation_blocks_new_builds_and_is_traced(self) -> None:
        self.svc.revoke_license(admin, "r-handbook-v1", reason="版权方终止授权")
        with self.assertRaises(BuildRejected):
            self.svc.build_package(
                provider, name="p", resource_ids=["r-handbook-v1"],
                recipient=CN_RECIPIENT)
        lineage = self.svc.get_lineage(provider, "r-handbook-v1")
        types = [e["event_type"] for e in lineage["events"]]
        self.assertIn("REVOCATION", types)
        statuses = [h["status"] for h in lineage["license_history"]]
        self.assertEqual(statuses, ["SUPERSEDED", "REVOKED"])

    def test_replacement_material_lineage_both_directions(self) -> None:
        self.svc.register_replacement(
            admin,
            old_resource_id="r-handbook-v1",
            new_resource_id="r-handbook-v2-intl",
            title="数控实训手册（国际版）",
            digest="sha256:handbook0002",
            resource_type="manual",
            rights_holder="本校出版社",
            territories=["*"],
            org_scope={"type": "ALL", "orgs": []},
            basis="替代材料授权函",
            note="替换本国限定版用于海外交付",
        )
        # 新材料全球可用，旧材料许可仍在（谱系可追溯）
        pkg = self.svc.build_package(
            provider, name="海外替代包",
            resource_ids=["r-handbook-v2-intl"], recipient=OVERSEAS_RECIPIENT)
        self.assertEqual(pkg["status"], "SUCCEEDED")
        lineage = self.svc.get_lineage(provider, "r-handbook-v1")
        repl = [e for e in lineage["events"] if e["event_type"] == "REPLACEMENT"]
        self.assertEqual(repl[0]["detail"]["replaced_by"], "r-handbook-v2-intl")
        new = self.svc.get_resource(provider, "r-handbook-v2-intl")
        self.assertEqual(new["metadata"]["replaces"], "r-handbook-v1")

    def test_replacement_with_revoke_old(self) -> None:
        self.svc.register_replacement(
            admin,
            old_resource_id="r-handbook-v1",
            new_resource_id="r-handbook-v3",
            title="手册 v3", digest="sha256:h3",
            rights_holder="本校出版社", territories=["CN"],
            org_scope={"type": "ALL", "orgs": []}, basis="b",
            revoke_old=True, note="旧版停用",
        )
        head = self.svc.get_resource(provider, "r-handbook-v1")["current_license"]
        self.assertIsNone(head)  # 旧材料 head 已撤
        events = [e["event_type"] for e in
                  self.svc.get_lineage(provider, "r-handbook-v1")["events"]]
        self.assertIn("REVOCATION", events)
        self.assertIn("REPLACEMENT", events)


class ImpactAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp, self.clock = make_service()
        _reg(self.svc, provider, HANDBOOK)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_impact_shows_packages_pinned_to_old_license_version(self) -> None:
        pkg1 = self.svc.build_package(
            provider, name="包A（2026春）", resource_ids=["r-handbook-v1"],
            recipient=CN_RECIPIENT, idempotency_key="a")
        pkg2 = self.svc.build_package(
            provider, name="包B（2026春，重复组包）", resource_ids=["r-handbook-v1"],
            recipient=CN_RECIPIENT, idempotency_key="b")
        # 权利变化：部分授权扩展到法国
        self.svc.grant_license(
            admin, resource_id="r-handbook-v1", rights_holder="本校出版社",
            territories=["CN", "FR"],
            org_scope={"type": "WHITELIST", "orgs": ["ORG_CN_BRANCH", OVERSEAS_ORG]},
            basis="补充协议", partial=True, note="扩围")
        impact = self.svc.impact_analysis(provider, "r-handbook-v1")
        self.assertEqual(impact["affected_package_count"], 2)
        ids = {p["package_id"] for p in impact["affected_packages"]}
        self.assertEqual(ids, {pkg1["package_id"], pkg2["package_id"]})
        for p in impact["affected_packages"]:
            pinned = p["pinned_versions"][0]
            self.assertEqual(pinned["license_version"], 1)
            self.assertFalse(pinned["is_current_head"])
            self.assertEqual(pinned["license_status_now"], "SUPERSEDED")
        # 撤回后同样能反查到受影响的包
        self.svc.revoke_license(admin, "r-handbook-v1", reason="终止")
        impact2 = self.svc.impact_analysis(provider, "r-handbook-v1")
        self.assertEqual(impact2["affected_package_count"], 2)
        self.assertIsNone(impact2["current_license"])


class IsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp, self.clock = make_service()
        _reg(self.svc, provider, HANDBOOK)
        self.pkg = self.svc.build_package(
            provider, name="内部包", resource_ids=["r-handbook-v1"],
            recipient=CN_RECIPIENT, idempotency_key="iso")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_other_org_cannot_read_resource_or_package(self) -> None:
        with self.assertRaises(NotFound):
            self.svc.get_resource(other_provider, "r-handbook-v1")
        with self.assertRaises(NotFound):
            self.svc.get_package(other_provider, self.pkg["package_id"])
        with self.assertRaises(NotFound):
            self.svc.download_package(other_provider, self.pkg["package_id"])
        with self.assertRaises(NotFound):
            self.svc.get_lineage(other_provider, "r-handbook-v1")
        with self.assertRaises(NotFound):
            self.svc.impact_analysis(other_provider, "r-handbook-v1")
        # 列表层面同样不可见
        self.assertEqual(self.svc.list_resources(other_provider), [])
        self.assertEqual(self.svc.list_packages(other_provider), [])
        # 其它机构管理员不能对本校资源做授权变更
        with self.assertRaises(NotFound):
            self.svc.revoke_license(other_admin, "r-handbook-v1", reason="越权撤回")
        # 原机构数据完好
        self.assertEqual(
            self.svc.get_resource(provider, "r-handbook-v1")["resource_id"],
            "r-handbook-v1")


class SnapshotImmutabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.tmp, self.clock = make_service()
        _reg(self.svc, provider, HANDBOOK)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_pinned_before_change_is_not_rewritten(self) -> None:
        pkg = self.svc.build_package(
            provider, name="冻结包", resource_ids=["r-handbook-v1"],
            recipient=CN_RECIPIENT)
        pinned = pkg["items"][0]["snapshot"]
        self.assertEqual(pinned["license"]["territories"], ["CN"])
        self.svc.grant_license(
            admin, resource_id="r-handbook-v1", rights_holder="本校出版社",
            territories=["CN", "DE"],
            org_scope={"type": "ALL", "orgs": []}, basis="补充协议",
            partial=True, note="增补德国")
        reread = self.svc.get_package(provider, pkg["package_id"])
        still = reread["items"][0]["snapshot"]
        self.assertEqual(still["license"]["territories"], ["CN"])
        self.assertEqual(still["item_digest"], pinned["item_digest"])
        self.assertEqual(reread["snapshot_digest"], pkg["snapshot_digest"])


if __name__ == "__main__":
    unittest.main()
