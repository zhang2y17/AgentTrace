"""工具层测试：参数校验、注册表、检索打分、确定性统计。

契约 IMPLEMENTATION_PLAN S4 第 11 项明确要求"工具参数校验测试（含非法参数）"。

本文件的重点是**结构性保证**而不是"函数返回值对不对"：

- 参数校验失败时，工具实现体**必须未被调用**。
  这是契约 §3.3 第 1 条的核心 —— 光断言"抛了异常"是不够的，
  因为实现体可能已经被执行过、已经产生了副作用。
  因此下面用 Spy 记录实现体的调用次数，断言它是 0。

- 检索必须确定性：同一输入两次调用得到完全相同的顺序。
  分数相同时若依赖字典遍历顺序，评测就不可复现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.tools.analytics import nearest_rank
from app.tools.base import (
    TOOL_CALCULATE_COST_SUMMARY,
    TOOL_CALCULATE_LATENCY_SUMMARY,
    TOOL_GET_DOCUMENT,
    TOOL_NAMES,
    TOOL_SEARCH_DOCUMENTS,
    CalculateCostSummaryArgs,
    CalculateLatencySummaryArgs,
    GetDocumentArgs,
    SearchDocumentsArgs,
    ToolSpec,
)
from app.tools.document_search import DocumentStore, _tokenize
from app.tools.registry import (
    InvalidToolArgumentsError,
    ToolNotFoundError,
    clear,
    get,
    invoke,
    is_registered,
    register,
    register_builtin_tools,
    registered_names,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DOCS_DIR = PROJECT_ROOT / "data" / "sample_docs"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> DocumentStore:
    """真实样例文档的存储实例。"""
    return DocumentStore(SAMPLE_DOCS_DIR)


class _SpyTool:
    """记录调用次数的工具实现体替身。

    用它来断言"参数非法时实现体未被调用"。
    """

    def __init__(self, result: Any = "ok", error: Exception | None = None) -> None:
        self.call_count = 0
        self.received: list[Any] = []
        self._result = result
        self._error = error

    def __call__(self, args: Any) -> Any:
        self.call_count += 1
        self.received.append(args)
        if self._error is not None:
            raise self._error
        return self._result


class _StubAnalytics:
    """分析工具替身：注册表只读取它的方法引用，不需要真的查库。"""

    def calculate_latency_summary(self, args: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def calculate_cost_summary(self, args: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _register_spy(
    name: str,
    args_model: type,
    *,
    result: Any = "ok",
    error: Exception | None = None,
) -> _SpyTool:
    """把一个 Spy 注册成工具，返回 Spy 以便断言。"""
    spy = _SpyTool(result=result, error=error)
    register(
        ToolSpec(
            name=name,
            version="0.0.1-test",
            description="测试用工具",
            args_model=args_model,
            implementation=spy,
            writes_database=False,
            uses_llm=False,
            tags=("test",),
        )
    )
    return spy


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """每个测试前后清空注册表，避免互相污染。"""
    clear()
    yield
    clear()


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------


class TestSearchDocumentsArgs:
    """``search_documents`` 的参数约束。"""

    def test_accepts_valid_arguments(self) -> None:
        args = SearchDocumentsArgs(query="证据不足", top_k=3)
        assert args.query == "证据不足"
        assert args.top_k == 3

    def test_top_k_has_default(self) -> None:
        """top_k 有默认值，调用方不必显式传。"""
        assert SearchDocumentsArgs(query="x").top_k == 3

    @pytest.mark.parametrize("bad_top_k", [0, -1, 11, 100])
    def test_rejects_top_k_out_of_range(self, bad_top_k: int) -> None:
        """top_k 必须落在 1~10。上界与契约的 top_k <= 10 一致。"""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchDocumentsArgs(query="x", top_k=bad_top_k)

    def test_rejects_empty_query(self) -> None:
        """空查询没有意义，直接拒绝而不是返回空结果。"""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchDocumentsArgs(query="")

    def test_rejects_overlong_query(self) -> None:
        """查询长度上限 500，避免超长文本拖慢检索。"""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchDocumentsArgs(query="x" * 501)


class TestGetDocumentArgs:
    """``get_document`` 的文档 ID 格式约束。"""

    def test_accepts_valid_id(self) -> None:
        assert GetDocumentArgs(document_id="doc-001").document_id == "doc-001"

    @pytest.mark.parametrize(
        "bad_id",
        [
            "doc-1",  # 位数不足
            "doc-0001",  # 位数过多
            "DOC-001",  # 大小写
            "document-001",
            "../../etc/passwd",  # 目录逃逸尝试
            "doc-001/../../secret",
            "",
            "doc-abc",
        ],
    )
    def test_rejects_malformed_id(self, bad_id: str) -> None:
        """非法 ID 必须在**参数层**被拦住。

        ``../../etc/passwd`` 这类构造是路径逃逸的典型尝试：
        在参数层拒绝它，比在实现体里做路径检查更早、更可靠。
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            GetDocumentArgs(document_id=bad_id)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class TestRegistryRegistration:
    """注册表的注册语义。"""

    def test_register_and_get(self) -> None:
        _register_spy("tool_a", SearchDocumentsArgs)
        assert is_registered("tool_a")
        assert get("tool_a").name == "tool_a"

    def test_duplicate_registration_raises(self) -> None:
        """重复注册必须报错。

        静默覆盖会让"改了工具却没生效"变成一个无声的 bug ——
        这类问题在评测里表现为"指标没变化"，极难定位。
        """
        _register_spy("tool_a", SearchDocumentsArgs)
        with pytest.raises(ValueError, match="已注册"):
            _register_spy("tool_a", SearchDocumentsArgs)

    def test_get_unknown_tool_raises(self) -> None:
        with pytest.raises(ToolNotFoundError):
            get("no_such_tool")

    def test_registered_names_is_sorted(self) -> None:
        """名字列表必须排序 —— 顺序不稳定会让日志与断言难以比较。"""
        _register_spy("tool_z", SearchDocumentsArgs)
        _register_spy("tool_a", SearchDocumentsArgs)
        assert registered_names() == ["tool_a", "tool_z"]

    def test_clear_empties_registry(self) -> None:
        _register_spy("tool_a", SearchDocumentsArgs)
        clear()
        assert registered_names() == []

    def test_register_builtin_tools_registers_four(self, store: DocumentStore) -> None:
        """四个内置工具全部注册，且名字与契约一致。"""
        from app.tools.document_search import DocumentSearchTools

        register_builtin_tools(
            document_tools=DocumentSearchTools(store),
            analytics_tools=_StubAnalytics(),
        )
        assert registered_names() == sorted(TOOL_NAMES)
        assert registered_names() == [
            "calculate_cost_summary",
            "calculate_latency_summary",
            "get_document",
            "search_documents",
        ]

    def test_register_builtin_tools_is_idempotent(self, store: DocumentStore) -> None:
        """重复调用不报错 —— 应用重启或多次 lifespan 不应炸。"""
        from app.tools.document_search import DocumentSearchTools

        tools = DocumentSearchTools(store)
        register_builtin_tools(document_tools=tools, analytics_tools=_StubAnalytics())
        register_builtin_tools(document_tools=tools, analytics_tools=_StubAnalytics())
        assert len(registered_names()) == 4

    def test_builtin_tools_declare_no_llm(self, store: DocumentStore) -> None:
        """契约 PROJECT_SPEC §3.3 第 2 条：四个工具都不得调用 LLM。

        统计与检索必须由确定性代码完成，不允许让模型猜数值。
        """
        from app.tools.document_search import DocumentSearchTools

        register_builtin_tools(
            document_tools=DocumentSearchTools(store),
            analytics_tools=_StubAnalytics(),
        )
        for name in TOOL_NAMES:
            assert get(name).uses_llm is False, f"{name} 不应调用 LLM"


