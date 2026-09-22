"""Trace 记录中间件。

职责：把"节点执行"与"工具/模型调用"包成可观测事件，并保证**写入失败不被吞掉**。

契约 ARCHITECTURE §4：Trace 写入失败必须显式暴露。
理由很直接：Trace 是本项目的核心产物，静默丢失会让"运行成功但查不到记录"
变成一个无法诊断的状态。因此本模块在写入失败时抛 ``TracePersistenceError``，
由调用方（RunService）决定如何降级（通常是标记 run 为 failed 并向上报错）。

两条设计约束：

1. **不重建仓储** —— ``TraceRecorder`` 组合 ``TraceRepository``，
   而不是自己写 SQL。sequence 分配、脱敏、计时逻辑只有一份实现；
2. **摘要必须脱敏截断** —— 所有 summary 经 ``summarize``，
   绝不打完整 Prompt 或大响应入库（契约 T10）。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.db.repository import TraceRepository

logger = get_logger(__name__)


class TraceRecorder:
    """把运行过程写成 Trace 事件。

    典型用法（节点包装）::

        with recorder.node(run_id, "question_parser", input_summary=...) as node:
            result = do_work()
            node.succeed(output_summary=result)   # 或 node.fail(...)

    或直接调用细粒度方法（工具注册表用这种方式）。
    """

    def __init__(self, repository: TraceRepository, *, summary_max_chars: int = 500) -> None:
        self.repository = repository
        self.summary_max_chars = summary_max_chars

    # ------------------------------------------------------------ 事件原语
    def start_event(
        self,
        *,
        run_id: str,
        event_type: str,
        name: str,
        parent_event_id: str | None = None,
        input_summary: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        """写入一条 ``status=running`` 的事件，返回 ``event_id``。

        ``running`` 而非 ``ok``：这是一次"开始"记录，结束状态由
        ``close_event`` 补写。留 ``running`` 状态的事件是"未正常结束"的信号，
        对排障有价值 —— 因此不能一开始就写 ``ok``。
        """
        event = self.repository.append_event(
            run_id=run_id,
            event_type=event_type,
            name=name,
            status="running",
            parent_event_id=parent_event_id,
            input_summary=input_summary,
            attributes=attributes,
            summary_max_chars=self.summary_max_chars,
        )
        return event.event_id

    def close_event(
        self,
        event_id: str,
        *,
        status: str,
        output_summary: Any = None,
        error_code: str | None = None,
        extra_attributes: dict[str, Any] | None = None,
        ended_at: datetime | None = None,
    ) -> None:
        """补写事件的结束状态与输出摘要。"""
        self.repository.close_event(
            event_id,
            status=status,
            output_summary=output_summary,
            error_code=error_code,
            extra_attributes=extra_attributes,
            ended_at=ended_at,
            summary_max_chars=self.summary_max_chars,
        )

    # ------------------------------------------------------------ 节点包装
    @contextmanager
    def node(
        self,
        *,
        run_id: str,
        name: str,
        parent_event_id: str | None = None,
        input_summary: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[NodeSpan]:
        """节点执行上下文。

        退出时**自动**补写 ``ok``（正常返回）或 ``failed``（抛异常）状态，
        保证"有开始必有结束" —— 否则事件永远停在 ``running``。
        异常会被重新抛出，不被吞掉。
        """
        started = time.perf_counter()
        event_id = self.start_event(
            run_id=run_id,
            event_type="node",
            name=name,
            parent_event_id=parent_event_id,
            input_summary=input_summary,
            attributes=attributes,
        )
        span = NodeSpan(recorder=self, event_id=event_id, name=name)
        span._started_perf = started

        try:
            yield span
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            span._settle(
                status="failed",
                output_summary=None,
                error_code=getattr(exc, "error_code", None) or "NODE_FAILED",
            )
            logger.warning(
                "node_failed",
                extra={
                    "run_id": run_id,
                    "node_name": name,
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        else:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if not span._settled:
                span._settle(status="ok", output_summary=span.output_summary, error_code=None)
            logger.debug(
                "node_completed",
                extra={"run_id": run_id, "node_name": name, "duration_ms": duration_ms},
            )

    # ------------------------------------------------------------ 工具/模型
    def record_tool_call(self, **kwargs: Any) -> Any:
        """写工具调用记录。

        委托给仓储层，签名与 ``TraceRepository.record_tool_call`` 一致。
        工具注册表调用本方法，因此它必须存在且不改变语义。
        """
        return self.repository.record_tool_call(**kwargs)

    def record_model_call(self, **kwargs: Any) -> Any:
        """写模型调用记录。委托给仓储层。"""
        return self.repository.record_model_call(**kwargs)

    def record_error_event(
        self,
        *,
        run_id: str,
        error_code: str,
        message: Any,
        parent_event_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        """写一条 ``event_type=error`` 的事件（已终结）。

        error 事件是"点事件"——没有持续时长，因此直接写 ``status=failed``
        并补 ``ended_at``，不经过 ``running`` 中间态。
        """
        event = self.repository.append_event(
            run_id=run_id,
            event_type="error",
            name=error_code,
            status="failed",
            parent_event_id=parent_event_id,
            output_summary=message,
            error_code=error_code,
            attributes=attributes,
            summary_max_chars=self.summary_max_chars,
        )
        self.repository.close_event(event.event_id, status="failed", error_code=error_code)
        return event.event_id

    def record_final_result(
        self,
        *,
        run_id: str,
        status: str,
        output_summary: Any = None,
        parent_event_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        """写 ``event_type=final_result`` 的终结事件。"""
        event = self.repository.append_event(
            run_id=run_id,
            event_type="final_result",
            name="final_result",
            status=status,
            parent_event_id=parent_event_id,
            output_summary=output_summary,
            attributes=attributes,
            summary_max_chars=self.summary_max_chars,
        )
        self.repository.close_event(event.event_id, status=status)
        return event.event_id


class NodeSpan:
    """一次节点执行的可变句柄。

    允许节点显式声明结果（``succeed`` / ``fail``），也可以在上下文退出时
    由默认逻辑补写。显式声明优先 —— 因为有些"业务失败"不通过异常表达
    （例如证据不足），需要节点自己说明。
    """

    __slots__ = ("_recorder", "_settled", "_started_perf", "event_id", "name", "output_summary")

    def __init__(self, *, recorder: TraceRecorder, event_id: str, name: str) -> None:
        self._recorder = recorder
        self._settled = False
        self._started_perf: float = 0.0
        self.event_id = event_id
        self.name = name
        self.output_summary: Any = None

    @property
    def settled(self) -> bool:
        """是否已写入结束状态。"""
        return self._settled

    def succeed(self, *, output_summary: Any = None, attributes: dict[str, Any] | None = None) -> None:
        """标记节点成功结束。"""
        self._settle(
            status="ok", output_summary=output_summary, error_code=None, attributes=attributes
        )

    def fail(
        self,
        *,
        error_code: str,
        output_summary: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """标记节点失败结束（不抛异常，仅记录）。"""
        self._settle(
            status="failed",
            output_summary=output_summary,
            error_code=error_code,
            attributes=attributes,
        )

    def skip(self, *, reason: Any = None, attributes: dict[str, Any] | None = None) -> None:
        """标记节点按策略跳过。"""
        self._settle(
            status="skipped", output_summary=reason, error_code=None, attributes=attributes
        )

    def mark_handoff(
        self, *, reason: Any = None, attributes: dict[str, Any] | None = None
    ) -> None:
        """标记需要人工接管（自动流程到此为止）。"""
        self._settle(
            status="handoff", output_summary=reason, error_code=None, attributes=attributes
        )

    def _settle(
        self,
        *,
        status: str,
        output_summary: Any = None,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """写入结束状态。重复调用只生效一次（避免覆盖首个结论）。"""
        if self._settled:
            return
        self._recorder.close_event(
            self.event_id,
            status=status,
            output_summary=output_summary,
            error_code=error_code,
            extra_attributes=attributes,
        )
        self._settled = True
        if output_summary is not None:
            self.output_summary = output_summary


__all__ = ["NodeSpan", "TraceRecorder"]
