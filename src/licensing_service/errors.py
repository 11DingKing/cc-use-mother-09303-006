"""领域错误与 HTTP 状态映射。"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class AuthenticationError(DomainError):
    status = 401
    code = "authentication_required"


class AuthorizationError(DomainError):
    status = 403
    code = "access_denied"


class NotFoundError(DomainError):
    """对无权感知的对象同样返回 404，避免跨机构探测。"""

    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class PackageVerificationFailed(DomainError):
    """组包核验失败：携带逐项违规与失败尝试编号。"""

    status = 422
    code = "package_verification_failed"

    def __init__(self, violations: list[dict[str, Any]], attempt_id: str) -> None:
        super().__init__("组包核验未通过，未生成任何可下载产物", {"violations": violations, "attempt_id": attempt_id})
        self.violations = violations
        self.attempt_id = attempt_id


class DeliveryBlocked(DomainError):
    status = 409
    code = "delivery_blocked"

    def __init__(self, violations: list[dict[str, Any]]) -> None:
        super().__init__("授权状态已变化，交付被阻断", {"violations": violations})
        self.violations = violations