class TestRegistryInvoke:
    """注册表的调用语义：校验 → 执行 → 重试。"""

    def test_successful_invoke_returns_result(self) -> None:
        spy = _register_spy("tool_a", SearchDocumentsArgs, result=["hit"])
        result = invoke(
            "tool_a",
            {"query": "x", "top_k": 3},
            run_id="run_x",
            node_name="document_search",
        )
        assert result == ["hit"]
        assert spy.call_count == 1

    def test_validated_arguments_reach_implementation(self) -> None:
        """实现体收到的是**已校验的 Pydantic 模型**，不是原始 dict。

        这是"非法参数不可能到达实现体"的结构性保证：
        实现体的签名决定了它收不到未校验的输入。
        """
        spy = _register_spy("tool_a", SearchDocumentsArgs)
        invoke("tool_a", {"query": "证据", "top_k": 5}, run_id="r", node_name="n")
        assert isinstance(spy.received[0], SearchDocumentsArgs)

    def test_invalid_arguments_do_not_call_implementation(self) -> None:
        """**核心断言**：参数非法时实现体调用次数为 0。

        只断言"抛了异常"不够 —— 实现体可能已经执行并产生了副作用。
        """
        spy = _register_spy("tool_a", SearchDocumentsArgs)
        with pytest.raises(InvalidToolArgumentsError):
            invoke("tool_a", {"query": "x", "top_k": 999}, run_id="r", node_name="n")
        assert spy.call_count == 0

    def test_invalid_arguments_error_carries_details(self) -> None:
        """异常要带上字段路径，便于定位是哪个参数错了。"""
        _register_spy("tool_a", SearchDocumentsArgs)
        with pytest.raises(InvalidToolArgumentsError) as exc_info:
            invoke("tool_a", {"query": "x", "top_k": 999}, run_id="r", node_name="n")
        assert exc_info.value.tool_name == "tool_a"
        assert exc_info.value.errors
        locations = {item["location"] for item in exc_info.value.errors}
        assert any("top_k" in location for location in locations)

    def test_invalid_arguments_do_not_retry(self) -> None:
        """参数非法不重试：同一非法参数重试不会有不同结果。"""
        spy = _register_spy("tool_a", SearchDocumentsArgs)
        with pytest.raises(InvalidToolArgumentsError):
            invoke(
                "tool_a",
                {"query": "x", "top_k": 999},
                run_id="r",
                node_name="n",
                max_retries=3,
            )
        assert spy.call_count == 0

    def test_execution_failure_retries_then_raises(self) -> None:
        """执行异常按 max_retries 重试，耗尽后抛 ToolExecutionError。"""
        from app.core.errors import ToolExecutionError

        spy = _register_spy("tool_a", SearchDocumentsArgs, error=RuntimeError("boom"))
        with pytest.raises(ToolExecutionError) as exc_info:
            invoke(
                "tool_a",
                {"query": "x"},
                run_id="r",
                node_name="n",
                max_retries=2,
            )
        # 1 次初始 + 2 次重试 = 3 次
        assert spy.call_count == 3
        assert exc_info.value.details["tool_name"] == "tool_a"

    def test_execution_succeeds_after_transient_failure(self) -> None:
        """瞬时故障：第一次失败、第二次成功，整体应成功。"""

        class _Flaky:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, args: Any) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient")
                return "recovered"

        flaky = _Flaky()
        register(
            ToolSpec(
                name="tool_flaky",
                version="0.0.1-test",
                description="瞬时故障工具",
                args_model=SearchDocumentsArgs,
                implementation=flaky,
                writes_database=False,
                uses_llm=False,
                tags=("test",),
            )
        )
        result = invoke(
            "tool_flaky",
            {"query": "x"},
            run_id="r",
            node_name="n",
            max_retries=2,
        )
        assert result == "recovered"
        assert flaky.calls == 2

    def test_no_retry_when_max_retries_zero(self) -> None:
        """max_retries=0 表示不重试，只执行一次。"""
        from app.core.errors import ToolExecutionError

        spy = _register_spy("tool_a", SearchDocumentsArgs, error=RuntimeError("boom"))
        with pytest.raises(ToolExecutionError):
            invoke("tool_a", {"query": "x"}, run_id="r", node_name="n", max_retries=0)
        assert spy.call_count == 1

    def test_invoke_unknown_tool_raises(self) -> None:
        with pytest.raises(ToolNotFoundError):
            invoke("ghost", {}, run_id="r", node_name="n")

    def test_arguments_are_redacted_before_recording(self) -> None:
        """落库前的参数要脱敏。

        虽然四个内置工具当前都不接收密钥，但注册表是通用设施 ——
        将来可能接入带凭证的工具，脱敏必须在这里就位。
        """
        spy = _register_spy("tool_secret", _ApiKeyArgs)

        class _Recorder:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def record_tool_call(self, **kwargs: Any) -> None:
                self.calls.append(kwargs)

        recorder = _Recorder()
        invoke(
            "tool_secret",
            {"api_key": "sk-abcdefghijklmnopqrstuvwxyz0123456789"},
            run_id="r",
            node_name="n",
            recorder=recorder,
        )
        # 实现体确实被调用了（这是成功路径）
        assert spy.call_count == 1
        recorded = recorder.calls[0]["arguments"]
        assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in str(recorded)
        assert "REDACTED" in str(recorded)


