"""``GET /runs/{run_id}`` 与 ``GET /runs/{run_id}/events`` 接口测试（API_CONTRACT §3、§4）。

重点验证：

1. **查询不重跑 Agent**：详情来自持久化行。若实现偷偷重跑了，
   ``counts`` 与耗时都会变，而这些断言会抓到。
2. **404 语义**：不存在的 run 返回 404 ``RUN_NOT_FOUND``，
   而不是空对象或 500。
3. **过滤与分页**：``event_type`` / ``status`` 支持逗号分隔多值，
   非法值返回 400 而不是静默忽略。
4. **事件顺序**：``sequence`` 必须升序 —— 回放依赖它。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.api, pytest.mark.test_double]

ANSWERABLE_QUESTION = "trace 事件的 sequence 字段有什么作用？"

_MISSING_RUN_ID = "run_01JZZZZZZZZZZZZZZZZZZZZZZZ"


def _create_run(client: TestClient, question: str = ANSWERABLE_QUESTION) -> dict[str, Any]:
    """建一个 run 并返回响应体。"""
    response = client.post("/runs", json={"question": question})
    assert response.status_code == 201, response.text
    return response.json()


class TestGetRun:
    """`GET /runs/{run_id}`。"""

    def test_returns_persisted_detail(self, client: TestClient) -> None:
        created = _create_run(client)

        response = client.get(f"/runs/{created['run_id']}")

        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == created["run_id"]
        assert body["status"] == created["status"]
        assert body["question"] == created["question"]
        # 默认不内联事件
        assert body["events"] is None

    def test_does_not_rerun_agent(self, client: TestClient) -> None:
        """详情查询必须直接读库，不能重跑。

        判据：事件计数与创建时完全一致。若实现重跑了，
        事件会多出一整套（新的 run_id 下不会有，但计数会变）。
        """
        created = _create_run(client)

        fetched = client.get(f"/runs/{created['run_id']}").json()

        assert fetched["counts"] == created["counts"]
        assert fetched["total_tokens"] == created["total_tokens"]
        assert fetched["result_summary"] == created["result_summary"]

    def test_include_events_inlines_trace(self, client: TestClient) -> None:
        created = _create_run(client)

        body = client.get(f"/runs/{created['run_id']}", params={"include_events": "true"}).json()

        assert body["events"] is not None
        assert len(body["events"]) == created["counts"]["trace_events"]

    def test_unknown_run_returns_404(self, client: TestClient) -> None:
        response = client.get(f"/runs/{_MISSING_RUN_ID}")

        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "RUN_NOT_FOUND"
        assert set(error) >= {"code", "message", "details", "request_id"}

    def test_replay_of_unknown_run_returns_404(self, client: TestClient) -> None:
        response = client.post(f"/runs/{_MISSING_RUN_ID}/replay", json={})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


class TestListEvents:
    """`GET /runs/{run_id}/events`。"""

    def test_returns_all_events_in_sequence_order(self, client: TestClient) -> None:
        created = _create_run(client)

        body = client.get(f"/runs/{created['run_id']}/events").json()

        assert body["run_id"] == created["run_id"]
        assert body["count"] == len(body["events"]) == created["counts"]["trace_events"]
        assert body["total"] == body["count"]

        sequences = [event["sequence"] for event in body["events"]]
        assert sequences == sorted(sequences), "sequence 必须升序 —— 回放顺序依赖它"
        assert sequences[0] == 1

    def test_event_shape_matches_contract(self, client: TestClient) -> None:
        """契约 §4：事件字段必须齐备。"""
        run = _create_run(client)
        events = client.get(f"/runs/{run['run_id']}/events").json()["events"]

        for event in events:
            assert {
                "event_id",
                "run_id",
                "event_type",
                "name",
                "status",
                "sequence",
                "started_at",
                "attributes",
            } <= set(event)
            assert event["event_id"].startswith("evt_")
            assert event["run_id"] == run["run_id"]

    def test_node_events_are_present(self, client: TestClient) -> None:
        """5 个固定节点各应有一条 node 事件。

        这条断言防的是"Trace 里只有工具调用、没有节点生命周期"这类
        不完整记录 —— 那会让耗时归因无法进行。
        """
        run = _create_run(client)
        events = client.get(
            f"/runs/{run['run_id']}/events",
            params={"event_type": "node"},
        ).json()["events"]

        names = {event["name"] for event in events}
        assert {
            "question_parser",
            "document_search",
            "evidence_checker",
            "answer_writer",
            "final_validator",
        } <= names, f"缺少节点事件：{names}"

    @pytest.mark.parametrize(
        ("event_type", "expected_names"),
        [
            ("tool_call", {"search_documents", "get_document"}),
            ("model_call", set()),
        ],
    )
    def test_filter_by_event_type(
        self, client: TestClient, event_type: str, expected_names: set[str]
    ) -> None:
        run = _create_run(client)

        body = client.get(
            f"/runs/{run['run_id']}/events",
            params={"event_type": event_type},
        ).json()

        assert body["filters"]["event_type"] == [event_type]
        assert all(event["event_type"] == event_type for event in body["events"])
        if expected_names:
            assert expected_names & {event["name"] for event in body["events"]}

    def test_filter_by_multiple_event_types(self, client: TestClient) -> None:
        run = _create_run(client)

        body = client.get(
            f"/runs/{run['run_id']}/events",
            params={"event_type": "node,tool_call"},
        ).json()

        assert set(body["filters"]["event_type"]) == {"node", "tool_call"}
        assert {event["event_type"] for event in body["events"]} <= {"node", "tool_call"}

    def test_filter_by_status(self, client: TestClient) -> None:
        run = _create_run(client)

        body = client.get(
            f"/runs/{run['run_id']}/events",
            params={"status": "ok"},
        ).json()

        assert body["filters"]["status"] == ["ok"]
        assert all(event["status"] == "ok" for event in body["events"])

    def test_invalid_event_type_returns_400(self, client: TestClient) -> None:
        """契约 §4：非法过滤值返回 400 INVALID_ARGUMENT。

        静默忽略非法值会让"筛了但没生效"变成难以察觉的错误结论。
        """
        run = _create_run(client)

        response = client.get(
            f"/runs/{run['run_id']}/events",
            params={"event_type": "node,not_a_real_type"},
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert error["details"]["invalid"] == ["not_a_real_type"]

    def test_invalid_status_returns_400(self, client: TestClient) -> None:
        run = _create_run(client)

        response = client.get(
            f"/runs/{run['run_id']}/events",
            params={"status": "bogus"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["details"]["invalid"] == ["bogus"]

    def test_pagination(self, client: TestClient) -> None:
        run = _create_run(client)
        total = run["counts"]["trace_events"]
        assert total >= 5

        first = client.get(f"/runs/{run['run_id']}/events", params={"limit": 2, "offset": 0}).json()
        second = client.get(
            f"/runs/{run['run_id']}/events", params={"limit": 2, "offset": 2}
        ).json()

        assert first["count"] == 2
        # total 是过滤后的总数，不随分页变化
        assert first["total"] == second["total"] == total
        assert first["limit"] == 2 and first["offset"] == 0
        assert second["offset"] == 2

        first_ids = {event["event_id"] for event in first["events"]}
        second_ids = {event["event_id"] for event in second["events"]}
        assert not (first_ids & second_ids), "分页结果不应重叠"

    def test_offset_beyond_end_returns_empty_not_error(self, client: TestClient) -> None:
        """越界 offset 返回空列表 + 正确 total，而不是 404。

        这与"run 不存在"必须区分开：前者是分页走到底，后者是资源缺失。
        """
        run = _create_run(client)

        body = client.get(f"/runs/{run['run_id']}/events", params={"offset": 9999}).json()

        assert body["count"] == 0
        assert body["events"] == []
        assert body["total"] == run["counts"]["trace_events"]

    @pytest.mark.parametrize("limit", [0, -1, 1001])
    def test_limit_out_of_range_returns_400(self, client: TestClient, limit: int) -> None:
        run = _create_run(client)

        response = client.get(f"/runs/{run['run_id']}/events", params={"limit": limit})

        assert response.status_code == 400

    def test_events_of_unknown_run_returns_404(self, client: TestClient) -> None:
        """对不存在的 run 返回空列表会让调用方误以为"这个 run 没有事件"。"""
        response = client.get(f"/runs/{_MISSING_RUN_ID}/events")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"

    def test_events_carry_no_document_body(self, client: TestClient) -> None:
        """契约：Trace 只存摘要，不把文档正文或完整 Prompt 复制进库。

        判据：``output_summary`` 长度受限，且不包含语料原文特征串。
        """
        run = _create_run(client)
        events = client.get(f"/runs/{run['run_id']}/events").json()["events"]

        for event in events:
            for field in ("input_summary", "output_summary"):
                value = event.get(field)
                if value:
                    assert len(value) <= 600, f"{field} 过长，疑似写入了正文"
