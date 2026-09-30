"""逐项核验规则。

所有规则为纯函数，输入仓储读出的快照字典，输出违规列表。
组包前与交付前共用同一套规则，保证“组包前逐项核验”和
“交付前再核验”结论一致。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional


def _loads(raw: str) -> Any:
    return json.loads(raw) if raw else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def check_license_status(license_row: dict[str, Any]) -> Optional[dict[str, Any]]:
    if license_row["status"] != "active":
        return {
            "rule": "license_status",
            "resource_id": license_row["resource_id"],
            "message": f"授权状态为 {license_row['status']}，不可传播",
        }
    return None


def check_territory(license_row: dict[str, Any], recipient: dict[str, Any]) -> Optional[dict[str, Any]]:
    """地域许可：接收方所在地域必须落在授权地域集合内。

    ``"*"`` 表示不限地域；否则为 ISO 国家码白名单。
    """
    allowed = _loads(license_row["territories_json"])
    target = recipient["territory"]
    if allowed != "*" and target not in allowed:
        return {
            "rule": "territory",
            "resource_id": license_row["resource_id"],
            "message": f"资源仅授权地域 {sorted(allowed) if isinstance(allowed, list) else allowed}，"
                       f"接收方所在地域 {target} 不在范围内",
        }
    return None


def check_org(license_row: dict[str, Any], recipient: dict[str, Any]) -> Optional[dict[str, Any]]:
    """机构范围：接收院校必须在授权机构白名单内（"*" 表示不限机构）。"""
    allowed = _loads(license_row["org_ids_json"])
    target = recipient["org_id"]
    if allowed != "*" and target not in allowed:
        return {
            "rule": "org_scope",
            "resource_id": license_row["resource_id"],
            "message": "接收机构不在授权机构范围内",
        }
    return None


def check_term(license_row: dict[str, Any], now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """期限：当前时间必须落在 [valid_from, valid_until) 内。"""
    moment = _now(now)
    start = _parse_dt(license_row["valid_from"])
    end = _parse_dt(license_row["valid_until"])
    if start and moment < start:
        return {"rule": "term", "resource_id": license_row["resource_id"],
                "message": "授权尚未生效"}
    if end and moment >= end:
        return {"rule": "term", "resource_id": license_row["resource_id"],
                "message": f"授权已于 {license_row['valid_until']} 到期"}
    return None


def check_version(license_row: dict[str, Any], resource_version: int) -> Optional[dict[str, Any]]:
    """版本依赖：授权只覆盖其签发时锁定的版本，版本升级需重新授权。"""
    if resource_version != license_row["current_version"]:
        return {
            "rule": "version",
            "resource_id": license_row["resource_id"],
            "message": f"资源版本 v{resource_version} 与授权锁定版本 v{license_row['current_version']} 不一致",
        }
    return None


def check_partial_scope(license_row: dict[str, Any],
                        grants: list[dict[str, Any]],
                        usage: str = "课程包整体交付") -> Optional[dict[str, Any]]:
    """部分授权：partial 授权必须逐项核验，存在禁止项即不可整体交付。"""
    if license_row["scope"] != "partial":
        return None
    denied = [g["subject"] for g in grants if not g["permitted"]]
    if denied:
        return {
            "rule": "partial_scope",
            "resource_id": license_row["resource_id"],
            "message": f"部分授权禁止以下要素用于{usage}：{denied}",
            "denied_subjects": denied,
        }
    return None


def check_recipient_qualified(recipient: dict[str, Any],
                              now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """接收方资格：状态合格且资格在有效期内。"""
    moment = _now(now)
    if recipient["status"] != "qualified":
        return {"rule": "recipient_status", "resource_id": None,
                "message": f"接收方资格状态为 {recipient['status']}"}
    end = _parse_dt(recipient["valid_until"])
    if end and moment >= end:
        return {"rule": "recipient_term", "resource_id": None,
                "message": "接收方资格已过期"}
    start = _parse_dt(recipient["valid_from"])
    if start and moment < start:
        return {"rule": "recipient_term", "resource_id": None,
                "message": "接收方资格尚未生效"}
    return None


def verify_item(*, license_row: dict[str, Any], grants: list[dict[str, Any]],
                resource_version: int, recipient: dict[str, Any],
                now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """对单个资源条目执行全部核验，返回违规列表（空列表即通过）。"""
    violations: list[dict[str, Any]] = []
    for finding in (
        check_license_status(license_row),
        check_territory(license_row, recipient),
        check_org(license_row, recipient),
        check_term(license_row, now),
        check_version(license_row, resource_version),
        check_partial_scope(license_row, grants),
        check_recipient_qualified(recipient, now),
    ):
        if finding:
            violations.append(finding)
    return violations


def verify_dependencies(resource_id: str,
                        included: set[str],
                        dependency_map: dict[str, list[str]]) -> list[dict[str, Any]]:
    """版本依赖图谱：资源声明的依赖必须全部随包包含，否则越权/残缺交付。"""
    missing = [dep for dep in dependency_map.get(resource_id, []) if dep not in included]
    if missing:
        return [{"rule": "dependency", "resource_id": resource_id,
                 "message": f"缺少依赖资源 {missing}", "missing": missing}]
    return []
