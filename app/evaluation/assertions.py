"""评测断言实现（契约 EVALUATION §4）。

七个断言，每个都是**纯函数**：输入是"一次 run 的结果快照"，
输出是"这条断言是否通过 + 为什么"。不碰数据库、不调模型 ——
断言必须是确定性代码，否则评测结论本身就不稳定。

**为什么 ``no_hallucinated_doc`` 是最重要的断言**

其余六个断言都在检查"答案说了什么"，只有它检查"答案有没有编造引用源"。
答案写得再漂亮，只要引用了不存在的 ``doc-xxx``，就是凭空捏造证据 ——
而这一点可以**确定性验证**，不需要人工判断也不需要模型打分。
这是本项目里"可验证"的基准线：能机器判的，绝不用人判。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ``[doc-001]`` 形式的引用。``document_search.py`` 的解析与这里保持一致：
# 只认三位数字，避免把 ``[doc-xxx]`` 这种占位符也当成引用。
_CITATION_RE = re.compile(r"\[(doc-\d{3})\]")

# ``has_evidence_section`` 的关键节标题词。答案里出现任一个即算有。
_EVIDENCE_SECTION_WORDS = ("证据", "依据")

# 断言名里 "mentions_<keyword>" 的前缀。
_MENTIONS_PREFIX = "mentions_"

# "min_citations>=N" 形式的断言正则。
_MIN_CITATIONS_RE = re.compile(r"^min_citations\s*>=\s*(\d+)$")


@dataclass(slots=True)
class AssertionResult:
    """单条断言的判定结果。"""

    assertion: str
    passed: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转成可写入 ``eval_run.assertion_results`` 的字典。

        ``detail`` 仅在失败时输出 —— 通过的断言不需要解释，
        带上 detail 只会让 JSON 变长且稀释失败项的注意力。
        """
        payload: dict[str, Any] = {"assertion": self.assertion, "passed": self.passed}
        if not self.passed and self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(slots=True)
class CaseFacts:
    """断言所需的、关于一次 run 的全部事实。

    刻意做成一个**扁平的事实包**而不是传 ``state`` 字典：
    断言只看这些字段，把它们显式列出来，"断言依赖什么"就是可审计的。
    传原始 state 会让断言能悄悄依赖任何新字段，评测口径随之漂移。
    """

    answer: str = ""
    answer_chars: int = 0
    citations: list[str] = field(default_factory=list)
    run_status: str = ""
    known_document_ids: frozenset[str] = field(default_factory=frozenset)
    required_citations: int = 0

    @classmethod
    def from_state(
        cls,
        state: dict[str, Any],
        *,
        run_status: str,
        known_document_ids: Any = (),
        required_citations: int = 0,
    ) -> CaseFacts:
        """从图状态构造事实包。

        引用来源以 ``state["citations"]`` 为准；缺失时退化为从答案正文
        正则提取 —— 两者的偏差本身由 ``final_validator`` 检查，
        这里只为"至少能拿到引用"提供一条兜底路径。
        """
        answer = str(state.get("answer") or "")
        citations = [str(item) for item in (state.get("citations") or [])]
        if not citations:
            citations = extract_citations(answer)

        return cls(
            answer=answer,
            answer_chars=int(state.get("answer_chars") or len(answer)),
            citations=citations,
            run_status=run_status,
            known_document_ids=frozenset(str(doc) for doc in known_document_ids),
            required_citations=required_citations,
        )


def extract_citations(answer: str) -> list[str]:
    """从答案正文提取 ``[doc-xxx]`` 引用，去重且保持首次出现顺序。

    去重是刻意的（与 ``answer_writer._extract_citations`` 同源）：
    重复引用同一篇文档不应被算成多条证据，否则
    ``min_citations>=N`` 可以用复制粘贴刷过。
    """
    return list(dict.fromkeys(_CITATION_RE.findall(answer)))


def evaluate_assertions(required: list[str], facts: CaseFacts) -> list[AssertionResult]:
    """按 ``required_assertions`` 逐条判定。

    未识别的断言名判 **失败** 而不是跳过：跳过会让一个拼错的
    断言名变得无声无息，使用者以为验了、实际没验。
    """
    return [evaluate_assertion(name, facts) for name in required]


