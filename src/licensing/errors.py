"""领域错误层次。"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """所有可预期领域错误的基类。"""

    code = "DOMAIN_ERROR"
    http_status = 400

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


class ValidationError(DomainError):
    """输入不满足领域约束。"""

    code = "VALIDATION_ERROR"
    http_status = 400


class NotFound(DomainError):
    """对象不存在，或对当前机构不可见（不泄露存在性）。"""

    code = "NOT_FOUND"
    http_status = 404


class AccessDenied(DomainError):
    """对象存在，但当前身份无权执行该动作。"""

    code = "ACCESS_DENIED"
    http_status = 403


class Conflict(DomainError):
    """与当前对象状态冲突（重复版本、重复幂等键等）。"""

    code = "CONFLICT"
    http_status = 409


class BuildRejected(DomainError):
    """组包核验未通过。

    details 为逐项核验失败原因，调用方可据此修正清单后重试。
    """

    code = "BUILD_REJECTED"
    http_status = 422

    def __init__(self, reasons: list[dict]) -> None:
        super().__init__("组包核验未通过", reasons)
        self.reasons = reasons
