"""运行服务：执行 Agent、落库 Trace、回放。

职责（ARCHITECTURE §2）：把"跑一次 Agent"这件事的编排收在这里，
HTTP 层只负责参数解析与错误映射。

**三个关键设计**

1. **超时控制在服务层而非路由层**。``AGENT_TIMEOUT_SECONDS`` 是业务约束
   （"一次运行最多允许多久"），不是 HTTP 层的关注点。超时后 run 状态
   落库为 ``timeout`` 而不是 ``failed`` —— 超时是"没跑完"，失败是"跑错了"，
   两者在指标里的含义完全不同（超时不计入 ``error_rate``）。

2. **执行与落库用同一个 Session 边界**。run 行先以 ``running`` 落库，
   执行完再 ``finalize_run`` 补终态。这样即使进程中途被杀，
   残留的 ``running`` 行本身就是一条可诊断的证据
   （而不是什么都不留下）。

3. **回放绝不覆盖原始记录**。回放生成新的 ``run_id``，靠
   ``source_run_id`` 指回原 run。回放是新 run 而不是"原 run 的副本" ——
   因此它有自己的 Trace 事件、自己的耗时与成本。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import (
    AgentTimeoutError,
    InvalidArgumentError,
    RunNotFoundError,
    RunNotReplayableError,
    TracePersistenceError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

# 可回放的状态集合。``running`` / ``pending`` 的 run 自身还没有定论，
# 拿它当回放基准没有意义（契约 API_CONTRACT §5 要求返回 409）。
_REPLAYABLE_STATUSES = frozenset({"succeeded", "failed", "degraded", "timeout"})

# 一次 run 执行时写入状态里的默认 top_k
_DEFAULT_TOP_K = 3


@dataclass(slots=True)
class RunExecutionResult:
    """执行结果的内部载体。

    刻意不复用 ``RunDetail``：那是 API 契约模型，字段随契约变动；
    本结构只承载"服务层交出去的东西"，由路由层负责组装成契约模型。
    """

    run_id: str
    status: str
    question: str
    agent_version: str
    prompt_version: str
    llm_provider: str
    model_name: str | None = None
    source_run_id: str | None = None
    is_test_double: bool = True
    duration_ms: int | None = None
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_estimation_unavailable: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    result_summary: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)


class RunService:
    """运行编排服务。

    Args:
        settings: 应用配置。
        session_factory: 返回 Session 的可调用对象。默认取全局工厂；
            测试可注入以便与测试库共享同一连接。
        graph_builder: 构造可执行图的工厂，签名为
            ``(deps) -> compiled_graph``。注入它是为了让测试能替换图，
            而不必真的跑 LangGraph。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: Any | None = None,
        graph_builder: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory
        self._graph_builder = graph_builder

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def execute(
        self,
        *,
        question: str,
        agent_version: str = "v1",
        prompt_version: str = "prompt-v1",
        top_k: int = _DEFAULT_TOP_K,
        metadata: dict[str, Any] | None = None,
        source_run_id: str | None = None,
    ) -> RunExecutionResult:
        """执行一次 Agent 运行并落库。

        Args:
            question: 用户问题。**调用方应已做长度与非空白校验**。
            agent_version: Agent 版本标签。
            prompt_version: Prompt 版本标签。
            top_k: 检索条数。
            metadata: 自由标签，写入 ``result_summary`` 之外不参与逻辑。
            source_run_id: 非 None 表示这是一次回放。

        Returns:
            执行结果。

        Raises:
            AgentTimeoutError: 超过 ``AGENT_TIMEOUT_SECONDS``。
            TracePersistenceError: 落库失败。
        """
        # 非空白校验放在服务层：路由层的 Pydantic 校验失败统一映射为 400，
        # 而契约要求空白问题返回 422 AGENT_VALIDATION_ERROR —— 见 app/api/runs.py。
        if not question or not question.strip():
            raise InvalidArgumentError("question 不能为空白字符串。")

        max_chars = self.settings.max_question_chars
        if len(question) > max_chars:
            raise InvalidArgumentError(
                f"question 超长：{len(question)} > {max_chars}。",
                details={"max_chars": max_chars, "actual": len(question)},
            )

        started_at = None
        run_id = ""
        factory = self._resolve_session_factory()

        # ---------------------------------------------------------- 1. 建 run 行
        # 先落 ``running``：进程若在中途被杀，这行残留在库里就是证据。
        with factory() as session:
            from app.db.repository import TraceRepository

            repository = TraceRepository(session)
            run = repository.create_run(
                question=question,
                status="running",
                agent_version=agent_version,
                prompt_version=prompt_version,
                llm_provider=self.settings.llm_provider,
                model_name=self._model_name(),
                is_test_double=self.settings.is_test_double_mode,
                source_run_id=source_run_id,
            )
            run_id = run.id
            started_at = run.started_at

            # 写 ``run`` 根事件。契约 TRACE_SCHEMA §5 的树形结构要求
            # 5 个 node 与 final_result 都直接挂在这个根下 ——
            # 没有它，Trace 就是一片无根的森林，"这次 run 整体如何"
            # 无法用一条自关联查询回答。
            #
            # 根事件是"点事件"：写入即 ``running``，由 finalize 阶段
            # 补终态（这样它的 duration_ms 覆盖整个 run 的耗时）。
            try:
                root_event = repository.append_event(
                    run_id=run_id,
                    event_type="run",
                    name="run",
                    status="running",
                    attributes={
                        "agent_version": agent_version,
                        "prompt_version": prompt_version,
                        "llm_provider": self.settings.llm_provider,
                        "model_name": self._model_name(),
                        "is_test_double": self.settings.is_test_double_mode,
                        "is_replay": source_run_id is not None,
                    },
                )
                root_event_id = root_event.event_id
            except Exception as exc:  # noqa: BLE001 —— 根事件缺失不阻断执行
                logger.warning(
                    "run_root_event_failed",
                    extra={"run_id": run_id, "error_type": type(exc).__name__},
                )
                root_event_id = None

        logger.info(
            "run_started",
            extra={
                "run_id": run_id,
                "agent_version": agent_version,
                "prompt_version": prompt_version,
                "is_replay": source_run_id is not None,
                "is_test_double": self.settings.is_test_double_mode,
            },
        )

        # ---------------------------------------------------------- 2. 执行图
        started_perf = time.perf_counter()
        try:
            state = self._invoke_graph(
                run_id=run_id,
                question=question,
                top_k=top_k,
                metadata=metadata or {},
            )
        except AgentTimeoutError:
            # 超时是独立终态：run 标记 timeout，**不计入错误率**。
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            self._finalize(
                run_id=run_id,
                status="timeout",
                duration_ms=duration_ms,
                error_code="AGENT_TIMEOUT",
                error_message=f"运行超过 {self.settings.agent_timeout_seconds} 秒未完成。",
                root_event_id=root_event_id,
            )
            logger.warning(
                "run_timeout",
                extra={"run_id": run_id, "timeout_seconds": self.settings.agent_timeout_seconds},
            )
            raise

        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            self._finalize(
                run_id=run_id,
                status="failed",
                duration_ms=duration_ms,
                error_code=getattr(exc, "error_code", "INTERNAL_ERROR"),
                error_message=str(exc)[:500],
                root_event_id=root_event_id,
            )
            logger.exception(
                "run_failed",
                extra={"run_id": run_id, "error_type": type(exc).__name__},
            )
            raise

        duration_ms = int((time.perf_counter() - started_perf) * 1000)

        # ---------------------------------------------------------- 3. 定终态
        final_status = self._resolve_final_status(state)
        result_summary = self._build_result_summary(state, metadata or {})
        tokens, cost, cost_unavailable = self._collect_tokens_and_cost(run_id)

        self._finalize(
            run_id=run_id,
            status=final_status,
            duration_ms=duration_ms,
            result_summary=result_summary,
            total_tokens=tokens,
            estimated_cost_usd=cost,
            root_event_id=root_event_id,
        )

        # 计数**必须在 _finalize 之后**做。
        #
        # _finalize 会补写 final_result 事件并把 run 根事件收尾，
        # 也就是说它会往 trace_event 表里再插一行。若在它之前计数，
        # 响应里报的 trace_events 会比实际落库的少 1 ——
        # 调用方拿这个数去核对 GET /runs/{id}/events 的 total 会对不上，
        # 从而怀疑"是不是有事件丢了"。这个偏差是最难查的那类：
        # 两个接口都"没错"，只是数的时机不同。
        counts = self._count_artifacts(run_id)

        logger.info(
            "run_finished",
            extra={
                "run_id": run_id,
                "status": final_status,
                "duration_ms": duration_ms,
                "trace_events": counts.get("trace_events", 0),
                "degraded": final_status == "degraded",
            },
        )

        return RunExecutionResult(
            run_id=run_id,
            status=final_status,
            question=question,
            agent_version=agent_version,
            prompt_version=prompt_version,
            llm_provider=self.settings.llm_provider,
            model_name=self._model_name(),
            source_run_id=source_run_id,
            is_test_double=self.settings.is_test_double_mode,
            duration_ms=duration_ms,
            total_tokens=tokens,
            estimated_cost_usd=cost,
            cost_estimation_unavailable=cost_unavailable,
            started_at=started_at,
            ended_at=self._now(),
            result_summary=result_summary,
            counts=counts,
            state=state,
        )

    # ------------------------------------------------------------------
    # 回放
    # ------------------------------------------------------------------

    def replay(
        self,
        run_id: str,
        *,
        question: str | None = None,
        agent_version: str | None = None,
        prompt_version: str | None = None,
        top_k: int | None = None,
        note: str | None = None,
    ) -> RunExecutionResult:
        """以原始运行的输入重新执行一次。

        **绝不覆盖原始记录**：新 run 用新 ID，靠 ``source_run_id`` 指回原 run。
        每次调用都产生一个新 run（非幂等，见 API_CONTRACT §5）——
        回放需要多次独立采样，复用同一个 run 反而会让对比失去意义。

        Args:
            run_id: 被回放的原始 run。
            question: 覆盖原问题；``None`` 表示沿用。
            agent_version: 覆盖 Agent 版本，用于版本对比。
            prompt_version: 覆盖 Prompt 版本。
            top_k: 覆盖检索条数。
            note: 回放备注，写入结果摘要。

        Raises:
            RunNotFoundError: 原 run 不存在。
            RunNotReplayableError: 原 run 仍在 ``running``/``pending``。
        """
        original = self.get_run_record(run_id)
        if original is None:
            raise RunNotFoundError(f"run 不存在：{run_id}")

        # 未定论的 run 不能当基准：它的输入可能还在变，回放没有可比性。
        if original.status not in _REPLAYABLE_STATUSES:
            raise RunNotReplayableError(
                f"run {run_id} 当前状态为 {original.status}，不可回放。",
                details={"status": original.status, "replayable": sorted(_REPLAYABLE_STATUSES)},
            )

        source_summary = original.result_summary or {}
        effective_top_k = top_k
        if effective_top_k is None:
            # 原 run 的 top_k 存在 result_summary 里；缺失则用默认值。
            raw_top_k = source_summary.get("top_k")
            effective_top_k = int(raw_top_k) if isinstance(raw_top_k, int) else _DEFAULT_TOP_K

        metadata: dict[str, Any] = {"replay_note": note} if note else {}
        metadata["replayed_from"] = run_id

        return self.execute(
            question=question if question is not None else original.question,
            agent_version=agent_version or original.agent_version,
            prompt_version=prompt_version or original.prompt_version,
            top_k=effective_top_k,
            metadata=metadata,
            source_run_id=run_id,
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_run_record(self, run_id: str) -> Any | None:
        """取原始 ORM run 行；不存在返回 None。"""
        factory = self._resolve_session_factory()
        with factory() as session:
            from app.db.repository import TraceRepository

            return TraceRepository(session).get_run(run_id)

    def get_run_counts(self, run_id: str) -> dict[str, int]:
        """统计一次 run 的事件、工具调用、模型调用数量。"""
        return self._count_artifacts(run_id)

    # ------------------------------------------------------------------
    # 内部：图执行
    # ------------------------------------------------------------------

    def _invoke_graph(
        self,
        *,
        run_id: str,
        question: str,
        top_k: int,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """构造图、挂 TraceRecorder、执行（含超时控制）。

        超时用 ``concurrent.futures`` 的线程池 + ``future.result(timeout=)``
        实现，而不是 ``signal.alarm``（Windows 不支持）或 ``asyncio.wait_for``
        （LangGraph 的 ``invoke`` 是同步阻塞的，包在协程里也逃不出 GIL，
        仍然会卡住事件循环）。
        """
        import concurrent.futures

        from app.agent.graph import AgentDeps
        from app.agent.llm import build_provider
        from app.agent.middleware import TraceRecorder
        from app.db.repository import TraceRepository
        from app.tools.bootstrap import get_document_store

        factory = self._resolve_session_factory()
        # 用 session_scope 而非裸 session：它负责 commit/rollback/close。
        # 先前写成 ``session = factory()`` + ``session.close()`` 是错的 ——
        # session_scope 是生成器上下文管理器，没有 close()，
        # 且裸 session 不提交事务，节点事件会全部丢失。
        with factory() as session:
            repository = TraceRepository(session)
            recorder = TraceRecorder(repository)

            provider = build_provider(self.settings)

            # 保证内置工具已注册。正常情况下应用 lifespan 已经注册过
            # （此处是幂等空操作）；但直接调用 RunService 的场景
            # （脚本、评测、集成测试）不经过 lifespan ——
            # 若依赖"调用方一定先启动过应用"，那些场景会以
            # "工具未注册"这种与真实原因无关的错误失败。
            self._ensure_tools_registered()

            deps = AgentDeps(
                provider=provider,
                recorder=recorder,
                known_document_ids=get_document_store().document_ids,
            )
            graph = self._build_graph(deps)

            initial_state: dict[str, Any] = {
                "run_id": run_id,
                "question": question,
                "top_k": top_k,
                "search_attempt": 0,
                "evidence_coverage_threshold": self.settings.evidence_coverage_threshold,
                "errors": [],
                "visited_nodes": [],
            }
            if metadata:
                initial_state["metadata"] = metadata

            timeout = self.settings.agent_timeout_seconds
            # 单线程池：预算可控（一次一个 run），且避免并发执行同一图实例。
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(graph.invoke, initial_state)
                try:
                    return future.result(timeout=timeout)
                except concurrent.futures.TimeoutError as exc:
                    # 注意：这里**不能**等待线程结束再返回。
                    # 超时返回后线程可能仍在跑，但它持有自己的 session，
                    # 不会影响响应。标记 run 为 timeout 由调用方负责。
                    raise AgentTimeoutError(
                        f"运行超过 {timeout} 秒未完成。",
                        details={"timeout_seconds": timeout, "run_id": run_id},
                    ) from exc

    def _build_graph(self, deps: Any) -> Any:
        """构造图；优先用注入的 builder，便于测试替换。"""
        if self._graph_builder is not None:
            return self._graph_builder(deps)
        from app.agent.graph import build_graph

        return build_graph(deps)

    @staticmethod
    def _ensure_tools_registered() -> None:
        """确保四个内置工具已注册（幂等）。

        注册表是模块级的，重复调用是空操作。失败时抛出而不是静默 ——
        工具没注册会让每个 run 都以 ToolNotFoundError 失败，
        那种错误的表象与真实原因（装配没跑）相差很远，难以定位。
        """
        from app.tools.bootstrap import register_default_tools

        register_default_tools()

    # ------------------------------------------------------------------
    # 内部：落库
    # ------------------------------------------------------------------

    def _finalize(
        self,
        *,
        run_id: str,
        status: str,
        duration_ms: int,
        result_summary: dict[str, Any] | None = None,
        total_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        error_code: str | None = None,
        error_message: str | None = None,
        root_event_id: str | None = None,
    ) -> None:
        """写入 run 终态，并收尾 Trace 的事件流。

        落库失败抛 ``TracePersistenceError`` 而不吞掉 —— run 已经跑完但
        结果没落地，属于必须暴露的问题（契约 API_CONTRACT §2 的
        ``TRACE_WRITE_FAILED``）。

        除更新 run 行外，这里还负责两件 Trace 收尾工作：

        1. 写 ``final_result`` 事件（挂在 ``run`` 根事件下）——
           它是"这次运行得出什么结论"的唯一权威记录；
        2. 给 ``run`` 根事件补终态，让它的 ``duration_ms`` 覆盖整次运行。

        这两步都在**同一次 finalize 调用**里完成，因为它们必须与
        run 终态一致：run 行说 succeeded 而 final_result 事件缺失，
        会造成"结论无从追溯"。
        """
        from decimal import Decimal

        factory = self._resolve_session_factory()
        try:
            with factory() as session:
                from app.db.repository import TraceRepository

                repository = TraceRepository(session)
                repository.finalize_run(
                    run_id,
                    status=status,
                    result_summary=result_summary,
                    total_tokens=total_tokens,
                    estimated_cost_usd=Decimal(str(estimated_cost_usd)),
                    error_code=error_code,
                    error_message=error_message,
                    duration_ms=duration_ms,
                )
                # ------------------------------------------------ Trace 收尾
                # 这两步失败**不**让整个 finalize 抛错：run 终态已经写入，
                # 这是业务上的既成事实；把补充视图的写入失败升级成
                # TracePersistenceError 会让调用方以为 run 状态也没落库，
                # 从而重试并产生重复 run。
                try:
                    if root_event_id:
                        self._close_run_root_event(
                            repository=repository,
                            root_event_id=root_event_id,
                            status=status,
                            duration_ms=duration_ms,
                        )
                    repository.record_final_result_event(
                        run_id=run_id,
                        status=self._event_status_for_run(status),
                        parent_event_id=root_event_id,
                        output_summary=result_summary,
                        error_code=error_code,
                        # run 级原始结论保留在 attributes 里 ——
                        # 事件枚举没有 degraded/timeout 这些词，
                        # 但"这次到底是降级还是超时"必须可查。
                        attributes={"run_status": status},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "trace_finalization_incomplete",
                        extra={
                            "run_id": run_id,
                            "error_type": type(exc).__name__,
                            "has_root_event": root_event_id is not None,
                        },
                    )
        except Exception as exc:  # noqa: BLE001 —— 统一转成领域错误
            raise TracePersistenceError(
                f"run {run_id} 的终态写入失败。",
                details={"run_id": run_id, "error_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _event_status_for_run(run_status: str) -> str:
        """把 run 级状态映射成事件级 ``EventStatus``。

        **这两个枚举的取值不同，不能混用**：

        - ``RunStatus``：``running`` / ``succeeded`` / ``failed`` /
          ``degraded`` / ``timeout`` / ``pending`` —— 描述"这次运行的结果"；
        - ``EventStatus``：``pending`` / ``running`` / ``ok`` / ``failed`` /
          ``skipped`` / ``retried`` / ``invalid_arguments`` / ``handoff``
          —— 描述"这一个步骤的结果"。

        事件层没有 ``succeeded`` / ``degraded`` / ``timeout`` 这些词，
        因此必须显式映射。映射规则：

        - ``succeeded`` → ``ok``（步骤正常完成）；
        - ``degraded`` → ``ok``：降级的真相写在 ``final_result`` 事件的
          ``output_summary`` 与 run 行里。在**事件层**把它标成失败是错的 ——
          run 确实从头跑到尾，每个节点都成功了，
          用 ``failed`` 会让 "Trace 里有失败事件" 与
          "error_rate = 0" 这两个读数互相矛盾；
        - ``failed`` / ``timeout`` → ``failed``（步骤没有正常完成）。
        """
        if run_status in {"succeeded", "degraded"}:
            return "ok"
        if run_status in {"running", "pending"}:
            return "running"
        return "failed"

    @staticmethod
    def _close_run_root_event(
        *,
        repository: Any,
        root_event_id: str,
        status: str,
        duration_ms: int,
    ) -> None:
        """给 ``run`` 根事件补终态。

        ``run`` 事件的最终 ``status`` 由 ``_event_status_for_run`` 映射而来；
        ``error_code`` 不重复写在事件上 —— ``final_result`` 事件才是
        结论的载体，根事件只描述"整次运行的起止与结果状态"。

        ``duration_ms`` 由 finalize 传入（``time.perf_counter`` 口径），
        而不是重新算 ``ended_at - started_at``：两者会有毫秒级差异，
        而 run 行与根事件报同一个数才不会让读的人以为是两次不同的运行。
        """
        repository.close_event(
            root_event_id,
            status=RunService._event_status_for_run(status),
            output_summary={"duration_ms": duration_ms, "run_status": status},
        )
        # close_event 会按 ended_at - started_at 重算 duration_ms，
        # 这里用显式值覆盖，保证 run 行与根事件严格一致。
        repository.set_event_duration(root_event_id, duration_ms)

    def _collect_tokens_and_cost(self, run_id: str) -> tuple[int, float, bool]:
        """从 model_call 表聚合 token 与成本。

        Returns:
            ``(总 token, 总成本 USD, 是否存在无法估算成本的调用)``

        第三个值是诚实的必要条件：若有调用的模型名不在定价表里，
        成本就是**不完整**的。返回 0 而不加说明会被读成"没有成本"。
        """
        factory = self._resolve_session_factory()
        with factory() as session:
            from app.db.repository import TraceRepository

            repository = TraceRepository(session)
            try:
                tokens, cost, unavailable = repository.aggregate_token_and_cost(run_id)
            except Exception as exc:  # noqa: BLE001 —— 聚合失败不应让 run 失败
                logger.warning(
                    "token_aggregation_failed",
                    extra={"run_id": run_id, "error_type": type(exc).__name__},
                )
                return 0, 0.0, True
        return int(tokens or 0), float(cost or 0.0), bool(unavailable)

    def _count_artifacts(self, run_id: str) -> dict[str, int]:
        """统计 trace_events / tool_calls / model_calls 数量。"""
        factory = self._resolve_session_factory()
        with factory() as session:
            from sqlalchemy import func, select

            from app.db.models import ModelCall, ToolCall, TraceEvent

            try:
                events = int(
                    session.execute(
                        select(func.count())
                        .select_from(TraceEvent)
                        .where(TraceEvent.run_id == run_id)
                    ).scalar_one()
                )
                tool_calls = int(
                    session.execute(
                        select(func.count())
                        .select_from(ToolCall)
                        .where(ToolCall.run_id == run_id)
                    ).scalar_one()
                )
                model_calls = int(
                    session.execute(
                        select(func.count())
                        .select_from(ModelCall)
                        .where(ModelCall.run_id == run_id)
                    ).scalar_one()
                )
            except Exception as exc:  # noqa: BLE001 —— 计数失败不应让 run 失败
                logger.warning(
                    "artifact_count_failed",
                    extra={"run_id": run_id, "error_type": type(exc).__name__},
                )
                return {"trace_events": 0, "tool_calls": 0, "model_calls": 0}

        return {
            "trace_events": events,
            "tool_calls": tool_calls,
            "model_calls": model_calls,
        }

    # ------------------------------------------------------------------
    # 内部：状态映射
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_final_status(state: dict[str, Any]) -> str:
        """从图状态推导 run 终态。

        优先用 ``final_result.status``（final_validator 的判定），
        因为那里才是"答案是否合格"的权威结论。
        """
        final_result = state.get("final_result") or {}
        status = str(final_result.get("status") or "").strip()
        if status in {"succeeded", "failed", "degraded"}:
            return status
        # 没有 final_result 说明图没跑完（异常路径已在别处处理），
        # 保守判 failed 而不是 succeeded。
        return "failed" if state.get("errors") else "succeeded"

    def _build_result_summary(
        self, state: dict[str, Any], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """构造存进 run.result_summary 的摘要。

        **只存摘要**：完整答案会被截断。Trace 的价值在于可观测，
        不是把整篇文档复制一份进数据库。
        """
        final_result = state.get("final_result") or {}
        answer = str(state.get("answer") or "")

        summary: dict[str, Any] = {
            "answer": answer[: self.settings.summary_max_chars],
            "answer_chars": int(state.get("answer_chars") or len(answer)),
            "citations": list(final_result.get("citations") or state.get("citations") or []),
            "evidence_sufficient": bool(state.get("evidence_sufficient", False)),
            "evidence_coverage": float(state.get("evidence_coverage") or 0.0),
            "final_status": str(final_result.get("status") or ""),
            "validation_errors": list(state.get("validation_errors") or []),
            "visited_nodes": list(state.get("visited_nodes") or []),
            "search_attempt": int(state.get("search_attempt") or 0),
            # top_k 存下来供回放复用（回放要能复现原始检索行为）
            "top_k": int(state.get("top_k") or _DEFAULT_TOP_K),
            "data_source_note": (
                "本次运行的模型调用由本地测试替身产生，"
                "非真实模型输出，不代表任何线上流量或生产环境表现。"
                if self.settings.is_test_double_mode
                else "本次运行使用配置的真实模型提供方。"
            ),
        }
        if metadata:
            summary["metadata"] = metadata
        return summary

    def _model_name(self) -> str:
        """当前生效的模型名。

        测试替身模式下报 ``fake-model`` 而不是配置里的模型名 ——
        报一个真实模型名会让"这是替身"这个事实被掩盖。
        """
        if self.settings.is_test_double_mode:
            return "fake-model"
        return self.settings.llm_model

    def _resolve_session_factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.session import session_scope

        return session_scope

    @staticmethod
    def _now() -> datetime:
        from app.db.base import utcnow

        return utcnow()


__all__ = ["RunExecutionResult", "RunService"]
