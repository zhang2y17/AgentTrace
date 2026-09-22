"""LangGraph 状态图装配。

契约 PROJECT_SPEC §3.1 的流程分叉规则：``evidence_checker`` 判定证据不足时，
沿条件边回到 ``document_search`` 进行一次放宽检索，最多
``MAX_EVIDENCE_RETRIES``（默认 1）次；重试耗尽后仍不足，
走 ``answer_writer`` 并在 ``final_result.status = "degraded"``。

图结构::

    question_parser
        ↓
    document_search  ←──────────┐
        ↓                       │ 证据不足且仍有重试额度
    evidence_checker ───────────┘
        ↓ 证据充分 / 重试耗尽
    answer_writer
        ↓
    final_validator

**依赖注入的设计**：图的节点需要 provider、工具注册表、TraceRecorder。
本模块通过 ``AgentDeps`` 容器注入，而不是让节点自己读全局配置。
这样测试可以在不触碰全局状态的前提下替换任意依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any

from app.agent import nodes as node_functions
from app.agent.state import (
    CONDITIONAL_EDGE_TO,
    NODE_ANSWER_WRITER,
    NODE_DOCUMENT_SEARCH,
    NODE_EVIDENCE_CHECKER,
    NODE_FINAL_VALIDATOR,
    NODE_ORDER,
    NODE_QUESTION_PARSER,
    AgentState,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class AgentDeps:
    """节点依赖容器。

    所有字段都有默认值：``None`` 表示"由节点自行按配置构造"。
    显式传入主要用于测试与评测（需要在同一进程内跑多次不同配置的图）。
    """

    provider: Any | None = None
    registry: Any | None = None
    recorder: Any | None = None
    known_document_ids: list[str] | None = None
    max_top_k: int = 10
    # 放宽检索次数上限。``-1`` 表示"取配置值"——
    # 用哨兵值而不是 None，是为了让 dataclass 字段保持 int 类型、
    # 比较与序列化都简单。
    max_evidence_retries: int = -1

    def resolved_max_evidence_retries(self) -> int:
        """取实际生效的放宽检索上限。"""
        if self.max_evidence_retries >= 0:
            return self.max_evidence_retries
        try:
            from app.core.config import get_settings

            return get_settings().max_evidence_retries
        except Exception:  # noqa: BLE001 —— 配置不可用时回退到契约默认值
            return 1

    def resolved_known_document_ids(self) -> list[str] | None:
        """取反幻觉校验用的已知文档 ID 列表。

        ``final_validator`` 用这份列表校验答案里的 ``[doc-xxx]`` 引用是否
        真实存在 —— 这是唯一能确定性兜住"编造引用"的机制。
        因此这里必须**尽力给出一份真实的列表**，而不是默认 ``None``：
        默认 ``None`` 会让这项检查在所有生产 run 里被静默跳过，
        并把每个 run 判成 ``failed``（校验错误里有
        ``hallucination_check_skipped``）。

        只有在文档目录不可用时才返回 ``None`` —— 此时跳过是诚实的，
        因为确实无从判断哪些 ID 合法。
        """
        if self.known_document_ids is not None:
            return list(self.known_document_ids)
        try:
            from app.tools.bootstrap import get_document_store

            return get_document_store().document_ids
        except Exception:  # noqa: BLE001 —— 语料不可用时不阻断图构建
            logger.warning("known_document_ids_unavailable")
            return None


def build_graph(deps: AgentDeps, *, checkpointer: Any | None = None) -> Any:
    """构造并编译 LangGraph 状态图。

    Args:
        deps: 节点依赖。
        checkpointer: 可选的 LangGraph checkpointer；本项目不使用
            （状态落库由 Trace 承担，不需要框架级检查点）。

    Returns:
        已编译的可执行图。

    Raises:
        RuntimeError: LangGraph 不可用。
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover —— 依赖缺失时的明确报错
        raise RuntimeError(
            "LangGraph 不可用。请确认已安装依赖：pip install -r requirements.txt"
        ) from exc

    graph = StateGraph(AgentState)

    # ---------------------------------------------------------- 注册节点
    #
    # 每个节点都被 ``_trace_node`` 包一层，由它负责写 ``node`` 事件的
    # 开始与结束。**为什么在这里包而不是让每个节点自己调用 recorder.node()**：
    #
    # 1. 节点函数因此只需报告"我在内部调用了哪些工具/模型"，
    #    不必关心"我这一次执行的起止与终态"—— 后者的职责边界属于图；
    # 2. "有开始必有结束"只在一个地方保证。若分散到 5 个节点里，
    #    任何一个节点忘记收尾都会留下永久 ``running`` 的事件；
    # 3. 节点函数可以完全脱离数据库单测（传 recorder=None 即可）。
    #
    # 早期版本没有这层包装，节点内部只调 record_tool_call /
    # record_model_call，结果 **一条 node 事件都不会写入** ——
    # 而 node 事件是回放与延迟统计的主干，等于 Trace 的主体缺失。
    graph.add_node(
        NODE_QUESTION_PARSER,
        _trace_node(
            NODE_QUESTION_PARSER,
            partial(
                node_functions.parse_question,
                provider=deps.provider,
                recorder=deps.recorder,
            ),
            recorder=deps.recorder,
        ),
    )
    graph.add_node(
        NODE_DOCUMENT_SEARCH,
        _trace_node(
            NODE_DOCUMENT_SEARCH,
            partial(
                node_functions.document_search,
                registry=deps.registry,
                recorder=deps.recorder,
                max_top_k=deps.max_top_k,
            ),
            recorder=deps.recorder,
        ),
    )
    graph.add_node(
        NODE_EVIDENCE_CHECKER,
        _trace_node(
            NODE_EVIDENCE_CHECKER,
            partial(node_functions.check_evidence, recorder=deps.recorder),
            recorder=deps.recorder,
        ),
    )
    graph.add_node(
        NODE_ANSWER_WRITER,
        _trace_node(
            NODE_ANSWER_WRITER,
            partial(
                node_functions.write_answer,
                provider=deps.provider,
                recorder=deps.recorder,
            ),
            recorder=deps.recorder,
        ),
    )
    graph.add_node(
        NODE_FINAL_VALIDATOR,
        _trace_node(
            NODE_FINAL_VALIDATOR,
            partial(
                node_functions.validate_final_answer,
                known_document_ids=deps.resolved_known_document_ids(),
                recorder=deps.recorder,
            ),
            recorder=deps.recorder,
        ),
    )

    # ---------------------------------------------------------- 顺序边
    graph.set_entry_point(NODE_QUESTION_PARSER)
    graph.add_edge(NODE_QUESTION_PARSER, NODE_DOCUMENT_SEARCH)
    graph.add_edge(NODE_DOCUMENT_SEARCH, NODE_EVIDENCE_CHECKER)
    graph.add_edge(NODE_ANSWER_WRITER, NODE_FINAL_VALIDATOR)
    graph.add_edge(NODE_FINAL_VALIDATOR, END)

    # ---------------------------------------------------------- 条件边
    graph.add_conditional_edges(
        NODE_EVIDENCE_CHECKER,
        partial(_route_after_evidence, max_retries=deps.resolved_max_evidence_retries()),
        {
            # 证据充分，或重试额度已耗尽 → 继续写答案
            NODE_ANSWER_WRITER: NODE_ANSWER_WRITER,
            # 证据不足但仍有额度 → 回退放宽检索
            CONDITIONAL_EDGE_TO: CONDITIONAL_EDGE_TO,
        },
    )

    compiled = graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()

    logger.info(
        "agent_graph_compiled",
        extra={
            "nodes": list(NODE_ORDER),
            "max_evidence_retries": deps.resolved_max_evidence_retries(),
        },
    )
    return compiled


