"""端到端冒烟测试：一次完整的 HTTP 生命周期（IMPLEMENTATION_PLAN §5 第 6 项）。

与前几个文件不同，本文件的视角是**使用者**而不是**实现者**：
它按 README 里写的顺序，用 HTTP 走完一条完整路径，
验证"这套东西作为一个产品能不能用"。

流程：

1. ``GET /health`` —— 服务活着（含依赖状态，允许 degraded）；
2. ``POST /runs`` —— 跑一次 Agent，拿到 run_id 与 Trace 计数；
3. ``GET /runs/{id}`` —— 详情与创建时一致（说明真的落库了）；
4. ``GET /runs/{id}/events`` —— 事件齐备且成树；
5. ``POST /runs/{id}/replay`` —— 回放产生新 run，原 run 不变；
6. ``GET /metrics/summary`` —— 指标把两次运行都算进去了。

这里**不**断言具体耗时或 token 数 —— 那些随实现细节变化，
把它们写死会让冒烟测试变成脆弱的回归测试。
冒烟测试要回答的是"链路通不通"，不是"数值对不对"。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.smoke, pytest.mark.test_double]

QUESTION = "trace 事件的 sequence 字段有什么作用？"


def _create_run(client: TestClient, question: str = QUESTION) -> dict[str, Any]:
    response = client.post("/runs", json={"question": question})
    assert response.status_code == 201, response.text
    return response.json()


def test_full_http_lifecycle(client: TestClient) -> None:
    """一次完整闭环：健康检查 → 运行 → 查询 → 事件 → 回放 → 指标。"""

    # ---------------------------------------------------------------- 1. 健康
    health = client.get("/health")
    assert health.status_code == 200, health.text
    health_body = health.json()
    # Redis 未启动时整体为 degraded，这是被允许的（非必需依赖）
    assert health_body["status"] in {"ok", "degraded"}
    assert health_body["components"]["api"]["status"] == "ok"

    # ---------------------------------------------------------------- 2. 运行
    created = _create_run(client)
    run_id = created["run_id"]

    assert created["status"] == "succeeded"
    assert created["is_test_double"] is True
    assert created["counts"]["trace_events"] > 0, "Trace 必须真的有内容"

    # ---------------------------------------------------------------- 3. 详情
    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    # 详情来自持久化行，因此与创建时逐字段一致
    assert detail_body["run_id"] == run_id
    assert detail_body["counts"] == created["counts"]
    assert detail_body["status"] == created["status"]
    assert detail_body["result_summary"]["citations"] == (created["result_summary"]["citations"])

    # ---------------------------------------------------------------- 4. 事件
    events_response = client.get(f"/runs/{run_id}/events", params={"limit": 1000})
    assert events_response.status_code == 200
    events_body = events_response.json()
    events = events_body["events"]

    assert events_body["total"] == created["counts"]["trace_events"]
    assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)

    # 契约 TRACE_SCHEMA §5 的树形结构：run 根 → 5 node → final_result
    by_id = {event["event_id"]: event for event in events}
    types = [event["event_type"] for event in events]
    assert "run" in types, "缺少 run 根事件"
    assert types.count("node") == 5, f"应有 5 个节点事件，实际 {types.count('node')}"
    assert types.count("final_result") == 1

    for event in events:
        parent_id = event["parent_event_id"]
        if event["event_type"] in {"tool_call", "model_call"}:
            parent = by_id.get(parent_id or "")
            assert parent is not None, f"{event['name']} 没有父事件"
            assert parent["event_type"] == "node", "工具/模型调用必须挂在 node 下"

    # 过滤也要能用
    tool_events = client.get(f"/runs/{run_id}/events", params={"event_type": "tool_call"}).json()
    assert tool_events["count"] >= 1

    # ---------------------------------------------------------------- 5. 回放
    replayed = client.post(f"/runs/{run_id}/replay", json={"note": "冒烟回放"})
    assert replayed.status_code == 201, replayed.text
    replayed_body = replayed.json()

    assert replayed_body["run_id"] != run_id
    assert replayed_body["source_run_id"] == run_id
    assert replayed_body["status"] == created["status"]

    # 原 run 未被改动
    after = client.get(f"/runs/{run_id}").json()
    assert after["counts"] == created["counts"]
    assert after["source_run_id"] is None
    assert after["result_summary"] == created["result_summary"]

    # ---------------------------------------------------------------- 6. 指标
    metrics_response = client.get("/metrics/summary")
    assert metrics_response.status_code == 200
    metrics_body = metrics_response.json()

    # 两次运行都算进去了
    assert metrics_body["run_count"] == 2
    assert metrics_body["scope"] == "sample_runs"
    assert metrics_body["metrics"]["case_count"] == 2
    assert metrics_body["data_source_note"]

    # 分组可用
    grouped = client.get("/metrics/summary", params={"group_by": "day"}).json()
    assert grouped["groups"]
    assert sum(group["run_count"] for group in grouped["groups"]) == 2


def test_degrades_gracefully_on_unrelated_question(client: TestClient) -> None:
    """无关问题走完整链路后应落 ``degraded``，而不是让平台报错。

    这是"诚实地承认没有依据"这条产品原则的端到端检验：
    平台的价值在于把这种状态**记录清楚**，而不是假装成功。
    """
    created = _create_run(client, "红烧肉怎么做才好吃")

    assert created["status"] == "degraded"
    assert created["result_summary"]["evidence_sufficient"] is False

    # 降级 run 的 Trace 仍然完整（可观测性不因降级而缺失）
    events = client.get(f"/runs/{created['run_id']}/events").json()
    assert events["total"] > 0

    # 且它不计入错误率
    metrics = client.get("/metrics/summary").json()["metrics"]
    assert metrics["error_rate"] == 0.0
    assert metrics["degraded_rate"] == 1.0


def test_openapi_document_is_available(client: TestClient) -> None:
    """OpenAPI 文档可用，且描述了全部 6 个已实现端点。

    契约 C-06 依赖 OpenAPI 来校验端点齐备，因此这里顺带守一道：
    如果文档生成坏了，契约校验会给出难以定位的报错。
    """
    spec = client.get("/openapi.json").json()

    paths = spec["paths"]
    for expected in (
        "/health",
        "/runs",
        "/runs/{run_id}",
        "/runs/{run_id}/events",
        "/runs/{run_id}/replay",
        "/metrics/summary",
    ):
        assert expected in paths, f"OpenAPI 缺少 {expected}"

    # POST /runs 的请求体必须是 CreateRunRequest，不能夹带内部依赖字段。
    # 这是为了守住一个真实踩过的坑：把 Pydantic 模型（Settings）
    # 或服务类写进路由签名，会让它们凭空变成请求体/查询参数。
    body_schema = paths["/runs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "CreateRunRequest" in body_schema["$ref"]

    # /metrics/summary 的查询参数必须正好是契约里的那 6 个
    param_names = {param["name"] for param in paths["/metrics/summary"]["get"]["parameters"]}
    assert param_names == {
        "started_after",
        "started_before",
        "agent_version",
        "prompt_version",
        "model_name",
        "group_by",
    }, f"多出或缺少查询参数：{param_names}"


def test_no_secret_leaks_across_endpoints(client: TestClient, monkeypatch) -> None:
    """整条链路都不应回显密钥（契约 SECURITY）。

    在一个真实密钥已配置的环境里跑一遍全部只读端点。
    """
    from app.core.config import get_settings

    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setenv("LLM_API_KEY", secret)
    get_settings.cache_clear()

    created = _create_run(client)
    run_id = created["run_id"]

    for path in (
        "/health",
        "/openapi.json",
        f"/runs/{run_id}",
        f"/runs/{run_id}/events",
        "/metrics/summary",
    ):
        response = client.get(path)
        assert secret not in response.text, f"{path} 泄露了密钥"
        assert "abcdefghijklmnopqrstuvwxyz0123456789" not in response.text
