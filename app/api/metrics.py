"""指标汇总结算端点：``GET /metrics/summary``。

契约 API_CONTRACT §8 与 EVALUATION §3。

**本层职责边界**：只做查询参数解析、服务调用、错误映射。
所有计算逻辑在 ``app/services/metrics_service.py``，本模块不碰数据库。

契约里有两处必须原样遵守的语义，都在响应的组装里体现：

1. **空数据返回 ``null`` 而不是 0**（``scope="empty"``）。新实例问
   "我跑了多少"的正确答案是"零次"，不是 404 也不是"成功率 0%"。
   这个分支由服务层完成，本层不做任何"补零"加工。

2. **``group_by`` 非法值返回 400**。静默忽略非法分组维度会让
   调用方拿到一个"看起来有分组但实际没分组"的响应 ——
   这比报错危险得多。校验在服务层做（保持唯一真源），
   本层负责把异常映射成契约要求的错误体。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import MetricsServiceDep
from app.core.logging import get_logger
from app.schemas.common import DataScope
from app.schemas.metrics import (
    DATA_SOURCE_NOTE,
    MetricsSummaryResponse,
)

logger = get_logger(__name__)

router = APIRouter(tags=["metrics"])

# 依赖用 ``MetricsServiceDep``（函数包装），不要写成 ``Depends(MetricsService)``。
# 直接依赖类会让 FastAPI 内省 ``__init__``，把其中的
# ``session_factory`` 当成查询参数暴露出去（详见 app/api/deps.py）。
ServiceDep = MetricsServiceDep

# 契约 §8 允许的四个分组维度。这里**只用于生成 OpenAPI 文档的枚举说明**，
# 真正的校验在 MetricsService（唯一真源），避免两处规则漂移。
GROUP_BY_VALUES = ("agent_version", "prompt_version", "model_name", "day")


@router.get(
    "/metrics/summary",
    response_model=MetricsSummaryResponse,
    summary="汇总运行级指标",
)
def get_metrics_summary(
    service: ServiceDep,
    started_after: Annotated[
        datetime | None,
        Query(description="ISO 8601，按 run.started_at 下界过滤（含）"),
    ] = None,
    started_before: Annotated[
        datetime | None,
        Query(description="ISO 8601，按 run.started_at 上界过滤（含）"),
    ] = None,
    agent_version: Annotated[str | None, Query(max_length=40)] = None,
    prompt_version: Annotated[str | None, Query(max_length=40)] = None,
    model_name: Annotated[str | None, Query(max_length=200)] = None,
    group_by: Annotated[
        str | None,
        Query(description="分组维度：agent_version / prompt_version / model_name / day"),
    ] = None,
) -> MetricsSummaryResponse:
    """汇总运行级指标。

    契约 §8 的响应字段与 EVALUATION §3 的口径一一对应，
    唯一的加工是：把 ``scope`` 归一化后再返回（见 ``_normalize``）。
    """
    result = service.summary(
        started_after=started_after,
        started_before=started_before,
        agent_version=agent_version,
        prompt_version=prompt_version,
        model_name=model_name,
        group_by=group_by,
    )

    logger.info(
        "metrics_summary_computed",
        extra={
            "run_count": result.run_count,
            "scope": result.scope.value,
            "group_by": group_by,
            "has_groups": bool(result.groups),
        },
    )

    return _normalize(result)


def _normalize(result: MetricsSummaryResponse) -> MetricsSummaryResponse:
    """保证响应在边界情况下仍然自洽。

    两处保险，都属于"绝不能撒谎"的范畴：

    1. **``data_source_note`` 不能被清空**。契约 B9 要求任何指标输出
       都带口径声明。若服务层因故给出空字符串（例如未来重构漏填），
       这里补回默认文案 —— 宁可重复声明，也不能让响应静默失去声明。
    2. **``run_count == 0`` 时 ``scope`` 必须是 ``empty``**。
       这两者在契约里是绑定的：只要声称"有数据"，就必须真的
       有一行 run 支撑；否则调用方会拿着 ``run_count=0`` 而
       ``scope=sample_runs`` 的响应去猜到底是哪种情况。
    """
    updates: dict[str, Any] = {}

    if not result.data_source_note or not result.data_source_note.strip():
        updates["data_source_note"] = DATA_SOURCE_NOTE

    if result.run_count == 0 and result.scope is not DataScope.EMPTY:
        logger.warning(
            "scope_run_count_mismatch",
            extra={"scope": result.scope.value, "run_count": result.run_count},
        )
        updates["scope"] = DataScope.EMPTY

    if not updates:
        return result
    return result.model_copy(update=updates)


__all__ = ["GROUP_BY_VALUES", "get_metrics_summary", "router"]
