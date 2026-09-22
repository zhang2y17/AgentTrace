"""把数据库与 Redis 探针注册到 /health。

设计考虑（ARCHITECTURE §2 分层规则 L1）：
``app.api.health`` 不直接依赖 ``app.db`` 或 ``app.services``，
因此注册动作放在本模块，由 ``app.main`` 在启动时调用一次。

这样做的收益：``health.py`` 保持对基础设施的零依赖，
新增依赖组件时只需在这里加一行注册。
"""

from __future__ import annotations

from app.api.health import register_component_probe
from app.core.config import Settings
from app.core.redaction import truncate
from app.schemas.common import HealthStatus


def _database_probe(settings: Settings) -> tuple[HealthStatus, str | None, str | None]:
    """探测数据库连通性。

    返回的 detail 只包含方言名（不含连接串），error 由 health 层统一脱敏。
    """
    from app.db.session import check_database_health

    healthy, error = check_database_health(settings)
    if healthy:
        return HealthStatus.OK, settings.database_dialect, None
    return HealthStatus.ERROR, None, truncate(error or "database unreachable", 200)


def _redis_probe(settings: Settings) -> tuple[HealthStatus, str | None, str | None]:
    """探测 Redis 连通性。

    注意：Redis 不可用时返回 ``DEGRADED`` 而非 ``ERROR``（当 ``redis_required=False``），
    因为它承载的是可选的短期任务状态，不是核心路径。
    """
    from app.services.cache import check_redis_health

    healthy, error = check_redis_health(settings)
    if healthy:
        return HealthStatus.OK, "short_term_task_state", None

    if settings.redis_required:
        return HealthStatus.ERROR, "required", truncate(error or "redis unreachable", 200)
    return HealthStatus.DEGRADED, "optional, degraded", truncate(error or "redis unreachable", 200)


def register_infrastructure_probes() -> None:
    """注册数据库与 Redis 探针。

    幂等：重复调用只覆盖同名探针，不会重复注册。
    """
    register_component_probe("database", _database_probe)
    register_component_probe("redis", _redis_probe)


__all__ = ["register_infrastructure_probes"]
