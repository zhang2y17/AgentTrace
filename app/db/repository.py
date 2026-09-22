"""仓储层：run / trace_event / tool_call / model_call 的增删查。

设计要点：

1. **写入失败不静默**：所有写入路径的 ``IntegrityError`` 与 ``SQLAlchemyError``
   都被转换为 ``TracePersistenceError`` 向上抛出（契约 ARCHITECTURE §4）；
2. **sequence 分配处理并发冲突**：``max(sequence)+1`` 在并发下可能撞唯一约束，
   这里捕获冲突并重试一次（IMPLEMENTATION_PLAN S3 风险表）；
3. **只存摘要**：所有 summary 字段在写入前经过脱敏与截断，
   仓储层接收的已应是处理过的值，但这里再做一次兜底。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import TracePersistenceError
from app.core.ids import (
    new_event_id,
    new_model_call_id,
    new_run_id,
    new_tool_call_id,
)
from app.core.logging import get_logger
from app.core.redaction import summarize, truncate
from app.db.models import ModelCall, Run, ToolCall, TraceEvent

logger = get_logger(__name__)

# sequence 分配的最大重试次数。并发冲突属预期情况，重试一次即可；
# 连续失败说明存在更严重的问题，应暴露而非无限重试。
_SEQUENCE_MAX_ATTEMPTS = 3


class TraceRepository:
    """Trace 数据仓储。

    所有方法接收一个 ``Session``，由调用方决定事务边界。
    这样服务层可以把"创建 run + 写入首个事件"放在同一事务里。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------ run
    def create_run(
        self,
        *,
        question: str,
        status: str,
        agent_version: str,
        prompt_version: str,
        llm_provider: str,
        model_name: str | None = None,
        is_test_double: bool = False,
        source_run_id: str | None = None,
        agent_definition_id: str | None = None,
        started_at: datetime | None = None,
    ) -> Run:
        """创建一次运行。

        Args:
            source_run_id: 非 None 表示这是回放，值应为被回放的原始 run_id。
        """
        from app.db.base import utcnow

        run = Run(
            id=new_run_id(),
            question=question,
            status=status,
            agent_version=agent_version,
            prompt_version=prompt_version,
            llm_provider=llm_provider,
            model_name=model_name,
            is_test_double=is_test_double,
            source_run_id=source_run_id,
            agent_definition_id=agent_definition_id,
            started_at=started_at or utcnow(),
            total_tokens=0,
            estimated_cost_usd=Decimal("0"),
        )
        self._add(run, entity="run")
        return run

    def get_run(self, run_id: str) -> Run | None:
        """按 ID 查询 run。"""
        try:
            return self.session.get(Run, run_id)
        except SQLAlchemyError as exc:
            raise TracePersistenceError(
                "查询 run 失败。", details={"run_id": run_id}
            ) from exc

    def finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        result_summary: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        total_tokens: int = 0,
        estimated_cost_usd: Decimal | float = Decimal("0"),
        ended_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """写入 run 的终态与汇总数值。

        ``ended_at`` 与 ``total_duration_ms`` 同时写入，保证二者自洽。

        ``duration_ms`` 显式传入时会覆盖按时间戳计算的耗时。用途：
        ``RunService`` 用 ``time.perf_counter`` 测的耗时比
        ``ended_at - started_at`` 更接近真实执行时长（后者包含
        建行与落库的开销），且 run 行与 ``run`` 根事件必须报同一个数，
        否则读数的人会以为是两次不同的运行。
        """
        from app.db.base import elapsed_ms, utcnow

        run = self.get_run(run_id)
        if run is None:
            raise TracePersistenceError(
                "无法终结不存在的 run。", details={"run_id": run_id}
            )

        finished = ended_at or utcnow()
        run.status = status
        run.ended_at = finished
        if duration_ms is not None:
            run.total_duration_ms = max(0, int(duration_ms))
        elif run.started_at is not None:
            # 同 close_event：SQLite 读回的时间戳是 naive，
            # 直接相减会在 SQLite 上抛 TypeError。
            run.total_duration_ms = elapsed_ms(run.started_at, finished)

        if result_summary is not None:
            # result_summary 是 JSON 字段，逐字符串值做脱敏截断
            run.result_summary = self._sanitize_json(result_summary)
        run.error_code = error_code
        run.error_message = truncate(error_message, 500) if error_message else None
        run.total_tokens = total_tokens
        run.estimated_cost_usd = (
            estimated_cost_usd
            if isinstance(estimated_cost_usd, Decimal)
            else Decimal(str(estimated_cost_usd))
        )

        self._flush(entity="run")

    def list_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        agent_version: str | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
        status: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
    ) -> list[Run]:
        """按条件列出 run，按开始时间倒序。"""
        stmt = select(Run)

        if agent_version is not None:
            stmt = stmt.where(Run.agent_version == agent_version)
        if prompt_version is not None:
            stmt = stmt.where(Run.prompt_version == prompt_version)
        if model_name is not None:
            stmt = stmt.where(Run.model_name == model_name)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        if started_after is not None:
            stmt = stmt.where(Run.started_at >= started_after)
        if started_before is not None:
            stmt = stmt.where(Run.started_at <= started_before)

        stmt = stmt.order_by(Run.started_at.desc()).limit(limit).offset(offset)

        try:
            return list(self.session.execute(stmt).scalars().all())
        except SQLAlchemyError as exc:
            raise TracePersistenceError("列出 run 失败。") from exc

    def count_runs(self, **filters: Any) -> int:
        """统计满足条件的 run 数量。"""
        stmt = select(func.count()).select_from(Run)
        if filters.get("agent_version") is not None:
            stmt = stmt.where(Run.agent_version == filters["agent_version"])
        if filters.get("prompt_version") is not None:
            stmt = stmt.where(Run.prompt_version == filters["prompt_version"])
        if filters.get("model_name") is not None:
            stmt = stmt.where(Run.model_name == filters["model_name"])
        try:
            return int(self.session.execute(stmt).scalar_one())
        except SQLAlchemyError as exc:
            raise TracePersistenceError("统计 run 数量失败。") from exc

    # ------------------------------------------------------------ trace_event
    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        name: str,
        status: str,
        parent_event_id: str | None = None,
        input_summary: Any = None,
        output_summary: Any = None,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        duration_ms: int | None = None,
        summary_max_chars: int = 500,
    ) -> TraceEvent:
        """追加一条 Trace 事件。

        ``sequence`` 由本方法分配（``max+1``），并在并发冲突时重试。
        ``input_summary`` / ``output_summary`` 接受任意值，内部用
        ``summarize`` 转为已脱敏、已截断的字符串。
        """
        from app.db.base import utcnow

        last_error: Exception | None = None

        for attempt in range(_SEQUENCE_MAX_ATTEMPTS):
            sequence = self._next_sequence(run_id)
            event = TraceEvent(
                event_id=new_event_id(),
                run_id=run_id,
                parent_event_id=parent_event_id,
                event_type=event_type,
                name=truncate(name, 120),
                status=status,
                sequence=sequence,
                started_at=started_at or utcnow(),
                ended_at=ended_at,
                duration_ms=duration_ms,
                input_summary=(
                    summarize(input_summary, summary_max_chars)
                    if input_summary is not None
                    else None
                ),
                output_summary=(
                    summarize(output_summary, summary_max_chars)
                    if output_summary is not None
                    else None
                ),
                error_code=error_code,
                attributes=self._sanitize_json(attributes) if attributes else None,
            )
            try:
                self.session.add(event)
                self.session.flush()
                return event
            except IntegrityError as exc:
                # 大概率是 (run_id, sequence) 唯一约束冲突：并发写入同一 run。
                # 回滚当前 flush 的影响后重新分配 sequence。
                self.session.rollback()
                last_error = exc
                logger.warning(
                    "trace_event_sequence_conflict",
                    extra={"run_id": run_id, "attempt": attempt + 1, "sequence": sequence},
                )
            except SQLAlchemyError as exc:
                self.session.rollback()
                raise TracePersistenceError(
                    "写入 trace_event 失败。",
                    details={"run_id": run_id, "event_type": event_type},
                ) from exc

        raise TracePersistenceError(
            "多次尝试后仍无法分配 trace_event 的 sequence。",
            details={"run_id": run_id, "attempts": _SEQUENCE_MAX_ATTEMPTS},
        ) from last_error

    def close_event(
        self,
        event_id: str,
        *,
        status: str,
        output_summary: Any = None,
        error_code: str | None = None,
        extra_attributes: dict[str, Any] | None = None,
        ended_at: datetime | None = None,
        summary_max_chars: int = 500,
    ) -> None:
        """补写事件的结束状态、耗时与输出摘要。

        对应 TRACE_SCHEMA 的 "INSERT 时 status=running，UPDATE 时补 ended_at"。
        """
        from app.db.base import elapsed_ms, utcnow

        event = self.session.get(TraceEvent, event_id)
        if event is None:
            raise TracePersistenceError(
                "无法关闭不存在的事件。", details={"event_id": event_id}
            )

        finished = ended_at or utcnow()
        event.ended_at = finished
        event.status = status
        if event.started_at is not None:
            # 用 elapsed_ms 而不是直接相减：SQLite 读回的时间戳是 naive，
            # 与 aware 的 finished 相减会抛 TypeError（跨方言陷阱）。
            event.duration_ms = elapsed_ms(event.started_at, finished)
        if output_summary is not None:
            event.output_summary = summarize(output_summary, summary_max_chars)
        if error_code is not None:
            event.error_code = error_code
        if extra_attributes:
            merged = dict(event.attributes or {})
            merged.update(self._sanitize_json(extra_attributes))
            event.attributes = merged

        self._flush(entity="trace_event")

    def set_event_duration(self, event_id: str, duration_ms: int) -> None:
        """用显式值覆盖事件的 ``duration_ms``。

        存在的理由：``close_event`` 按 ``ended_at - started_at`` 计算耗时，
        而那个差值包含"写入 ended_at 之前的所有代码"（包括落库开销）。
        对于 ``run`` 根事件，整次运行的耗时应当与 run 行报同一个数 ——
        两个地方报不同的数会让读数的人以为是两次不同的运行。

        本方法只改 ``duration_ms``，不动 ``ended_at`` 与状态。
        """
        event = self.session.get(TraceEvent, event_id)
        if event is None:
            raise TracePersistenceError(
                "无法设置不存在事件的耗时。", details={"event_id": event_id}
            )
        event.duration_ms = max(0, int(duration_ms))
        self._flush(entity="trace_event")

    def record_final_result_event(
        self,
        *,
        run_id: str,
        status: str,
        parent_event_id: str | None = None,
        output_summary: Any = None,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
        summary_max_chars: int = 500,
    ) -> str:
        """写 ``event_type=final_result`` 的终结事件。

        契约 TRACE_SCHEMA §5：``final_result`` 与 5 个 ``node`` 同级，
        直接挂在 ``run`` 根事件下。它是"这次运行得出什么结论"的
        唯一权威记录 —— 没有它，Trace 只能说明"跑过哪些节点"，
        无法回答"最终判定是什么"。

        **``status`` 是事件级状态（EventStatus），不是 run 级状态。**
        两个枚举取值不同（事件层没有 ``succeeded`` / ``degraded`` /
        ``timeout``），调用方必须先映射。这里把 run 级的原始结论
        记在 ``attributes.run_status`` 里 —— 映射会丢掉信息，
        而"这次到底是 degraded 还是 succeeded"是有诊断价值的，
        不能因为枚举对不上就丢掉。

        ``final_result`` 是点事件：写入即终态。
        """
        attributes = dict(attributes or {})
        attributes.setdefault("run_status", status)

        event = self.append_event(
            run_id=run_id,
            event_type="final_result",
            name="final_result",
            status=status,
            parent_event_id=parent_event_id,
            output_summary=output_summary,
            error_code=error_code,
            attributes=attributes,
            summary_max_chars=summary_max_chars,
        )
        self.close_event(event.event_id, status=status, error_code=error_code)
        return event.event_id

    def list_events(
        self,
        run_id: str,
        *,
        event_types: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[TraceEvent], int]:
        """按条件分页查询事件，按 ``sequence`` 升序。

        Returns:
            ``(当前页事件, 过滤后总数)``
        """
        base = select(TraceEvent).where(TraceEvent.run_id == run_id)
        if event_types:
            base = base.where(TraceEvent.event_type.in_(event_types))
        if statuses:
            base = base.where(TraceEvent.status.in_(statuses))

        count_stmt = select(func.count()).select_from(base.subquery())
        page_stmt = base.order_by(TraceEvent.sequence.asc()).limit(limit).offset(offset)

        try:
            total = int(self.session.execute(count_stmt).scalar_one())
            events = list(self.session.execute(page_stmt).scalars().all())
        except SQLAlchemyError as exc:
            raise TracePersistenceError(
                "查询 trace_event 失败。", details={"run_id": run_id}
            ) from exc

        return events, total

    def get_event(self, event_id: str) -> TraceEvent | None:
        """按 ID 获取事件。"""
        return self.session.get(TraceEvent, event_id)

    def count_events(self, run_id: str) -> int:
        """统计某个 run 的事件数。"""
        stmt = select(func.count()).select_from(TraceEvent).where(TraceEvent.run_id == run_id)
        try:
            return int(self.session.execute(stmt).scalar_one())
        except SQLAlchemyError as exc:
            raise TracePersistenceError("统计事件数失败。") from exc

    # ------------------------------------------------------------ tool_call
    def record_tool_call(
        self,
        *,
        run_id: str,
        node_name: str,
        tool_name: str,
        tool_version: str = "1.0.0",
        arguments: dict[str, Any] | None = None,
        validated: bool = False,
        validation_error: str | None = None,
        status: str,
        result_summary: str | None = None,
        result_count: int | None = None,
        retry_count: int = 0,
        duration_ms: int | None = None,
        error_code: str | None = None,
        event_id: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> ToolCall:
        """记录一次工具调用。"""
        from app.db.base import utcnow

        finished = ended_at or utcnow()
        call = ToolCall(
            id=new_tool_call_id(),
            run_id=run_id,
            event_id=event_id,
            node_name=truncate(node_name, 120),
            tool_name=tool_name,
            tool_version=tool_version,
            arguments=self._sanitize_json(arguments) if arguments else None,
            validated=validated,
            validation_error=truncate(validation_error, 500) if validation_error else None,
            status=status,
            result_summary=truncate(result_summary, 500) if result_summary else None,
            result_count=result_count,
            retry_count=retry_count,
            duration_ms=duration_ms,
            error_code=error_code,
            started_at=started_at or finished,
            ended_at=ended_at,
        )
        self._add(call, entity="tool_call")
        return call

    def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        """列出某个 run 的全部工具调用，按开始时间升序。

        **排序语义**（EVALUATION §3 M3 依赖）：用于推导实际工具调用序列时，
        必须按时间顺序，因此这里固定升序。
        """
        stmt = (
            select(ToolCall)
            .where(ToolCall.run_id == run_id)
            .order_by(ToolCall.started_at.asc(), ToolCall.id.asc())
        )
        try:
            return list(self.session.execute(stmt).scalars().all())
        except SQLAlchemyError as exc:
            raise TracePersistenceError("查询 tool_call 失败。") from exc

    def count_tool_calls(self, run_id: str) -> int:
        """统计某个 run 的工具调用数。"""
        stmt = select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
        try:
            return int(self.session.execute(stmt).scalar_one())
        except SQLAlchemyError as exc:
            raise TracePersistenceError("统计工具调用数失败。") from exc

    # ------------------------------------------------------------ model_call
    def record_model_call(
        self,
        *,
        run_id: str,
        node_name: str,
        provider: str,
        model_name: str,
        is_test_double: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        estimated_cost_usd: Decimal | float = Decimal("0"),
        cost_estimation_unavailable: bool = False,
        status: str,
        latency_ms: int | None = None,
        retry_count: int = 0,
        error_code: str | None = None,
        event_id: str | None = None,
    ) -> ModelCall:
        """记录一次模型调用。"""
        call = ModelCall(
            id=new_model_call_id(),
            run_id=run_id,
            event_id=event_id,
            node_name=truncate(node_name, 120),
            provider=provider,
            model_name=model_name,
            is_test_double=is_test_double,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=(
                estimated_cost_usd
                if isinstance(estimated_cost_usd, Decimal)
                else Decimal(str(estimated_cost_usd))
            ),
            cost_estimation_unavailable=cost_estimation_unavailable,
            status=status,
            latency_ms=latency_ms,
            retry_count=retry_count,
            error_code=error_code,
        )
        self._add(call, entity="model_call")
        return call

    def list_model_calls(self, run_id: str) -> list[ModelCall]:
        """列出某个 run 的全部模型调用。"""
        stmt = (
            select(ModelCall)
            .where(ModelCall.run_id == run_id)
            .order_by(ModelCall.created_at.asc(), ModelCall.id.asc())
        )
        try:
            return list(self.session.execute(stmt).scalars().all())
        except SQLAlchemyError as exc:
            raise TracePersistenceError("查询 model_call 失败。") from exc

    def count_model_calls(self, run_id: str) -> int:
        """统计某个 run 的模型调用数。"""
        stmt = select(func.count()).select_from(ModelCall).where(ModelCall.run_id == run_id)
        try:
            return int(self.session.execute(stmt).scalar_one())
        except SQLAlchemyError as exc:
            raise TracePersistenceError("统计模型调用数失败。") from exc

    # ------------------------------------------------------------ 统计聚合
    def aggregate_token_and_cost(self, run_id: str) -> tuple[int, Decimal, bool]:
        """聚合某个 run 的 Token 与成本。

        Returns:
            ``(总 Token, 总成本, 是否存在无法估算成本的调用)``

        这是确定性代码，不经过 LLM（契约 PROJECT_SPEC §3.3）。
        三个值一次查询取回，避免三次往返。
        """
        stmt = select(
            func.coalesce(func.sum(ModelCall.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(ModelCall.estimated_cost_usd), 0).label("cost"),
            # 布尔列求和：PostgreSQL 与 SQLite 都不支持直接 sum(bool)，
            # 因此转为 Integer 后取 max，等价于 any()。
            func.coalesce(
                func.max(cast(ModelCall.cost_estimation_unavailable, Integer)), 0
            ).label("unavailable"),
        ).where(ModelCall.run_id == run_id)

        try:
            row = self.session.execute(stmt).one()
        except SQLAlchemyError as exc:
            raise TracePersistenceError("聚合 Token 与成本失败。") from exc

        total_tokens = int(row.tokens or 0)
        raw_cost = row.cost
        total_cost = raw_cost if isinstance(raw_cost, Decimal) else Decimal(str(raw_cost or 0))
        unavailable = bool(row.unavailable)

        return total_tokens, total_cost, unavailable

    # ------------------------------------------------------------ 内部工具
    def _next_sequence(self, run_id: str) -> int:
        """计算下一个 sequence 值。"""
        stmt = select(func.coalesce(func.max(TraceEvent.sequence), 0)).where(
            TraceEvent.run_id == run_id
        )
        try:
            current = int(self.session.execute(stmt).scalar_one() or 0)
        except SQLAlchemyError as exc:
            raise TracePersistenceError(
                "计算下一步 sequence 失败。", details={"run_id": run_id}
            ) from exc
        return current + 1

    def _add(self, instance: Any, *, entity: str) -> None:
        """添加并 flush，失败时抛 TracePersistenceError。

        flush 而非 commit：事务边界由调用方（服务层）掌握。
        """
        try:
            self.session.add(instance)
            self.session.flush()
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise TracePersistenceError(
                f"写入 {entity} 失败。", details={"entity": entity}
            ) from exc

    def _flush(self, *, entity: str) -> None:
        """flush 当前会话变更。"""
        try:
            self.session.flush()
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise TracePersistenceError(
                f"更新 {entity} 失败。", details={"entity": entity}
            ) from exc

    @staticmethod
    def _sanitize_json(data: dict[str, Any]) -> dict[str, Any]:
        """对将写入 JSON 列的数据逐字符串值做脱敏与截断。

        只处理顶层字符串值 + 一层嵌套，深度处理交给 ``summarize``；
        这里的目标是"不把密钥原样写进数据库"。
        """
        from app.core.redaction import redact_mapping

        return redact_mapping(data)


__all__ = ["TraceRepository"]
