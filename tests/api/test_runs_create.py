"""``POST /runs`` 接口测试（API_CONTRACT §2）。

重点验证的不只是"能跑通"，而是几条容易做错的契约语义：

1. **201 + Location 头**：创建资源返回 201 而不是 200，
   并且 Location 指向可再次 GET 的地址。
2. **空白 question → 422 AGENT_VALIDATION_ERROR**：不是 400，不是 200。
3. **Trace 真的落库**：``counts.trace_events`` 必须 > 0 ——
   一个"成功但零事件"的 run 意味着可观测性根本没生效。
4. **测试替身标注**：``is_test_double`` 必须为 true（默认 fake provider）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.api, pytest.mark.test_double]

# 一个能命中自建语料的问题（doc-002 讲 trace 事件与 sequence）
ANSWERABLE_QUESTION = "trace 事件的 sequence 字段有什么作用？"

# 一个语料里完全没有的领域
UNANSWERABLE_QUESTION = "红烧肉怎么做才好吃"


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


class TestCreateRunSuccess:
    """成功路径。"""

    def test_returns_201_with_location_header(self, client: TestClient) -> None:
        response = client.post("/runs", json={"question": ANSWERABLE_QUESTION})

        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]
        assert response.headers["Location"] == f"/runs/{run_id}"

    def test_response_matches_contract_shape(self, client: TestClient) -> None:
        body = client.post("/runs", json={"question": ANSWERABLE_QUESTION}).json()

        # 契约 §2 的必需字段
        for field in (
            "run_id",
            "status",
            "question",
            "agent_version",
            "prompt_version",
            "llm_provider",
            "is_test_double",
            "total_tokens",
            "estimated_cost_usd",
            "result_summary",
            "counts",
        ):
            assert field in body, f"缺少字段 {field}"

        assert body["run_id"].startswith("run_")
        assert body["question"] == ANSWERABLE_QUESTION
        assert body["status"] in {"succeeded", "failed", "degraded"}

    def test_test_double_is_marked(self, client: TestClient) -> None:
        """契约 B5：测试替身必须被明确标注，不能不声明。"""
        body = client.post("/runs", json={"question": ANSWERABLE_QUESTION}).json()

        assert body["is_test_double"] is True
        assert body["llm_provider"] == "fake"
        # 报 fake-model 而不是真实模型名 —— 避免掩盖"这是替身"的事实
        assert body["model_name"] == "fake-model"

    def test_trace_events_actually_persisted(self, client: TestClient) -> None:
        """核心断言：一次成功的 run 必须真的写入了 Trace 事件。

        "状态 succeeded 但 trace_events=0"是本项目最危险的一类假成功 ——
        平台的核心价值就是可观测，事件为空等于平台没工作。
        5 个节点各写一条 node 事件，因此下界是 5。
        """
        body = client.post("/runs", json={"question": ANSWERABLE_QUESTION}).json()

        assert body["status"] == "succeeded"
        counts = body["counts"]
        assert counts["trace_events"] >= 5, counts
        assert counts["tool_calls"] >= 1, counts
        assert counts["model_calls"] >= 1, counts

    def test_answer_has_citations(self, client: TestClient) -> None:
        """可回答的问题应当产出带引用的答案。"""
        body = client.post("/runs", json={"question": ANSWERABLE_QUESTION}).json()
        summary = body["result_summary"]

        assert summary["evidence_sufficient"] is True
        assert summary["citations"], "答案没有引用任何文档"
        assert summary["answer_chars"] > 0

    def test_unanswerable_question_degrades_not_fails(self, client: TestClient) -> None:
        """无关问题应判 ``degraded``（跑完了但依据不足），而不是 ``failed``。

        这个区分是契约的核心语义：降级不计入 error_rate，
        因为它不是"跑错了"，而是"诚实地报告没有依据"。
        """
        body = client.post("/runs", json={"question": UNANSWERABLE_QUESTION}).json()

        assert body["status"] == "degraded", body
        assert body["result_summary"]["evidence_sufficient"] is False

    def test_defaults_are_applied(self, client: TestClient) -> None:
        body = client.post("/runs", json={"question": ANSWERABLE_QUESTION}).json()

        assert body["agent_version"] == "v1"
        assert body["prompt_version"] == "prompt-v1"
        assert body["source_run_id"] is None

    def test_bounded_question_length(self, client: TestClient) -> None:
        """超过 2000 字符的问题应被拒绝（契约 max_length）。"""
        too_long = "测" * 2001
        response = client.post("/runs", json={"question": too_long})

        assert response.status_code == 400
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")


class TestCreateRunValidation:
    """参数校验路径。"""

    @pytest.mark.parametrize("question", ["", "   ", "\t\n"])
    def test_blank_question_returns_422(self, client: TestClient, question: str) -> None:
        """空白问题必须是 422 AGENT_VALIDATION_ERROR（契约 §2 明确要求）。

        注意这不是 400：Pydantic 的校验失败统一映射为 400，
        而契约对"空白问题"单独规定了 422，因此路由层做了额外处理。
        """
        response = client.post("/runs", json={"question": question})

        assert response.status_code == 422, response.text
        _assert_error_shape(response.json(), "AGENT_VALIDATION_ERROR")

    def test_missing_question_returns_400(self, client: TestClient) -> None:
        """字段缺失属于请求体结构错误 → 400（区别于空白值的 422）。"""
        response = client.post("/runs", json={})

        assert response.status_code == 400
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    def test_unknown_field_rejected(self, client: TestClient) -> None:
        """契约使用 extra="forbid"：拼错的字段名必须报错而不是被忽略。

        静默忽略拼错的字段会让调用方以为过滤生效了，实际没有。
        """
        response = client.post(
            "/runs",
            json={"question": ANSWERABLE_QUESTION, "unknown_param": "x"},
        )

        assert response.status_code == 400
        _assert_error_shape(response.json(), "INVALID_ARGUMENT")

    @pytest.mark.parametrize("top_k", [0, 11, -1])
    def test_top_k_out_of_range(self, client: TestClient, top_k: int) -> None:
        response = client.post("/runs", json={"question": ANSWERABLE_QUESTION, "top_k": top_k})

        assert response.status_code == 400
        _assert_error_shape(response.json())


class TestCreateRunNoSecretLeak:
    """契约 SECURITY：响应与日志不得泄露密钥。"""

    def test_api_key_not_echoed(self, client: TestClient, monkeypatch) -> None:
        from app.core.config import get_settings

        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        monkeypatch.setenv("LLM_API_KEY", secret)
        # 保持 fake provider（真的用 openai 会需要网络）
        get_settings.cache_clear()

        response = client.post("/runs", json={"question": ANSWERABLE_QUESTION})

        assert secret not in response.text