class _ApiKeyArgs(BaseModel):
    """带密钥字段的参数模型，用于验证落库脱敏。"""

    api_key: str = ""


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


class TestTokenize:
    """中文切分：字 + 二元组 + 停用词过滤。"""

    def test_produces_bigrams_for_cjk(self) -> None:
        """连续中文串必须生成二元组。

        只按单字切分时，"证据不足"变成四个单字，打分器里
        ``len(term) < 2`` 的过滤会把它们全丢掉 —— 这是实际发现的缺陷。
        """
        tokens = _tokenize("证据不足")
        assert "证据" in tokens
        assert "据不" in tokens
        assert "不足" in tokens

    def test_filters_stopwords(self) -> None:
        """疑问词与功能词必须被剔除。

        否则"今天晚饭吃什么"能靠"什么"命中每一篇文档，
        覆盖率恒为 1，"证据不足"分支永不触发。
        """
        tokens = _tokenize("今天晚饭吃什么")
        assert "什么" not in tokens
        assert "今天" not in tokens

    def test_keeps_latin_identifiers(self) -> None:
        """下划线标识符按整词保留。"""
        assert "parent_event_id" in _tokenize("父事件 parent_event_id")

    def test_bigrams_do_not_cross_punctuation(self) -> None:
        """二元组不跨标点生成，避免产出无意义的组合。"""
        tokens = _tokenize("证据，不足")
        assert "据不" not in tokens


