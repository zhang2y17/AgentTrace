"""``GET /health`` —— 健康检查（API_CONTRACT §1）。

契约 SECURITY：
1. 响应**不得**包含任何密钥信息，只暴露"是否已配置"的布尔值；
2. 组件异常时错误信息必须脱敏且截断。

契约 ARCHITECTURE §6：
- 任一组件不可用时 HTTP 仍返回 200，``status`` 变 ``degraded``，
  由调用方按组件自行决断。这样编排系统可以区分"进程活着但依赖挂了"
  与"进程都起不来"两种情况。

S2 阶段：只实现 ``api`` 组件与 ``llm_provider``；
``database`` / ``redis`` 组件在 S3 阶段接入（此处预留探针注册机制）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.redaction import redact, truncate
from app.schemas.common import HealthStatus
from app.schemas.health import ComponentHealth, HealthResponse, LlmProviderHealth

router = APIRouter(tags=["health"])

# 错误信息与补充说明的长度上限
_MAX_PROBE_DETAIL_CHARS = 300


def _sanitize_probe_text(value: str | None) -> str | None:
    """对探针返回的说明/错误信息统一脱敏与截断。

    探针来源于数据库驱动、Redis 客户端等第三方组件，其错误消息**可能内嵌
    连接串或密钥**（例如 ``auth failed: redis://:password@host``）。
    因此所有探针输出在进入响应前都必须过一遍脱敏。
    """
    if value is None:
        return None
    return truncate(redact(value), _MAX_PROBE_DETAIL_CHARS)


# 探针类型：返回 (状态, 补充说明或 None, 错误信息或 None)
ComponentProbe = Callable[[Settings], tuple[HealthStatus, str | None, str | None]]

_component_probes: dict[str, ComponentProbe] = {}


def register_component_probe(name: str, probe: ComponentProbe) -> None:
    """注册一个组件探针。

    S3 阶段会通过本函数注册 database 与 redis 探针，
    这样 ``health.py`` 不需要反向依赖 ``app.db`` 或 ``app.services``
    （符合 ARCHITECTURE §2 分层规则：api 层不直接依赖 db 层）。
    """
    _component_probes[name] = probe


def clear_component_probes() -> None:
    """清空探针注册表。仅用于测试。"""
    _component_probes.clear()


def _probe_api(_settings: Settings) -> tuple[HealthStatus, str | None, str | None]:
    """API 自身探针：能执行到这里说明进程健康。"""
    return HealthStatus.OK, "asgi_app_alive", None


def _build_llm_health(settings: Settings) -> LlmProviderHealth:
    """构造 LLM provider 健康信息。

    注意：``api_key_configured`` 只暴露布尔值。
    契约 B8 要求密钥不得出现在任何响应中。
    """
    key_configured = settings.llm_api_key is not None and bool(
        settings.llm_api_key.get_secret_value()
    )

    if settings.llm_provider == "fake":
        status = HealthStatus.OK
    elif settings.llm_provider == "openai" and not key_configured:
        # 配置了真实 provider 但没有密钥：属于降级状态，需要调用方知晓
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.OK

    return LlmProviderHealth(
        status=status,
        provider=settings.llm_provider,
        is_test_double=settings.is_test_double_mode,
        model_name=settings.llm_model,
        api_key_configured=key_configured,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="健康检查",
    description=(
        "返回 API 与各依赖组件的状态。任一组件异常时 HTTP 仍为 200，"
        "但 status 变为 degraded。响应中不含任何密钥信息。"
    ),
)
def get_health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """检查 API 与依赖组件状态。"""
    components: dict[str, ComponentHealth] = {}

    started = time.perf_counter()
    api_status, api_detail, api_error = _probe_api(settings)
    components["api"] = ComponentHealth(
        status=api_status,
        latency_ms=int((time.perf_counter() - started) * 1000),
        detail=_sanitize_probe_text(api_detail),
        error=_sanitize_probe_text(api_error),
    )

    # 已注册的依赖探针（S3 会注册 database / redis）
    for name, probe in _component_probes.items():
        probe_started = time.perf_counter()
        try:
            status, detail, error = probe(settings)
        except Exception as exc:  # noqa: BLE001 —— 探针本身绝不能把 /health 打挂
            status = HealthStatus.ERROR
            detail = None
            error = f"{type(exc).__name__}: {exc}"
        components[name] = ComponentHealth(
            status=status,
            latency_ms=int((time.perf_counter() - probe_started) * 1000),
            detail=_sanitize_probe_text(detail),
            error=_sanitize_probe_text(error),
        )

    llm_health = _build_llm_health(settings)
    overall = _aggregate_status(components, llm_health)

    return HealthResponse(
        status=overall,
        service=settings.app_name,
        version=settings.app_version,
        components=components,
        llm_provider=llm_health,
        checked_at=datetime.now(UTC),
    )


def _aggregate_status(
    components: dict[str, ComponentHealth], llm_health: LlmProviderHealth
) -> HealthStatus:
    """汇总整体健康状态。

    ``llm_provider`` 也参与汇总：配置了真实 provider 却没有密钥是必须
    让调用方知晓的降级状态，不能因为"其他组件都正常"就报 ``ok``。
    否则部署时漏配密钥会静默通过健康检查。
    """
    if any(c.status in (HealthStatus.ERROR, HealthStatus.DEGRADED) for c in components.values()):
        return HealthStatus.DEGRADED
    if llm_health.status is not HealthStatus.OK:
        return HealthStatus.DEGRADED
    return HealthStatus.OK


__all__ = [
    "ComponentProbe",
    "clear_component_probes",
    "get_health",
    "register_component_probe",
    "router",
]
