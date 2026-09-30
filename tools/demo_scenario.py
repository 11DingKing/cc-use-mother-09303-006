"""端到端业务场景演示（不依赖 HTTP，直接走领域服务）。

运行：python3 tools/demo_scenario.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from licensing_service.errors import (  # noqa: E402
    DeliveryBlocked,
    PackageVerificationFailed,
)
from licensing_service.services import LicensingService  # noqa: E402
from licensing_service.store import Store  # noqa: E402


def show(title: str, payload=None) -> None:
    print(f"\n=== {title} ===")
    if payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    art = Path(tempfile.mkdtemp()) / "artifacts"
    svc = LicensingService(Store(":memory:"), art,
                           clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))

    svc.create_institution("CNU", "华北职业技术学院", "CN")
    svc.create_institution("OVS", "新加坡海外伙伴学院", "SG")
    teacher = svc.create_user("t1", "CNU", "王老师（资源提供）", "provider")
    officer = svc.create_user("c1", "CNU", "李版权（版权管理员）", "copyright")
    foreign = svc.create_user("r1", "OVS", "陈老师（接收方）", "recipient")

    show("1. 登记资源摘要与版本依赖")
    manual = svc.register_resource(
        owner_org_id="CNU", title="数控加工实训手册", kind="manual",
        summary="仅允许本国校内使用的实训手册", content=b"manual-v1", actor=teacher)
    slides = svc.register_resource(
        owner_org_id="CNU", title="数控加工课件", kind="slides",
        summary="可向海外伙伴交付的课件", content=b"slides-v1", actor=teacher)
    print(f"手册 {manual['id'][:8]}… / 课件 {slides['id'][:8]}…，当前版本均为 v1")

    show("2. 版权管理员授予授权：地域 / 机构 / 期限")
    svc.grant_license(
        resource_id=manual["id"], licensor_org_id="CNU", scope="full",
        territories=["CN"], org_ids=["CNU"],
        valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
        actor=officer)
    svc.grant_license(
        resource_id=slides["id"], licensor_org_id="CNU", scope="full",
        territories=["CN", "SG"], org_ids=["CNU", "OVS"],
        valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
        actor=officer)
    recipient = svc.register_recipient(
        owner_org_id="CNU", org_id="OVS", territory="SG",
        qualifications=["职教合作院校", "数控实训基地资质"],
        valid_from="2026-01-01T00:00:00Z", valid_until="2027-06-01T00:00:00Z",
        actor=teacher)
    print("手册：仅 CN/CNU；课件：CN+SG、CNU+OVS；接收方资格合格（SG）")

    show("3. 组包前逐项核验：整包交付被阻断，失败不留半成品")
    try:
        svc.build_package(
            name="中新合作数控课程包",
            resource_ids=[manual["id"], slides["id"]],
            recipient_id=recipient["id"], actor=teacher)
    except PackageVerificationFailed as exc:
        for v in exc.violations:
            print(f"  [{v['rule']}] {v['message']}")
        print(f"失败尝试 {exc.attempt_id[:8]}… 已留审计记录；产物目录内容："
              f"{list(art.iterdir()) or '空（无可下载半成品）'}")

    show("4. 替代材料：登记国际版手册（部分授权，逐要素许可）")
    intl = svc.register_resource(
        owner_org_id="CNU", title="数控加工实训手册（国际版）", kind="manual",
        summary="替代本国手册的海外版本", content=b"manual-intl-v1", actor=teacher)
    intl_lic = svc.grant_license(
        resource_id=intl["id"], licensor_org_id="CNU", scope="partial",
        territories=["CN", "SG"], org_ids=["CNU", "OVS"],
        valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
        grants=[{"subject": "课堂教学", "permitted": True},
                {"subject": "校内实训", "permitted": True}],
        actor=officer)
    pkg = svc.build_package(
        name="中新合作数控课程包", resource_ids=[intl["id"], slides["id"]],
        recipient_id=recipient["id"], actor=teacher)
    print(f"核验通过，包 {pkg['id'][:8]}… 已固定授权快照 {pkg['snapshot_digest'][:16]}…")

    show("5. 交付成功，接收方获得产物")
    svc.deliver(pkg["id"], actor=teacher)
    data, _ = svc.download_artifact(pkg["id"], viewer=foreign)
    print(f"接收方下载 {len(data)} 字节，包状态：delivered")

    show("6. 许可撤回：再次交付被再核验阻断")
    svc.revoke_license(intl_lic["id"], "版权方通知：海外实训许可撤回", officer)
    try:
        svc.deliver(pkg["id"], actor=teacher)
    except DeliveryBlocked as exc:
        for v in exc.violations:
            print(f"  [{v['rule']}] {v['message']}")

    show("7. 受权人员查看权利变化影响了哪些包")
    impact = svc.rights_change_impact(intl_lic["id"], viewer=officer)
    for a in impact["affected_packages"]:
        print(f"  包 {a['package_id'][:8]}…（{a['name']}/{a['status']}）→ {a['impact']}")

    show("8. 谱系（撤回与固定快照长期保留）")
    lineage = svc.get_lineage(pkg["id"], viewer=officer)
    for e in lineage["lineage"]:
        print(f"  {e['created_at']} {e['event_type']}: {e['detail']}")

    print("\n演示完成。")


if __name__ == "__main__":
    main()
