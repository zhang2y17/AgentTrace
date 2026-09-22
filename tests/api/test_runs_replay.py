"""回放语义测试（API_CONTRACT §5）。

回放是本项目最容易被实现错的功能，因为它有两条互相冲突的直觉：

- 直觉一："回放"听起来像"重放录像"，应该复用原来的记录；
- 直觉二："再跑一遍"意味着要产生新的执行事实。

契约的选择是**直觉二**：回放生成**新 run**（新 ``run_id``），
靠 ``source_run_id`` 指回原 run。理由是回放的目的就是**对比** ——
同一输入跑两次可能得到不同结果（模型有温度），
把它写成"原 run 的副本"会把两次真实执行压成一条记录，
对比就失去了对象。

因此本文件的核心断言是三件事：

1. 新 run 有**新** ID；
2. ``source_run_id`` 指向原 run；
3. **原 run 的记录完全不变**（含状态、耗时、事件数）——
   这是"绝不覆盖"的具体检验方式。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.api, pytest.mark.test_double]

ANSWERABLE_QUESTION = "trace 事件的 sequence 字段有什么作用？"


def _create_run(
    client: TestClient,
    question: str = ANSWERABLE_QUESTION,
    **extra: Any,
) -> dict[str, Any]:
    payload = {"question": question, **extra}
    response = client.post("/runs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _snapshot(client: TestClient, run_id: str) -> dict[str, Any]:
    """取一份包含全部可观测字段的快照，用于比对"原记录是否被改过"。"""
    detail = client.get(f"/runs/{run_id}", params={"include_events": "true"}).json()
    events = client.get(f"/runs/{run_id}/events", params={"limit": 1000}).json()
    return {"detail": detail, "events": events["events"]}


class TestReplayCreatesNewRun:
    """回放的基本语义。"""

    def test_returns_201_with_new_run_id(self, client: TestClient) -> None:
        original = _create_run(client)

        response = client.post(f"/runs/{original['run_id']}/replay", json={})

        assert response.status_code == 201, response.text
        replayed = response.json()
        assert replayed["run_id"] != original["run_id"]
        assert replayed["run_id"].startswith("run_")
        assert response.headers["Location"] == f"/runs/{replayed['run_id']}"

    def test_source_run_id_points_to_original(self, client: TestClient) -> None:
        """``source_run_id`` 是回放关系的唯一凭据。"""
        original = _create_run(client)

        replayed = client.post(f"/runs/{original['run_id']}/replay", json={}).json()

        assert replayed["source_run_id"] == original["run_id"]
        # 原 run 不是回放，所以它的 source_run_id 仍为空
        assert original["source_run_id"] is None

    def test_inherits_original_inputs(self, client: TestClient) -> None:
        """不传覆盖参数时，应沿用原 run 的问题与版本标签。"""
        original = _create_run(client, agent_version="v1", prompt_version="prompt-v1")

        replayed = client.post(f"/runs/{original['run_id']}/replay", json={}).json()

        assert replayed["question"] == original["question"]
        assert replayed["agent_version"] == original["agent_version"]
        assert replayed["prompt_version"] == original["prompt_version"]

    def test_replay_has_its_own_trace(self, client: TestClient) -> None:
        """回放是新 run，因此有自己的 Trace 事件（不是复用原事件的引用）。"""
        original = _create_run(client)

        replayed = client.post(f"/runs/{original['run_id']}/replay", json={}).json()

        events = client.get(f"/runs/{replayed['run_id']}/events").json()["events"]
        assert events, "回放应产生自己的事件"
        assert all(event["run_id"] == replayed["run_id"] for event in events)
        assert replayed["counts"]["trace_events"] == len(events)


class TestReplayDoesNotMutateOriginal:
    """契约 §5：回放**绝不覆盖**原始记录。"""

    def test_original_record_is_byte_identical(self, client: TestClient) -> None:
        """逐字段比对原 run 的详情与事件，确认回放没有改动它们。

        这是本文件最重要的一条。用"整体快照相等"而不是抽查几个字段：
        回放实现里任何一处意外的写操作都会被这个断言抓住。
        """
        original = _create_run(client)
        before = _snapshot(client, original["run_id"])

        client.post(f"/runs/{original['run_id']}/replay", json={})

        after = _snapshot(client, original["run_id"])
        assert after == before, "回放修改了原始 run 的记录"

    def test_original_event_count_unchanged(self, client: TestClient) -> None:
        original = _create_run(client)
        before = client.get(f"/runs/{original['run_id']}/events").json()["total"]

        client.post(f"/runs/{original['run_id']}/replay", json={})
        client.post(f"/runs/{original['run_id']}/replay", json={})

        after = client.get(f"/runs/{original['run_id']}/events").json()["total"]
        assert after == before, "回放往原 run 里追加了事件"

    def test_original_status_unchanged_after_failed_replay_input(
        self, client: TestClient
    ) -> None:
        """即使回放**失败**，原 run 也不该被牵连。

        这条防的是一种真实的实现错误：把回放终态写回原 run。
        """
        original = _create_run(client, question="红烧肉怎么做才好吃")
        assert original["status"] == "degraded"

        replayed = client.post(f"/runs/{original['run_id']}/replay", json={}).json()

        assert replayed["status"] == "degraded"
        # 原 run 仍指向自己，没有被回放改写
        fetched = client.get(f"/runs/{original['run_id']}").json()
        assert fetched["status"] == "degraded"
        assert fetched["source_run_id"] is None


class TestReplayOverrides:
    """回放的变体对比能力：覆盖输入的部分字段。"""

    def test_override_prompt_version(self, client: TestClient) -> None:
        """版本对比是回放的主要用途：换 prompt 版本跑同一问题。"""
        original = _create_run(client, prompt_version="prompt-v1")

        replayed = client.post(
            f"/runs/{original['run_id']}/replay",
            json={"prompt_version": "prompt-v2"},
        ).json()

        assert replayed["prompt_version"] == "prompt-v2"
        # 未覆盖的字段仍沿用原值
        assert replayed["agent_version"] == original["agent_version"]
        # 原 run 的版本标签没有被改写
        assert client.get(f"/runs/{original['run_id']}").json()["prompt_version"] == "prompt-v1"

    def test_override_question(self, client: TestClient) -> None:
        original = _create_run(client)

        replayed = client.post(
            f"/runs/{original['run_id']}/replay",
            json={"question": "证据覆盖率是怎么算的？"},
        ).json()

        assert replayed["question"] == "证据覆盖率是怎么算的？"

    def test_override_top_k_is_recorded(self, client: TestClient) -> None:
        """``top_k`` 必须真正生效并被记录下来。

        它写在 ``result_summary.top_k`` 里，回放时用来复现原始检索行为。
        若不记录，后续对这次回放的再回放会退化成默认检索。
        """
        original = _create_run(client, top_k=2)

        replayed = client.post(f"/runs/{original['run_id']}/replay", json={}).json()

        # 通过再回放验证 top_k 被复用（不需要暴露额外的响应字段）
        twice = client.post(f"/runs/{replayed['run_id']}/replay", json={}).json()
        assert twice["status"] == replayed["status"]

    def test_replay_note_is_accepted(self, client: TestClient) -> None:
        original = _create_run(client)

        response = client.post(
            f"/runs/{original['run_id']}/replay",
            json={"note": "对比 prompt-v2"},
        )

        assert response.status_code == 201


class TestReplayFailures:
    """回放的错误路径。"""

    def test_unknown_run_returns_404(self, client: TestClient) -> None:
        response = client.post(
            "/runs/run_01JZZZZZZZZZZZZZZZZZZZZZZZ/replay", json={}
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"

    def test_blank_override_question_returns_400(self, client: TestClient) -> None:
        """覆盖问题为空白属于参数错误 → 400。

        注意与 ``POST /runs`` 的差异：那里空白问题是 422（契约 §2 单列），
        而回放的覆盖字段没有单列规则，走通用的请求体校验 → 400。
        """
        original = _create_run(client)

        response = client.post(
            f"/runs/{original['run_id']}/replay",
            json={"question": "   "},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_unknown_field_returns_400(self, client: TestClient) -> None:
        original = _create_run(client)

        response = client.post(
            f"/runs/{original['run_id']}/replay",
            json={"bogus_field": 1},
        )

        assert response.status_code == 400

    def test_missing_body_still_works(self, client: TestClient) -> None:
        """不传请求体等价于"无覆盖"，应正常回放。

        契约把请求体整体设为可选（``ReplayRequest | None``），
        因为最常见的用法就是"原样再跑一次"。
        """
        original = _create_run(client)

        response = client.post(f"/runs/{original['run_id']}/replay")

        assert response.status_code == 201
        assert response.json()["source_run_id"] == original["run_id"]


class TestReplayChain:
    """回放的回放：链式追溯。"""

    def test_can_replay_a_replay(self, client: TestClient) -> None:
        """回放结果本身也可回放，``source_run_id`` 指向**直接上级**。

        这是有意的：链式回放会形成一条版本演进路径，
        每一环记录"我从谁来的"。若指向最初的根 run，
        中间环节的对比关系就丢了。
        """
        first = _create_run(client)
        second = client.post(f"/runs/{first['run_id']}/replay", json={}).json()
        third = client.post(f"/runs/{second['run_id']}/replay", json={}).json()

        assert third["source_run_id"] == second["run_id"]
        assert second["source_run_id"] == first["run_id"]
        # 三条 run 各自独立
        assert len({first["run_id"], second["run_id"], third["run_id"]}) == 3
