"""指标公式单元测试（IMPLEMENTATION_PLAN S6 第 10 项）。

重点覆盖**边界条件**，这是指标实现最容易出错的地方：

- ``N = 0``（空评测集）：比率必须是 ``None`` 而不是 0，
  分位数必须是 ``None``，计数型必须是 0；
- ``N = 1``（单样本）：p50 = p95 = 该值，且必须标注样本过小；
- **分母为 0**：M3/M4 在"没有 case 有工具/参数期望"时返回 ``None``；
- ``degraded`` 的三重口径：不计成功、不计错误、单独统计；
- nearest-rank 的取位：必须是**样本中真实存在**的值。

这些边界之所以重要，是因为它们出错时**不会抛异常** ——
只会安静地报出一个错的数，而那正是本项目要消灭的东西。
"""

from __future__ import annotations

import pytest

from app.evaluation.metrics import (
    CaseOutcome,
    compute_metrics,
    coverage_for_case,
    degraded_rate,
    error_rate,
    evidence_coverage,
    human_review_rate,
    percentile,
    run_success_rate,
    task_completion_rate,
    tool_argument_accuracy,
    tool_selection_accuracy,
    tool_selection_eligible_count,
)

pytestmark = pytest.mark.unit


def _outcome(**kwargs) -> CaseOutcome:  # type: ignore[no-untyped-def]
    """构造 CaseOutcome 的小工厂，只覆盖关心的字段。"""
    defaults = {"case_key": "c", "status": "succeeded"}
    defaults.update(kwargs)
    return CaseOutcome(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# N = 0：空数据
# ---------------------------------------------------------------------------


class TestEmptyDataset:
    """空评测集。契约 EVALUATION §3 要求返回 ``None`` 而不是 0。"""

    def test_all_ratios_are_none(self) -> None:
        """所有比率型指标必须是 None。

        若返回 0.0，读者会把"没跑过"读成"全军覆没" ——
        这两个结论的处置方式完全相反。
        """
        assert run_success_rate([]) is None
        assert task_completion_rate([]) is None
        assert tool_selection_accuracy([]) is None
        assert tool_argument_accuracy([]) is None
        assert evidence_coverage([]) is None
        assert error_rate([]) is None
        assert degraded_rate([]) is None
        assert human_review_rate([]) is None

    def test_counters_are_zero_not_none(self) -> None:
        """计数型指标保留 0。

        "花了 0 个 token"是真实的观测值，与"无从计算"是两回事。
        把计数也变成 None 会让调用方无法区分这两种情况。
        """
        metrics = compute_metrics([])
        assert metrics["total_tokens"] == 0
        assert metrics["case_count"] == 0
        assert metrics["tool_selection_eligible_count"] == 0
        assert metrics["tool_argument_eligible_count"] == 0

    def test_compute_metrics_has_no_crash(self) -> None:
        """空列表不能让任何指标抛异常（除零）。"""
        metrics = compute_metrics([])
        assert metrics["latency_ms_p50"] is None
        assert metrics["latency_ms_p95"] is None
        assert metrics["latency_ms_mean"] is None
        assert metrics["latency_sample_size"] == 0


# ---------------------------------------------------------------------------
# N = 1：单样本
# ---------------------------------------------------------------------------


class TestSingleSample:
    """单样本。``p50 == p95 == 该值``，且必须标注样本过小。"""

    def test_percentiles_equal_the_only_value(self) -> None:
        metrics = compute_metrics([_outcome(latency_ms=402)])
        assert metrics["latency_ms_p50"] == 402
        assert metrics["latency_ms_p95"] == 402
        assert metrics["latency_ms_mean"] == 402.0

    def test_small_sample_is_flagged(self) -> None:
        """契约 M6 边界明确要求"N = 1 时必须标注样本过小"。

        只给数字不给样本量，读者会把 1 个样本的 p95 当作可信分位数。
        """
        metrics = compute_metrics([_outcome(latency_ms=402)])
        assert metrics["latency_sample_size"] == 1
        assert metrics["latency_p95_small_sample"] is True

    def test_large_sample_not_flagged(self) -> None:
        outcomes = [_outcome(latency_ms=100 + i) for i in range(10)]
        metrics = compute_metrics(outcomes)
        assert metrics["latency_p95_small_sample"] is False

    def test_single_success(self) -> None:
        assert run_success_rate([_outcome(status="succeeded")]) == 1.0
        assert run_success_rate([_outcome(status="failed")]) == 0.0


# ---------------------------------------------------------------------------
# nearest-rank 分位数
# ---------------------------------------------------------------------------


class TestPercentile:
    """nearest-rank 取位（EVALUATION §3 M6）。

    关键性质：返回值**必须是样本里真实存在的一个值**。
    """

    def test_empty_returns_none(self) -> None:
        assert percentile([], 0.5) is None
        assert percentile([], 0.95) is None

    def test_two_samples_p50_takes_lower(self) -> None:
        # ceil(0.5 * 2) = 1 → 最小的那个
        assert percentile([100, 200], 0.50) == 100

    def test_two_samples_p95_takes_upper(self) -> None:
        # ceil(0.95 * 2) = 2 → 最大的那个
        assert percentile([100, 200], 0.95) == 200

    def test_ten_samples_p95_is_max(self) -> None:
        samples = list(range(1, 11))
        # ceil(0.95 * 10) = ceil(9.5) = 10
        assert percentile(samples, 0.95) == 10

    def test_twenty_samples_p95_is_the_19th(self) -> None:
        """``ceil(0.95 × 20) = ceil(19) = 19``。

        **这里刻意不用"p95 就是最大值"的直觉**。那个直觉在 N ≤ 20 时
        恰好成立（因为 ``0.95 × n ≤ 19`` 向上取整后 ≤ n，且 n=20 时刚好
        等于 n−1 而非 n），容易掩盖取位错误。

        真正的浮点陷阱在别处：``0.95 * 20`` 用 float 算是
        ``18.999999999999996``，``math.ceil`` 得 19 —— 同一个结果，
        却是**碰巧对**的。所以真正的验证要用浮点敏感的 p 值，
        见下一个测试。
        """
        samples = list(range(1, 21))
        assert percentile(samples, 0.95) == 19

    def test_float_sensitive_p_is_exact(self) -> None:
        """``p = 0.29``、``n = 100``：``0.29 × 100 = 28.999999999999996``（float）。

        这才是真陷阱：float 算法 ``ceil`` 得 29，而精确十进制是
        ``ceil(29.00) = 29``。两者在这里一致，但换 ``n = 7`` 就分岔：

        ``0.29 * 7`` 的 float 结果是 ``2.0300000000000002``（偏大），
        ``0.07 * 100`` 的 float 结果是 ``7.000000000000001``（偏大），
        而 ``0.07 * 7 = 0.48999999999999994``（偏小）会让
        ``ceil(0.49) = 1`` 变成 ``ceil(0.4899…) = 1`` —— 仍一致。

        真正分岔的组合是"float 结果略小于整数"的那些。
        逐一对拍最可靠：本测试对多组 (p, n) 断言结果落在样本内，
        并给出精确的期望排名。
        """
        from decimal import ROUND_CEILING, Decimal

        for n in range(1, 41):
            samples = list(range(1, n + 1))
            for step in range(0, 101):
                p = step / 100
                expected_rank = max(
                    1,
                    min(
                        int(
                            (Decimal(str(p)) * Decimal(n)).to_integral_value(rounding=ROUND_CEILING)
                        ),
                        n,
                    ),
                )
                assert percentile(samples, p) == expected_rank, (
                    f"n={n}, p={p} 取到了第 "
                    f"{samples.index(percentile(samples, p)) + 1} 位，"
                    f"期望第 {expected_rank} 位"
                )

    def test_result_always_in_sample(self) -> None:
        """对任意 p，返回值必须命中样本中的某个元素。"""
        samples = [13, 42, 7, 99, 55]
        samples.sort()
        for step in range(0, 101):
            p = step / 100
            value = percentile(samples, p)
            assert value in samples, f"p={p} 返回了未观测到的值 {value}"

    def test_p50_of_odd_count_is_middle(self) -> None:
        assert percentile([1, 2, 3, 4, 5], 0.50) == 3

    def test_p0_returns_minimum(self) -> None:
        # ceil(0 * n) = 0 → 夹紧到 1
        assert percentile([5, 10, 15], 0.0) == 5

    def test_p100_returns_maximum(self) -> None:
        assert percentile([5, 10, 15], 1.0) == 15

    def test_invalid_p_raises(self) -> None:
        with pytest.raises(ValueError):
            percentile([1, 2], 1.5)
        with pytest.raises(ValueError):
            percentile([1, 2], -0.1)


# ---------------------------------------------------------------------------
# M1 成功率 / degraded 三重口径
# ---------------------------------------------------------------------------


class TestRunSuccessRate:
    """M1 与 ``degraded`` 的三重口径（EVALUATION §3 M1/M9 原则二）。"""

    def test_degraded_is_not_success(self) -> None:
        """``degraded`` 不计入成功。这是刻意的严格定义。"""
        outcomes = [_outcome(status="degraded")]
        assert run_success_rate(outcomes) == 0.0

    def test_degraded_is_not_error(self) -> None:
        """``degraded`` 也不计入错误率。它有独立口径。"""
        outcomes = [_outcome(status="degraded")]
        assert error_rate(outcomes) == 0.0

    def test_degraded_has_own_rate(self) -> None:
        outcomes = [_outcome(status="degraded")]
        assert degraded_rate(outcomes) == 1.0

    def test_three_rates_partition_the_space(self) -> None:
        """``success + degraded + error == 1``（三者互斥且完备）。

        这条恒等式是"degraded 不算成功也不算错误"这个口径的必然结果。
        它成立说明三个指标的分母定义一致；不成立说明有 case 落进了
        某个口径的缝隙里，而那种 case 会被所有指标一起忽略。
        """
        outcomes = [
            _outcome(status="succeeded"),
            _outcome(status="degraded"),
            _outcome(status="failed"),
            _outcome(status="timeout"),
        ]
        total = run_success_rate(outcomes) + degraded_rate(outcomes) + error_rate(outcomes)
        assert total == pytest.approx(1.0)

    def test_timeout_counts_as_error(self) -> None:
        """M9 的 ``error_rate`` 把 ``timeout`` 计入错误。

        注意这与运行级 ``/metrics/summary`` 的口径**不同**（那里超时
        不算错误）。差异是刻意的：运行级关心服务健康度，
        评测级关心"有多少 case 没产出结论"。
        """
        assert error_rate([_outcome(status="timeout")]) == 1.0

    def test_denominator_includes_failures(self) -> None:
        """分母是评测集大小，含失败 case（EVALUATION §3 首段）。

        用"成功执行数"做分母会让失败同时从分子分母消失，
        指标虚高。
        """
        outcomes = [
            _outcome(status="succeeded"),
            _outcome(status="failed"),
            _outcome(status="failed"),
            _outcome(status="failed"),
        ]
        assert run_success_rate(outcomes) == 0.25


# ---------------------------------------------------------------------------
# M2 完成率
# ---------------------------------------------------------------------------


class TestTaskCompletionRate:
    """M2 与 M1 的关系（``M2 <= M1`` 恒成立）。"""

    def test_uses_task_completed_flag(self) -> None:
        outcomes = [
            _outcome(status="succeeded", task_completed=True),
            _outcome(status="succeeded", task_completed=False),
        ]
        assert task_completion_rate(outcomes) == 0.5

    def test_m2_never_exceeds_m1(self) -> None:
        """契约明确写"若出现 M2 > M1，说明断言逻辑有 bug"。"""
        outcomes = [
            _outcome(status="succeeded", task_completed=True),
            _outcome(status="succeeded", task_completed=True),
            _outcome(status="failed", task_completed=False),
            _outcome(status="degraded", task_completed=False),
        ]
        m1 = run_success_rate(outcomes)
        m2 = task_completion_rate(outcomes)
        assert m1 is not None and m2 is not None
        assert m2 <= m1

    def test_success_but_not_completed_counts_for_m1_only(self) -> None:
        """run 成功但内容断言没过：M1 计入，M2 不计入。

        这正是 M1 与 M2 必须分开的理由 ——
        "跑通了" 与 "答对了" 是两个不同的问题。
        """
        outcomes = [_outcome(status="succeeded", task_completed=False)]
        assert run_success_rate(outcomes) == 1.0
        assert task_completion_rate(outcomes) == 0.0


# ---------------------------------------------------------------------------
# M3 工具选择（分母为 0 的边界）
# ---------------------------------------------------------------------------


class TestToolSelectionAccuracy:
    """M3 的分母只含"有工具期望"的 case。"""

    def test_none_when_no_case_has_expectation(self) -> None:
        """所有 case 都无工具期望 → ``None``（该指标不参与门禁）。

        若返回 0.0，门禁会把它判成"工具选择全错"，
        而事实是"这个指标无从计算"。
        """
        outcomes = [_outcome(tool_selection_correct=None) for _ in range(3)]
        assert tool_selection_accuracy(outcomes) is None
        assert tool_selection_eligible_count(outcomes) == 0

    def test_eligible_count_excludes_no_expectation(self) -> None:
        """无期望的 case 既不在分子也不在分母。"""
        outcomes = [
            _outcome(tool_selection_correct=True),
            _outcome(tool_selection_correct=False),
            _outcome(tool_selection_correct=None),
            _outcome(tool_selection_correct=None),
        ]
        assert tool_selection_eligible_count(outcomes) == 2
        assert tool_selection_accuracy(outcomes) == 0.5

    def test_false_is_distinct_from_none(self) -> None:
        """``False``（参与了但错）与 ``None``（没参与）语义不同。

        把 None 当成 False 会无谓拉低准确率：分母凭空变大。
        """
        only_none = [_outcome(tool_selection_correct=None)]
        only_false = [_outcome(tool_selection_correct=False)]

        assert tool_selection_accuracy(only_none) is None
        assert tool_selection_accuracy(only_false) == 0.0

    def test_all_correct(self) -> None:
        outcomes = [_outcome(tool_selection_correct=True) for _ in range(4)]
        assert tool_selection_accuracy(outcomes) == 1.0


# ---------------------------------------------------------------------------
# M4 工具参数
# ---------------------------------------------------------------------------


class TestToolArgumentAccuracy:
    """M4 的粒度是 ``(case, tool)`` 对。"""

    def test_none_when_no_expectation(self) -> None:
        outcomes = [_outcome(tool_argument_correct=None)]
        assert tool_argument_accuracy(outcomes) is None

    def test_mixed(self) -> None:
        outcomes = [
            _outcome(tool_argument_correct=True),
            _outcome(tool_argument_correct=True),
            _outcome(tool_argument_correct=False),
        ]
        assert tool_argument_accuracy(outcomes) == pytest.approx(2 / 3, abs=1e-4)

    def test_no_expectation_excluded_from_denominator(self) -> None:
        outcomes = [
            _outcome(tool_argument_correct=True),
            _outcome(tool_argument_correct=None),
            _outcome(tool_argument_correct=None),
        ]
        assert tool_argument_accuracy(outcomes) == 1.0


# ---------------------------------------------------------------------------
# M5 证据覆盖率
# ---------------------------------------------------------------------------


class TestEvidenceCoverage:
    """M5：单 case 覆盖率上限截断为 1.0。"""

    def test_coverage_is_capped_at_one(self) -> None:
        """多引用不加分。否则堆砌引用可以刷分。"""
        assert coverage_for_case(citations=5, required_citations=1) == 1.0
        assert coverage_for_case(citations=10, required_citations=2) == 1.0

    def test_zero_required_treated_as_one(self) -> None:
        """``max(1, required)`` 让"无要求"等价于"要求 1 条"。

        于是无引用要求的 case 给出 0 条引用时得 0 分，
        而不是被隐式跳过 —— "该不该引用"由数据决定。
        """
        assert coverage_for_case(citations=0, required_citations=0) == 0.0
        assert coverage_for_case(citations=1, required_citations=0) == 1.0

    def test_partial_coverage(self) -> None:
        assert coverage_for_case(citations=1, required_citations=4) == 0.25

    def test_mean_uses_full_denominator(self) -> None:
        """M5 的分母是 N，不是"有要求的 case 数"。"""
        outcomes = [
            _outcome(evidence_coverage=1.0),
            _outcome(evidence_coverage=0.0),
        ]
        assert evidence_coverage(outcomes) == 0.5

    def test_missing_value_counts_as_zero(self) -> None:
        """缺数据的 case 按 0 计：在该口径下与"零引用"后果相同。"""
        outcomes = [
            _outcome(evidence_coverage=1.0),
            _outcome(evidence_coverage=None),
        ]
        assert evidence_coverage(outcomes) == 0.5


# ---------------------------------------------------------------------------
# M7/M8 计数与成本
# ---------------------------------------------------------------------------


class TestTokenAndCostSummary:
    """M7 求和与 M8 成本合计。"""

    def test_tokens_summed(self) -> None:
        outcomes = [_outcome(total_tokens=100), _outcome(total_tokens=250)]
        assert compute_metrics(outcomes)["total_tokens"] == 350

    def test_tokens_not_normalized_but_case_count_reported(self) -> None:
        """契约要求"解读时必须同时看 case_count"。"""
        outcomes = [_outcome(total_tokens=100) for _ in range(4)]
        metrics = compute_metrics(outcomes)
        assert metrics["total_tokens"] == 400
        assert metrics["case_count"] == 4

    def test_cost_summed(self) -> None:
        outcomes = [
            _outcome(estimated_cost_usd=0.0001),
            _outcome(estimated_cost_usd=0.0002),
        ]
        assert compute_metrics(outcomes)["estimated_cost_usd"] == pytest.approx(0.0003)


# ---------------------------------------------------------------------------
# M10 人工复核率
# ---------------------------------------------------------------------------


class TestHumanReviewRate:
    """M10：``handoff`` 与超时的占比。"""

    def test_flag_drives_rate(self) -> None:
        outcomes = [
            _outcome(needs_human_review=True),
            _outcome(needs_human_review=False),
        ]
        assert human_review_rate(outcomes) == 0.5

    def test_none_when_empty(self) -> None:
        assert human_review_rate([]) is None


# ---------------------------------------------------------------------------
# 延迟样本过滤
# ---------------------------------------------------------------------------


class TestLatencySampleFiltering:
    """``latency_ms = None`` 的 case 不进样本。"""

    def test_none_excluded_from_sample(self) -> None:
        """把它当 0 会显著拉低延迟，制造"性能很好"的假象。"""
        outcomes = [
            _outcome(latency_ms=None),
            _outcome(latency_ms=100),
            _outcome(latency_ms=200),
        ]
        metrics = compute_metrics(outcomes)
        assert metrics["latency_sample_size"] == 2
        assert metrics["latency_ms_mean"] == 150.0

    def test_all_none_gives_null(self) -> None:
        outcomes = [_outcome(latency_ms=None) for _ in range(3)]
        metrics = compute_metrics(outcomes)
        assert metrics["latency_ms_p50"] is None
        assert metrics["latency_ms_p95"] is None
        assert metrics["latency_ms_mean"] is None


# ---------------------------------------------------------------------------
# compute_metrics 的形状
# ---------------------------------------------------------------------------


class TestComputeMetricsShape:
    """输出字典必须覆盖契约 §6 的 metrics 形状。"""

    def test_has_all_contract_metric_keys(self) -> None:
        from app.evaluation.metrics import METRIC_NAMES

        metrics = compute_metrics([_outcome()])
        for name in METRIC_NAMES:
            assert name in metrics, f"缺少契约指标 {name}"

    def test_has_supplementary_degraded_rate(self) -> None:
        """``degraded_rate`` 不在契约锁定的 12 个名字里，
        但 §3 M9 正文明确要求报告。"""
        from app.evaluation.metrics import METRIC_NAMES, SUPPLEMENTARY_METRIC_NAMES

        assert "degraded_rate" in SUPPLEMENTARY_METRIC_NAMES
        assert "degraded_rate" not in METRIC_NAMES
        assert "degraded_rate" in compute_metrics([_outcome()])

    def test_ratio_metrics_are_bounded(self) -> None:
        """所有比率都必须在 [0, 1] 内。越界说明公式有 bug。"""
        outcomes = [
            _outcome(status="succeeded", task_completed=True, tool_selection_correct=True),
            _outcome(status="failed", tool_selection_correct=False),
            _outcome(status="degraded"),
        ]
        metrics = compute_metrics(outcomes)
        for name in (
            "run_success_rate",
            "task_completion_rate",
            "tool_selection_accuracy",
            "tool_argument_accuracy",
            "evidence_coverage",
            "error_rate",
            "degraded_rate",
            "human_review_rate",
        ):
            value = metrics[name]
            if value is not None:
                assert 0.0 <= value <= 1.0, f"{name} = {value} 越界"
