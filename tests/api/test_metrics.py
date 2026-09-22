"""``GET /metrics/summary`` 接口测试（API_CONTRACT §8、EVALUATION §3）。

指标接口最容易犯的错不是算错，而是**算得太漂亮**。本文件因此把
重点放在几条"诚实性"断言上：

1. **无数据返回 null 而不是 0**：把"从未运行"显示成"成功率 0%"
   是最典型的指标撒谎方式；
2. **degraded 不算成功、也不算错误**：它既不属于成功（依据不足），
   也不计入 ``error_rate``（流程确实跑完了）；
3. **必须带数据来源声明**（契约 B9）；
4. **分母透明**：``case_count`` 要等于参与统计的 run 数，
   含失败 case —— 否则"只统计成功的那些"，成功率永远是 100%。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.api, pytest.mark.test_double]

ANSWERABLE_QUESTION = "trace 事件的 sequence 字段有什么作用？"
UNANSWERABLE_QUESTION = "红烧肉怎么做才好吃"


def _run(client: TestClient, question: str, **extra: Any) -> dict[str, Any]:
    response = client.post("/runs", json={"question": question, **extra})
    assert response.status_code == 201, response.text
    return response.json()


class TestMetricsEmpty:
    """无数据时的行为 —— 契约里最容易被实现错的分支。"""

    def test_returns_200_not_404(self, client: TestClient) -> None:
        """新实例问"我跑了多少"的答案是"零次"，不是 404。"""
        response = client.get("/metrics/summary")

        assert response.status_code == 200

    def test_scope_is_empty_and_metrics_are_null(self, client: TestClient) -> None:
        """**所有**指标必须是 null，不能是 0。

        ``run_success_rate = 0`` 会被读成"跑了但全失败"，
        与事实（压根没跑过）完全不同。
        """
        body = client.get("/metrics/summary").json()

        assert body["scope"] == "empty"
        assert body["run_count"] == 0
        assert body["groups"] == []
        assert body["tools"] == {}

        metrics = body["metrics"]
        for field in (
            "run_success_rate",
            "task_completion_rate",
            "error_rate",
            "human_review_rate",
            "degraded_rate",
            "evidence_coverage",
            "latency_ms_p50",
            "latency_ms_p95",
            "latency_ms_mean",
            "total_tokens",
            "estimated_cost_usd",
        ):
            assert metrics[field] is None, f"{field} 应为 null，实际 {metrics[field]!r}"

    def test_data_source_note_present_even_when_empty(self, client: TestClient) -> None:
        """契约 B9：即使没有数据，口径声明也必须存在。"""
        body = client.get("/metrics/summary").json()

        assert body["data_source_note"]
        assert "不代表任何线上流量" in body["data_source_note"]


class TestMetricsWithData:
    """有数据时的计算正确性。"""

    def test_scope_is_sample_runs(self, client: TestClient) -> None:
        """零散运行的 scope 是 ``sample_runs``（不是 offline_evaluation）。"""
        _run(client, ANSWERABLE_QUESTION)

        body = client.get("/metrics/summary").json()

        assert body["scope"] == "sample_runs"
        assert body["run_count"] == 1

    def test_success_rate_reflects_actual_outcomes(self, client: TestClient) -> None:
        _run(client, ANSWERABLE_QUESTION)
        _run(client, ANSWERABLE_QUESTION)

        metrics = client.get("/metrics/summary").json()["metrics"]

        assert metrics["run_success_rate"] == 1.0
        assert metrics["error_rate"] == 0.0
        assert metrics["case_count"] == 2

    def test_degraded_counts_as_neither_success_nor_error(self, client: TestClient) -> None:
        """降级率、成功率、错误率三者的关系是本项目的核心口径。

        跑 1 次降级 + 1 次成功：
        - 成功率 = 0.5（降级不算成功）；
        - 错误率 = 0.0（降级不算错误）；
        - 降级率 = 0.5。
        三者之和不必为 1 —— 这正是"降级是独立终态"的体现。
        """
        _run(client, ANSWERABLE_QUESTION)
        degraded = _run(client, UNANSWERABLE_QUESTION)
        assert degraded["status"] == "degraded"

        metrics = client.get("/metrics/summary").json()["metrics"]

        assert metrics["run_success_rate"] == 0.5
        assert metrics["error_rate"] == 0.0
        assert metrics["degraded_rate"] == 0.5
        assert metrics["case_count"] == 2

    def test_denominator_includes_failed_cases(self, client: TestClient) -> None:
        """分母是参与统计的 run 数，含失败 case（EVALUATION §3）。

        用"成功数 / 成功数"之类的口径会让成功率永远漂亮。
        """
        _run(client, ANSWERABLE_QUESTION)
        _run(client, UNANSWERABLE_QUESTION)

        metrics = client.get("/metrics/summary").json()["metrics"]

        # 分母是 2 而不是 1
        assert metrics["case_count"] == 2
        assert metrics["run_success_rate"] == 0.5

    def test_latency_percentiles_are_observed_values(self, client: TestClient) -> None:
        """分位数用 nearest-rank，因此必须是**样本里真实出现过**的值。

        线性插值会产生"从未观测到的耗时"，拿它做验收是不诚实的。
        """
        runs = [_run(client, ANSWERABLE_QUESTION) for _ in range(3)]
        observed = {run["duration_ms"] for run in runs}

        metrics = client.get("/metrics/summary").json()["metrics"]

        assert metrics["latency_ms_p50"] in observed
        assert metrics["latency_ms_p95"] in observed
        assert metrics["latency_ms_p50"] <= metrics["latency_ms_p95"]

    def test_total_tokens_and_cost_are_aggregated(self, client: TestClient) -> None:
        runs = [_run(client, ANSWERABLE_QUESTION) for _ in range(2)]
        expected_tokens = sum(run["total_tokens"] for run in runs)

        metrics = client.get("/metrics/summary").json()["metrics"]

        assert metrics["total_tokens"] == expected_tokens
        assert metrics["estimated_cost_usd"] is not None

    def test_tool_usage_is_aggregated(self, client: TestClient) -> None:
        _run(client, ANSWERABLE_QUESTION)

        tools = client.get("/metrics/summary").json()["tools"]

        assert "search_documents" in tools
        search = tools["search_documents"]
        assert search["calls"] >= 1
        assert search["ok"] >= 1
        assert set(search) >= {"calls", "ok", "invalid_arguments", "error"}


class TestMetricsFilters:
    """过滤参数。"""

    def test_filter_by_agent_version(self, client: TestClient) -> None:
        _run(client, ANSWERABLE_QUESTION, agent_version="v1")
        _run(client, ANSWERABLE_QUESTION, agent_version="v2")

        body = client.get("/metrics/summary", params={"agent_version": "v1"}).json()

        assert body["run_count"] == 1
        assert body["filters"]["agent_version"] == "v1"

    def test_filter_by_prompt_version(self, client: TestClient) -> None:
        _run(client, ANSWERABLE_QUESTION, prompt_version="prompt-v1")
        _run(client, ANSWERABLE_QUESTION, prompt_version="prompt-v2")

        body = client.get("/metrics/summary", params={"prompt_version": "prompt-v2"}).json()

        assert body["run_count"] == 1
        assert body["filters"]["prompt_version"] == "prompt-v2"

    def test_filters_echo_back(self, client: TestClient) -> None:
        """未传的过滤条件必须在 filters 里显式是 null。

        这比省略字段好：调用方一眼能看出"我没筛这一项"，
        而不是去猜"这个字段缺失是因为没筛还是因为接口没实现"。
        """
        body = client.get("/metrics/summary", params={"agent_version": "v1"}).json()

        assert body["filters"]["agent_version"] == "v1"
        assert body["filters"]["prompt_version"] is None
        assert body["filters"]["model_name"] is None

    def test_unmatched_filter_returns_empty_scope(self, client: TestClient) -> None:
        """筛不到任何 run 时要退回 ``empty`` + null，而不是报错。"""
        _run(client, ANSWERABLE_QUESTION, agent_version="v1")

        body = client.get("/metrics/summary", params={"agent_version": "no-such-version"}).json()

        assert body["run_count"] == 0
        assert body["scope"] == "empty"
        assert body["metrics"]["run_success_rate"] is None

    def test_time_window_filter(self, client: TestClient) -> None:
        _run(client, ANSWERABLE_QUESTION)

        # 未来窗口 → 筛不到
        future = "2099-01-01T00:00:00Z"
        body = client.get("/metrics/summary", params={"started_after": future}).json()
        assert body["run_count"] == 0

        # 过去窗口 → 全都能筛到
        past = "2000-01-01T00:00:00Z"
        body = client.get("/metrics/summary", params={"started_after": past}).json()
        assert body["run_count"] == 1


class TestMetricsGroupBy:
    """``group_by`` 分组。"""

    @pytest.mark.parametrize("dimension", ["agent_version", "prompt_version", "model_name", "day"])
    def test_supported_dimensions(self, client: TestClient, dimension: str) -> None:
        _run(client, ANSWERABLE_QUESTION)

        body = client.get("/metrics/summary", params={"group_by": dimension}).json()

        assert body["group_by"] == dimension
        assert body["groups"], f"{dimension} 应产生至少一个分组"
        group = body["groups"][0]
        assert group["key"] == dimension
        assert group["run_count"] >= 1
        assert "metrics" in group

    def test_groups_partition_all_runs(self, client: TestClient) -> None:
        """分组必须覆盖全部 run（不能有 run 落不进任何组）。

        否则"按版本对比"会静默漏掉一部分数据，而读的人不知道。
        """
        _run(client, ANSWERABLE_QUESTION, agent_version="v1")
        _run(client, ANSWERABLE_QUESTION, agent_version="v2")
        _run(client, ANSWERABLE_QUESTION, agent_version="v2")

        body = client.get("/metrics/summary", params={"group_by": "agent_version"}).json()

        assert body["run_count"] == 3
        assert sum(group["run_count"] for group in body["groups"]) == 3
        values = {group["value"] for group in body["groups"]}
        assert values == {"v1", "v2"}

    def test_groups_are_deterministically_ordered(self, client: TestClient) -> None:
        """分组顺序必须稳定，否则调用方无法可靠断言。

        dict 遍历顺序取决于插入顺序，跨请求可能不同 ——
        因此实现里做了显式排序。
        """
        for version in ("v3", "v1", "v2"):
            _run(client, ANSWERABLE_QUESTION, agent_version=version)

        first = client.get("/metrics/summary", params={"group_by": "agent_version"}).json()
        second = client.get("/metrics/summary", params={"group_by": "agent_version"}).json()

        assert [g["value"] for g in first["groups"]] == ["v1", "v2", "v3"]
        assert first["groups"] == second["groups"]

    def test_no_group_by_returns_empty_groups(self, client: TestClient) -> None:
        _run(client, ANSWERABLE_QUESTION)

        body = client.get("/metrics/summary").json()

        assert body["group_by"] is None
        assert body["groups"] == []

    @pytest.mark.parametrize("bad_value", ["status", "question", "", "AGENT_VERSION"])
    def test_invalid_group_by_returns_400(self, client: TestClient, bad_value: str) -> None:
        """非法分组维度必须报错，不能静默当作"不分组"。

        静默忽略会让调用方拿到一个"看起来有分组但实际没分组"的响应。
        """
        response = client.get("/metrics/summary", params={"group_by": bad_value})

        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert "agent_version" in error["details"]["allowed"]


class TestMetricsHonesty:
    """数据来源与替身标注的诚实性（契约 B5 / B9）。"""

    def test_data_source_note_never_claims_production(self, client: TestClient) -> None:
        """指标口径声明必须明确否认线下数据代表线上表现。"""
        _run(client, ANSWERABLE_QUESTION)

        note = client.get("/metrics/summary").json()["data_source_note"]

        assert "不代表任何线上流量" in note

    def test_concurrent_runs_counted_once_each(self, client: TestClient) -> None:
        """重复运行产生的 run 必须各自计数，不能被去重。

        去重会把"跑了很多次"读成"跑了一次"。
        """
        run_ids = {_run(client, ANSWERABLE_QUESTION)["run_id"] for _ in range(3)}

        body = client.get("/metrics/summary").json()

        assert len(run_ids) == 3
        assert body["run_count"] == 3
