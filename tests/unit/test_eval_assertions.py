"""评测断言单测（EVALUATION §4）。

断言是评测里唯一**确定性**的部分 —— 指标算错了可以重算，
断言判错了会让整批结论反向。这里的重点不在"每个分支都过一遍"，
而在**边界应该判成什么**：

- ``no_hallucinated_doc`` 在"没有引用"时该通过（空集是合法子集），
  但在"不知道有哪些文档"时必须**失败**（否则把没检查报成通过）；
- 未识别的断言名必须**失败**（跳过会让拼错的名字无声无息）；
- 重复引用同一篇文档只算一条（否则 ``min_citations`` 能用复制粘贴刷过）。
"""

from __future__ import annotations

import pytest

from app.evaluation.assertions import (
    AssertionResult,
    CaseFacts,
    evaluate_assertion,
    evaluate_assertions,
    extract_citations,
)

KNOWN_DOCS = frozenset({"doc-001", "doc-002", "doc-003"})


def _facts(**overrides: object) -> CaseFacts:
    """构造事实包，默认是一份"有引用、有证据、终态成功"的正常结果。"""
    base: dict[str, object] = {
        "answer": "见文档 [doc-001] 的说明。\n\n## 证据\n[doc-001]",
        "answer_chars": 30,
        "citations": ["doc-001"],
        "run_status": "succeeded",
        "known_document_ids": KNOWN_DOCS,
        "required_citations": 0,
    }
    base.update(overrides)
    return CaseFacts(**base)  # type: ignore[arg-type]


class TestExtractCitations:
    def test_dedupes_and_keeps_first_appearance_order(self) -> None:
        """重复引用同一篇文档只算一条，且顺序按首次出现。

        去重的理由不是"好看"：``min_citations>=3`` 如果允许重复计数，
        答案里把 ``[doc-001]`` 写三遍就能刷过 —— 而那提供不了三份证据。
        """
        answer = "[doc-003] 先说这个，然后 [doc-001]，再 [doc-003]。"
        assert extract_citations(answer) == ["doc-003", "doc-001"]

    def test_ignores_non_three_digit_placeholders(self) -> None:
        """只认三位数字，避免把文档里的 ``[doc-xxx]`` 占位符当引用。"""
        assert extract_citations("见 [doc-xxx] 与 [doc-1] 与 [doc-0012]") == []

    def test_returns_empty_for_no_citations(self) -> None:
        assert extract_citations("没有任何引用。") == []


class TestFromState:
    def test_falls_back_to_regex_when_state_citations_absent(self) -> None:
        """``state["citations"]`` 缺失时从正文正则提取。"""
        facts = CaseFacts.from_state(
            {"answer": "见 [doc-002]。", "citations": None},
            run_status="succeeded",
            known_document_ids=KNOWN_DOCS,
        )
        assert facts.citations == ["doc-002"]

    def test_state_citations_take_precedence_over_regex(self) -> None:
        """两条路径结果不一致时以 state 为准 —— 正文提取只是兜底。"""
        facts = CaseFacts.from_state(
            {"answer": "正文里写着 [doc-001]。", "citations": ["doc-003"]},
            run_status="succeeded",
        )
        assert facts.citations == ["doc-003"]

    def test_answer_chars_falls_back_to_len(self) -> None:
        facts = CaseFacts.from_state({"answer": "abc"}, run_status="succeeded")
        assert facts.answer_chars == 3


class TestContainsCitation:
    def test_passes_with_citation(self) -> None:
        assert evaluate_assertion("contains_citation", _facts()).passed is True

    def test_fails_without_citation(self) -> None:
        result = evaluate_assertion("contains_citation", _facts(citations=[]))
        assert result.passed is False
        assert "answer_chars" in (result.detail or ""), "失败详情应给出定位线索"


class TestHasEvidenceSection:
    @pytest.mark.parametrize("word", ["证据", "依据"])
    def test_either_keyword_counts(self, word: str) -> None:
        result = evaluate_assertion("has_evidence_section", _facts(answer=f"## {word}\n内容"))
        assert result.passed is True

    def test_fails_without_keyword(self) -> None:
        result = evaluate_assertion("has_evidence_section", _facts(answer="只有结论，没有小节。"))
        assert result.passed is False


