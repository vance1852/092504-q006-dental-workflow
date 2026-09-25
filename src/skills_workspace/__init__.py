"""技能赛训协作基础服务的服务端基础包。"""

from .service import DomainService
from .tracing import TracingService

__all__ = ["DomainService", "TracingService"]
