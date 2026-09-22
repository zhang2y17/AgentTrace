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
    graph.add_node(
        NODE_QUESTION_PARSER,
        partial(
            node_functions.parse_question,
            provider=deps.provider,
            recorder=deps.recorder,
        ),
    )
    graph.add_node(
        NODE_DOCUMENT_SEARCH,
        partial(
            node_functions.document_search,
            registry=deps.registry,
            recorder=deps.recorder,
            max_top_k=deps.max_top_k,
        ),
    )
    graph.add_node(
        NODE_EVIDENCE_CHECKER,
        partial(node_functions.check_evidence, recorder=deps.recorder),
    )
    graph.add_node(
        NODE_ANSWER_WRITER,
        partial(
            node_functions.write_answer,
            provider=deps.provider,
            recorder=deps.recorder,
        ),
    )
    graph.add_node(
        NODE_FINAL_VALIDATOR,
        partial(
            node_functions.validate_final_answer,
            known_document_ids=deps.resolved_known_document_ids(),
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
        extra={"nodes": list(NODE_ORDER), "max_evidence_retries": deps.resolved_max_evidence_retries()},
    )
    return compiled


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