class TestNoHallucinatedDoc:
    def test_passes_when_all_citations_known(self) -> None:
        assert evaluate_assertion("no_hallucinated_doc", _facts()).passed is True

    def test_passes_when_there_are_no_citations(self) -> None:
        """空集是合法子集。

        这条断言要回答的是"有没有**编造**"，不是"有没有引用" ——
        "有没有引用"归 ``contains_citation`` 管。
        两件事混在一处，失败原因就没法准确归因。
        """
        assert evaluate_assertion("no_hallucinated_doc", _facts(citations=[])).passed is True

    def test_fails_on_unknown_doc_id(self) -> None:
        result = evaluate_assertion("no_hallucinated_doc", _facts(citations=["doc-001", "doc-999"]))
        assert result.passed is False
        assert "doc-999" in (result.detail or "")

    def test_fails_when_known_documents_are_empty(self) -> None:
        """缺少基准时判失败，不判通过。

        这是本文件里最容易被"优化"掉的一条边界：直觉上"没有已知文档"
        应该放行。但此时无法区分"没编造"与"没检查" ——
        后者会把一次根本没验的评测报成通过，比误报失败危险得多。
        """
        result = evaluate_assertion("no_hallucinated_doc", _facts(known_document_ids=frozenset()))
        assert result.passed is False
        assert "无法校验" in (result.detail or "")


class TestStatusAssertions:
    def test_status_is_succeeded(self) -> None:
        assert evaluate_assertion("status_is_succeeded", _facts()).passed is True
        failed = evaluate_assertion("status_is_succeeded", _facts(run_status="failed"))
        assert failed.passed is False
        assert "failed" in (failed.detail or "")

    def test_status_is_failed(self) -> None:
        assert evaluate_assertion("status_is_failed", _facts(run_status="failed")).passed is True
        assert evaluate_assertion("status_is_failed", _facts()).passed is False


class TestMinCitations:
    def test_boundary_equal_to_threshold_passes(self) -> None:
        """``>=`` 的边界：恰好等于要求也算通过。"""
        facts = _facts(citations=["doc-001", "doc-002"])
        assert evaluate_assertion("min_citations>=2", facts).passed is True

    def test_below_threshold_fails(self) -> None:
        facts = _facts(citations=["doc-001"])
        result = evaluate_assertion("min_citations>=2", facts)
        assert result.passed is False
        assert "1" in (result.detail or "") and "2" in (result.detail or "")

    def test_tolerates_whitespace_around_operator(self) -> None:
        assert evaluate_assertion("min_citations  >=  1", _facts()).passed is True


class TestMentions:
    def test_case_insensitive(self) -> None:
        facts = _facts(answer="关于 LangGraph 的说明。")
        assert evaluate_assertion("mentions_langgraph", facts).passed is True

    def test_missing_keyword_fails(self) -> None:
        result = evaluate_assertion("mentions_rag", _facts(answer="没有这个词。"))
        assert result.passed is False

    def test_empty_keyword_fails(self) -> None:
        """``mentions_`` 后面不写关键词，等于"命中任意字符即通过" ——
        那是一个永远为真的断言，必须判失败。
        """
        result = evaluate_assertion("mentions_", _facts())
        assert result.passed is False
        assert "未指定关键词" in (result.detail or "")


class TestUnknownAssertion:
    def test_unknown_name_fails_rather_than_skips(self) -> None:
        """拼错的断言名必须失败。

        跳过会让 ``contains_citaton``（少一个 i）这样的笔误
        变成"看着验了、实际没验"，而失败的断言会立刻暴露问题。
        """
        result = evaluate_assertion("contains_citaton", _facts())
        assert result.passed is False
        assert "未识别" in (result.detail or "")

    def test_name_is_stripped(self) -> None:
        assert evaluate_assertion("  contains_citation  ", _facts()).passed is True


class TestEvaluateAssertions:
    def test_returns_one_result_per_name_in_order(self) -> None:
        names = ["contains_citation", "has_evidence_section", "no_hallucinated_doc"]
        results = evaluate_assertions(names, _facts())
        assert [item.assertion for item in results] == names

    def test_empty_required_is_empty_result(self) -> None:
        assert evaluate_assertions([], _facts()) == []

    def test_mixed_pass_and_fail_are_both_reported(self) -> None:
        results = evaluate_assertions(["contains_citation", "mentions_不存在"], _facts())
        assert [item.passed for item in results] == [True, False]


class TestAssertionResultToDict:
    def test_detail_omitted_on_pass(self) -> None:
        """通过的断言不带 detail：只有失败项才需要解释。"""
        assert AssertionResult("x", True).to_dict() == {"assertion": "x", "passed": True}

    def test_detail_included_on_failure(self) -> None:
        payload = AssertionResult("x", False, "原因").to_dict()
        assert payload["detail"] == "原因"

    def test_failure_without_detail_stays_two_keys(self) -> None:
        assert AssertionResult("x", False).to_dict() == {"assertion": "x", "passed": False}
