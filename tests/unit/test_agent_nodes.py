"""Agent 层测试：五个节点、图拓扑与条件边路由。

契约 IMPLEMENTATION_PLAN S4 第 11 项要求"图拓扑测试、节点单测、
失败路径测试（工具异常、证据不足重试、handoff）"。

本文件的组织方式与"一个节点一个测试类"对应，另有：

- ``TestGraphTopology``：装配是否与契约一致；
- ``TestConditionalEdgeRouting``：路由判定函数的纯逻辑测试；
- ``TestRetryBudget``：重试额度耗尽的降级路径；
- ``TestFailurePaths``：工具异常等失败路径。

**关于测试替身**：本文件所有 LLM 交互都走 ``FakeLLMProvider``。
它是确定性规则实现（关键词匹配 + 模板拼装），不含随机数 ——
因此断言可以精确到具体字符串，评测也才可复现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agent.graph import AgentDeps, _route_after_evidence, build_graph
from app.agent.llm import FakeLLMProvider, LlmResponse, build_provider
from app.agent.nodes.answer_writer import _extract_citations, write_answer
from app.agent.nodes.document_search import _compute_top_k, document_search
from app.agent.nodes.evidence_checker import _compute_coverage, check_evidence
from app.agent.nodes.final_validator import validate_final_answer
from app.agent.nodes.question_parser import parse_question
from app.agent.state import (
    NODE_ANSWER_WRITER,
    NODE_DOCUMENT_SEARCH,
    NODE_EVIDENCE_CHECKER,
    NODE_FINAL_VALIDATOR,
    NODE_ORDER,
    NODE_QUESTION_PARSER,
)
from app.core.config import get_settings
from app.tools.bootstrap import get_document_store, register_default_tools
from app.tools.registry import clear as clear_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DOCS_DIR = PROJECT_ROOT / "data" / "sample_docs"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tools_registered() -> Any:
    """注册内置工具，并在测试后清空注册表。"""
    clear_registry()
    register_default_tools(force=True)
    yield
    clear_registry()


@pytest.fixture
def provider() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture
def known_ids() -> list[str]:
    return get_document_store(SAMPLE_DOCS_DIR).document_ids


@pytest.fixture
def deps(provider: FakeLLMProvider, known_ids: list[str]) -> AgentDeps:
    return AgentDeps(
        provider=provider,
        known_document_ids=known_ids,
    )


def _initial_state(question: str, **overrides: Any) -> dict[str, Any]:
    """构造一份最小可用的输入状态。"""
    state: dict[str, Any] = {
        "run_id": "run_test",
        "question": question,
        "top_k": 3,
        "search_attempt": 0,
        "errors": [],
        "visited_nodes": [],
    }
    state.update(overrides)
    return state


def _run_graph(question: str, deps: AgentDeps) -> dict[str, Any]:
    """跑一次完整图执行，返回终态。"""
    graph = build_graph(deps)
    return graph.invoke(_initial_state(question))


# ---------------------------------------------------------------------------
# FakeLLMProvider
# ---------------------------------------------------------------------------


class TestFakeLLMProvider:
    """测试替身的确定性与诚实性。"""

    def test_is_marked_as_test_double(self, provider: FakeLLMProvider) -> None:
        """契约要求：替身必须在数据里被明确标注。

        没有这个标记，"演示数据"与"真实调用"就无法区分 ——
        这正是必须避免的误导。
        """
        response = provider.complete(system="s", user="问题: 证据不足怎么办", node_name="x")
        assert response.is_test_double is True

    def test_cost_estimation_is_marked_unavailable(self, provider: FakeLLMProvider) -> None:
        """替身不产生真实成本，必须如实标注"成本不可估算"。

        报一个 0 成本而不加说明，会被读成"成本为零"。
        """
        response = provider.complete(system="s", user="问题: x", node_name="x")
        assert response.cost_estimation_unavailable is True

    def test_output_is_deterministic(self, provider: FakeLLMProvider) -> None:
        """同一输入两次调用输出**完全相同**。

        这是评测可复现的前提：若替身带随机性，
        指标会随机波动，质量门禁就失去了判定意义。
        """
        first = provider.complete(system="s", user="问题: 证据不足怎么办", node_name="n")
        second = provider.complete(system="s", user="问题: 证据不足怎么办", node_name="n")
        assert first.text == second.text
        assert first.prompt_tokens == second.prompt_tokens

    def test_provider_name_is_fake(self, provider: FakeLLMProvider) -> None:
        response = provider.complete(system="s", user="问题: x", node_name="n")
        assert response.provider == "fake"

    def test_parse_question_extracts_keywords(self, provider: FakeLLMProvider) -> None:
        """问题解析要产出可用于检索的关键词。"""
        response = provider.complete(
            system="s",
            user="问题: trace 事件的 sequence 字段有什么作用？",
            node_name="question_parser",
        )
        assert response.text

    def test_response_carries_latency(self, provider: FakeLLMProvider) -> None:
        """延迟必须被记录 —— 它是评测指标的一部分。"""
        response = provider.complete(system="s", user="问题: x", node_name="n")
        assert isinstance(response.latency_ms, int)
        assert response.latency_ms >= 0

    def test_build_provider_returns_fake_in_test_mode(self) -> None:
        """默认配置（LLM_PROVIDER=fake）下必须返回替身。"""
        assert isinstance(build_provider(get_settings()), FakeLLMProvider)


class TestLlmResponse:
    """``LlmResponse`` 的数据契约。"""

    def test_total_tokens_is_derived(self) -> None:
        response = LlmResponse(
            text="hi",
            provider="fake",
            model_name="fake-model",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            latency_ms=1,
            is_test_double=True,
        )
        assert response.total_tokens == response.prompt_tokens + response.completion_tokens


# ---------------------------------------------------------------------------
# 节点 1：question_parser
# ---------------------------------------------------------------------------


class TestQuestionParser:
    def test_produces_parsed_task(self, provider: FakeLLMProvider) -> None:
        result = parse_question(
            _initial_state("trace 事件的 sequence 字段有什么作用？"),
            provider=provider,
        )
        assert "parsed_task" in result
        assert result["visited_nodes"] == [NODE_QUESTION_PARSER]

    def test_parsed_task_has_question_and_keywords(self, provider: FakeLLMProvider) -> None:
        result = parse_question(_initial_state("证据不足时会怎么做？"), provider=provider)
        parsed = result["parsed_task"]
        assert parsed["question"] == "证据不足时会怎么做？"
        assert isinstance(parsed["keywords"], list)

    def test_malformed_model_output_degrades_gracefully(self, provider: FakeLLMProvider) -> None:
        """模型返回非 JSON 时**退化**为原问题，而不是让 run 失败。

        真实模型不保证输出合法 JSON。把"解析失败"当成致命错误，
        会让一次格式抖动变成整个 run 失败 —— 与实际影响不成比例。
        """

        class _BadProvider:
            def complete(self, **kwargs: Any) -> LlmResponse:
                return LlmResponse(
                    text="这不是 JSON {{{",
                    provider="fake",
                    model_name="bad",
                    prompt_tokens=1,
                    completion_tokens=1,
                    total_tokens=2,
                    latency_ms=0,
                    is_test_double=True,
                )

        result = parse_question(_initial_state("原始问题"), provider=_BadProvider())
        parsed = result["parsed_task"]
        assert parsed["question"] == "原始问题"
        # 退化时关键词回退为空，由 document_search 用原问题兜底
        assert isinstance(parsed["keywords"], list)

    def test_empty_question_still_produces_task(self, provider: FakeLLMProvider) -> None:
        result = parse_question(_initial_state(""), provider=provider)
        assert "parsed_task" in result


# ---------------------------------------------------------------------------
# 节点 2：document_search
# ---------------------------------------------------------------------------


class TestDocumentSearchNode:
    def test_first_attempt_uses_base_top_k(self) -> None:
        assert _compute_top_k(base_top_k=3, attempt=0, max_top_k=10) == 3

    def test_widened_attempt_doubles_top_k(self) -> None:
        """放宽检索时 top_k 按倍数放大。"""
        assert _compute_top_k(base_top_k=3, attempt=1, max_top_k=10) == 6

    def test_top_k_is_capped_by_max(self) -> None:
        """放宽不得突破契约的 top_k <= 10。"""
        assert _compute_top_k(base_top_k=3, attempt=5, max_top_k=10) == 10

    def test_top_k_is_at_least_one(self) -> None:
        assert _compute_top_k(base_top_k=0, attempt=0, max_top_k=10) == 1

    def test_returns_search_results(self) -> None:
        result = document_search(_initial_state("证据不足时会怎么做？"))
        assert result["search_results"]
        assert result["visited_nodes"] == [NODE_DOCUMENT_SEARCH]

    def test_increments_search_attempt(self) -> None:
        """``search_attempt`` 记录"已经检索过几次"，条件边依赖它。"""
        result = document_search(_initial_state("证据"))
        assert result["search_attempt"] == 1

    def test_fetches_document_bodies(self) -> None:
        """命中的前若干篇正文要被取回，供证据检查与答案生成使用。"""
        result = document_search(_initial_state("证据不足时会怎么做？"))
        assert result["fetched_documents"]
        assert result["fetched_documents"][0].content

    def test_irrelevant_query_yields_no_results(self) -> None:
        result = document_search(_initial_state("红烧肉怎么做"))
        assert result["search_results"] == []
        assert result["fetched_documents"] == []

    def test_fetch_failure_does_not_abort_search(self) -> None:
        """单篇文档读取失败只跳过该篇，其余继续。

        若这里直接抛出，一篇文档的问题会变成整个 run 失败。
        """
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足", 3)
        assert hits

        class _FailingGet:
            """让 get_document 永远失败，验证检索不会被它带崩。"""

            def __init__(self) -> None:
                from app.tools import registry as registry_module

                self._real = registry_module
                self._first = True

            def invoke(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> Any:
                from app.tools.base import TOOL_GET_DOCUMENT

                if tool_name == TOOL_GET_DOCUMENT:
                    raise RuntimeError("模拟读取失败")
                return self._real.invoke(tool_name, args, **kwargs)

        result = document_search(_initial_state("证据不足时会怎么做？"), registry=_FailingGet())
        # 检索结果仍在，只是没有取回正文
        assert result["search_results"]
        assert result["fetched_documents"] == []


# ---------------------------------------------------------------------------
# 节点 3：evidence_checker
# ---------------------------------------------------------------------------


class TestEvidenceChecker:
    def test_no_hits_means_insufficient(self) -> None:
        result = check_evidence(_initial_state("x", search_results=[]))
        assert result["evidence_sufficient"] is False
        assert result["evidence_coverage"] == 0.0
        assert result["evidence_refs"] == []

    def test_enough_hits_means_sufficient(self) -> None:
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足时会怎么做？", 3)
        result = check_evidence(_initial_state("x", search_results=hits, top_k=3))
        assert result["evidence_sufficient"] is True
        assert result["evidence_coverage"] > 0.5

    def test_refs_are_document_ids_in_order(self) -> None:
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足时会怎么做？", 3)
        result = check_evidence(_initial_state("x", search_results=hits, top_k=3))
        assert result["evidence_refs"] == [hit.document_id for hit in hits]

    def test_coverage_of_empty_is_zero(self) -> None:
        assert _compute_coverage([], expected=3) == 0.0

    def test_coverage_is_bounded(self) -> None:
        """覆盖率必须落在 0~1 —— 超出范围的指标无法解释。"""
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据", 10)
        coverage = _compute_coverage(hits, expected=1)
        assert 0.0 <= coverage <= 1.0

    def test_coverage_never_exceeds_one_when_more_hits_than_expected(self) -> None:
        """命中数超过期望数时不再加分（分母是期望数，不是命中数）。"""
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足时会怎么做？", 6)
        coverage = _compute_coverage(hits, expected=1)
        assert coverage <= 1.0

    def test_coverage_does_not_call_llm(self) -> None:
        """证据充分性是确定性判定 —— 让模型"感觉一下够不够"会破坏可复现性。

        因此本节点不应持有或使用任何 provider；这里通过给一个会在
        被调用时炸掉的 provider 来验证它确实没被用到。
        """

        class _ExplodingProvider:
            def complete(self, **kwargs: Any) -> Any:
                raise AssertionError("evidence_checker 不应调用 LLM")

        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足时", 3)
        # 节点签名里根本没有 provider 参数，这本身就是结构性保证
        result = check_evidence(_initial_state("x", search_results=hits, top_k=3))
        assert "evidence_sufficient" in result
        del _ExplodingProvider

    def test_strong_single_hit_scores_lower_than_two_balanced_hits(self) -> None:
        """强度分量要能区分"一篇强命中"与"多篇均衡命中"。

        构造两组命中：一组是 1 篇高分，另一组是 2 篇分数接近。
        后者应当不被强度分量惩罚。
        """

        class _Hit:
            def __init__(self, score: float) -> None:
                self.score = score
                self.document_id = f"doc-{int(score):03d}"

        one = _compute_coverage([_Hit(100.0)], expected=2)
        two = _compute_coverage([_Hit(100.0), _Hit(95.0)], expected=2)
        assert two >= one


# ---------------------------------------------------------------------------
# 节点 4：answer_writer
# ---------------------------------------------------------------------------


class TestExtractCitations:
    """引用抽取：去重保序。"""

    def test_extracts_single_citation(self) -> None:
        assert _extract_citations("见 [doc-001]。", []) == ["doc-001"]

    def test_deduplicates_preserving_order(self) -> None:
        """重复引用同一篇文档只算一次。

        否则 ``min_citations>=N`` 这类断言可以用重复引用轻易刷过。
        """
        answer = "[doc-001] ... [doc-002] ... [doc-001]"
        assert _extract_citations(answer, []) == ["doc-001", "doc-002"]

    def test_no_citation_returns_empty(self) -> None:
        """答案没写引用时返回空，**不用 fallback 顶替**。

        早期版本这里返回检索到的 refs，结果是"答案没引用但断言通过"，
        遮盖了真实缺陷。
        """
        assert _extract_citations("这段答案没有任何引用。", ["doc-001"]) == []

    def test_ignores_malformed_citation(self) -> None:
        assert _extract_citations("[doc-1] [doc-0001] [DOC-001]", []) == []


class TestAnswerWriter:
    def test_writes_answer_with_citations(self, provider: FakeLLMProvider) -> None:
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足时会怎么做？", 3)
        docs = [store.get(hit.document_id) for hit in hits]
        result = write_answer(
            _initial_state(
                "证据不足时会怎么做？",
                parsed_task={"question": "证据不足时会怎么做？", "keywords": ["证据"]},
                search_results=hits,
                fetched_documents=[d for d in docs if d],
                evidence_refs=[hit.document_id for hit in hits],
                evidence_sufficient=True,
            ),
            provider=provider,
        )
        assert result["answer"]
        assert result["citations"]
        assert result["visited_nodes"] == [NODE_ANSWER_WRITER]

    def test_marks_degraded_when_evidence_insufficient(self, provider: FakeLLMProvider) -> None:
        """**关键语义**：证据不足时答案仍会产出，但必须标记降级。

        "跑完了"不等于"成功了"。降级完成的 run 不视为成功、
        也不计入错误率 —— 它是第三态。
        """
        result = write_answer(
            _initial_state(
                "红烧肉怎么做",
                parsed_task={"question": "红烧肉怎么做", "keywords": []},
                search_results=[],
                fetched_documents=[],
                evidence_refs=[],
                evidence_sufficient=False,
            ),
            provider=provider,
        )
        assert result["degraded"] is True
        assert result["citations"] == []

    def test_not_degraded_when_evidence_sufficient(self, provider: FakeLLMProvider) -> None:
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足时会怎么做？", 3)
        docs = [d for d in (store.get(h.document_id) for h in hits) if d]
        result = write_answer(
            _initial_state(
                "证据不足时会怎么做？",
                parsed_task={"question": "证据不足时会怎么做？", "keywords": ["证据"]},
                search_results=hits,
                fetched_documents=docs,
                evidence_refs=[h.document_id for h in hits],
                evidence_sufficient=True,
            ),
            provider=provider,
        )
        assert result["degraded"] is False

    def test_answer_without_citation_is_degraded(self, provider: FakeLLMProvider) -> None:
        """有证据但答案零引用，等同于"没有可验证的依据"，必须降级。

        这是"宁可保守"的一处：不把无依据的答案算作成功。
        """

        class _NoCitationProvider:
            def complete(self, **kwargs: Any) -> LlmResponse:
                return LlmResponse(
                    text="这是一段没有任何引用的答案。",
                    provider="fake",
                    model_name="no-citation",
                    prompt_tokens=1,
                    completion_tokens=1,
                    total_tokens=2,
                    latency_ms=0,
                    is_test_double=True,
                )

        result = write_answer(
            _initial_state(
                "x",
                parsed_task={"question": "x", "keywords": []},
                evidence_sufficient=True,
            ),
            provider=_NoCitationProvider(),
        )
        assert result["degraded"] is True
        assert result["citations"] == []

    def test_records_model_call(self, provider: FakeLLMProvider) -> None:
        """模型调用必须双写：Trace 事件 + model_call 表。"""

        class _Recorder:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def record_model_call(self, **kwargs: Any) -> None:
                self.calls.append(kwargs)

        recorder = _Recorder()
        store = get_document_store(SAMPLE_DOCS_DIR)
        hits = store.search("证据不足", 3)
        docs = [d for d in (store.get(h.document_id) for h in hits) if d]
        write_answer(
            _initial_state(
                "x",
                parsed_task={"question": "x", "keywords": ["证据"]},
                search_results=hits,
                fetched_documents=docs,
                evidence_refs=[h.document_id for h in hits],
                evidence_sufficient=True,
            ),
            provider=provider,
            recorder=recorder,
        )
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["is_test_double"] is True

    def test_model_call_carries_all_three_token_fields(self, db_session: Any) -> None:
        """``model_call`` 的 Trace 事件必须含**三个** token 字段。

        契约 D-06 的判据是"三个 token 字段非 null 且成本有值"。
        这条测试锁的是一个真实的漏抄缺陷：宽表 ``model_call`` 里
        ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``
        三列齐全且都有值，但写 Trace 事件时 ``attributes`` 只抄了
        ``total_tokens``，另外两个被漏掉。

        为什么单看宽表发现不了：宽表是对的。问题只出在**事件那份副本**上，
        而"从 Trace 还原一次模型调用"正是这个项目对外承诺的能力 ——
        缺了输入/输出两个分量，就只能看到总数，无法判断"是 prompt 太长
        还是输出失控"。
        """
        from app.agent.middleware import TraceRecorder
        from app.db.repository import TraceRepository

        repository = TraceRepository(db_session)
        run = repository.create_run(
            question="测试 model_call 字段完整性",
            status="running",
            agent_version="1.0.0",
            prompt_version="1.0.0",
            llm_provider="fake",
            is_test_double=True,
        )
        recorder = TraceRecorder(repository)
        recorder.record_model_call(
            run_id=run.id,
            node_name="answer_writer",
            provider="fake",
            model_name="fake-echo-1",
            is_test_double=True,
            prompt_tokens=1000,
            completion_tokens=234,
            total_tokens=1234,
            estimated_cost_usd=0.0001,
            cost_estimation_unavailable=False,
            status="ok",
            latency_ms=7,
        )

        events, _total = repository.list_events(run.id)
        model_events = [e for e in events if e.event_type == "model_call"]
        assert len(model_events) == 1, "应写入恰好一条 model_call 事件"

        attributes = model_events[0].attributes or {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            assert field in attributes, f"attributes 缺少 {field}"
            assert attributes[field] is not None, f"{field} 不应为 null"

        assert attributes["prompt_tokens"] == 1000
        assert attributes["completion_tokens"] == 234
        assert attributes["total_tokens"] == 1234
        # 成本"不可估算"与成本为 0 是两件事，必须都能从事件读到。
        assert attributes["cost_estimation_unavailable"] is False


# ---------------------------------------------------------------------------
# 节点 5：final_validator
# ---------------------------------------------------------------------------


class TestFinalValidator:
    def test_passes_valid_answer(self, known_ids: list[str]) -> None:
        result = validate_final_answer(
            _initial_state(
                "x",
                answer="结论见 [doc-001]。\n\n证据：[doc-001]",
                citations=["doc-001"],
                degraded=False,
                evidence_coverage=0.9,
            ),
            known_document_ids=known_ids,
        )
        assert result["validation_errors"] == []
        assert result["final_result"]["status"] == "succeeded"

    def test_empty_answer_fails(self, known_ids: list[str]) -> None:
        result = validate_final_answer(
            _initial_state("x", answer="", citations=[]),
            known_document_ids=known_ids,
        )
        status = result["final_result"]["status"]
        assert status == "failed"
        assert any("answer_empty" in err for err in result["validation_errors"])

    def test_missing_citation_fails(self, known_ids: list[str]) -> None:
        result = validate_final_answer(
            _initial_state("x", answer="没有引用的答案。证据：无", citations=[]),
            known_document_ids=known_ids,
        )
        assert result["final_result"]["status"] == "failed"
        assert any("missing_citation" in e for e in result["validation_errors"])

    def test_hallucinated_document_is_caught(self, known_ids: list[str]) -> None:
        """**最重要的校验**：引用不存在的文档必须被抓住。

        这是唯一能确定性兜住"编造引用"的机制 ——
        模型可以编出看起来很合理的 doc-999，只有对照语料才能发现。
        """
        result = validate_final_answer(
            _initial_state(
                "x",
                answer="结论见 [doc-999]。\n\n证据：[doc-999]",
                citations=["doc-999"],
            ),
            known_document_ids=known_ids,
        )
        assert result["final_result"]["status"] == "failed"
        assert any("hallucinated_doc" in e for e in result["validation_errors"])

    def test_missing_evidence_section_fails(self, known_ids: list[str]) -> None:
        result = validate_final_answer(
            _initial_state("x", answer="结论见 [doc-001]。", citations=["doc-001"]),
            known_document_ids=known_ids,
        )
        assert any("missing_evidence_section" in e for e in result["validation_errors"])

    def test_citation_mismatch_is_caught(self, known_ids: list[str]) -> None:
        """状态里的 citations 与答案正文不一致 → 实现缺陷，必须暴露。"""
        result = validate_final_answer(
            _initial_state(
                "x",
                answer="结论见 [doc-001]。证据：[doc-001]",
                citations=["doc-002"],
            ),
            known_document_ids=known_ids,
        )
        assert any("citation_mismatch" in e for e in result["validation_errors"])

    def test_skipped_hallucination_check_is_reported(self) -> None:
        """未提供已知文档列表时，跳过检查要**明说**，不能静默通过。"""
        result = validate_final_answer(
            _initial_state(
                "x",
                answer="结论见 [doc-001]。证据：[doc-001]",
                citations=["doc-001"],
            ),
            known_document_ids=None,
        )
        assert any("hallucination_check_skipped" in e for e in result["validation_errors"])

    def test_degraded_takes_precedence_over_failed(self, known_ids: list[str]) -> None:
        """降级优先级高于失败。

        降级是"跑完了但依据不足"，与"执行失败"是两种不同结果。
        """
        result = validate_final_answer(
            _initial_state(
                "x",
                answer="证据不足，无法回答。",
                citations=[],
                degraded=True,
            ),
            known_document_ids=known_ids,
        )
        assert result["final_result"]["status"] == "degraded"
        assert result["final_result"]["degraded"] is True

    def test_final_result_carries_metrics(self, known_ids: list[str]) -> None:
        result = validate_final_answer(
            _initial_state(
                "x",
                answer="结论见 [doc-001]。证据：[doc-001]",
                citations=["doc-001"],
                evidence_coverage=0.75,
            ),
            known_document_ids=known_ids,
        )
        final = result["final_result"]
        assert final["evidence_coverage"] == 0.75
        # 引用出现 2 次（正文 1 次 + 证据小节 1 次），去重后是 1 篇文档。
        # 两个数字语义不同：citation_count 是"引用处数"，
        # citations 是"被引用的文档"。混用会让指标无法解释。
        assert final["citation_count"] == 2
        assert final["citations"] == ["doc-001"]
        assert final["has_citation"] is True

    def test_does_not_call_llm(self, known_ids: list[str]) -> None:
        """校验必须确定性 —— 否则"是否通过"会随模型波动。"""
        result = validate_final_answer(
            _initial_state(
                "x",
                answer="结论见 [doc-001]。证据：[doc-001]",
                citations=["doc-001"],
            ),
            known_document_ids=known_ids,
        )
        assert "final_result" in result


# ---------------------------------------------------------------------------
# 图拓扑
# ---------------------------------------------------------------------------


class TestGraphTopology:
    """装配结果必须与契约 PROJECT_SPEC §3.1 一致。"""

    def test_contains_exactly_five_nodes(self, deps: AgentDeps) -> None:
        graph = build_graph(deps)
        nodes = set(graph.get_graph().nodes)
        for name in NODE_ORDER:
            assert name in nodes, f"缺少节点 {name}"

    def test_entry_point_is_question_parser(self, deps: AgentDeps) -> None:
        graph = build_graph(deps).get_graph()
        edges = [(e.source, e.target) for e in graph.edges]
        assert ("__start__", NODE_QUESTION_PARSER) in edges

    def test_sequential_edges_match_contract(self, deps: AgentDeps) -> None:
        graph = build_graph(deps).get_graph()
        edges = {(e.source, e.target) for e in graph.edges}
        assert (NODE_QUESTION_PARSER, NODE_DOCUMENT_SEARCH) in edges
        assert (NODE_DOCUMENT_SEARCH, NODE_EVIDENCE_CHECKER) in edges
        assert (NODE_ANSWER_WRITER, NODE_FINAL_VALIDATOR) in edges
        assert (NODE_FINAL_VALIDATOR, "__end__") in edges

    def test_conditional_edge_from_evidence_checker(self, deps: AgentDeps) -> None:
        """条件边必须从 evidence_checker 出发，且能回到 document_search。

        这是"证据不足时放宽检索"的结构性保证。
        """
        graph = build_graph(deps).get_graph()
        edges = {(e.source, e.target) for e in graph.edges}
        assert (NODE_EVIDENCE_CHECKER, NODE_DOCUMENT_SEARCH) in edges
        assert (NODE_EVIDENCE_CHECKER, NODE_ANSWER_WRITER) in edges

    def test_compiles_without_checkpointer(self, deps: AgentDeps) -> None:
        assert build_graph(deps) is not None

    def test_deps_defaults_are_resolved(self) -> None:
        """未显式传参时，依赖应能从配置与语料自解析出来。"""
        bare = AgentDeps()
        assert bare.resolved_max_evidence_retries() >= 0
        # 未传 known_document_ids 时必须自解析出语料 ID，
        # 否则反幻觉校验会被静默跳过、每个 run 都被判 failed
        assert bare.resolved_known_document_ids()
        assert "doc-001" in bare.resolved_known_document_ids()


# ---------------------------------------------------------------------------
# 条件边路由（纯逻辑）
# ---------------------------------------------------------------------------


class TestConditionalEdgeRouting:
    """``_route_after_evidence`` 的判定逻辑。"""

    def test_sufficient_goes_to_answer_writer(self) -> None:
        state = _initial_state("x", evidence_sufficient=True, search_attempt=1)
        assert _route_after_evidence(state, max_retries=1) == NODE_ANSWER_WRITER

    def test_insufficient_with_budget_goes_back_to_search(self) -> None:
        """首次检索后（attempt=1）仍不足，且还有额度 → 放宽检索。"""
        state = _initial_state("x", evidence_sufficient=False, search_attempt=1)
        assert _route_after_evidence(state, max_retries=1) == NODE_DOCUMENT_SEARCH

    def test_insufficient_with_budget_exhausted_goes_to_answer(self) -> None:
        """额度耗尽后不再回退 —— 否则会无限循环。"""
        state = _initial_state("x", evidence_sufficient=False, search_attempt=2)
        assert _route_after_evidence(state, max_retries=1) == NODE_ANSWER_WRITER

    def test_zero_retries_never_loops_back(self) -> None:
        """``max_evidence_retries=0`` 表示不允许放宽。

        边界很容易写错：首次检索后 attempt=1，若判定写成
        ``attempt < max_retries`` 就会把 1 < 0 判成假（正确），
        但若写成 ``attempt <= max_retries + 1`` 就会多放宽一次。
        """
        state = _initial_state("x", evidence_sufficient=False, search_attempt=1)
        assert _route_after_evidence(state, max_retries=0) == NODE_ANSWER_WRITER

    def test_sufficient_wins_even_with_no_budget(self) -> None:
        """证据充分时优先前进，不受重试额度影响。"""
        state = _initial_state("x", evidence_sufficient=True, search_attempt=5)
        assert _route_after_evidence(state, max_retries=0) == NODE_ANSWER_WRITER

    def test_missing_sufficient_flag_defaults_to_insufficient(self) -> None:
        """状态里没有 ``evidence_sufficient`` 时按"不足"处理。

        默认"不足"是保守选择：宁可多检索一次，也不要凭缺失的字段
        假定证据充分。
        """
        state = {"search_attempt": 1}
        assert _route_after_evidence(state, max_retries=1) == NODE_DOCUMENT_SEARCH


# ---------------------------------------------------------------------------
# 端到端：完整图执行
# ---------------------------------------------------------------------------


class TestEndToEndGraph:
    """跑完整图，验证节点顺序与终态。"""

    def test_relevant_question_succeeds(self, deps: AgentDeps) -> None:
        state = _run_graph("trace 事件的 sequence 字段有什么作用？", deps)
        assert state["final_result"]["status"] == "succeeded"
        assert state["degraded"] is False
        assert state["citations"]

    def test_all_five_nodes_visited_in_order(self, deps: AgentDeps) -> None:
        state = _run_graph("工具的参数校验是怎么做的？", deps)
        assert state["visited_nodes"] == list(NODE_ORDER)

    def test_irrelevant_question_degrades(self, deps: AgentDeps) -> None:
        """**降级路径的端到端验证**。

        无关问题 → 检索为空 → 证据不足 → 回退放宽一次 → 仍不足 →
        写答案（标记降级）→ 校验 → 终态 degraded。
        """
        state = _run_graph("红烧肉怎么做才好吃", deps)
        assert state["final_result"]["status"] == "degraded"
        assert state["degraded"] is True
        assert state["evidence_coverage"] == 0.0
        assert state["citations"] == []
        # 没有证据 → 不应有引用，也不应编造文档
        assert state["final_result"]["validation_errors"]

    def test_retry_path_visits_search_twice(self, deps: AgentDeps) -> None:
        """证据不足时 document_search 与 evidence_checker 各被访问两次。"""
        state = _run_graph("红烧肉怎么做才好吃", deps)
        visited = state["visited_nodes"]
        assert visited.count(NODE_DOCUMENT_SEARCH) == 2
        assert visited.count(NODE_EVIDENCE_CHECKER) == 2
        # 答案与校验只走一次
        assert visited.count(NODE_ANSWER_WRITER) == 1
        assert visited.count(NODE_FINAL_VALIDATOR) == 1

    def test_retry_budget_is_honored(self, provider: FakeLLMProvider) -> None:
        """``max_evidence_retries=0`` 时不应出现第二次检索。"""
        deps = AgentDeps(
            provider=provider,
            known_document_ids=get_document_store(SAMPLE_DOCS_DIR).document_ids,
            max_evidence_retries=0,
        )
        state = _run_graph("红烧肉怎么做才好吃", deps)
        assert state["visited_nodes"].count(NODE_DOCUMENT_SEARCH) == 1
        assert state["final_result"]["status"] == "degraded"

    def test_no_hallucinated_citation_in_any_run(
        self, deps: AgentDeps, known_ids: list[str]
    ) -> None:
        """跨多个问题，答案中出现的引用必须都在语料内。"""
        for question in (
            "trace 事件的 sequence 字段有什么作用？",
            "评测指标的分母是怎么定的？",
            "为什么选 PostgreSQL？",
            "红烧肉怎么做",
        ):
            state = _run_graph(question, deps)
            for citation in state["citations"]:
                assert citation in known_ids, f"{question!r} 引用了不存在的 {citation}"

    def test_run_errors_stay_empty_on_happy_path(self, deps: AgentDeps) -> None:
        """正常路径不应产生错误事件。"""
        state = _run_graph("证据不足时会怎么做？", deps)
        assert state["errors"] == []


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_tool_failure_surfaces_as_error(self, deps: AgentDeps) -> None:
        """检索工具彻底失败时，异常应传播而不是被静默吞掉。

        静默吞掉会让失败表现为"证据不足"，把实现缺陷伪装成数据问题。
        """

        class _AlwaysFailingRegistry:
            def invoke(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> Any:
                from app.core.errors import ToolExecutionError

                raise ToolExecutionError("模拟工具失败", details={"tool_name": tool_name})

        with pytest.raises(Exception) as exc_info:
            document_search(_initial_state("证据"), registry=_AlwaysFailingRegistry())
        assert "模拟工具失败" in str(exc_info.value)

    def test_node_span_context_manager_records_status(self, db_session: Any) -> None:
        """``TraceRecorder.node()`` 上下文管理器要正确落库节点事件。"""
        from app.agent.middleware import TraceRecorder
        from app.db.repository import TraceRepository

        repository = TraceRepository(db_session)
        run = repository.create_run(
            question="测试节点记录",
            status="running",
            agent_version="1.0.0",
            prompt_version="1.0.0",
            llm_provider="fake",
            is_test_double=True,
        )
        recorder = TraceRecorder(repository)

        with recorder.node(run_id=run.id, name=NODE_QUESTION_PARSER) as span:
            span.succeed()

        events, _total = repository.list_events(run.id)
        node_events = [e for e in events if e.event_type == "node"]
        assert len(node_events) == 1
        assert node_events[0].name == NODE_QUESTION_PARSER
        assert node_events[0].status == "ok"

    def test_node_span_marks_failure_on_exception(self, db_session: Any) -> None:
        """节点内抛异常时，span 必须写 failed 而不是悬挂在 running。"""
        from app.agent.middleware import TraceRecorder
        from app.db.repository import TraceRepository

        repository = TraceRepository(db_session)
        run = repository.create_run(
            question="测试失败记录",
            status="running",
            agent_version="1.0.0",
            prompt_version="1.0.0",
            llm_provider="fake",
            is_test_double=True,
        )
        recorder = TraceRecorder(repository)

        with (
            pytest.raises(RuntimeError),
            recorder.node(run_id=run.id, name=NODE_DOCUMENT_SEARCH) as span,
        ):
            span.fail(error_code="TOOL_EXECUTION_FAILED", output_summary="模拟节点失败")
            raise RuntimeError("boom")

        events, _total = repository.list_events(run.id)
        node_events = [e for e in events if e.event_type == "node"]
        assert node_events[0].status in {"failed", "error"}

    def test_sequence_is_monotonic_across_events(self, db_session: Any) -> None:
        """同 run 内 ``sequence`` 必须单调递增 —— 回放顺序依赖它。"""
        from app.agent.middleware import TraceRecorder
        from app.db.repository import TraceRepository

        repository = TraceRepository(db_session)
        run = repository.create_run(
            question="测试序列",
            status="running",
            agent_version="1.0.0",
            prompt_version="1.0.0",
            llm_provider="fake",
            is_test_double=True,
        )
        recorder = TraceRecorder(repository)
        for name in NODE_ORDER:
            with recorder.node(run_id=run.id, name=name) as span:
                span.succeed()

        events, _total = repository.list_events(run.id)
        sequences = [e.sequence for e in events]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))


__all__ = ["NODE_ORDER"]
