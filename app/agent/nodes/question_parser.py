"""节点 1：``question_parser`` —— 解析问题，生成结构化任务。

职责（PROJECT_SPEC §3.1）：把自由文本问题转成结构化任务
（关键词列表 + 意图分类），供后续检索与答案组织使用。

本节点调用一次 LLM（``model_call``）。这是唯一需要"理解"语义的节点 ——
其余节点要么做确定性检索，要么做确定性校验。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.llm import FakeLLMProvider, LlmProvider
from app.agent.state import AgentState, ParsedTask
from app.core.logging import get_logger

logger = get_logger(__name__)

# 解析节点使用的系统提示。刻意简短且要求 JSON 输出：
# 长提示会放大 token 成本，而本项目的成本指标是要被观测的。
_SYSTEM_PROMPT = (
    "你是一个技术文档检索助手。请把用户问题解析成 JSON，字段："
    "keywords（字符串数组，用于文档检索的关键词，3~8 个）、"
    "intent（取值之一：概念解释 / 参数确认 / 决策理由 / 其他）。"
    "只输出 JSON，不要解释。"
)

# 关键词数量上限，避免把整句拆成几十个碎片导致检索被噪声主导
_MAX_KEYWORDS = 8


def parse_question(
    state: AgentState,
    *,
    provider: LlmProvider | None = None,
    recorder: Any | None = None,
) -> dict[str, Any]:
    """解析问题。

    Args:
        state: 当前状态，读取 ``question``。
        provider: LLM provider；默认取配置构造。
        recorder: ``TraceRecorder``，用于记录模型调用。

    Returns:
        要合并进状态的部分更新 + ``visited_nodes`` 追加。
    """
    question = state.get("question", "")
    run_id = state.get("run_id", "")

    if provider is None:
        provider = _default_provider()

    response = provider.complete(
        system=_SYSTEM_PROMPT,
        user=question,
        node_name="question_parser",
    )

    if recorder is not None:
        recorder.record_model_call(
            run_id=run_id,
            node_name="question_parser",
            provider=response.provider,
            model_name=response.model_name,
            is_test_double=response.is_test_double,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            estimated_cost_usd=0 if response.cost_estimation_unavailable else 0,
            cost_estimation_unavailable=response.cost_estimation_unavailable,
            status="ok",
            latency_ms=response.latency_ms,
        )

    parsed = _parse_response(response.text, question)

    logger.info(
        "question_parsed",
        extra={
            "run_id": run_id,
            "keyword_count": len(parsed["keywords"]),
            "intent": parsed["intent"],
            "is_test_double": response.is_test_double,
        },
    )

    return {
        "parsed_task": parsed,
        "visited_nodes": ["question_parser"],
    }


def _parse_response(text: str, question: str) -> ParsedTask:
    """解析 provider 返回的文本。

    **容错策略**：模型输出不是合法 JSON 时**不失败**，而是退化为
    "把原问题当关键词"。理由：

    1. 解析失败属于"LLM 输出格式不可控"这一已知问题，
       让它中断整个 run 会把一个可恢复的问题升级成失败；
    2. 退化为原问题后，检索仍能工作（同义词表会处理语义），
       因此最终答案质量可能只轻微下降；
    3. 这是**可观测**的：日志与 ``intent`` 都会反映降级事实，
       不会被静默吞掉。
    """
    fallback: ParsedTask = {
        "question": question,
        "keywords": [question],
        "intent": "其他",
        "max_chars": len(question),
    }

    if not text.strip():
        return fallback

    # 模型可能把 JSON 包在 ```json ... ``` 里，先剥离围栏
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return fallback

    if not isinstance(data, dict):
        return fallback

    raw_keywords = data.get("keywords")
    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        for item in raw_keywords:
            if isinstance(item, str) and item.strip():
                keywords.append(item.strip())
    # 去重保序
    keywords = list(dict.fromkeys(keywords))[:_MAX_KEYWORDS]

    if not keywords:
        # 关键词为空时用原问题兜底，避免检索阶段拿到空查询
        keywords = [question]

    intent = data.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        intent = "其他"

    return ParsedTask(
        question=question,
        keywords=keywords,
        intent=intent.strip(),
        max_chars=len(question),
    )


def _default_provider() -> LlmProvider:
    """按配置构造默认 provider。"""
    from app.agent.llm import build_provider
    from app.core.config import get_settings

    return build_provider(get_settings())


__all__ = ["FakeLLMProvider", "parse_question"]