class TestDocumentStoreSearch:
    """检索的排序、门槛与确定性。"""

    def test_loads_all_sample_documents(self, store: DocumentStore) -> None:
        assert len(store.document_ids) == 6
        assert store.document_ids[0] == "doc-001"

    def test_document_ids_are_sorted(self, store: DocumentStore) -> None:
        assert store.document_ids == sorted(store.document_ids)

    @pytest.mark.parametrize(
        ("query", "expected_top"),
        [
            ("trace 事件的 sequence 字段有什么作用？", "doc-002"),
            ("工具的参数校验是怎么做的？", "doc-004"),
            ("证据不足时会怎么做？", "doc-003"),
            ("为什么选 PostgreSQL 而不是其他数据库？", "doc-005"),
            ("评测指标的分母是怎么定的？", "doc-006"),
            ("AgentTrace 是什么项目？", "doc-001"),
            ("nearest-rank 分位数怎么算？", "doc-006"),
            ("降级是什么意思？", "doc-002"),
        ],
    )
    def test_relevant_queries_hit_expected_document(
        self, store: DocumentStore, query: str, expected_top: str
    ) -> None:
        """相关查询的 top-1 必须命中预期文档。

        这些用例同时是检索质量的回归基线：
        若将来调整打分权重导致某类查询跑偏，这里会先失败。
        """
        hits = store.search(query, 3)
        assert hits, f"查询 {query!r} 未命中任何文档"
        assert hits[0].document_id == expected_top

    @pytest.mark.parametrize(
        "query",
        [
            "今天晚饭吃什么",
            "红烧肉怎么做才好吃",
            "北京到上海的机票多少钱",
        ],
    )
    def test_irrelevant_queries_return_no_hits(self, store: DocumentStore, query: str) -> None:
        """**关键断言**：与项目无关的查询必须返回空结果。

        没有分数门槛时，任何非空查询都能凑出 top_k 篇文档
        （总能命中某个单字或二元组），于是 evidence_checker 的
        "命中数"恒等于 top_k、覆盖率恒为 1 —— "证据不足"分支
        永远不会触发，degraded 语义也就无从验证。
        """
        assert store.search(query, 3) == []

    def test_hits_are_sorted_by_score_desc(self, store: DocumentStore) -> None:
        hits = store.search("证据不足", 3)
        scores = [hit.score for hit in hits]
        assert scores == sorted(scores, reverse=True)

    def test_search_is_deterministic(self, store: DocumentStore) -> None:
        """同一输入两次调用结果完全一致。

        分数相同时必须用 document_id 做次级排序键 ——
        若依赖字典遍历顺序，评测就不可复现。
        """
        first = store.search("工具治理与参数校验", 5)
        second = store.search("工具治理与参数校验", 5)
        assert [(h.document_id, h.score) for h in first] == [
            (h.document_id, h.score) for h in second
        ]

    def test_respects_top_k(self, store: DocumentStore) -> None:
        assert len(store.search("证据", 1)) <= 1

    def test_empty_query_returns_nothing(self, store: DocumentStore) -> None:
        assert store.search("", 3) == []
        assert store.search("   ", 3) == []

    def test_hits_carry_snippet_and_matched_terms(self, store: DocumentStore) -> None:
        hits = store.search("sequence 字段", 1)
        assert hits
        assert hits[0].snippet
        assert hits[0].matched_terms

    def test_get_returns_none_for_unknown_id(self, store: DocumentStore) -> None:
        assert store.get("doc-999") is None

    def test_get_returns_document_for_known_id(self, store: DocumentStore) -> None:
        document = store.get("doc-001")
        assert document is not None
        assert document.document_id == "doc-001"
        assert document.title
        assert document.content

    def test_missing_directory_yields_empty_store(self, tmp_path: Path) -> None:
        """文档目录不存在时不抛异常，只是没有文档。

        这样"文档还没放好"表现为"证据不足"（可诊断），
        而不是"服务启动失败"（难诊断）。
        """
        empty_store = DocumentStore(tmp_path / "does-not-exist")
        assert empty_store.document_ids == []
        assert empty_store.search("任意查询", 3) == []