def _trace_node(
    name: str,
    node_fn: Any,
    *,
    recorder: Any | None,
) -> Any:
    """把节点函数包成"自带 node 事件生命周期"的可调用对象。

    Args:
        name: 节点名，写入 ``trace_event.name``。
        node_fn: 原始节点函数，签名 ``(state) -> dict``。
        recorder: ``TraceRecorder``；``None`` 时退化为直接调用
            （纯逻辑单测不需要数据库）。

    Returns:
        包装后的函数，签名与 ``node_fn`` 一致。

    包装后每次执行会写入**一条** ``node`` 事件：
    开始写 ``status=running``，正常返回补 ``ok``，抛异常补 ``failed``。
    节点的输入/输出摘要取自状态里的关键字段，而不是整个状态 ——
    状态里有文档正文，整份写进 Trace 会让事件体积失控。
    """
    if recorder is None:
        return node_fn

    def _wrapper(state: Any) -> Any:
        run_id = str((state or {}).get("run_id") or "")
        if not run_id:
            # 没有 run_id 就无法归属事件。这种情况下直接执行：
            # 记录一条无处归属的事件比不记录更糟（会破坏外键语义）。
            return node_fn(state)

        with recorder.node(
            run_id=run_id,
            name=name,
            input_summary=_node_input_summary(name, state),
        ) as span:
            # 把本节点的 event_id 塞进 state，供节点内的工具/模型调用
            # 作为 parent_event_id。契约 TRACE_SCHEMA §5 规则 3 要求
            # tool_call / model_call 必须挂在发起它们的 node 之下。
            #
            # 注入发生在**进入节点之前**，且节点内部读取的是自己那份
            # 局部 state —— LangGraph 的 state 是不可变增量更新，
            # 这里改的是传入的 dict，因此必须确保节点读到的是同一个对象。
            if isinstance(state, dict):
                state["current_node_event_id"] = span.event_id

            result = node_fn(state)
            span.succeed(output_summary=_node_output_summary(name, result))
            return result

    # 保留原名，便于日志与调试时辨识
    _wrapper.__name__ = f"traced_{name}"
    return _wrapper


