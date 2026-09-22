"""指标汇总服务：``GET /metrics/summary`` 的数据计算。

契约 API_CONTRACT §8 与 EVALUATION §3。

**本模块最重要的两条原则**

1. **无数据时返回 ``None`` 而不是 0**。``run_count = 0`` 时所有指标为
   ``None``：把"从未运行过"显示成"成功率 0%"是彻头彻尾的误导。
   这也是 ``MetricsBlock`` 里字段都声明为 ``float | None`` 的原因。

2. **必须标注数据来源**。任何指标输出都带 ``scope`` 与
   ``data_source_note``（契约 B9）。本实例的数据来自样例运行或离线评测，
   与线上流量无关 —— 这个边界必须写在响应体里，而不是只写在 README 里。

**数据来源范围的判定**：``scope`` 由数据本身决定，不由调用方声明。

- 无 run → ``empty``；
- 有 run 但都来自评测批次（``eval_run`` 有引用）→ ``offline_evaluation``；
- 其余 → ``sample_runs``。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import TracePersistenceError
from app.core.logging import get_logger
from app.schemas.common import DataScope
from app.schemas.metrics import (
    DATA_SOURCE_NOTE,
    MetricsBlock,
    MetricsGroup,
    MetricsSummaryResponse,
    ToolUsageStat,
)

logger = get_logger(__name__)

# 参与汇总的最大 run 数。
#
# 为什么要有上限：``GET /metrics/summary`` 是**运行级**汇总，
# 需要在 Python 里算分位数（nearest-rank 不便用 SQL 表达）。
# 无上限会让某个大实例的这次请求把全表读进内存。
# 取 5000 是"够用且不会失控"的工程判断，超出时按最近优先截断，
# 并把实际参与计算的 run 数如实回报在 run_count 里。
_MAX_RUNS_FOR_SUMMARY = 5000

# 视为"成功"的状态集合。
#
# 注意 **degraded 不在其中**：降级是"跑完了但依据不足"，
# 契约明确它不视为成功。把它算作成功会虚高成功率。
_SUCCESS_STATUSES = frozenset({"succeeded"})

# 视为"错误"的状态集合。
#
# 注意 **timeout 不在其中**：超时是"没跑完"，与"跑错了"是两回事。
# 契约 EVALUATION §3 要求 timeout 不计入 error_rate。
_ERROR_STATUSES = frozenset({"failed"})

# 需要人工复核的状态：降级与超时都属于"结果不可直接采信"。
_REVIEW_STATUSES = frozenset({"degraded", "timeout"})

# 分组的合法维度
_GROUP_BY_FIELDS = {
    "agent_version": "agent_version",
    "prompt_version": "prompt_version",
    "model_name": "model_name",
    "day": "day",
}


def nearest_rank(sorted_values: list[float], percentile: float) -> float | None:
    """nearest-rank 分位数（与工具层同一实现口径）。

    不用线性插值（EVALUATION §3 原则三）：插值会产生样本中
    **从未观测到**的数值，用未观测值做验收是不诚实的。
    """
    if not sorted_values:
        return None
    if not 0.0 <= percentile <= 1.0:
        raise ValueError("percentile 必须在 0~1 之间")

    n = len(sorted_values)
    rank = -(-int(round(percentile * 1000)) * n // 1000)  # ceil(p*n)，纯整数运算
    rank = max(1, min(rank, n))
    return sorted_values[rank - 1]


class MetricsService:
    """运行级指标汇总。

    Args:
        session_factory: 返回 Session 的可调用对象；默认取全局 ``session_scope``。
    """

    def __init__(self, *, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    def summary(
        self,
        *,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        agent_version: str | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
        group_by: str | None = None,
    ) -> MetricsSummaryResponse:
        """计算运行级汇总。

        Raises:
            InvalidArgumentError: ``group_by`` 取值非法。
        """
        from app.core.errors import InvalidArgumentError

        if group_by is not None and group_by not in _GROUP_BY_FIELDS:
            raise InvalidArgumentError(
                f"group_by 取值非法：{group_by}",
                details={"allowed": sorted(_GROUP_BY_FIELDS)},
            )

        runs = self._load_runs(
            started_after=started_after,
            started_before=started_before,
            agent_version=agent_version,
            prompt_version=prompt_version,
            model_name=model_name,
        )

        filters: dict[str, str | None] = {
            "agent_version": agent_version,
            "prompt_version": prompt_version,
            "model_name": model_name,
        }
        if started_after is not None:
            filters["started_after"] = started_after.isoformat()
        if started_before is not None:
            filters["started_before"] = started_before.isoformat()

        if not runs:
            # 空数据：所有指标为 None，并明确 scope=empty。
            # 这不是错误状态 —— 新实例问"我跑了多少"的正确答案是"零次"，
            # 而不是 404 或 500。
            return MetricsSummaryResponse(
                scope=DataScope.EMPTY,
                data_source_note=DATA_SOURCE_NOTE,
                filters=filters,
                group_by=group_by,
                run_count=0,
                metrics=MetricsBlock(),
                tools={},
                groups=[],
            )

        run_ids = [run.id for run in runs]
        tools = self._aggregate_tools(run_ids)

        return MetricsSummaryResponse(
            scope=self._resolve_scope(runs),
            data_source_note=DATA_SOURCE_NOTE,
            filters=filters,
            group_by=group_by,
            run_count=len(runs),
            metrics=self._compute_metrics(runs),
            tools=tools,
            groups=self._build_groups(runs, group_by) if group_by else [],
        )

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------

    def _load_runs(
        self,
        *,
        started_after: datetime | None,
        started_before: datetime | None,
        agent_version: str | None,
        prompt_version: str | None,
        model_name: str | None,
    ) -> list[Any]:
        """按过滤条件取 run 行。"""
        stmt = select(self._run_model())
        if started_after is not None:
            stmt = stmt.where(self._run_model().started_at >= started_after)
        if started_before is not None:
            stmt = stmt.where(self._run_model().started_at <= started_before)
        if agent_version is not None:
            stmt = stmt.where(self._run_model().agent_version == agent_version)
        if prompt_version is not None:
            stmt = stmt.where(self._run_model().prompt_version == prompt_version)
        if model_name is not None:
            stmt = stmt.where(self._run_model().model_name == model_name)

        stmt = stmt.order_by(self._run_model().started_at.desc()).limit(_MAX_RUNS_FOR_SUMMARY)

        factory = self._resolve_session_factory()
        with factory() as session:
            try:
                return list(session.execute(stmt).scalars().all())
            except SQLAlchemyError as exc:
                raise TracePersistenceError("加载 run 列表失败。") from exc

    def _aggregate_tools(self, run_ids: list[str]) -> dict[str, ToolUsageStat]:
        """按工具名聚合调用情况。"""
        model = self._tool_call_model()
        stmt = (
            select(
                model.tool_name,
                func.count().label("calls"),
                func.coalesce(func.avg(model.duration_ms), 0).label("mean_duration_ms"),
            )
            .where(model.run_id.in_(run_ids))
            .group_by(model.tool_name)
        )

        factory = self._resolve_session_factory()
        with factory() as session:
            try:
                rows = list(session.execute(stmt).all())
                if not rows:
                    return {}

                # 状态分布单独查：把"状态分组"和"平均耗时"拆成两条查询，
                # 比一条带 CASE WHEN 的复杂 SQL 更好读，且这里的行数很小。
                status_stmt = (
                    select(model.tool_name, model.status, func.count().label("n"))
                    .where(model.run_id.in_(run_ids))
                    .group_by(model.tool_name, model.status)
                )
                status_rows = list(session.execute(status_stmt).all())
            except SQLAlchemyError as exc:
                # 工具统计失败不应让整个指标接口失败 —— 它是附加信息。
                logger.warning(
                    "tool_aggregation_failed",
                    extra={"error_type": type(exc).__name__},
                )
                return {}

        statuses: dict[str, dict[str, int]] = defaultdict(dict)
        for tool_name, status_value, count in status_rows:
            statuses[tool_name][status_value] = int(count)

        result: dict[str, ToolUsageStat] = {}
        for tool_name, calls, mean_duration in rows:
            per_status = statuses.get(tool_name, {})
            result[tool_name] = ToolUsageStat(
                calls=int(calls),
                ok=int(per_status.get("ok", 0)),
                invalid_arguments=int(per_status.get("invalid_arguments", 0)),
                error=int(per_status.get("error", 0) + per_status.get("failed", 0)),
                mean_duration_ms=round(float(mean_duration or 0), 2),
            )
        return result

    # ------------------------------------------------------------------
    # 计算
    # ------------------------------------------------------------------

    def _compute_metrics(self, runs: list[Any]) -> MetricsBlock:
        """计算 10 个运行级指标。

        分母规则（EVALUATION §3）：**分母始终是参与统计的 run 数**，
        含失败与被排除的 case。用"成功数 / 成功数"之类的口径
        会让成功率永远漂亮，失去诊断价值。
        """
        total = len(runs)
        if total == 0:  # pragma: no cover —— 调用方已保证非空
            return MetricsBlock()

        success_count = sum(1 for run in runs if run.status in _SUCCESS_STATUSES)
        error_count = sum(1 for run in runs if run.status in _ERROR_STATUSES)
        review_count = sum(1 for run in runs if run.status in _REVIEW_STATUSES)
        degraded_count = sum(1 for run in runs if run.status == "degraded")

        durations = sorted(
            float(run.total_duration_ms)
            for run in runs
            if run.total_duration_ms is not None
        )
        total_tokens = sum(int(run.total_tokens or 0) for run in runs)
        total_cost = sum(float(run.estimated_cost_usd or 0) for run in runs)

        # 证据充分率：从 result_summary 里取，取不到的 run 不计入分子，
        # 但**仍计入分母** —— 否则"没有摘要的 run"会被悄悄排除，
        # 让指标看起来更好。
        sufficient_count = sum(
            1
            for run in runs
            if bool((run.result_summary or {}).get("evidence_sufficient", False))
        )

        # 成本完整性：只要有任何一次模型调用的成本无法估算，整批的成本
        # 就是**不完整**的。
        #
        # 这个标志必须从 ``model_call`` 表推导，不能从 run 行读 ——
        # ``Run`` 没有这个列（它是 ``ModelCall`` 的属性）。
        # 早期实现写了 ``run.cost_estimation_unavailable``，会让
        # ``GET /metrics/summary`` 在**任何有数据**的实例上直接 500，
        # 而空数据分支能正常返回 —— 于是这个 bug 在没跑过 run 的
        # 环境里永远不会暴露。
        cost_unavailable = self._has_unavailable_cost([run.id for run in runs])

        return MetricsBlock(
            run_success_rate=round(success_count / total, 4),
            # 任务完成率与成功率在本项目口径一致：
            # "完成"的判定就是 final_validator 的结论（见 final_validator 文档）。
            task_completion_rate=round(success_count / total, 4),
            error_rate=round(error_count / total, 4),
            human_review_rate=round(review_count / total, 4),
            degraded_rate=round(degraded_count / total, 4),
            # 以下三项是**评测级**指标，运行级汇总无从计算（需要 ground truth），
            # 因此如实留空而不是拿近似值充数。
            tool_selection_accuracy=None,
            tool_argument_accuracy=None,
            evidence_coverage=round(sufficient_count / total, 4),
            latency_ms_p50=nearest_rank(durations, 0.50),
            latency_ms_p95=nearest_rank(durations, 0.95),
            latency_ms_mean=round(sum(durations) / len(durations), 2) if durations else None,
            total_tokens=total_tokens,
            estimated_cost_usd=round(total_cost, 6),
            cost_estimation_unavailable=cost_unavailable,
            case_count=total,
        )

    def _has_unavailable_cost(self, run_ids: list[str]) -> bool:
        """判断这批 run 里是否存在"成本无法估算"的模型调用。

        查询失败时返回 ``True``（**保守**）而不是 ``False``：
        把它当成"成本完整"会让读数的人相信一个未经验证的数字，
        而报"不完整"只是让成本字段带上存疑标记。
        不确定时选择更小的断言，是指标类代码应守的规矩。
        """
        if not run_ids:
            return False
        model = self._model_call_model()
        stmt = (
            select(func.count())
            .select_from(model)
            .where(model.run_id.in_(run_ids), model.cost_estimation_unavailable.is_(True))
        )
        factory = self._resolve_session_factory()
        try:
            with factory() as session:
                return int(session.execute(stmt).scalar_one()) > 0
        except SQLAlchemyError as exc:
            logger.warning(
                "cost_completeness_check_failed",
                extra={"error_type": type(exc).__name__},
            )
            return True

    def _build_groups(self, runs: list[Any], group_by: str | None) -> list[MetricsGroup]:
        """按维度分组计算指标。"""
        if not group_by:
            return []

        field = _GROUP_BY_FIELDS[group_by]
        buckets: dict[str, list[Any]] = defaultdict(list)
        for run in runs:
            buckets[self._group_key(run, field)].append(run)

        groups = [
            MetricsGroup(
                key=group_by,
                value=value,
                run_count=len(bucket),
                metrics=self._compute_metrics(bucket),
            )
            for value, bucket in buckets.items()
        ]
        # 排序保证响应稳定（否则分组顺序取决于 set/dict 遍历，难以断言）
        groups.sort(key=lambda item: item.value)
        return groups

    @staticmethod
    def _group_key(run: Any, field: str) -> str:
        """取分组键值。"""
        if field == "day":
            started = run.started_at
            if started is None:
                return "unknown"
            # naive 时间戳（SQLite）也要能分组
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            return started.strftime("%Y-%m-%d")
        value = getattr(run, field, None)
        return str(value) if value else "unknown"

    def _resolve_scope(self, runs: list[Any]) -> DataScope:
        """判定数据来源范围。

        **由数据本身决定，不由调用方声明** —— 让调用方声明 scope 等于
        允许它把离线评测结果标成线上数据。

        判定依据：``eval_run.run_id`` 是否存在引用。
        若这批 run **全部**来自评测批次，说明这批数据是离线评测结果，
        标为 ``offline_evaluation``；否则标为 ``sample_runs``。

        这个区分对读数是实质性的：``offline_evaluation`` 的数值来自固定
        评测集，可与其他批次横向对比；``sample_runs`` 只是零散运行，
        两者的可比性完全不同。
        """
        if not runs:
            return DataScope.EMPTY

        run_ids = [run.id for run in runs]
        try:
            from app.db.models import EvalRun

            stmt = (
                select(func.count())
                .select_from(EvalRun)
                .where(EvalRun.run_id.in_(run_ids))
            )
            factory = self._resolve_session_factory()
            with factory() as session:
                linked = int(session.execute(stmt).scalar_one())
        except Exception as exc:  # noqa: BLE001 —— 判定失败时退到更保守的标注
            # 退回 sample_runs 而不是 offline_evaluation：
            # 后者会让读数者以为数据可横向对比，而实际上我们并不知道。
            logger.warning(
                "scope_resolution_failed",
                extra={"error_type": type(exc).__name__},
            )
            return DataScope.SAMPLE_RUNS

        # 全部 run 都被评测引用 → 离线评测数据
        if linked >= len(run_ids):
            return DataScope.OFFLINE_EVALUATION
        return DataScope.SAMPLE_RUNS

    # ------------------------------------------------------------------
    # 延迟加载的模型引用（避免模块级循环导入）
    # ------------------------------------------------------------------

    @staticmethod
    def _run_model() -> Any:
        from app.db.models import Run

        return Run

    @staticmethod
    def _tool_call_model() -> Any:
        from app.db.models import ToolCall

        return ToolCall

    @staticmethod
    def _model_call_model() -> Any:
        from app.db.models import ModelCall

        return ModelCall

    def _resolve_session_factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.session import session_scope

        return session_scope


def default_window() -> tuple[datetime, datetime]:
    """默认统计窗口：最近 7 天。

    提供一个默认窗口而不是"全表"，是为了让接口在数据积累后仍然快速。
    """
    end = datetime.now(UTC)
    return end - timedelta(days=7), end


__all__ = ["MetricsService", "default_window", "nearest_rank"]
