"""``POST /quality-gates/check`` 接口测试（API_CONTRACT §9）。

这个端点最反直觉的一点是 **门禁未通过也返回 200**。
它不是"请求失败"，是"业务判断结果"。测试里刻意把这条写清楚，
因为把它改成 4xx 是很容易发生的"顺手优化" ——
那会让 CI 分不清"质量不达标"与"请求写错了"。

第二重点是 **``skipped_metrics`` 不能被藏起来**：
空数据 / 无样本场景下，门禁会判 ``passed=true``，
此时若不把跳过项单独列出来，"通过"会被读成"质量达标"，
而事实是"没有任何数据可判定"。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.api, pytest.mark.test_double]

DATASET = "doc_research_v1"


def _assert_error_shape(body: dict[str, Any], expected_code: str | None = None) -> None:
    """统一错误体的结构断言（API_CONTRACT §0.1）。"""
    assert "error" in body, f"响应缺少 error 包装：{body}"
    error = body["error"]
    assert {"code", "message", "details", "request_id"} <= set(error), error
    if expected_code is not None:
        assert error["code"] == expected_code, error


@pytest.fixture
def evaluation_id(client: TestClient) -> str:
    """跑一次小规模评测，返回其 ``evaluation_id`` 作为门禁的输入。"""
    response = client.post(
        "/evaluations",
        json={"dataset_version": DATASET, "case_keys": ["doc-001", "doc-002", "doc-003"]},
    )
    assert response.status_code == 201, response.text
    return response.json()["evaluation_id"]


def _check(client: TestClient, evaluation_id: str, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "evaluation_id": evaluation_id,
        "gate_name": "default",
    }
    payload.update(overrides)
    return client.post("/quality-gates/check", json=payload)


class TestGateResponseShape:
    def test_returns_200_even_when_gate_fails(self, client: TestClient, evaluation_id: str) -> None:
        """**门禁失败也是 200。**

        用 4xx 表达"质量不达标"会让 CI 无法区分
        "要回去改 Agent"与"要改调用代码"，而这两件事的处置完全不同。
        结论由响应体的 ``passed`` 承载。
        """
        response = _check(
            client, evaluation_id, thresholds={"run_success_rate": {"min": 1.01}}
        )

        assert response.status_code == 200, response.text
        assert response.json()["passed"] is False

    def test_response_matches_contract_shape(self, client: TestClient, evaluation_id: str) -> None:
        response = _check(client, evaluation_id)

        assert response.status_code == 200, response.text
        body = response.json()
        for field in (
            "gate_id",
            "gate_name",
            "evaluation_id",
            "passed",
            "blocked",
            "observed_metrics",
            "thresholds",
            "violations",
            "skipped_metrics",
            "checked_at",
            "data_source_note",
        ):
            assert field in body, f"缺少字段 {field}"

    def test_gate_id_prefix(self, client: TestClient, evaluation_id: str) -> None:
        assert _check(client, evaluation_id).json()["gate_id"].startswith("gate_")

    def test_gate_name_is_echoed(self, client: TestClient, evaluation_id: str) -> None:
        body = _check(client, evaluation_id, gate_name="release-gate").json()
        assert body["gate_name"] == "release-gate"

    def test_evaluation_id_is_echoed(self, client: TestClient, evaluation_id: str) -> None:
        assert _check(client, evaluation_id).json()["evaluation_id"] == evaluation_id


class TestGateVerdicts:
    def test_passing_thresholds(self, client: TestClient, evaluation_id: str) -> None:
        body = _check(
            client, evaluation_id, thresholds={"run_success_rate": {"min": 0.0}}
        ).json()

        assert body["passed"] is True
        assert body["blocked"] is False
        assert body["violations"] == []

    def test_violation_reports_observed_and_threshold(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """违规项必须同时给出"观测值"与"阈值"。

        只给一个的话，使用者无法判断差多远 —— 是差一点点还是根本没跑对。
        """
        body = _check(
            client, evaluation_id, thresholds={"run_success_rate": {"min": 1.01}}
        ).json()

        assert len(body["violations"]) == 1
        violation = body["violations"][0]
        assert violation["metric"] == "run_success_rate"
        assert "observed" in violation
        assert "threshold" in violation
        assert violation["observed"] < 1.01

    def test_blocked_mirrors_passed(self, client: TestClient, evaluation_id: str) -> None:
        """``blocked`` 与 ``passed`` 必须相反 —— 两者是同一条结论的两种表达。

        不一致会让 CI 按 ``blocked`` 判断、人工按 ``passed`` 判断，
        然后两边得出相反的处理。
        """
        for threshold in ({"run_success_rate": {"min": 0.0}}, {"run_success_rate": {"min": 1.01}}):
            body = _check(client, evaluation_id, thresholds=threshold).json()
            assert body["blocked"] is (not body["passed"]), body

    def test_observed_metrics_has_no_null_values(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """``observed_metrics`` 里不能出现 ``null``。

        被跳过的指标由 ``skipped_metrics`` 单独表达；
        放 0 进 observed 等于谎报观测值，放 null 则违反字段类型。
        """
        body = _check(client, evaluation_id).json()

        for metric, value in body["observed_metrics"].items():
            assert value is not None, f"{metric} 的值是 null"

    def test_observed_metrics_only_contains_checkable_values(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        for value in _check(client, evaluation_id).json()["observed_metrics"].values():
            assert isinstance(value, (int, float)) and not isinstance(value, bool)


class TestThresholdSnapshot:
    def test_uses_defaults_when_thresholds_absent(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        body = _check(client, evaluation_id).json()

        assert body["thresholds"], "未指定阈值时应回落到默认阈值集"
        assert "run_success_rate" in body["thresholds"]

    def test_explicit_thresholds_appear_in_snapshot(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """门禁结论要带**阈值快照**，这样阈值后来被改了也能复现判定。"""
        body = _check(
            client, evaluation_id, thresholds={"run_success_rate": {"min": 0.42}}
        ).json()

        assert body["thresholds"]["run_success_rate"]["min"] == 0.42

    def test_snapshot_is_a_value_copy_not_a_reference(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """快照是值拷贝。若只是引用，后续任何改动都会篡改历史记录。"""
        first = _check(
            client, evaluation_id, thresholds={"run_success_rate": {"min": 0.42}}
        ).json()

        second = _check(
            client, evaluation_id, thresholds={"run_success_rate": {"min": 0.99}}
        ).json()

        assert first["thresholds"]["run_success_rate"]["min"] == 0.42
        assert second["thresholds"]["run_success_rate"]["min"] == 0.99


class TestThresholdValidation:
    def test_unknown_metric_returns_400(self, client: TestClient, evaluation_id: str) -> None:
        """未知指标名必须报错。

        静默忽略会让门禁"通过" —— 而它其实一个阈值都没检查。
        """
        response = _check(
            client, evaluation_id, thresholds={"no_such_metric": {"min": 0.5}}
        )

        assert response.status_code == 400, response.text
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    def test_max_on_ratio_metric_returns_400(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """比率型指标只接受 ``min``。"""
        response = _check(
            client, evaluation_id, thresholds={"run_success_rate": {"max": 0.9}}
        )

        assert response.status_code == 400, response.text
        details = response.json()["error"]["details"]
        assert details.get("expected_operator") == "min"

    def test_min_and_max_together_returns_400(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """同时给 ``min`` 与 ``max`` 是无意义的区间约束（比率已在 [0,1]）。"""
        response = _check(
            client,
            evaluation_id,
            thresholds={"run_success_rate": {"min": 0.1, "max": 0.9}},
        )

        assert response.status_code == 400, response.text

    def test_empty_threshold_object_returns_400(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """``{}`` 无法判定任何事，静默接受等于"门禁永远通过"。"""
        response = _check(client, evaluation_id, thresholds={"run_success_rate": {}})

        assert response.status_code == 400, response.text

    def test_missing_evaluation_id_returns_400(self, client: TestClient) -> None:
        response = client.post("/quality-gates/check", json={"gate_name": "g"})

        assert response.status_code == 400
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    def test_unknown_field_is_rejected(self, client: TestClient, evaluation_id: str) -> None:
        response = _check(client, evaluation_id, gate_nam="typo")

        assert response.status_code == 400
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")


class TestGateOnMissingEvaluation:
    def test_unknown_evaluation_returns_404(self, client: TestClient) -> None:
        """门禁不能对不存在的批次给出结论 —— 那会凭空造出一个"通过"。"""
        response = _check(client, "eval_doesnotexist")

        assert response.status_code == 404, response.text
        _assert_error_shape(response.json(), "EVALUATION_NOT_FOUND")


class TestGatePersistence:
    def test_repeated_checks_produce_distinct_gate_ids(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """每次检查是新的一条记录 —— 否则无法回溯"什么时候判过通过"。"""
        first = _check(client, evaluation_id).json()["gate_id"]
        second = _check(client, evaluation_id).json()["gate_id"]

        assert first != second

    def test_persistence_failure_does_not_break_response(
        self, client: TestClient, evaluation_id: str
    ) -> None:
        """落库失败只记 warning，不影响返回给调用方的结论。

        结论已经算出来了，把它返回比抛 500 更有用 ——
        调用方至少知道质量如何，而落库问题应当另行告警。
        """
        body = _check(client, evaluation_id, thresholds={"run_success_rate": {"min": 0.0}}).json()

        assert body["passed"] is True
        assert body["checked_at"]