def evaluate_assertion(name: str, facts: CaseFacts) -> AssertionResult:  # noqa: C901
    """判定单条断言。"""
    normalized = name.strip()

    if normalized == "contains_citation":
        if facts.citations:
            return AssertionResult(normalized, True)
        return AssertionResult(
            normalized,
            False,
            f"答案中未找到 [doc-xxx] 形式的引用（answer_chars={facts.answer_chars}）",
        )

    if normalized == "has_evidence_section":
        # 契约的措辞是"含证据小节"。这里按**关键词出现**判定而不是
        # 严格解析 Markdown 标题：假答案的形态不受我们控制，
        # 要求它是规范的 ``## 证据`` 会让断言变成"格式检查"而非"内容检查"。
        for word in _EVIDENCE_SECTION_WORDS:
            if word in facts.answer:
                return AssertionResult(normalized, True, None)
        return AssertionResult(
            normalized,
            False,
            f"答案不含'证据'或'依据'小节（已检查关键词：{list(_EVIDENCE_SECTION_WORDS)}）",
        )

    if normalized == "no_hallucinated_doc":
        return _check_no_hallucinated_doc(normalized, facts)

    if normalized == "status_is_succeeded":
        if facts.run_status == "succeeded":
            return AssertionResult(normalized, True)
        return AssertionResult(
            normalized, False, f"run 终态为 {facts.run_status!r}，期望 'succeeded'"
        )

    if normalized == "status_is_failed":
        if facts.run_status == "failed":
            return AssertionResult(normalized, True)
        return AssertionResult(normalized, False, f"run 终态为 {facts.run_status!r}，期望 'failed'")

    match = _MIN_CITATIONS_RE.match(normalized)
    if match:
        threshold = int(match.group(1))
        actual = len(facts.citations)
        if actual >= threshold:
            return AssertionResult(normalized, True)
        return AssertionResult(normalized, False, f"引用数 {actual} 少于要求 {threshold}")

    if normalized.startswith(_MENTIONS_PREFIX):
        keyword = normalized[len(_MENTIONS_PREFIX) :]
        return _check_mentions(normalized, keyword, facts)

    return AssertionResult(
        normalized,
        False,
        f"未识别的断言名 {normalized!r}。支持的断言见 EVALUATION §4。",
    )


def _check_no_hallucinated_doc(name: str, facts: CaseFacts) -> AssertionResult:
    """反幻觉：引用的每个 ``doc-xxx`` 都必须真实存在。

    边界：答案里**一个引用都没有**时判通过（空集是合法子集）。
    理由是这个断言要回答的是"有没有编造"，不是"有没有引用" ——
    "有没有引用"由 ``contains_citation`` / ``required_citations`` 负责。
    两个断言职责分开，失败原因才能被准确归因。

    另一个边界：``known_document_ids`` 为空时判**失败**而不是通过。
    集合为空意味着"我不知道有哪些文档"，此时无法区分
    "没编造" 与 "没检查" —— 后者更危险，因为它会把一次
    根本没验的评测报成通过。
    """
    if not facts.known_document_ids:
        return AssertionResult(
            name,
            False,
            "无法校验：已知文档 ID 集合为空（文档目录未加载）。"
            "缺少基准时不能判通过，否则会把'未检查'报成'通过'。",
        )

    if not facts.citations:
        return AssertionResult(name, True)

    hallucinated = sorted(set(facts.citations) - facts.known_document_ids)
    if not hallucinated:
        return AssertionResult(name, True)

    return AssertionResult(
        name,
        False,
        f"引用了不存在的文档 ID：{hallucinated}；已知文档：{sorted(facts.known_document_ids)}",
    )


def _check_mentions(name: str, keyword: str, facts: CaseFacts) -> AssertionResult:
    """``mentions_<keyword>``：答案（大小写不敏感）含指定关键词。

    ``keyword`` 为空时判失败：``mentions_`` 后面什么都不写
    等于"随便命中一个字符就通过"，这是一个永远为真的断言。
    """
    if not keyword:
        return AssertionResult(name, False, "mentions_ 后未指定关键词。")

    if keyword.lower() in facts.answer.lower():
        return AssertionResult(name, True)

    return AssertionResult(name, False, f"答案未提及关键词 {keyword!r}")


__all__ = [
    "AssertionResult",
    "CaseFacts",
    "evaluate_assertion",
    "evaluate_assertions",
    "extract_citations",
]
