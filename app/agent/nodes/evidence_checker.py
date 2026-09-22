"""节点 3：``evidence_checker`` —— 判断证据是否充分。

职责（PROJECT_SPEC §3.1）：根据 ``search_results`` 判定证据是否足以支撑答案，
输出 ``evidence_sufficient`` 与 ``evidence_coverage``。

**这是流程图的分叉点**：证据不足时沿条件边回到 ``document_search``
放宽检索；重试耗尽后仍不足，则继续到 ``answer_writer`` 但标记降级。

证据覆盖率的定义（可被单测逐项核对）::

    coverage = 命中文档数 / 期望命中文档数

期望命中数取 ``min(top_k, 存在相关文档数)``：
用 top_k 当分母会让"问题本身只能命中 1 篇文档"的情况永远不达标 ——
那不是证据不足，是问题太窄。

本节点**不调用 LLM**：证据充分性是确定性判定，
让模型"感觉一下够不够"会让同一输入产生不同结论，评测就不可复现了。
"""

from __future__ import annotations

from typing import Any

from app.agent.state import AgentState
from app.core.logging import get_logger

logger = get_logger(__name__)

# 覆盖率计算的细节：单篇文档的分数达到该比例的最高分即视为"强命中"。
# 这样"一篇高度相关" 与 "三篇勉强相关" 不会得到同样高的覆盖率。
_STRONG_HIT_RATIO = 0.5


def check_evidence(
    state: AgentState,
    *,
    recorder: Any | None = None,
) -> dict[str, Any]:
    """判定证据是否充分。

    Args:
        state: 当前状态，读取 ``search_results`` / ``evidence_coverage_threshold``。
        recorder: 保留参数以保持节点签名一致（本节点不产生子调用事件）。

    Returns:
        状态更新：``evidence_sufficient`` / ``evidence_coverage`` /
        ``evidence_refs`` / ``visited_nodes``。
    """
    run_id = state.get("run_id", "")
    hits = list(state.get("search_results") or [])
    threshold = float(state.get("evidence_coverage_threshold", 0.5))
    expected = max(1, int(state.get("top_k", 3)))

    coverage = _compute_coverage(hits, expected)
    sufficient = coverage >= threshold
    refs = [hit.document_id for hit in hits]

    logger.info(
        "evidence_checked",
        extra={
            "run_id": run_id,
            "hit_count": len(hits),
            "coverage": round(coverage, 4),
            "threshold": threshold,
            "sufficient": sufficient,
            "attempt": state.get("search_attempt", 0),
        },
    )

    return {
        "evidence_sufficient": sufficient,
        "evidence_coverage": coverage,
        "evidence_refs": refs,
        "visited_nodes": ["evidence_checker"],
    }


def _compute_coverage(hits: list[Any], expected: int) -> float:
    """计算证据覆盖率（0~1）。

    公式分两部分：

    1. **命中广度**：``min(命中数, expected) / expected``
       —— 命中越多越好，但超过期望数不再加分；
    2. **命中强度**：最高分与次高分的比值，用于区分"一篇强命中"
       与"三篇弱命中"。这部分只在命中数 ≥ 2 时起作用。

    两者取加权平均（广度 0.7 / 强度 0.3）。
    权重是可调的工程判断，但**计算过程是确定性的** ——
    这才是可被评测的关键。

    Args:
        hits: 检索命中列表（已按分数降序）。
        expected: 期望命中数，至少为 1。

    Returns:
        0.0 ~ 1.0 之间的覆盖率。
    """
    if not hits:
        return 0.0

    breadth = min(len(hits), expected) / expected

    scores = [float(getattr(hit, "score", 0.0) or 0.0) for hit in hits]
    top = scores[0] if scores else 0.0

    if len(scores) >= 2 and top > 0:
        # 强度：次高分相对最高分的比例。两者接近说明有多篇同等相关的证据，
        # 比"一篇高、其余很低"更稳健。
        second = scores[1]
        strength = min(1.0, second / top) if top > 0 else 0.0
        # 只有次高分达到最高分的 _STRONG_HIT_RATIO 才认为强度有效，
        # 否则说明只有一篇文档真正相关。
        if second < top * _STRONG_HIT_RATIO:
            strength *= 0.5
    else:
        # 只有一篇命中：广度已经反映了这一事实，强度不额外区分
        strength = breadth

    coverage = 0.7 * breadth + 0.3 * strength
    return round(max(0.0, min(1.0, coverage)), 4)


__all__ = ["check_evidence"]
