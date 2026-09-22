"""质量门禁单元测试（IMPLEMENTATION_PLAN S6 第 10 项）。

覆盖三类边界，每一类都对应一条"绝不能撒谎"的规则：

1. **``null`` 指标不进判定** —— 空数据/无工具期望会让指标是 ``None``。
   当成 0 参与比较会把"压根没测"判成"严重不达标"；
2. **跳过 ≠ 通过** —— 跳过的指标必须列进 ``skipped_metrics``，
   且不得因此声称"全部通过"（EVALUATION §6.3 与反模式清单）；
3. **阈值配置错误必须 400** —— 静默忽略出错条目会让调用方以为
   阈值生效了，实际门禁用的是另一套阈值。
"""

from __future__ import annotations

import pytest

from app.core.errors import InvalidArgumentError
from app.evaluation.gate import (
    check_gate,
    check_metric_invariants,
    default_thresholds,
    validate_thresholds,
)

pytestmark = pytest.mark.unit


def _passing_metrics() -> dict:
    """一组全部达标的指标，作为基准。"""
    return {
        "run_success_rate": 0.95,
        "task_completion_rate": 0.90,
        "tool_selection_accuracy": 0.95,
        "tool_argument_accuracy": 0.90,
        "evidence_coverage": 0.80,
        "latency_ms_p95": 800,
        "estimated_cost_usd": 0.01,
        "error_rate": 0.02,
        "human_review_rate": 0.0,
    }


# ---------------------------------------------------------------------------
# 默认阈值
# ---------------------------------------------------------------------------


class TestDefaultThresholds:
    """默认阈值必须与 EVALUATION §6.1 一致。"""

    def test_matches_spec(self) -> None:
        thresholds = default_thresholds()
        assert thresholds["run_success_rate"] == {"min": 0.90}
        assert thresholds["task_completion_rate"] == {"min": 0.85}
        assert thresholds["tool_selection_accuracy"] == {"min": 0.90}
        assert thresholds["tool_argument_accuracy"] == {"min": 0.85}
        assert thresholds["evidence_coverage"] == {"min": 0.70}
        assert thresholds["latency_ms_p95"] == {"max": 2000}
        assert thresholds["estimated_cost_usd"] == {"max": 0.05}
        assert thresholds["error_rate"] == {"max": 0.05}
        assert thresholds["human_review_rate"] == {"max": 0.00}

    def test_nine_metrics_declared(self) -> None:
        assert len(default_thresholds()) == 9

    def test_ratio_metrics_use_min(self) -> None:
        thresholds = default_thresholds()
        for metric in (
            "run_success_rate",
            "task_completion_rate",
            "tool_selection_accuracy",
            "tool_argument_accuracy",
            "evidence_coverage",
        ):
            assert "min" in thresholds[metric], f"{metric} 应用 min"

    def test_cost_metrics_use_max(self) -> None:
        thresholds = default_thresholds()
        for metric in (
            "latency_ms_p95",
            "estimated_cost_usd",
            "error_rate",
            "human_review_rate",
        ):
            assert "max" in thresholds[metric], f"{metric} 应用 max"


# ---------------------------------------------------------------------------
# 通过 / 未通过
# ---------------------------------------------------------------------------


