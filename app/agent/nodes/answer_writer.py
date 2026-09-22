"""节点 4：``answer_writer`` —— 基于证据生成结构化答案。

职责（PROJECT_SPEC §3.1）：用 ``parsed_task`` 与 ``evidence_refs``
生成带引用的答案。

**降级语义**（契约的关键约定）：本节点在处理"证据不足"的状态时，
必须把 ``degraded`` 标记为 True。降级完成的 run **不视为成功**，
因此这个标记必须如实反映事实，不能被"反正跑完了"掩盖。

本节点调用一次 LLM（``model_call``）。传给模型的是**证据摘要**，
不是整篇文档 —— 既控制 token 成本，也避免把大段原文写进 Trace 摘要。
"""

from __future__ import annotations

from typing import Any

from app.agent.state import AgentState
from app.core.logging import get_logger

logger = get_logger(__name__)

# 传给模型的单篇文档正文上限。超出部分截断：
# 成本指标是要被观测的，把整篇文档塞进提示会让 token 数失去可比性。
_EVIDENCE_EXCERPT_CHARS = 600

# 最多传给模型的文档数，与 document_search 取回的篇数一致
_MAX_EVIDENCE_DOCS = 3

_SYSTEM_PROMPT = (
    "你是一个严谨的技术文档助手。请只依据给定的证据回答问题，"
    "不得编造文档 ID。答案中必须用 [doc-xxx] 形式标注引用来源。"
    "答案需包含一个「证据」小节列出所用引用。"
)


def write_answer(
    state: AgentState,
    *,
    provider: Any | None = None,
    recorder: Any | None = None,
) -> dict[str, Any]:
    """生成答案。

    Args:
        state: 当前状态，读取 ``parsed_task`` / ``evidence_refs`` /
            ``fetched_documents`` / ``evidence_sufficient``。
        provider: LLM provider；默认按配置构造。
        recorder: ``TraceRecorder``，用于记录模型调用。

    Returns:
        状态更新：``answer`` / ``answer_chars`` / ``citations`` /
        ``degraded`` / ``visited_nodes``。
    """
    run_id = state.get("run_id", "")
    parsed = state.get("parsed_task") or {}
    question = parsed.get("question") or state.get("question", "")
    documents = list(state.get("fetched_documents") or [])
    refs = list(state.get("evidence_refs") or [])
    sufficient = bool(state.get("evidence_sufficient", False))

    if provider is None:
        provider = _default_provider()

    user_prompt = _build_user_prompt(question, documents, refs, sufficient)

    response = provider.complete(
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        node_name="answer_writer",
    )

    if recorder is not None:
        recorder.record_model_call(
            run_id=run_id,
            node_name="answer_writer",
            provider=response.provider,
            model_name=response.model_name,
            is_test_double=response.is_test_double,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            estimated_cost_usd=0,
            cost_estimation_unavailable=response.cost_estimation_unavailable,
            status="ok",
            latency_ms=response.latency_ms,
        )

    answer = response.text.strip()
    citations = _extract_citations(answer, refs)

    # 降级判定的两个来源：
    # 1. 证据不足（evidence_checker 的结论）；
    # 2. 答案里没有任何引用（等价于"没有可验证的依据"）。
    # 只要任一成立就标记降级 —— 宁可保守，不可把无依据的答案算作成功。
    degraded = (not sufficient) or (not citations)

    logger.info(
        "answer_written",
        extra={
            "run_id": run_id,
            "answer_chars": len(answer),
            "citation_count": len(citations),
            "evidence_sufficient": sufficient,
            "degraded": degraded,
        },
    )

    return {
        "answer": answer,
        "answer_chars": len(answer),
        "citations": citations,
        "degraded": degraded,
        "visited_nodes": ["answer_writer"],
    }


def _build_user_prompt(
    question: str, documents: list[Any], refs: list[str], sufficient: bool
) -> str:
    """组装用户提示。

    格式固定（``问题:`` / ``证据:`` 前缀），因为 ``FakeLLMProvider``
    需要按这个格式解析出引用 —— 替身与真实 provider 共用同一份提示构造，
    避免"替身能过、真实模型过不了"这类只在一边暴露的问题。
    """
    lines = [f"问题: {question}", ""]

    if documents:
        lines.append("证据:")
        for document in documents[:_MAX_EVIDENCE_DOCS]:
            document_id = getattr(document, "document_id", None) or (
                document.get("document_id") if isinstance(document, dict) else None
            )
            content = getattr(document, "content", None) or (
                document.get("content") if isinstance(document, dict) else ""
            )
            title = getattr(document, "title", None) or (
                document.get("title") if isinstance(document, dict) else ""
            )
            if not document_id:
                continue
            excerpt = str(content or "")[:_EVIDENCE_EXCERPT_CHARS]
            lines.append(f"[{document_id}] {title}")
            lines.append(excerpt)
            lines.append("")
    elif refs:
        lines.append("证据:")
        for document_id in refs[:_MAX_EVIDENCE_DOCS]:
            lines.append(f"[{document_id}]")
        lines.append("")
    else:
        lines.append("证据: （无）")
        lines.append("")

    if not sufficient:
        lines.append(
            "注意：检索到的证据不足以充分回答问题。请在答案中明确说明这一点，"
            "不要编造未在证据中出现的内容。"
        )

    return "\n".join(lines)


def _extract_citations(answer: str, fallback_refs: list[str]) -> list[str]:
    """从答案中抽取 ``[doc-xxx]`` 形式的引用。

    只保留**答案里真实出现**的引用；``fallback_refs`` 仅作为
    "答案没写引用但检索到了文档"时的依据，且这种情况下
    ``degraded`` 会被标记 —— 因此不会把"检索到但没引用"当成合格答案。

    注意返回值是**去重保序**的：重复引用同一篇文档只算一次，
    否则 ``min_citations>=N`` 断言可以用重复引用轻易刷过。
    """
    import re

    found = re.findall(r"\[(doc-\d{3})\]", answer)
    if found:
        return list(dict.fromkeys(found))

    # 答案完全没有引用：不返回 fallback，让调用方看到"零引用"的真实情况。
    # 早期版本这里返回 fallback_refs，结果是"答案没引用但断言通过"，
    # 遮盖了真实缺陷。
    del fallback_refs
    return []


def _default_provider() -> Any:
    """按配置构造默认 provider。"""
    from app.agent.llm import build_provider
    from app.core.config import get_settings

    return build_provider(get_settings())


__all__ = ["write_answer"]
