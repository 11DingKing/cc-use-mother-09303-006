"""职业教育资源授权后端。

领域子模块：

- errors：领域错误
- clock：可注入时钟
- canon：确定性序列化与摘要
- store：SQLite 持久化
- policy：逐项授权核验
- service：领域服务（登记、授权、组包、谱系、影响分析）
- api：HTTP/JSON 接口
"""
from .clock import Clock, SystemClock
from .errors import (
    AccessDenied,
    BuildRejected,
    Conflict,
    DomainError,
    NotFound,
    ValidationError,
)
from .service import Actor, LicensingService
from .store import Store

__all__ = [
    "AccessDenied",
    "Actor",
    "BuildRejected",
    "Clock",
    "Conflict",
    "DomainError",
    "LicensingService",
    "NotFound",
    "Store",
    "SystemClock",
    "ValidationError",
]