class TestGateVerdict:
    """``passed = (len(violations) == 0)``。"""

    def test_all_pass(self) -> None:
        result = check_gate(
            evaluation_id="eval_test",
            gate_name="release-gate",
            metrics=_passing_metrics(),
        )
        assert result.passed is True
        assert result.blocked is False
        assert result.status == "passed"
        assert result.violations == []

    def test_ratio_below_min_fails(self) -> None:
        metrics = _passing_metrics() | {"run_success_rate": 0.80}
        result = check_gate(evaluation_id="eval_test", gate_name="g", metrics=metrics)
        assert result.passed is False
        assert result.blocked is True
        assert len(result.violations) == 1
        violation = result.violations[0]
        assert violation.metric == "run_success_rate"
        assert violation.operator == "min"
        assert violation.threshold == 0.90
        assert violation.observed == 0.80
        assert "低于阈值" in violation.reason

    def test_cost_above_max_fails(self) -> None:
        metrics = _passing_metrics() | {"latency_ms_p95": 5000}
        result = check_gate(evaluation_id="e", gate_name="g", metrics=metrics)
        assert result.passed is False
        violation = result.violations[0]
        assert violation.metric == "latency_ms_p95"
        assert violation.operator == "max"
        assert "高于阈值" in violation.reason

    def test_multiple_violations_all_reported(self) -> None:
        """所有违规项都要列出，不能只报第一个。

        只报第一个会让使用者改完一处再跑一次，才发现还有第二处 ——
        迭代成本被无谓放大。
        """
        metrics = _passing_metrics() | {
            "run_success_rate": 0.50,
            "error_rate": 0.40,
        }
        result = check_gate(evaluation_id="e", gate_name="g", metrics=metrics)
        assert len(result.violations) == 2
        names = {item.metric for item in result.violations}
        assert names == {"run_success_rate", "error_rate"}

    def test_exactly_at_threshold_passes(self) -> None:
        """等于阈值不算违规（契约用 ``<`` 与 ``>``，不是 ``<=``/``>=``）。"""
        metrics = _passing_metrics() | {
            "run_success_rate": 0.90,
            "error_rate": 0.05,
        }
        result = check_gate(evaluation_id="e", gate_name="g", metrics=metrics)
        assert result.passed is True

    def test_zero_human_review_rate_threshold(self) -> None:
        """``human_review_rate`` 默认阈值是 ``max: 0.00``。"""
        metrics = _passing_metrics() | {"human_review_rate": 0.0}
        assert check_gate(evaluation_id="e", gate_name="g", metrics=metrics).passed

        metrics = _passing_metrics() | {"human_review_rate": 0.1}
        assert not check_gate(evaluation_id="e", gate_name="g", metrics=metrics).passed


# ---------------------------------------------------------------------------
# skipped_metrics：null 不进判定
# ---------------------------------------------------------------------------


class TestSkippedMetrics:
    """``None`` 指标不参与判定（EVALUATION §6.3 第 2 条）。"""

    def test_none_metric_is_skipped_not_violated(self) -> None:
        """这是本模块最重要的一条语义。

        空评测集下所有比率都是 ``None``。若把 ``None`` 当 0，
        门禁会报"9 项全部不达标"，而事实是"没有任何数据可判定"。
        两种结论指向完全不同的处置。
        """
        metrics = dict.fromkeys(default_thresholds(), None)
        result = check_gate(evaluation_id="e", gate_name="g", metrics=metrics)
        assert result.passed is True, "null 指标不应被判违规"
        assert result.violations == []
        assert len(result.skipped_metrics) == 9

    def test_skipped_metrics_are_listed(self) -> None:
        """跳过项必须带上，否则读者会把"3 项没检查"当成"9 项全过"。"""
        metrics = _passing_metrics() | {
            "tool_selection_accuracy": None,
            "tool_argument_accuracy": None,
        }
        result = check_gate(evaluation_id="e", gate_name="g", metrics=metrics)
        assert sorted(result.skipped_metrics) == [
            "tool_argument_accuracy",
            "tool_selection_accuracy",
        ]

    def test_partial_skip_still_evaluates_others(self) -> None:
        """有数据的那几项照常判定。"""
        metrics = _passing_metrics() | {
            "tool_selection_accuracy": None,
            "run_success_rate": 0.10,  # 这一项不达标
        }
        result = check_gate(evaluation_id="e", gate_name="g", metrics=metrics)
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].metric == "run_success_rate"
        assert result.skipped_metrics == ["tool_selection_accuracy"]

    def test_missing_key_also_skipped(self) -> None:
        """指标键完全缺失也按跳过处理（等价于无从计算）。"""
        result = check_gate(evaluation_id="e", gate_name="g", metrics={})
        assert result.passed is True
        assert len(result.skipped_metrics) == 9

    def test_skipped_does_not_mean_passed_in_payload(self) -> None:
        """响应体里必须同时看到 ``passed`` 与 ``skipped_metrics``，
        调用方才能知道"通过"是在多少项跳过的情况下得出的。"""
        metrics = dict.fromkeys(default_thresholds(), None)
        payload = check_gate(evaluation_id="e", gate_name="g", metrics=metrics).to_dict()
        assert payload["passed"] is True
        assert payload["skipped_metrics"], "跳过项不能为空 —— 否则'通过'会被误读"

    def test_non_numeric_value_skipped(self) -> None:
        """非数值（数据形状不对）同样跳过，而不是判违规。

        判违规会把"数据形状不对"误报成"质量不达标"。
        """
        metrics = _passing_metrics() | {"latency_ms_p95": "not-a-number"}
        result = check_gate(evaluation_id="e", gate_name="g", metrics=metrics)
        assert "latency_ms_p95" in result.skipped_metrics


