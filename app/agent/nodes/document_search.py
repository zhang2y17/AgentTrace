"""节点 2：``document_search`` —— 调用本地样例文档搜索工具。

职责（PROJECT_SPEC §3.1）：把 ``parsed_task`` 的关键词送进检索工具，
把命中的文档写进 ``search_results``。

**放宽检索**是本节点的关键路径：当 ``evidence_checker`` 判定证据不足
并沿条件边回到本节点时，``top_k`` 会增大、查询词会放宽。
放宽的累计次数由 ``search_attempt`` 记录，上限由配置
``MAX_EVIDENCE_RETRIES`` 控制。

**契约要点**：本节点**不直接**调用检索函数，而是经工具注册表 ——
契约 §3.3 第 3 条禁止在节点里硬编码"工具名 → 函数"映射。
"""

from __future__ import annotations

from typing import Any

from app.agent.state import AgentState
from app.core.logging import get_logger
from app.tools.base import TOOL_GET_DOCUMENT, TOOL_SEARCH_DOCUMENTS

logger = get_logger(__name__)

# 放宽检索时 top_k 的放大倍数。
# 用乘法而不是"加一个固定值"：倍数语义更稳定，
# 不会因为默认 top_k 变化而让放宽幅度失真。
_TOP_K_WIDEN_FACTOR = 2

# 放宽时的绝对上限，与契约的 top_k <= 10 保持一致
_TOP_K_WIDEN_CAP = 10


def document_search(
    state: AgentState,
    *,
    registry: Any | None = None,
    recorder: Any | None = None,
    max_top_k: int = 10,
) -> dict[str, Any]:
    """检索样例文档。

    Args:
        state: 当前状态，读取 ``parsed_task`` / ``top_k`` / ``search_attempt``。
        registry: 工具注册表；默认取全局注册表。
        recorder: ``TraceRecorder``，用于落库工具调用。
        max_top_k: top_k 上限（来自配置 ``TOOL_SEARCH_MAX_TOP_K``）。

    Returns:
        状态更新：``search_results`` / ``search_attempt`` / ``visited_nodes``。
    """
    run_id = state.get("run_id", "")
    attempt = int(state.get("search_attempt", 0))
    parsed = state.get("parsed_task") or {}
    keywords: list[str] = list(parsed.get("keywords") or [])

    base_top_k = int(state.get("top_k", 3))
    top_k = _compute_top_k(base_top_k, attempt, max_top_k=max_top_k)
    # 放宽时把查询词从"整个关键词列表"扩到"原问题 + 关键词"，
    # 提升召回。首次检索只用关键词，保持精确。
    query = _build_query(state, keywords, attempt)

    if registry is None:
        from app.tools import registry as registry_module

        registry = registry_module

    hits = registry.invoke(
        TOOL_SEARCH_DOCUMENTS,
        {"query": query, "top_k": top_k},
        run_id=run_id,
        node_name="document_search",
        recorder=recorder,
    )

    # 把命中的前若干篇正文取回来，供 evidence_checker 判断覆盖度、
    # 供 answer_writer 组织答案。取前 3 篇，避免把整个语料库读进内存。
    fetched: list[Any] = []
    for hit in hits[:3]:
        try:
            document = registry.invoke(
                TOOL_GET_DOCUMENT,
                {"document_id": hit.document_id},
                run_id=run_id,
                node_name="document_search",
                recorder=recorder,
            )
            fetched.append(document)
        except Exception as exc:  # noqa: BLE001 —— 单篇读取失败不应中断检索
            # 这是一个**可恢复**的局部失败：跳过这篇，其余继续。
            # 若这里直接抛出，一篇文档的读取问题会变成整个 run 失败，
            # 与实际影响不成比例。
            logger.warning(
                "document_fetch_skipped",
                extra={
                    "run_id": run_id,
                    "document_id": hit.document_id,
                    "error_type": type(exc).__name__,
                },
            )

    logger.info(
        "documents_searched",
        extra={
            "run_id": run_id,
            "attempt": attempt,
            "top_k": top_k,
            "hit_count": len(hits),
            "fetched_count": len(fetched),
            "widened": attempt > 0,
        },
    )

    return {
        "search_results": list(hits),
        "fetched_documents": fetched,
        "search_attempt": attempt + 1,
        "visited_nodes": ["document_search"],
    }


def _compute_top_k(base_top_k: int, attempt: int, *, max_top_k: int) -> int:
    """计算本次检索的 top_k。

    ``attempt == 0`` 为首次检索；``attempt >= 1`` 为放宽检索。
    """
    if attempt <= 0:
        return max(1, min(base_top_k, max_top_k))
    widened = base_top_k * (_TOP_K_WIDEN_FACTOR**attempt)
    return max(1, min(widened, max_top_k, _TOP_K_WIDEN_CAP))


def _build_query(state: AgentState, keywords: list[str], attempt: int) -> str:
    """构造检索查询串。

    首次检索：只用关键词（精确）。
    放宽检索：把原问题也拼进去（提升召回）。
    """
    if attempt <= 0:
        query = " ".join(keywords) if keywords else state.get("question", "")
    else:
        question = state.get("question", "")
        parts = [question] + keywords
        query = " ".join(part for part in parts if part)

    # 检索工具的参数模型限制 query 长度 <= 500
    return query[:500]


__all__ = ["document_search"]
