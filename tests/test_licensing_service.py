"""领域服务端到端测试：覆盖契约四不变式与题目全部谱系要求。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from licensing_service.errors import (  # noqa: E402
    AuthorizationError,
    DeliveryBlocked,
    NotFoundError,
    PackageVerificationFailed,
)
from licensing_service.services import LicensingService  # noqa: E402
from licensing_service.store import Store  # noqa: E402

FIXED_NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.art = Path(self.tmp) / "artifacts"
        self.svc = LicensingService(Store(":memory:"), self.art, clock=lambda: FIXED_NOW)
        s = self.svc
        s.create_institution("CNU", "国内职业学院", "CN")
        s.create_institution("OVS", "海外伙伴学院", "SG")
        s.create_institution("OTH", "第三方学院", "US")
        self.provider = s.create_user("u1", "CNU", "王老师", "provider")
        self.copyright = s.create_user("u2", "CNU", "李版权", "copyright")
        self.recipient_user = s.create_user("u3", "OVS", "海外接收员", "recipient")
        self.outsider = s.create_user("u4", "OTH", "外机构人员", "provider")
        self.manual = s.register_resource(
            owner_org_id="CNU", title="数控实训手册", kind="manual",
            summary="仅限本国校内使用", content=b"manual-v1", actor=self.provider)
        self.course = s.register_resource(
            owner_org_id="CNU", title="数控课件", kind="slides",
            summary="海外合作课件", content=b"course-v1",
            depends_on=[], actor=self.provider)
        self.lic_manual = s.grant_license(
            resource_id=self.manual["id"], licensor_org_id="CNU", scope="full",
            territories=["CN"], org_ids=["CNU"],
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
            actor=self.copyright)
        self.lic_course = s.grant_license(
            resource_id=self.course["id"], licensor_org_id="CNU", scope="full",
            territories=["CN", "SG"], org_ids=["CNU", "OVS"],
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
            actor=self.copyright)
        self.recipient = s.register_recipient(
            owner_org_id="CNU", org_id="OVS", territory="SG",
            qualifications=["vocational-partner"],
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-06-01T00:00:00Z",
            actor=self.provider)

    def grant_intl_manual(self) -> tuple[dict, dict]:
        res = self.svc.register_resource(
            owner_org_id="CNU", title="数控实训手册(国际版)", kind="manual",
            summary="替代材料，已获海外授权", content=b"manual-intl",
            actor=self.provider)
        lic = self.svc.grant_license(
            resource_id=res["id"], licensor_org_id="CNU", scope="partial",
            territories=["CN", "SG"], org_ids=["CNU", "OVS"],
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
            grants=[{"subject": "课堂教学", "permitted": True},
                    {"subject": "校内实训", "permitted": True}],
            actor=self.copyright)
        return res, lic


class TestVerification(ServiceTestBase):
    def test_domestic_only_manual_blocks_overseas_package(self) -> None:
        """题目主场景：本国校内手册随包出海，逐项核验命中地域与机构范围。"""
        with self.assertRaises(PackageVerificationFailed) as ctx:
            self.svc.build_package(
                name="合作课程包", resource_ids=[self.manual["id"], self.course["id"]],
                recipient_id=self.recipient["id"], actor=self.provider)
        rules = {v["rule"] for v in ctx.exception.violations}
        self.assertIn("territory", rules)
        self.assertIn("org_scope", rules)

    def test_failed_build_leaves_no_downloadable_artifact(self) -> None:
        """半成品清理：失败后产物目录为空，审计记录 artifact_path 为 NULL。"""
        with self.assertRaises(PackageVerificationFailed):
            self.svc.build_package(
                name="合作课程包", resource_ids=[self.manual["id"]],
                recipient_id=self.recipient["id"], actor=self.provider)
        self.assertEqual([], list(self.art.iterdir()))
        attempts = self.svc.list_attempts("合作课程包", self.provider)
        self.assertEqual(1, len(attempts))
        self.assertFalse(attempts[0]["ok"])
        self.assertIsNone(attempts[0]["artifact_path"])
        self.assertNotEqual([], attempts[0]["violations"])

    def test_expired_license_blocks(self) -> None:
        self.svc.grant_license(
            resource_id=self.manual["id"], licensor_org_id="CNU", scope="full",
            territories="*", org_ids="*",
            valid_from="2025-01-01T00:00:00Z", valid_until="2026-01-01T00:00:00Z",
            supersedes_id=self.lic_manual["id"], actor=self.copyright)
        with self.assertRaises(PackageVerificationFailed) as ctx:
            self.svc.build_package(
                name="包", resource_ids=[self.manual["id"]],
                recipient_id=self.recipient["id"], actor=self.provider)
        self.assertIn("term", {v["rule"] for v in ctx.exception.violations})

    def test_suspended_recipient_blocks(self) -> None:
        self.svc.suspend_recipient(self.recipient["id"], self.provider)
        with self.assertRaises(PackageVerificationFailed) as ctx:
            self.svc.build_package(
                name="包", resource_ids=[self.course["id"]],
                recipient_id=self.recipient["id"], actor=self.provider)
        self.assertIn("recipient_status", {v["rule"] for v in ctx.exception.violations})

    def test_version_dependency_mismatch_blocks(self) -> None:
        """版本依赖：授权锁定 v1，资源升级到 v2 后未重新授权不得组包。"""
        self.svc.new_resource_version(
            self.course["id"], content=b"course-v2", note="改版", actor=self.provider)
        with self.assertRaises(PackageVerificationFailed) as ctx:
            self.svc.build_package(
                name="包", resource_ids=[self.course["id"]],
                recipient_id=self.recipient["id"], actor=self.provider)
        self.assertIn("version", {v["rule"] for v in ctx.exception.violations})

    def test_missing_dependency_in_graph_blocks(self) -> None:
        """依赖图谱：声明依赖未随包包含即残缺交付。"""
        self.svc.new_resource_version(
            self.course["id"], content=b"course-v2",
            depends_on=[self.manual["id"]], note="增加对手册的依赖",
            actor=self.provider)
        # 重新授权课件 v2 给海外（地域/机构通过），但不加入手册
        self.svc.grant_license(
            resource_id=self.course["id"], licensor_org_id="CNU", scope="full",
            territories=["CN", "SG"], org_ids=["CNU", "OVS"],
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
            supersedes_id=self.lic_course["id"], actor=self.copyright)
        with self.assertRaises(PackageVerificationFailed) as ctx:
            self.svc.build_package(
                name="包", resource_ids=[self.course["id"]],
                recipient_id=self.recipient["id"], actor=self.provider)
        self.assertIn("dependency", {v["rule"] for v in ctx.exception.violations})

    def test_partial_grant_denied_subject_blocks(self) -> None:
        """部分授权：禁止项存在即不可整体交付。"""
        res, lic = self.grant_intl_manual()
        self.svc.revoke_license(lic["id"], "换发含禁止项的授权", self.copyright)
        self.svc.grant_license(
            resource_id=res["id"], licensor_org_id="CNU", scope="partial",
            territories=["CN", "SG"], org_ids=["CNU", "OVS"],
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
            grants=[{"subject": "课堂教学", "permitted": True},
                    {"subject": "商业再许可", "permitted": False}],
            actor=self.copyright)
        with self.assertRaises(PackageVerificationFailed) as ctx:
            self.svc.build_package(
                name="包", resource_ids=[res["id"]],
                recipient_id=self.recipient["id"], actor=self.provider)
        self.assertIn("partial_scope", {v["rule"] for v in ctx.exception.violations})

    def _latest_license(self, resource_id: str) -> str:
        row = self.svc.store.query_one(
            "SELECT id FROM licenses WHERE resource_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (resource_id,))
        return row["id"]


class TestSnapshotAndLineage(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        # 重复组包应基于同一份资源/授权，故夹具只建一次国际版手册
        self.intl, self.intl_lic = self.grant_intl_manual()

    def _build_good_package(self, name: str = "合作课程包") -> dict:
        return self.svc.build_package(
            name=name, resource_ids=[self.intl["id"], self.course["id"]],
            recipient_id=self.recipient["id"], actor=self.provider)

    def test_snapshot_is_fixed_and_artifact_atomic(self) -> None:
        pkg = self._build_good_package()
        self.assertEqual("verified", pkg["status"])
        self.assertIsNotNone(pkg["snapshot_digest"])
        data, stored = self.svc.download_artifact(pkg["id"], viewer=self.provider)
        self.assertTrue(data)
        # 撤回授权不改变已固定的快照内容
        self.svc.revoke_license(self.intl_lic["id"], "版权方撤回", self.copyright)
        again = self.svc.get_package(pkg["id"], viewer=self.provider)
        self.assertEqual(pkg["snapshot_digest"], again["snapshot_digest"])
        self.assertEqual("active", again["snapshot"]["items"][0]["license"]["status"])

    def test_repeated_build_keeps_lineage(self) -> None:
        p1 = self._build_good_package()
        p2 = self._build_good_package()
        self.assertEqual(2, p2["attempt_no"])
        self.assertEqual(p1["id"], p2["supersedes_package_id"])
        self.assertEqual("rebuild", p2["lineage"][0]["event_type"])
        # 两个版本的产物都在，可独立下载
        self.assertTrue(self.svc.download_artifact(p1["id"], viewer=self.provider)[0])
        self.assertTrue(self.svc.download_artifact(p2["id"], viewer=self.provider)[0])

    def test_material_replacement_lineage(self) -> None:
        p1 = self._build_good_package()
        alt = self.svc.register_resource(
            owner_org_id="CNU", title="通用实训指引", kind="manual",
            summary="全球可用替代材料", content=b"alt", actor=self.provider)
        self.svc.grant_license(
            resource_id=alt["id"], licensor_org_id="CNU", scope="full",
            territories="*", org_ids="*",
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
            actor=self.copyright)
        p2 = self.svc.replace_material(
            package_id=p1["id"], old_resource_id=self._intl_id(p1),
            new_resource_id=alt["id"], actor=self.provider)
        self.assertEqual(p1["id"], p2["supersedes_package_id"])
        types = {e["event_type"] for e in p2["lineage"]}
        self.assertIn("material_replaced", types)
        items = {i["resource_id"]: i for i in p2["items"]}
        self.assertEqual(self.intl_lic["id"], items[alt["id"]]["replaced_license_id"])

    def _intl_id(self, pkg: dict) -> str:
        return [i["resource_id"] for i in pkg["items"]
                if i["resource_id"] != self.course["id"]][0]

    def test_revocation_blocks_redelivery_and_records_lineage(self) -> None:
        pkg = self._build_good_package()
        self.svc.deliver(pkg["id"], actor=self.provider)
        self.svc.revoke_license(self.intl_lic["id"], "版权方撤回海外许可", self.copyright)
        with self.assertRaises(DeliveryBlocked) as ctx:
            self.svc.deliver(pkg["id"], actor=self.provider)
        rules = {v["rule"] for v in ctx.exception.violations}
        self.assertIn("license_status", rules)
        self.assertIn("snapshot_drift", rules)
        lineage = self.svc.get_lineage(pkg["id"], viewer=self.provider)["lineage"]
        self.assertTrue(any(e["event_type"] == "license_revoked" for e in lineage))

    def test_impact_analysis_permissions_and_states(self) -> None:
        p1 = self._build_good_package()
        p2 = self._build_good_package()
        alt = self.svc.register_resource(
            owner_org_id="CNU", title="通用实训指引", kind="manual",
            summary="替代", content=b"alt", actor=self.provider)
        self.svc.grant_license(
            resource_id=alt["id"], licensor_org_id="CNU", scope="full",
            territories="*", org_ids="*",
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
            actor=self.copyright)
        p3 = self.svc.replace_material(
            package_id=p2["id"], old_resource_id=self._intl_id(p2),
            new_resource_id=alt["id"], actor=self.provider)
        self.svc.deliver(p3["id"], actor=self.provider)
        self.svc.revoke_license(self.intl_lic["id"], "撤回", self.copyright)

        impact = {a["package_id"]: a["impact"]
                  for a in self.svc.rights_change_impact(self.intl_lic["id"], self.copyright)
                  ["affected_packages"]}
        self.assertEqual("rebuild_required", impact[p1["id"]])
        self.assertEqual("rebuild_required", impact[p2["id"]])
        self.assertEqual("superseded_by_replacement", impact[p3["id"]])
        # 无权机构既看不到该授权，影响分析返回 404
        with self.assertRaises(NotFoundError):
            self.svc.rights_change_impact(self.intl_lic["id"], self.outsider)
        # 接收方只能看到已交付给本机构的包
        viewer = {a["package_id"] for a in
                  self.svc.rights_change_impact(self.intl_lic["id"], self.recipient_user)
                  ["affected_packages"]}
        self.assertEqual({p3["id"]}, viewer)


class TestAccessControl(ServiceTestBase):
    def test_other_org_cannot_read_or_probe(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.get_resource(self.manual["id"], viewer=self.outsider)
        with self.assertRaises(NotFoundError):
            self.svc.get_license(self.lic_manual["id"], viewer=self.outsider)
        self.assertEqual([], self.svc.list_resources(self.outsider))

    def test_role_enforcement(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.svc.grant_license(
                resource_id=self.course["id"], licensor_org_id="CNU", scope="full",
                territories="*", org_ids="*",
                valid_from="2026-01-01T00:00:00Z", actor=self.provider)
        with self.assertRaises(AuthorizationError):
            self.svc.register_resource(
                owner_org_id="OTH", title="x", kind="k", summary="s",
                content=b"x", actor=self.provider)

    def test_recipient_read_only_after_delivery(self) -> None:
        intl, _ = self.grant_intl_manual()
        pkg = self.svc.build_package(
            name="合作课程包", resource_ids=[intl["id"], self.course["id"]],
            recipient_id=self.recipient["id"], actor=self.provider)
        with self.assertRaises(NotFoundError):
            self.svc.get_package(pkg["id"], viewer=self.recipient_user)
        self.svc.deliver(pkg["id"], actor=self.provider)
        seen = self.svc.get_package(pkg["id"], viewer=self.recipient_user)
        self.assertEqual(pkg["id"], seen["id"])
        with self.assertRaises(AuthorizationError):
            self.svc.deliver(pkg["id"], actor=self.recipient_user)


if __name__ == "__main__":
    unittest.main()