# ---------------------------------------------------------------------------
# 阈值校验（必须 400）
# ---------------------------------------------------------------------------


class TestThresholdValidation:
    """配置错误必须报错，不能静默忽略。"""

    def test_none_uses_default_thresholds(self) -> None:
        """没传阈值 → 用默认阈值。"""
        assert validate_thresholds(None) == default_thresholds()

    def test_empty_mapping_raises_rather_than_using_defaults(self) -> None:
        """``{}`` 必须报错，**不能**退化成"用默认阈值"。

        ``{}`` 与 ``None`` 在 Python 里同样 falsy，看起来可以合并处理。
        但 ``{}`` 会覆盖默认阈值，让门禁退化成"违规集恒为空"，
        于是 ``passed=true`` 永远成立 —— 一个永远通过的门禁。

        这个 bug 极隐蔽：调用方收到 200 与 ``passed=true``，
        会读成"默认基线检查通过了"，而事实是一个指标都没检查。
        """
        with pytest.raises(InvalidArgumentError) as exc:
            validate_thresholds({})

        assert exc.value.http_status == 400
        assert exc.value.details["default_metric_count"] == len(default_thresholds())

    def test_empty_mapping_does_not_silently_pass_a_gate(self) -> None:
        """端到端确认：空阈值不能产出一个"通过"的门禁结论。"""
        with pytest.raises(InvalidArgumentError):
            check_gate(
                evaluation_id="e",
                gate_name="g",
                metrics=_passing_metrics(),
                thresholds=validate_thresholds({}),
            )

    def test_unknown_metric_raises(self) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            validate_thresholds({"not_a_metric": {"min": 0.5}})
        assert exc.value.error_code == "INVALID_ARGUMENT"
        assert exc.value.http_status == 400
        assert "not_a_metric" in str(exc.value.details)

    def test_wrong_operator_for_ratio_raises(self) -> None:
        """给比率型指标加 ``max`` 是纯粹的配置错误。

        若静默接受，``run_success_rate: {max: 0.9}`` 会变成
        "成功率越低越好" —— 门禁完全反向。
        """
        with pytest.raises(InvalidArgumentError) as exc:
            validate_thresholds({"run_success_rate": {"max": 0.9}})
        assert exc.value.details["expected_operator"] == "min"
        assert exc.value.details["received_operator"] == "max"

    def test_wrong_operator_for_cost_raises(self) -> None:
        with pytest.raises(InvalidArgumentError) as exc:
            validate_thresholds({"latency_ms_p95": {"min": 100}})
        assert exc.value.details["expected_operator"] == "max"

    def test_unknown_operator_raises(self) -> None:
        with pytest.raises(InvalidArgumentError):
            validate_thresholds({"run_success_rate": {"at_least": 0.9}})

    def test_both_min_and_max_raises(self) -> None:
        """同时给 min 与 max 没有明确定义（取哪个？）。

        报错比隐式选一个安全 —— 后者会让使用者以为自己设的和实际用的
        是同一个。
        """
        with pytest.raises(InvalidArgumentError):
            validate_thresholds({"run_success_rate": {"min": 0.5, "max": 0.9}})

    def test_non_numeric_threshold_raises(self) -> None:
        with pytest.raises(InvalidArgumentError):
            validate_thresholds({"run_success_rate": {"min": "high"}})

    def test_bool_is_not_numeric(self) -> None:
        """``True`` 是 ``int`` 的子类，但作为阈值是无意义的。"""
        with pytest.raises(InvalidArgumentError):
            validate_thresholds({"run_success_rate": {"min": True}})

    def test_empty_bounds_raises(self) -> None:
        with pytest.raises(InvalidArgumentError):
            validate_thresholds({"run_success_rate": {}})

    def test_non_dict_thresholds_raises(self) -> None:
        with pytest.raises(InvalidArgumentError):
            validate_thresholds(["run_success_rate"])  # type: ignore[arg-type]

    def test_valid_override_accepted(self) -> None:
        result = validate_thresholds({"run_success_rate": {"min": 0.5}})
        assert result == {"run_success_rate": {"min": 0.5}}

    def test_none_returns_defaults(self) -> None:
        assert validate_thresholds(None) == default_thresholds()

    def test_override_replaces_defaults_entirely(self) -> None:
        """显式传阈值时**只**用传入的那几项。

        若与默认值合并，调用方"只想改一项"的意图会被满足，
        但"只检查我关心的这一项"的意图会被违背 ——
        而后者是更常见的用法（聚焦排查某个指标）。
        """
        result = validate_thresholds({"run_success_rate": {"min": 0.5}})
        assert set(result) == {"run_success_rate"}


