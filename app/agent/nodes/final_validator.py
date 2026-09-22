"""节点 5：``final_validator`` —— 校验答案是否含引用与必要字段。

职责（PROJECT_SPEC §3.1）：校验答案的引用与结构，
产出 ``final_result`` 与 ``validation_errors``。

**这是 run 终态的判定点**。判定规则（与 EVALUATION §3 M2 的
``task_completed`` 条件对齐，保证"运行期判定"与"评测期判定"口径一致）：

1. 答案非空；
2. 至少含 1 处 ``[doc-xxx]`` 引用；
3. 引用的文档 **真实存在**（反幻觉检查）；
4. 含"证据"小节。

第 3 条是本项目最重要的校验：它可确定性验证，
且是唯一能自动兜住"编造引用"的机制。

本节点**不调用 LLM**：校验必须是确定性的，
否则"是否通过校验"会随模型波动而变化。
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.state import AgentState
from app.core.logging import get_logger

logger = get_logger(__name__)

_CITATION_PATTERN = re.compile(r"\[(doc-\d{3})\]")

# 答案中应出现的"证据/依据"小节关键词
_EVIDENCE_SECTION_KEYWORDS = ("证据", "依据")


def validate_final_answer(
    state: AgentState,
    *,
    known_document_ids: list[str] | None = None,
    recorder: Any | None = None,
) -> dict[str, Any]:
    """校验答案并产出最终结果。

    Args:
        state: 当前状态，读取 ``answer`` / ``citations`` / ``degraded`` /
            ``evidence_coverage``。
        known_document_ids: 语料库中真实存在的文档 ID，用于反幻觉校验。
            ``None`` 表示跳过该检查（并在 ``validation_errors`` 中说明，
            而不是静默通过）。
        recorder: 保留参数以保持签名一致。

    Returns:
        状态更新：``final_result`` / ``validation_errors`` / ``visited_nodes``。
    """
    run_id = state.get("run_id", "")
    answer = str(state.get("answer") or "")
    citations = list(state.get("citations") or [])
    degraded = bool(state.get("degraded", False))
    coverage = float(state.get("evidence_coverage", 0.0) or 0.0)

    errors: list[str] = []

    # ---------------------------------------------------------- 1. 非空
    if not answer.strip():
        errors.append("answer_empty：答案为空")

    # ---------------------------------------------------------- 2. 有引用
    found_in_answer = _CITATION_PATTERN.findall(answer)
    if not found_in_answer:
        errors.append("missing_citation：答案不含 [doc-xxx] 形式的引用")

    # 一致性：citations 字段与答案正文里的引用应当一致。
    # 不一致说明某个环节计算错了，属于实现缺陷，必须暴露。
    if found_in_answer and set(found_in_answer) != set(citations):
        errors.append(
            "citation_mismatch：citations 字段与答案正文不一致 "
            f"(answers={sorted(set(found_in_answer))}, state={sorted(set(citations))})"
        )

    # ---------------------------------------------------------- 3. 反幻觉
    if known_document_ids is None:
        errors.append("hallucination_check_skipped：未提供已知文档列表，已跳过反幻觉校验")
    else:
        known = set(known_document_ids)
        hallucinated = sorted({item for item in found_in_answer if item not in known})
        if hallucinated:
            errors.append(f"hallucinated_doc：引用了不存在的文档 {hallucinated}")

    # ---------------------------------------------------------- 4. 证据小节
    if not any(keyword in answer for keyword in _EVIDENCE_SECTION_KEYWORDS):
        errors.append("missing_evidence_section：答案不含「证据」或「依据」小节")

    # ---------------------------------------------------------- 终态判定
    # degraded 优先级高于 failed：
    # 降级是"跑完了但依据不足"，与"执行失败"是两种不同的结果。
    # 若已标记 degraded，即使有校验错误也保持 degraded 语义（不升级为 failed）。
    final_status = _resolve_status(degraded=degraded, has_errors=bool(errors))

    final_result = {
        "status": final_status,
        "answer_chars": len(answer),
        "citation_count": len(found_in_answer),
        "citations": list(dict.fromkeys(found_in_answer)),
        "evidence_coverage": coverage,
        "degraded": degraded,
        "validation_errors": errors,
        "has_citation": bool(found_in_answer),
        "evidence_sufficient": bool(state.get("evidence_sufficient", False)),
    }

    logger.info(
        "final_validated",
        extra={
            "run_id": run_id,
            "final_status": final_status,
            "error_count": len(errors),
            "citation_count": len(found_in_answer),
            "degraded": degraded,
        },
    )

    return {
        "final_result": final_result,
        "validation_errors": errors,
        "visited_nodes": ["final_validator"],
    }


def _resolve_status(*, degraded: bool, has_errors: bool) -> str:
    """决定最终状态。

    规则（与 EVALUATION §3 M1/M10 的口径一致）：

    - 有非致命校验错误但**未**降级 → ``failed``
      （例如答案为空：这不是"依据不足"，是实现/模型输出缺陷）
    - 已标记 degraded → ``degraded``
      （跑完了但依据不足；**不视为成功**）
    - 无错误且未降级 → ``succeeded``
    """
    if degraded:
        return "degraded"
    if has_errors:
        return "failed"
    return "succeeded"


__all__ = ["validate_final_answer"]