def _node_input_summary(name: str, state: Any) -> dict[str, Any]:
    """按节点抽取输入摘要。

    刻意逐个节点挑选字段：把整个 state 序列化会把检索到的文档正文
    一并写进 Trace，既膨胀存储又违背"只存摘要"的契约。
    """
    state = state or {}
    if name == NODE_QUESTION_PARSER:
        return {"question_chars": len(str(state.get("question") or ""))}
    if name == NODE_DOCUMENT_SEARCH:
        return {
            "top_k": state.get("top_k"),
            "search_attempt": state.get("search_attempt", 0),
            "keyword_count": len((state.get("parsed_task") or {}).get("keywords") or []),
        }
    if name == NODE_EVIDENCE_CHECKER:
        return {"hit_count": len(state.get("search_results") or [])}
    if name == NODE_ANSWER_WRITER:
        return {
            "document_count": len(state.get("fetched_documents") or []),
            "evidence_sufficient": bool(state.get("evidence_sufficient", False)),
        }
    if name == NODE_FINAL_VALIDATOR:
        return {"answer_chars": state.get("answer_chars", 0)}
    return {}


def _node_output_summary(name: str, result: Any) -> dict[str, Any]:
    """按节点抽取输出摘要。"""
    if not isinstance(result, dict):
        return {}
    if name == NODE_QUESTION_PARSER:
        parsed = result.get("parsed_task") or {}
        return {
            "intent": parsed.get("intent"),
            "keyword_count": len(parsed.get("keywords") or []),
        }
    if name == NODE_DOCUMENT_SEARCH:
        return {
            "hit_count": len(result.get("search_results") or []),
            "fetched_count": len(result.get("fetched_documents") or []),
        }
    if name == NODE_EVIDENCE_CHECKER:
        return {
            "evidence_sufficient": result.get("evidence_sufficient"),
            "evidence_coverage": result.get("evidence_coverage"),
        }
    if name == NODE_ANSWER_WRITER:
        return {
            "answer_chars": result.get("answer_chars", 0),
            "citation_count": len(result.get("citations") or []),
            "degraded": result.get("degraded"),
        }
    if name == NODE_FINAL_VALIDATOR:
        final = result.get("final_result") or {}
        return {
            "status": final.get("status"),
            "validation_error_count": len(result.get("validation_errors") or []),
        }
    return {}


def _route_after_evidence(state: AgentState, *, max_retries: int) -> str:
    """条件边判定：证据检查后往哪走。

    Args:
        state: 当前状态。
        max_retries: 允许的放宽检索次数上限。

    Returns:
        下一个节点名。

    判定顺序（重要）：

    1. 证据充分 → 直接 ``answer_writer``；
    2. 证据不足但 ``search_attempt`` 未超过额度 → 回 ``document_search``；
    3. 证据不足且额度耗尽 → ``answer_writer``（由该节点标记 degraded）。

    ``search_attempt`` 是"已经检索过的次数"（初始 0，首次检索后变 1）。
    因此"还能放宽几次" = ``max_retries + 1 - search_attempt``。
    """
    if state.get("evidence_sufficient", False):
        return NODE_ANSWER_WRITER

    attempts_used = int(state.get("search_attempt", 0))
    # 首次检索后 attempts_used = 1；max_retries = 1 时允许再放宽一次，
    # 即 attempts_used <= 1 时还可以回退。
    if attempts_used <= max_retries:
        logger.debug(
            "evidence_insufficient_routing_to_widened_search",
            extra={"attempts_used": attempts_used, "max_retries": max_retries},
        )
        return CONDITIONAL_EDGE_TO

    logger.info(
        "evidence_retry_budget_exhausted",
        extra={"attempts_used": attempts_used, "max_retries": max_retries},
    )
    return NODE_ANSWER_WRITER


__all__ = ["AgentDeps", "build_graph"]