# ---------------------------------------------------------------------------
# 阈值快照
# ---------------------------------------------------------------------------


class TestThresholdSnapshot:
    """契约 §6.3：必须持久化阈值快照，保证可复现判断依据。"""

    def test_result_carries_normalized_thresholds(self) -> None:
        result = check_gate(
            evaluation_id="e",
            gate_name="g",
            metrics=_passing_metrics(),
            thresholds={"run_success_rate": {"min": 0.5}},
        )
        assert result.thresholds == {"run_success_rate": {"min": 0.5}}

    def test_default_snapshot_recorded_when_not_overridden(self) -> None:
        result = check_gate(evaluation_id="e", gate_name="g", metrics=_passing_metrics())
        assert result.thresholds == default_thresholds()

    def test_changing_defaults_later_does_not_alter_snapshot(self) -> None:
        """快照是值拷贝，不是引用。

        若共享同一个字典对象，后续对它的修改会让历史结论的
        "判断依据"跟着变 —— 那就不叫快照了。
        """
        result = check_gate(evaluation_id="e", gate_name="g", metrics=_passing_metrics())
        snapshot = result.thresholds
        snapshot["run_success_rate"]["min"] = 0.99
        assert default_thresholds()["run_success_rate"]["min"] == 0.90


# ---------------------------------------------------------------------------
# 响应形状
# ---------------------------------------------------------------------------


class TestGatePayload:
    """``to_dict`` 的形状与契约 §9 一致。"""

    def test_payload_fields(self) -> None:
        payload = check_gate(
            evaluation_id="eval_x", gate_name="release-gate", metrics=_passing_metrics()
        ).to_dict()
        for key in (
            "gate_id",
            "gate_name",
            "evaluation_id",
            "passed",
            "blocked",
            "status",
            "thresholds",
            "observed_metrics",
            "violations",
            "skipped_metrics",
            "checked_at",
            "data_source_note",
        ):
            assert key in payload, f"缺少字段 {key}"

    def test_blocked_is_negation_of_passed(self) -> None:
        metrics = _passing_metrics() | {"error_rate": 0.9}
        payload = check_gate(evaluation_id="e", gate_name="g", metrics=metrics).to_dict()
        assert payload["blocked"] is (not payload["passed"])

    def test_data_source_note_mentions_offline(self) -> None:
        """契约 §9：必须声明"通过门禁仅表示满足离线质量基线"。"""
        payload = check_gate(evaluation_id="e", gate_name="g", metrics={}).to_dict()
        assert "离线" in payload["data_source_note"]
        assert "不等于可生产发布" in payload["data_source_note"]

    def test_gate_id_has_prefix(self) -> None:
        payload = check_gate(evaluation_id="e", gate_name="g", metrics={}).to_dict()
        assert payload["gate_id"].startswith("gate_")


# ---------------------------------------------------------------------------
# 指标不变式自检
# ---------------------------------------------------------------------------


class TestMetricInvariants:
    """指标之间的数学关系。违反说明实现有 bug，不是质量差。"""

    def test_no_violation_for_consistent_metrics(self) -> None:
        assert check_metric_invariants(_passing_metrics()) == []

    def test_m2_exceeding_m1_detected(self) -> None:
        """契约明确写"M2 > M1 说明断言逻辑有 bug"。"""
        problems = check_metric_invariants({"run_success_rate": 0.5, "task_completion_rate": 0.9})
        assert problems
        assert "task_completion_rate" in problems[0]

    def test_error_rate_exceeding_complement_detected(self) -> None:
        problems = check_metric_invariants({"run_success_rate": 0.9, "error_rate": 0.5})
        assert problems

    def test_none_values_ignored(self) -> None:
        assert (
            check_metric_invariants(
                {"run_success_rate": None, "task_completion_rate": None, "error_rate": None}
            )
            == []
        )