class TestBuiltinSearchTools:
    """``DocumentSearchTools`` 实现体。"""

    def test_search_documents_returns_hits(self, store: DocumentStore) -> None:
        from app.tools.document_search import DocumentSearchTools

        tools = DocumentSearchTools(store)
        hits = tools.search_documents(SearchDocumentsArgs(query="证据不足", top_k=3))
        assert hits
        assert hits[0].document_id == "doc-003"

    def test_get_document_returns_document(self, store: DocumentStore) -> None:
        from app.tools.document_search import DocumentSearchTools

        tools = DocumentSearchTools(store)
        document = tools.get_document(GetDocumentArgs(document_id="doc-002"))
        assert document.document_id == "doc-002"

    def test_get_document_raises_keyerror_for_unknown(self, store: DocumentStore) -> None:
        """未知文档抛 KeyError，由注册表转成 failed 状态（而非 500）。"""
        from app.tools.document_search import DocumentSearchTools

        tools = DocumentSearchTools(store)
        with pytest.raises(KeyError):
            tools.get_document(GetDocumentArgs(document_id="doc-999"))


# ---------------------------------------------------------------------------
# 确定性统计
# ---------------------------------------------------------------------------


class TestNearestRank:
    """nearest-rank 分位数。

    不用线性插值（EVALUATION §3 原则三）：插值会产生样本中
    **从未观测到**的数值，用未观测值做验收是不诚实的。
    """

    def test_empty_sample_returns_none(self) -> None:
        assert nearest_rank([], 0.5) is None

    def test_single_sample(self) -> None:
        assert nearest_rank([42], 0.5) == 42
        assert nearest_rank([42], 0.95) == 42

    def test_p50_of_odd_sample(self) -> None:
        assert nearest_rank([10, 20, 30], 0.5) == 20

    def test_result_always_comes_from_sample(self) -> None:
        """**核心性质**：返回值必然是样本里真实存在的值。"""
        sample = sorted([7, 13, 29, 41, 58])
        for percentile in (0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 1.0):
            assert nearest_rank(sample, percentile) in sample

    def test_p95_of_small_sample_is_max(self) -> None:
        """小样本下 p95 就是最大值 —— 比插值更保守、更诚实。"""
        sample = sorted([10, 20, 30, 40, 50])
        assert nearest_rank(sample, 0.95) == 50

    def test_p100_is_max(self) -> None:
        sample = sorted([3, 1, 2])
        assert nearest_rank(sample, 1.0) == 3

    def test_rejects_out_of_range_percentile(self) -> None:
        with pytest.raises(ValueError):
            nearest_rank([1, 2, 3], 1.5)
        with pytest.raises(ValueError):
            nearest_rank([1, 2, 3], -0.1)


