"""确定性 JSON 序列化与摘要，用于授权快照与不可变记录指纹。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> bytes:
    """键排序、无空白、UTF-8 的规范编码，跨进程稳定。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    ).encode("utf-8")


def _default(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"无法规范化类型：{type(obj)!r}")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
