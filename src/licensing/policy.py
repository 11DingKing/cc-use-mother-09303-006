"""授权核验引擎：组包前对每个资源逐项核验。

核验维度（与领域契约不变量“地域许可”对应）：

1. LICENSED：资源存在且有 ACTIVE 授权版本；
2. TIME_WINDOW：当前时间落在 valid_from / valid_until 内；
3. TERRITORY：接收方所在国家/地区在许可地域内；
4. ORG_SCOPE：接收机构在机构范围内；
5. RECIPIENT_QUALIFICATION：接收方满足资格条件；
6. DEPENDENCY：必选版本依赖同时在包内且通过核验。

核验结果是纯数据，可直接固化进授权快照。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .clock import iso


@dataclass
class Recipient:
    org_id: str
    org_name: str
    country: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "org_name": self.org_name,
            "country": self.country,
            "attrs": self.attrs,
        }


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Verification:
    resource_id: str
    ok: bool
    checks: list[Check]
    license_row: Any = None  # sqlite3.Row，核验通过时存在
    reasons: list[str] = field(default_factory=list)

    def failure(self) -> dict | None:
        if self.ok:
            return None
        return {
            "resource_id": self.resource_id,
            "reasons": self.reasons,
            "checks": [c.to_dict() for c in self.checks],
        }


def _check_territory(license_row: Any, recipient: Recipient) -> Check:
    territories: list[str] = _json(license_row["territories_json"])
    passed = "*" in territories or recipient.country in territories
    detail = (
        f"接收方地区 {recipient.country} 在许可地域内"
        if passed
        else f"接收方地区 {recipient.country} 不在许可地域 {territories} 内"
    )
    return Check("TERRITORY", passed, detail)


def _check_org_scope(license_row: Any, recipient: Recipient) -> Check:
    scope = _json(license_row["org_scope_json"])
    kind = scope.get("type", "WHITELIST")
    orgs = set(scope.get("orgs", []))
    if kind == "ALL":
        passed = True
    elif kind == "EXCLUDE":
        passed = recipient.org_id not in orgs
    else:  # WHITELIST（默认，最保守）
        passed = recipient.org_id in orgs
    detail = (
        f"接收机构 {recipient.org_id} 符合机构范围（{kind}）"
        if passed
        else f"接收机构 {recipient.org_id} 不在机构范围（{kind}: {sorted(orgs)}）内"
    )
    return Check("ORG_SCOPE", passed, detail)


def _check_time_window(license_row: Any, now: datetime) -> Check:
    start = license_row["valid_from"]
    end = license_row["valid_until"]
    after_start = start is None or iso(now) >= start
    before_end = end is None or iso(now) < end
    passed = after_start and before_end
    if not passed:
        detail = f"当前时间 {iso(now)} 不在授权期限 [{start}, {end}) 内"
    elif start is None and end is None:
        detail = "授权长期有效"
    else:
        detail = f"当前时间 {iso(now)} 在授权期限 [{start}, {end}) 内"
    return Check("TIME_WINDOW", passed, detail)


_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "truthy": lambda a, b: bool(a) == bool(b),
}


def _check_qualification(license_row: Any, recipient: Recipient) -> Check:
    rules = _json(license_row["recipient_qual_json"]).get("conditions", [])
    failed: list[str] = []
    for rule in rules:
        attr = rule["attr"]
        op = rule.get("op", "eq")
        expected = rule.get("value")
        if attr not in recipient.attrs:
            failed.append(f"缺少资格属性 {attr}")
            continue
        actual = recipient.attrs[attr]
        if not _OPS[op](actual, expected):
            failed.append(f"{attr}={actual!r} 不满足 {op} {expected!r}")
    passed = not failed
    detail = "接收方满足全部资格条件" if passed else "；".join(failed)
    return Check("RECIPIENT_QUALIFICATION", passed, detail)


def _json(value: str) -> Any:
    import json

    return json.loads(value)


def verify_resource(
    resource_id: str,
    store: Any,
    recipient: Recipient,
    now: datetime,
) -> Verification:
    """核验单个资源；不递归依赖（依赖由 expand_and_verify 统一处理）。"""
    resource = store.get_resource(resource_id)
    checks: list[Check] = []
    if resource is None:
        return Verification(
            resource_id,
            False,
            [Check("LICENSED", False, "资源未登记")],
            reasons=["资源未登记"],
        )
    head = store.get_head_license(resource_id)
    if head is None:
        return Verification(
            resource_id,
            False,
            [Check("LICENSED", False, "资源不存在生效中的授权")],
            reasons=["资源不存在生效中的授权"],
        )
    checks.append(Check("LICENSED", True, f"授权版本 v{head['version']}（{head['license_id']}）生效中"))
    for check in (
        _check_time_window(head, now),
        _check_territory(head, recipient),
        _check_org_scope(head, recipient),
        _check_qualification(head, recipient),
    ):
        checks.append(check)
    reasons = [c.detail for c in checks if not c.passed]
    return Verification(resource_id, not reasons, checks, license_row=head, reasons=reasons)


def expand_required(resource_id: str, store: Any, seen: set[str] | None = None) -> list[str]:
    """求必选依赖闭包（含自身），有环时安全终止。"""
    seen = seen if seen is not None else set()
    order: list[str] = []
    if resource_id in seen:
        return order
    seen.add(resource_id)
    for dep in store.list_dependencies(resource_id):
        if dep["required"]:
            order.extend(expand_required(dep["dep_resource_id"], store, seen))
    order.append(resource_id)
    return order


def expand_and_verify(
    requested: list[str],
    store: Any,
    recipient: Recipient,
    now: datetime,
) -> tuple[dict[str, Verification], dict[str, list[dict]]]:
    """对清单做必选依赖闭包扩展并逐项核验。

    返回：
    - 资源 -> 核验结果（含自动纳入的必选依赖）；
    - 缺失依赖映射：资源 -> 未登记的必选依赖描述。
    """
    selected: list[str] = []
    chosen: set[str] = set()
    missing: dict[str, list[dict]] = {}
    for rid in requested:
        closure = expand_required(rid, store)
        for dep_id in closure:
            if store.get_resource(dep_id) is None:
                missing.setdefault(rid, []).append({"dep_resource_id": dep_id, "required": True})
            if dep_id not in chosen and store.get_resource(dep_id) is not None:
                chosen.add(dep_id)
                selected.append(dep_id)
    # 清单中显式列出的可选依赖若未被闭包带入，也纳入核验
    for rid in requested:
        if rid not in chosen and store.get_resource(rid) is not None:
            chosen.add(rid)
            selected.append(rid)
    results = {
        rid: verify_resource(rid, store, recipient, now) for rid in selected
    }
    return results, missing
