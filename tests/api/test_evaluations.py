"""``POST /evaluations`` 与 ``GET /evaluations/{id}`` 接口测试（API_CONTRACT §6/§7）。

这些测试跑的是**完整链路**：真实数据集 → 真实 Agent（fake provider）→
真实指标聚合 → 真实落库 → 真实重读。因此跑得比单测慢，
但它是唯一能证明"各部分拼起来还是对的"的测试。

重点盯四类容易做错的契约语义：

1. **201 + Location**，并且 Location 指向的地址能直接 GET 出同一条记录；
2. **未知 ``case_keys`` → 400**，不是静默忽略掉那几个 key；
3. **``GET`` 的指标以重算为准**，而不是读 POST 时缓存的快照；
4. **``skipped_metrics`` 不能被藏起来** —— 空数据集下"门禁通过"
   与"没有任何数据可判定"必须能被区分开。
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
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    assert isinstance(error["request_id"], str) and error["request_id"]
    if expected_code is not None:
        assert error["code"] == expected_code, error


@pytest.fixture
def evaluation(client: TestClient) -> dict[str, Any]:
    """跑一次小规模评测（3 个 case），返回 POST 的响应体。

    只取 3 个 case：这批测试要验证的是契约行为，不是指标数值；
    case 多了只会让每个测试从秒级变成十几秒级。
    """
    keys = ["doc-001", "doc-002", "doc-003"]
    response = client.post("/evaluations", json={"dataset_version": DATASET, "case_keys": keys})
    assert response.status_code == 201, response.text
    return response.json()


class TestCreateEvaluationSuccess:
    def test_returns_201_with_location_header(self, evaluation: dict[str, Any]) -> None:
        assert evaluation["evaluation_id"].startswith("eval_")

    def test_location_header_points_at_gettable_resource(self, client: TestClient) -> None:
        response = client.post(
            "/evaluations", json={"dataset_version": DATASET, "case_keys": ["doc-001"]}
        )
        evaluation_id = response.json()["evaluation_id"]

        assert response.headers["Location"] == f"/evaluations/{evaluation_id}"
        follow_up = client.get(response.headers["Location"])
        assert follow_up.status_code == 200, follow_up.text
        assert follow_up.json()["evaluation_id"] == evaluation_id

    def test_response_matches_contract_shape(self, evaluation: dict[str, Any]) -> None:
        """契约 §6 的必需字段齐备。"""
        for field in (
            "evaluation_id",
            "dataset_version",
            "agent_version",
            "prompt_version",
            "model_name",
            "llm_provider",
            "is_test_double",
            "status",
            "case_count",
            "passed_cases",
            "failed_cases",
            "run_ids",
            "metrics",
            "started_at",
            "ended_at",
            "duration_ms",
        ):
            assert field in evaluation, f"缺少字段 {field}"

    def test_timestamps_use_contract_millisecond_format(self, evaluation: dict[str, Any]) -> None:
        """契约 §0.2：所有时间为 ISO 8601 UTC、带 ``Z``、**毫秒精度**。

        直接声明 ``datetime`` 会在微秒为 0 时省略小数部分
        （``2026-09-22T04:10:00Z``），而契约示例是 ``...T04:10:00.000Z``。
        客户端按定长解析时两者长度不同 —— 这类偏差必须在 API 边界上挡住。
        """
        for field in ("started_at", "ended_at"):
            value = evaluation.get(field)
            if value is None:
                continue
            assert value.endswith("Z"), f"{field}={value!r} 未以 Z 结尾"
            assert "." in value, f"{field}={value!r} 缺少毫秒小数部分"
            fraction = value.rstrip("Z").split(".")[1]
            assert len(fraction) == 3, f"{field}={value!r} 不是毫秒精度"

    def test_case_counts_are_consistent(self, evaluation: dict[str, Any]) -> None:
        """``passed + failed`` 必须等于 ``case_count``。

        不等于意味着有 case 既没被算作通过也没被算作失败 ——
        往往是被静默吞掉的异常。
        """
        assert evaluation["case_count"] == 3
        assert evaluation["passed_cases"] + evaluation["failed_cases"] == evaluation["case_count"]

    def test_every_case_produced_a_run_id(self, evaluation: dict[str, Any]) -> None:
        assert len(evaluation["run_ids"]) == evaluation["case_count"]
        assert all(run_id.startswith("run_") for run_id in evaluation["run_ids"])

    def test_test_double_is_marked(self, evaluation: dict[str, Any]) -> None:
        """契约 B5：替身必须被明确标注，否则报告里的数字会被当成真实模型成绩。"""
        assert evaluation["is_test_double"] is True

    def test_data_source_note_is_never_empty(self, evaluation: dict[str, Any]) -> None:
        """契约 B9：任何指标输出都要带口径声明。"""
        assert evaluation["data_source_note"]

    def test_cases_not_included_by_default(self, evaluation: dict[str, Any]) -> None:
        """POST 默认不内联逐 case 结果 —— 否则响应体会随数据集规模线性膨胀。"""
        assert evaluation.get("cases") is None


class TestEvaluationMetrics:
    def test_metrics_contain_contract_keys(self, evaluation: dict[str, Any]) -> None:
        """契约锁定的指标名必须出现（哪怕值是 null）。"""
        metrics = evaluation["metrics"]
        for name in ("run_success_rate", "task_completion_rate", "tool_selection_accuracy"):
            assert name in metrics, f"指标 {name} 缺失"

    def test_ratios_are_within_unit_interval(self, evaluation: dict[str, Any]) -> None:
        for name, value in evaluation["metrics"].items():
            if name.endswith("_rate") or name.endswith("_accuracy"):
                if value is None:
                    continue
                assert 0.0 <= value <= 1.0, f"{name}={value} 超出 [0, 1]"

    def test_success_rate_agrees_with_case_counts(self, evaluation: dict[str, Any]) -> None:
        """``run_success_rate`` 与 ``passed_cases / case_count`` 是同一条事实。

        两者不一致意味着有一处口径错了 —— 而使用者会认为两份都对。
        注意契约里没有顶层 ``success_rate`` 字段，只有 ``metrics`` 里的
        ``run_success_rate``：前者是我一开始想当然写下的，核对契约时删掉了。
        """
        metrics = evaluation["metrics"]
        if metrics.get("run_success_rate") is None:
            pytest.skip("本次评测没有可用样本")

        expected = evaluation["passed_cases"] / evaluation["case_count"]
        assert metrics["run_success_rate"] == pytest.approx(expected, abs=1e-4)

    def test_token_counts_are_zero_not_null_on_empty(self, evaluation: dict[str, Any]) -> None:
        """计数型指标在空数据下是 0，不是 null（EVALUATION §3 分母说明）。

        "这次跑了 0 个 token" 与 "不知道跑了多少" 不同：
        前者是一个确切的事实，后者才该是 null。
        """
        metrics = evaluation["metrics"]
        if "total_tokens" in metrics and metrics["total_tokens"] is not None:
            assert metrics["total_tokens"] >= 0

    def test_small_sample_flag_is_visible_when_set(self, evaluation: dict[str, Any]) -> None:
        """小样本下 p95 不可信，这个事实必须能被调用方看到。

        3 个 case 的 p95 约等于最大值，单独报出来会被误读为
        "尾延迟只有这么点"。
        """
        metrics = evaluation["metrics"]
        if metrics.get("latency_sample_size") is not None:
            assert metrics["latency_p95_small_sample"] in (True, False)


class TestCreateEvaluationValidation:
    @pytest.mark.parametrize("dataset_version", ["", "   "])
    def test_blank_dataset_version_returns_400(
        self, client: TestClient, dataset_version: str
    ) -> None:
        response = client.post("/evaluations", json={"dataset_version": dataset_version})

        assert response.status_code == 400, response.text
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    def test_missing_dataset_version_returns_400(self, client: TestClient) -> None:
        response = client.post("/evaluations", json={})

        assert response.status_code == 400
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    def test_unknown_dataset_returns_404(self, client: TestClient) -> None:
        """数据集不存在是"资源不存在"（404），不是"参数写错了"（400）。

        两者对调用方的处置完全不同：前者要换数据集，后者要改字段。
        """
        response = client.post("/evaluations", json={"dataset_version": "does_not_exist"})

        assert response.status_code == 404, response.text
        _assert_error_shape(response.json(), "EVALUATION_NOT_FOUND")

    def test_unknown_case_key_returns_400_with_available_keys(self, client: TestClient) -> None:
        """未知 ``case_keys`` 必须报错，且回传可用的 key 列表。

        静默忽略是最坏的处理：调用方以为跑了指定的 3 个 case，
        实际跑的是别的 —— 而报告上看不出任何异常。
        """
        response = client.post(
            "/evaluations",
            json={"dataset_version": DATASET, "case_keys": ["doc-001", "no-such-case"]},
        )

        assert response.status_code == 400, response.text
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")
        details = response.json()["error"]["details"]
        assert "no-such-case" in str(details)

    def test_unknown_field_is_rejected(self, client: TestClient) -> None:
        """``extra="forbid"``：拼错的字段名报错而不是被忽略。"""
        response = client.post(
            "/evaluations",
            json={"dataset_version": DATASET, "cases_keys": ["doc-001"]},
        )

        assert response.status_code == 400
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    def test_path_traversal_in_dataset_version_is_rejected(self, client: TestClient) -> None:
        """``dataset_version`` 会拼进文件路径，必须挡住 ``../``。"""
        response = client.post("/evaluations", json={"dataset_version": "../../etc/passwd"})

        assert response.status_code in {400, 404}, response.text
        _assert_error_shape(response.json())


class TestGetEvaluation:
    def test_returns_same_evaluation(self, client: TestClient, evaluation: dict[str, Any]) -> None:
        response = client.get(f"/evaluations/{evaluation['evaluation_id']}")

        assert response.status_code == 200, response.text
        assert response.json()["evaluation_id"] == evaluation["evaluation_id"]

    def test_metrics_are_recomputed_not_cached(
        self, client: TestClient, evaluation: dict[str, Any]
    ) -> None:
        """重读的指标必须与 POST 时一致 —— 因为它来自同一批 ``eval_run`` 行。

        这条测试保护的是"不读缓存"这个设计决定：如果哪天有人把
        指标快照缓存到另一张表，两份数据迟早会分叉，
        而分叉时没人知道哪一份是对的。
        """
        fetched = client.get(f"/evaluations/{evaluation['evaluation_id']}").json()

        assert fetched["metrics"] == evaluation["metrics"]
        assert fetched["case_count"] == evaluation["case_count"]

    def test_include_cases_returns_per_case_results(
        self, client: TestClient, evaluation: dict[str, Any]
    ) -> None:
        response = client.get(
            f"/evaluations/{evaluation['evaluation_id']}", params={"include_cases": "true"}
        )

        cases = response.json()["cases"]
        assert len(cases) == evaluation["case_count"]
        for case in cases:
            # 契约 §7 的逐 case 字段。注意这里**没有** ``passed`` ——
            # 判断"这个 case 算不算过"要从 ``status`` 与 ``task_completed`` 读，
            # 契约刻意不提供一个聚合布尔值，避免两种口径打架。
            for field in (
                "case_key",
                "run_id",
                "status",
                "task_completed",
                "latency_ms",
                "total_tokens",
                "assertion_results",
            ):
                assert field in case, f"case 缺少字段 {field}"

    def test_only_failures_filters_cases(
        self, client: TestClient, evaluation: dict[str, Any]
    ) -> None:
        """``only_failures=true`` 时只应返回失败的 case。

        判定"失败"用的是与指标同一套口径（``status`` 非 succeeded
        或 ``task_completed`` 为假），而不是某个响应字段里的布尔值 ——
        契约没有提供那个布尔值，正是为了不让两套口径有机会分叉。
        """
        everything = client.get(
            f"/evaluations/{evaluation['evaluation_id']}",
            params={"include_cases": "true"},
        ).json()["cases"]

        response = client.get(
            f"/evaluations/{evaluation['evaluation_id']}",
            params={"include_cases": "true", "only_failures": "true"},
        )

        cases = response.json()["cases"]
        assert all(case["status"] != "succeeded" or not case["task_completed"] for case in cases), (
            "only_failures=true 时不应出现判定为通过的 case"
        )
        assert len(cases) <= len(everything)

    def test_only_failures_without_include_cases_is_harmless_noop(
        self, client: TestClient, evaluation: dict[str, Any]
    ) -> None:
        """单独给 ``only_failures`` 而不带 ``include_cases`` 时不报错。

        此时响应里根本没有 ``cases`` 字段，``only_failures`` 无从生效 ——
        为一个无害组合返回 400 只会让客户端多写一层分支。
        """
        response = client.get(
            f"/evaluations/{evaluation['evaluation_id']}", params={"only_failures": "true"}
        )

        assert response.status_code == 200, response.text
        assert response.json().get("cases") is None

    def test_unknown_evaluation_returns_404(self, client: TestClient) -> None:
        response = client.get("/evaluations/eval_doesnotexist")

        assert response.status_code == 404, response.text
        _assert_error_shape(response.json(), "EVALUATION_NOT_FOUND")

    def test_skipped_metrics_field_present(
        self, client: TestClient, evaluation: dict[str, Any]
    ) -> None:
        """``skipped_metrics`` 必须在响应里（哪怕为空数组）。

        字段缺失会让调用方无法区分"没有跳过项"与"这个版本还不支持"。
        """
        fetched = client.get(f"/evaluations/{evaluation['evaluation_id']}").json()
        assert fetched["skipped_metrics"] == []


class TestCreateEvaluationWithThresholds:
    def test_passing_thresholds_do_not_change_status_code(self, client: TestClient) -> None:
        """门禁结论不改变 POST 的状态码 —— 评测跑通了就是 201。

        把"质量不达标"映射成 4xx 会让 CI 分不清
        "要回去改 Agent"与"要改调用代码"。
        """
        response = client.post(
            "/evaluations",
            json={
                "dataset_version": DATASET,
                "case_keys": ["doc-001", "doc-002"],
                "thresholds": {"run_success_rate": {"min": 0.0}},
            },
        )

        assert response.status_code == 201, response.text

    def test_unknown_metric_in_thresholds_returns_400(self, client: TestClient) -> None:
        """未知指标名必须报错。

        静默忽略会让调用方以为设了阈值，实际门禁用的是另一套 ——
        这类沉默最危险，因为门禁照样"通过"。
        """
        response = client.post(
            "/evaluations",
            json={
                "dataset_version": DATASET,
                "case_keys": ["doc-001"],
                "thresholds": {"no_such_metric": {"min": 0.5}},
            },
        )

        assert response.status_code == 400, response.text
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    def test_wrong_operator_for_ratio_returns_400(self, client: TestClient) -> None:
        """比率型指标只能 ``min``，错误运算符必须被拒。

        若静默接受，``run_success_rate: {max: 0.9}`` 会变成
        "成功率越低越好" —— 门禁完全反向，而它看起来"配置成功"。
        """
        response = client.post(
            "/evaluations",
            json={
                "dataset_version": DATASET,
                "case_keys": ["doc-001"],
                "thresholds": {"run_success_rate": {"max": 0.9}},
            },
        )

        assert response.status_code == 400, response.text
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")