class TestAnalyticsToolsMissingRun:
    """分析工具对不存在的 run 的行为。

    对不存在的 run 请求统计是**非法参数**（调用方给的 ID 不对），
    因此抛 ``KeyError`` 是正确的 —— 注册表会把它映射为
    ``INVALID_ARGUMENT`` 而不是 ``TOOL_EXECUTION_FAILED``。

    这里不能返回"零样本"的汇总：那会让"run 不存在"和
    "run 存在但还没产生任何节点"看起来一模一样。
    """

    def test_latency_summary_raises_for_unknown_run(self, db_session: Any) -> None:
        from app.tools.analytics import AnalyticsTools

        tools = AnalyticsTools(session_factory=lambda: db_session)
        with pytest.raises(KeyError):
            tools.calculate_latency_summary(CalculateLatencySummaryArgs(run_id="run_nonexistent"))

    def test_cost_summary_raises_for_unknown_run(self, db_session: Any) -> None:
        from app.tools.analytics import AnalyticsTools

        tools = AnalyticsTools(session_factory=lambda: db_session)
        with pytest.raises(KeyError):
            tools.calculate_cost_summary(CalculateCostSummaryArgs(run_id="run_nonexistent"))

    def test_latency_summary_on_run_without_events(self, db_session: Any, repository: Any) -> None:
        """run 存在但没有任何 node 事件时，返回空统计而不是报错。"""
        from app.tools.analytics import AnalyticsTools

        run = repository.create_run(
            question="空 run",
            status="succeeded",
            agent_version="1.0.0",
            prompt_version="1.0.0",
            llm_provider="fake",
            is_test_double=True,
        )
        tools = AnalyticsTools(session_factory=lambda: db_session)
        summary = tools.calculate_latency_summary(CalculateLatencySummaryArgs(run_id=run.id))
        assert summary.run_id == run.id
        assert summary.node_count == 0
        assert summary.tool_call_count == 0
        assert summary.model_call_count == 0
        # 零样本时分位数是 None —— 不能编造 0
        assert summary.p50_node_ms is None
        assert summary.p95_node_ms is None
        assert summary.slowest_node is None


__all__ = [
    "TOOL_CALCULATE_COST_SUMMARY",
    "TOOL_CALCULATE_LATENCY_SUMMARY",
    "TOOL_GET_DOCUMENT",
    "TOOL_SEARCH_DOCUMENTS",
]
