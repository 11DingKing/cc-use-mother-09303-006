"""规范化 JSON 与摘要算法。

授权快照要求“固定”：同一组字段无论何时重算都得到同一哈希，因此统一使用
键排序、无空白、UTF-8 的规范编码，禁止 NaN/Infinity。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonicalize(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonicalize(value)).hexdigest()


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
