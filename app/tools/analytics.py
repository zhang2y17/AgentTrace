"""统计工具：``calculate_latency_summary`` 与 ``calculate_cost_summary``。

契约 PROJECT_SPEC §3.3 第 2 条：

> 统计、阈值和成本计算必须由确定性代码完成，**不允许让 LLM 猜测数值**。

本模块的实现全部由 SQL 聚合 + 纯 Python 计算构成，不调用任何模型。
这一点由 ``ToolSpec.uses_llm = False`` 显式标注，并被契约校验检查。

分位数采用 **nearest-rank** 而非线性插值（EVALUATION §3 原则三）：
插值会产生样本中**从未观测到**的数值，用未观测值做验收是不诚实的。
nearest-rank 的结果一定是样本里真实存在的某个值。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import ModelCall, Run, TraceEvent
from app.tools.base import CostSummary, LatencySummary

logger = get_logger(__name__)

# 节点耗时样本的排序结果中，取分位数时的目标索引基准。
# nearest-rank 的定义：p 分位数 = 升序样本中第 ceil(p * n) 个元素（1-based）。
_P50: Final = 0.50
_P95: Final = 0.95


def nearest_rank(sorted_values: list[int], percentile: float) -> int | None:
    """nearest-rank 分位数。

    Args:
        sorted_values: **已升序排序**的样本。
        percentile: 0~1 之间，例如 0.95。

    Returns:
        分位数值；样本为空时返回 None。

    实现说明：nearest-rank 取 ``ceil(p * n)`` 位置（1-based）。
    以 5 个样本求 p95 为例，``ceil(0.95 * 5) = 5``，取最大值 ——
    这与"小样本下 p95 就是最大值"的直觉一致，
    也是本方法相比插值更保守、更诚实的地方。
    """
    if not sorted_values:
        return None
    if not 0.0 <= percentile <= 1.0:
        raise ValueError("percentile 必须在 0~1 之间")

    n = len(sorted_values)
    # ceil(p * n) 的整数实现，避免引入 math.ceil 的浮点边界问题
    rank = -(-int(round(percentile * 1000)) * n // 1000)  # 向上取整
    rank = max(1, min(rank, n))
    return sorted_values[rank - 1]


class AnalyticsTools:
    """两个统计工具的实现。

    Args:
        session_factory: 返回 ``Session`` 的可调用对象。
            刻意传入工厂而非 Session 实例：工具可能在请求结束后被调用，
            持有长生命周期 Session 会带来并发与事务边界问题。
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def calculate_latency_summary(self, args: Any) -> LatencySummary:
        """计算某个 run 的延迟统计。

        Args:
            args: 已校验的 ``CalculateLatencySummaryArgs``。

        Raises:
            KeyError: run 不存在。
        """
        run_id = args.run_id

        with self._session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(f"run 不存在：{run_id}")

            latency = self._latency_breakdown(session, run_id, run)

        logger.info(
            "latency_summary_computed",
            extra={
                "run_id": run_id,
                "node_count": latency.node_count,
                "p95_node_ms": latency.p95_node_ms,
            },
        )
        return latency

    def calculate_cost_summary(self, args: Any) -> CostSummary:
        """计算某个 run 的 Token 与成本汇总。

        Args:
            args: 已校验的 ``CalculateCostSummaryArgs``。

        Raises:
            KeyError: run 不存在。
        """
        run_id = args.run_id

        with self._session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(f"run 不存在：{run_id}")

            cost = self._cost_breakdown(session, run_id, run)

        logger.info(
            "cost_summary_computed",
            extra={
                "run_id": run_id,
                "total_tokens": cost.total_tokens,
                "cost_unavailable": cost.cost_estimation_unavailable,
            },
        )
        return cost

    # ------------------------------------------------------------ 内部实现
    @staticmethod
    def _latency_breakdown(session: Session, run_id: str, run: Run) -> LatencySummary:
        """从 trace_event 与 tool_call / model_call 聚合延迟。"""
        stmt = (
            select(TraceEvent.name, TraceEvent.duration_ms)
            .where(TraceEvent.run_id == run_id)
            .where(TraceEvent.event_type == "node")
        )
        try:
            rows = session.execute(stmt).all()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"查询节点事件失败：{type(exc).__name__}") from exc

        node_durations = sorted(int(row.duration_ms) for row in rows if row.duration_ms is not None)

        # 最慢节点：按耗时降序，耗时相同时按名称升序（确定性）
        slowest_name: str | None = None
        slowest_ms: int | None = None
        for name, duration in sorted(rows, key=lambda r: (-(r.duration_ms or 0), r.name)):
            if duration is not None:
                slowest_name = name
                slowest_ms = int(duration)
                break

        return LatencySummary(
            run_id=run_id,
            total_duration_ms=run.total_duration_ms,
            node_count=len(rows),
            tool_call_count=_count(session, "tool_call", run_id),
            model_call_count=_count(session, "model_call", run_id),
            slowest_node=slowest_name,
            slowest_node_ms=slowest_ms,
            p50_node_ms=nearest_rank(node_durations, _P50),
            p95_node_ms=nearest_rank(node_durations, _P95),
        )

    @staticmethod
    def _cost_breakdown(session: Session, run_id: str, run: Run) -> CostSummary:
        """从 model_call 聚合 Token 与成本。

        注意 ``cost_estimation_unavailable`` 的聚合：只要**任意一次**调用
        标记为不可估算，整个汇总就标记为不可估算。
        否则一个"部分可估算"的数字会被误读为完整成本。

        跨方言注意：PostgreSQL 与 SQLite 都不支持直接 ``sum(bool)``，
        因此把布尔列转 Integer 后取 max（等价于 any()）。
        """
        from sqlalchemy import Integer, cast, func

        stmt = select(
            func.coalesce(func.sum(ModelCall.prompt_tokens), 0),
            func.coalesce(func.sum(ModelCall.completion_tokens), 0),
            func.coalesce(func.sum(ModelCall.total_tokens), 0),
            func.coalesce(func.sum(ModelCall.estimated_cost_usd), 0),
            func.count(ModelCall.id),
            func.coalesce(func.max(cast(ModelCall.cost_estimation_unavailable, Integer)), 0),
        ).where(ModelCall.run_id == run_id)

        try:
            row = session.execute(stmt).one()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"聚合模型调用失败：{type(exc).__name__}") from exc

        raw_cost = row[3]
        estimated_cost = (
            raw_cost if isinstance(raw_cost, Decimal) else Decimal(str(raw_cost or "0"))
        )

        return CostSummary(
            run_id=run_id,
            prompt_tokens=int(row[0] or 0),
            completion_tokens=int(row[1] or 0),
            total_tokens=int(row[2] or 0),
            estimated_cost_usd=estimated_cost,
            model_call_count=int(row[4] or 0),
            cost_estimation_unavailable=bool(row[5]),
        )


def _count(session: Session, event_type: str, run_id: str) -> int:
    """统计某类事件的数量。"""
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(TraceEvent)
        .where(TraceEvent.run_id == run_id)
        .where(TraceEvent.event_type == event_type)
    )
    try:
        return int(session.execute(stmt).scalar_one() or 0)
    except SQLAlchemyError:
        return 0


__all__ = ["AnalyticsTools", "nearest_rank"]
