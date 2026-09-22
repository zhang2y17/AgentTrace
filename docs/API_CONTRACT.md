# API_CONTRACT.md — AgentTrace API 契约

Base URL（本地）：`http://localhost:8000`
OpenAPI 文档：`GET /docs`（Swagger UI）、`GET /openapi.json`

## 0. 通用约定

### 0.1 统一错误响应

所有非 2xx 响应体：

```json
{
  "error": {
    "code": "RUN_NOT_FOUND",
    "message": "Run 'run_xxx' was not found.",
    "details": {"run_id": "run_xxx"},
    "request_id": "req_01J8ZQ4K..."
  }
}
```

| HTTP | code | 场景 |
|---|---|---|
| 400 | `INVALID_ARGUMENT` | 请求体校验失败、字段超长 |
| 404 | `RUN_NOT_FOUND` | run_id 不存在 |
| 404 | `EVALUATION_NOT_FOUND` | evaluation_id 不存在 |
| 409 | `RUN_NOT_REPLAYABLE` | run 状态为 `running`/`pending`，无法回放 |
| 422 | `AGENT_VALIDATION_ERROR` | 业务语义非法（如 question 全为空白） |
| 500 | `TRACE_WRITE_FAILED` | Trace 落库失败 |
| 500 | `INTERNAL_ERROR` | 未归类异常 |
| 502 | `LLM_PROVIDER_ERROR` | 真实 LLM 调用失败 |
| 504 | `AGENT_TIMEOUT` | 超过 `AGENT_TIMEOUT_SECONDS` |

### 0.2 时间格式

所有时间为 ISO 8601 UTC，带 `Z` 后缀，毫秒精度：`2026-09-22T04:00:00.412Z`。

### 0.3 测试替身标注

响应中凡涉及模型调用的对象都带 `is_test_double: boolean`。当 `LLM_PROVIDER=fake` 时为 `true`，表示数值由本地替身产生，不是真实模型结果。

---

## 1. `GET /health`

检查 API 与各依赖组件状态。**不返回任何密钥信息。**

**响应 200**

```json
{
  "status": "ok",
  "service": "agenttrace",
  "version": "0.1.0",
  "components": {
    "api": {"status": "ok", "latency_ms": 0},
    "database": {"status": "ok", "latency_ms": 3, "dialect": "postgresql"},
    "redis": {"status": "ok", "latency_ms": 1},
    "llm_provider": {"status": "ok", "provider": "fake", "is_test_double": true}
  },
  "checked_at": "2026-09-22T04:00:00.000Z"
}
```

**降级行为**：任一组件不可用时 HTTP 仍为 200，但 `status` 变为 `degraded`，对应组件 `status` 为 `error` 并带 `error` 字段（已脱敏）。

`status` 取值：`ok`（全部正常）/ `degraded`（部分异常）。

---

## 2. `POST /runs`

提交并同步执行一次 Agent 运行。

**请求体**

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `question` | `string` | 是 | 1 ~ `MAX_QUESTION_CHARS`（默认 2000），不允许全空白 |
| `agent_version` | `string` | 否 | 默认 `v1`，最长 40 |
| `prompt_version` | `string` | 否 | 默认 `prompt-v1`，最长 40 |
| `top_k` | `int` | 否 | 1 ~ 10，默认 3 |
| `metadata` | `object` | 否 | 自由标签，仅存于运行上下文 |

```json
{
  "question": "AgentTrace 如何记录一次工具调用？",
  "agent_version": "v1",
  "prompt_version": "prompt-v1",
  "top_k": 3
}
```

**响应 201**

```json
{
  "run_id": "run_01J8ZQ4K7X2M9P3T5V6W8Y0B1C",
  "status": "succeeded",
  "source_run_id": null,
  "question": "AgentTrace 如何记录一次工具调用？",
  "agent_version": "v1",
  "prompt_version": "prompt-v1",
  "llm_provider": "fake",
  "model_name": "fake-model",
  "is_test_double": true,
  "duration_ms": 412,
  "total_tokens": 340,
  "estimated_cost_usd": 0.000187,
  "started_at": "2026-09-22T04:00:00.000Z",
  "ended_at": "2026-09-22T04:00:00.412Z",
  "error_code": null,
  "error_message": null,
  "result_summary": {
    "answer": "AgentTrace 通过 TraceMiddleware 在工具调用前后各写一条 trace_event……[citations: doc-trace-schema, doc-architecture]",
    "answer_chars": 486,
    "citations": ["doc-trace-schema", "doc-architecture"],
    "evidence_sufficient": true,
    "final_status": "succeeded"
  },
  "counts": {"trace_events": 9, "tool_calls": 1, "model_calls": 2}
}
```

**`status` 取值**：`succeeded` / `failed` / `degraded` / `timeout`。

- `succeeded`：全部节点正常，答案通过校验；
- `degraded`：流程完成但证据不足或校验有非致命问题（**不视为成功**，`final_result` 中体现）；
- `failed`：节点或工具失败且无法继续；
- `timeout`：超过 `AGENT_TIMEOUT_SECONDS`。

> **同步执行说明**：`POST /runs` 同步等待 Agent 完成，最长 `AGENT_TIMEOUT_SECONDS`。超时返回 504 且 run 状态落库为 `timeout`。异步化不在本版本范围（见 IMPLEMENTATION_PLAN.md 的未完成项）。

**错误**

| 场景 | HTTP | code |
|---|---|---|
| `question` 为空/全空白 | 422 | `AGENT_VALIDATION_ERROR` |
| `question` 超长 | 400 | `INVALID_ARGUMENT` |
| `top_k` 越界 | 400 | `INVALID_ARGUMENT` |
| Agent 超时 | 504 | `AGENT_TIMEOUT` |
| Trace 写入失败 | 500 | `TRACE_WRITE_FAILED` |

---

## 3. `GET /runs/{run_id}`

查询单次运行详情。

**路径参数**：`run_id`（`run_` 前缀）

**响应 200**：与 `POST /runs` 响应结构相同（`RunDetail`）。

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `include_events` | `bool` | `false` | 为 `true` 时内联 `events` 数组 |

**错误**：404 `RUN_NOT_FOUND`

---

## 4. `GET /runs/{run_id}/events`

查询运行的全部 Trace 事件，按 `sequence` 升序。

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `event_type` | `string` | 无 | 逗号分隔，如 `node,tool_call`；取值见 TRACE_SCHEMA §2 |
| `status` | `string` | 无 | 逗号分隔，如 `failed,retried`；取值见 TRACE_SCHEMA §4 |
| `limit` | `int` | `200` | 1 ~ 1000 |
| `offset` | `int` | `0` | ≥ 0 |

**响应 200**

```json
{
  "run_id": "run_01J8ZQ4K7X2M9P3T5V6W8Y0B1C",
  "count": 9,
  "total": 9,
  "limit": 200,
  "offset": 0,
  "filters": {"event_type": ["node"], "status": ["failed"]},
  "events": [
    {
      "event_id": "evt_01J8ZQ4K800000000000000002",
      "run_id": "run_01J8ZQ4K7X2M9P3T5V6W8Y0B1C",
      "parent_event_id": "evt_01J8ZQ4K800000000000000001",
      "event_type": "node",
      "name": "question_parser",
      "status": "ok",
      "sequence": 2,
      "started_at": "2026-09-22T04:00:00.010Z",
      "ended_at": "2026-09-22T04:00:00.098Z",
      "duration_ms": 88,
      "input_summary": "{\"question\":\"...\"}",
      "output_summary": "{\"intent\":\"explain\"}",
      "error_code": null,
      "attributes": {"node_index": 1}
    }
  ]
}
```

**错误**

| 场景 | HTTP | code |
|---|---|---|
| run 不存在 | 404 | `RUN_NOT_FOUND` |
| `event_type` 含非法值 | 400 | `INVALID_ARGUMENT` |
| `status` 含非法值 | 400 | `INVALID_ARGUMENT` |
| `limit` 越界 | 400 | `INVALID_ARGUMENT` |

---

## 5. `POST /runs/{run_id}/replay`

以原始运行的输入重新执行一次，**生成新的 `run_id`，并保留 `source_run_id`**。绝不覆盖原始记录。

**请求体**（全部可选，用于覆盖原始输入以做变体对比）

| 字段 | 类型 | 说明 |
|---|---|---|
| `question` | `string` | 覆盖原问题；不传则用原值 |
| `agent_version` | `string` | 覆盖 Agent 版本，用于版本对比 |
| `prompt_version` | `string` | 覆盖 Prompt 版本 |
| `top_k` | `int` | 覆盖检索条数 |
| `note` | `string` | 回放备注，最长 200 |

```json
{
  "prompt_version": "prompt-v2",
  "note": "对比 prompt-v2 的工具选择准确率"
}
```

**响应 201**：`RunDetail` 结构，其中：
- `run_id` 为**新** ID；
- `source_run_id` 为原始 run_id；
- 其余字段为本次回放的实测值。

**错误**

| 场景 | HTTP | code |
|---|---|---|
| 原 run 不存在 | 404 | `RUN_NOT_FOUND` |
| 原 run 仍在 `running`/`pending` | 409 | `RUN_NOT_REPLAYABLE` |
| 覆盖字段非法 | 400 | `INVALID_ARGUMENT` |

**幂等性**：非幂等。每次调用产生一个新 run，这是有意设计（回放需要多次独立采样）。

---

## 6. `POST /evaluations`

对指定评测集执行一次评测，返回批次 ID 与指标。

**请求体**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `dataset_version` | `string` | 是 | — | 如 `doc_research_v1`，须与 `data/eval/*.jsonl` 匹配 |
| `agent_version` | `string` | 否 | `v1` | |
| `prompt_version` | `string` | 否 | `prompt-v1` | |
| `case_keys` | `string[]` | 否 | 全部 | 只跑指定用例，便于快速验证 |
| `thresholds` | `object` | 否 | 见 §6.4 | 阈值覆盖，用于本次门禁判断 |

```json
{
  "dataset_version": "doc_research_v1",
  "agent_version": "v1",
  "prompt_version": "prompt-v1"
}
```

**响应 201**

```json
{
  "evaluation_id": "eval_01J8ZQ4K900000000000000001",
  "dataset_version": "doc_research_v1",
  "agent_version": "v1",
  "prompt_version": "prompt-v1",
  "model_name": "fake-model",
  "llm_provider": "fake",
  "is_test_double": true,
  "status": "succeeded",
  "case_count": 12,
  "passed_cases": 10,
  "failed_cases": 2,
  "metrics": {
    "run_success_rate": 0.8333,
    "task_completion_rate": 0.8333,
    "tool_selection_accuracy": 0.9167,
    "tool_argument_accuracy": 0.8333,
    "evidence_coverage": 0.7222,
    "latency_ms_p50": 402,
    "latency_ms_p95": 588,
    "latency_ms_mean": 431.5,
    "total_tokens": 4080,
    "estimated_cost_usd": 0.002244,
    "error_rate": 0.1667,
    "human_review_rate": 0.0
  },
  "started_at": "2026-09-22T04:10:00.000Z",
  "ended_at": "2026-09-22T04:10:06.100Z",
  "duration_ms": 6100
}
```

> 上述数值为**离线评测结果（测试替身）**，不代表真实模型或线上表现。

**错误**

| 场景 | HTTP | code |
|---|---|---|
| 评测集不存在 | 404 | `EVALUATION_NOT_FOUND` |
| `case_keys` 含未知用例 | 400 | `INVALID_ARGUMENT` |
| 评测集内存在无法通过校验的用例 | 400 | `INVALID_ARGUMENT` |

---

## 7. `GET /evaluations/{evaluation_id}`

查询评测结果。

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `include_cases` | `bool` | `false` | 内联逐 case 结果 |
| `only_failures` | `bool` | `false` | 仅返回失败 case（需 `include_cases=true`） |

**响应 200**：`POST /evaluations` 响应 + 可选 `cases` 数组。

```json
{
  "evaluation_id": "eval_01J8ZQ4K900000000000000001",
  "metrics": { "...": "同 POST /evaluations" },
  "cases": [
    {
      "case_key": "doc-003",
      "run_id": "run_01J8ZQ4K9B...",
      "status": "failed",
      "task_completed": false,
      "tool_selection_correct": true,
      "tool_argument_correct": false,
      "evidence_coverage": 0.25,
      "latency_ms": 455,
      "total_tokens": 310,
      "estimated_cost_usd": 0.000171,
      "failure_reason": "tool_argument_mismatch: expected top_k=5, got top_k=3",
      "assertion_results": [
        {"assertion": "contains_citation", "passed": true},
        {"assertion": "min_citations>=2", "passed": false, "detail": "found 1"}
      ]
    }
  ]
}
```

**错误**：404 `EVALUATION_NOT_FOUND`

---

## 8. `GET /metrics/summary`

汇总运行级指标。**数据来源必须明确标注。**

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `started_after` | `datetime` | 无 | ISO 8601，按 `run.started_at` 过滤 |
| `started_before` | `datetime` | 无 | 同上 |
| `agent_version` | `string` | 无 | 过滤 |
| `prompt_version` | `string` | 无 | 过滤 |
| `model_name` | `string` | 无 | 过滤 |
| `group_by` | `string` | 无 | `agent_version` / `prompt_version` / `model_name` / `day` |

**响应 200**

```json
{
  "scope": "sample_runs",
  "data_source_note": "基于本实例已记录的运行结果（样例运行 / 离线评测），不代表任何线上流量或生产环境表现。",
  "filters": {"agent_version": null, "prompt_version": null, "model_name": null},
  "run_count": 5,
  "metrics": {
    "run_success_rate": 0.8,
    "task_completion_rate": 0.8,
    "error_rate": 0.2,
    "human_review_rate": 0.0,
    "latency_ms_p50": 402,
    "latency_ms_p95": 588,
    "latency_ms_mean": 431.5,
    "total_tokens": 1700,
    "estimated_cost_usd": 0.000935
  },
  "tools": {
    "search_documents": {"calls": 6, "ok": 5, "invalid_arguments": 1, "error": 0, "mean_duration_ms": 41.2},
    "get_document": {"calls": 1, "ok": 1, "invalid_arguments": 0, "error": 0, "mean_duration_ms": 2.1}
  },
  "groups": []
}
```

`scope` 取值：
- `sample_runs`：仅有零散运行记录，属于样例运行结果；
- `offline_evaluation`：数据来自指定评测批次，属于离线评测结果；
- `empty`：无数据，所有指标为 `null`。

**空数据行为**：`run_count = 0` 时，所有指标返回 `null` 而非 `0`，避免把"没跑过"误读为"成功率 0"。

---

## 9. `POST /quality-gates/check`

按阈值检查评测结果，**指标低于阈值时返回失败并阻断**。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `evaluation_id` | `string` | 是 | 被检查的评测批次 |
| `gate_name` | `string` | 否 | 默认 `release-gate` |
| `thresholds` | `object` | 否 | 覆盖默认阈值 |

```json
{
  "evaluation_id": "eval_01J8ZQ4K900000000000000001",
  "gate_name": "release-gate",
  "thresholds": {
    "run_success_rate": {"min": 0.9},
    "tool_selection_accuracy": {"min": 0.9},
    "tool_argument_accuracy": {"min": 0.85},
    "latency_ms_p95": {"max": 2000},
    "estimated_cost_usd": {"max": 0.05},
    "error_rate": {"max": 0.05}
  }
}
```

**阈值运算符**

| 指标类型 | 支持运算符 | 说明 |
|---|---|---|
| 比率类（`*_rate`, `*_accuracy`, `*_coverage`） | `min` | 越大越好，低于 `min` 即违规 |
| 延迟/成本/错误率类 | `max` | 越小越好，高于 `max` 即违规 |

**响应 200（无论通过与否，都返回 200，用 `passed` 表示结果）**

```json
{
  "gate_id": "gate_01J8ZQ4KA00000000000000001",
  "gate_name": "release-gate",
  "evaluation_id": "eval_01J8ZQ4K900000000000000001",
  "passed": false,
  "blocked": true,
  "status": "failed",
  "observed_metrics": {
    "run_success_rate": 0.8333,
    "tool_selection_accuracy": 0.9167,
    "tool_argument_accuracy": 0.8333,
    "latency_ms_p95": 588,
    "estimated_cost_usd": 0.002244,
    "error_rate": 0.1667
  },
  "violations": [
    {
      "metric": "run_success_rate",
      "operator": "min",
      "threshold": 0.9,
      "observed": 0.8333,
      "reason": "run_success_rate 0.8333 低于阈值 0.9"
    },
    {
      "metric": "error_rate",
      "operator": "max",
      "threshold": 0.05,
      "observed": 0.1667,
      "reason": "error_rate 0.1667 高于阈值 0.05（越低越好）"
    }
  ],
  "checked_at": "2026-09-22T04:12:00.000Z",
  "data_source_note": "阈值判定基于离线评测结果，通过门禁仅表示满足本项目定义的离线质量基线。"
}
```

**`blocked` 语义**：`blocked = true` 表示**该版本不应通过门禁**。CI 或本地脚本应据此返回非 0 退出码。

**判定规则**：`passed = (len(violations) == 0)`。

**错误**

| 场景 | HTTP | code |
|---|---|---|
| 评测不存在 | 404 | `EVALUATION_NOT_FOUND` |
| 阈值含未知指标名 | 400 | `INVALID_ARGUMENT` |
| 阈值运算符与该指标类型不匹配 | 400 | `INVALID_ARGUMENT` |

---

## 10. `GET /docs`

FastAPI 自动生成的 Swagger UI。附带 `GET /openapi.json`。

---

## 11. 完整调用示例（curl）

```bash
# 1. 健康检查
curl -s http://localhost:8000/health

# 2. 执行一次运行
curl -s -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"question":"AgentTrace 如何记录一次工具调用？","agent_version":"v1","prompt_version":"prompt-v1","top_k":3}'

# 3. 查询运行详情（含事件）
curl -s "http://localhost:8000/runs/run_xxx?include_events=true"

# 4. 只查失败与重试事件
curl -s "http://localhost:8000/runs/run_xxx/events?status=failed,retried"

# 5. 只查节点事件
curl -s "http://localhost:8000/runs/run_xxx/events?event_type=node"

# 6. 回放（换 prompt 版本）
curl -s -X POST "http://localhost:8000/runs/run_xxx/replay" \
  -H "Content-Type: application/json" \
  -d '{"prompt_version":"prompt-v2","note":"对比 prompt-v2"}'

# 7. 跑一次离线评测
curl -s -X POST http://localhost:8000/evaluations \
  -H "Content-Type: application/json" \
  -d '{"dataset_version":"doc_research_v1","agent_version":"v1","prompt_version":"prompt-v1"}'

# 8. 查看评测失败用例
curl -s "http://localhost:8000/evaluations/eval_xxx?include_cases=true&only_failures=true"

# 9. 指标汇总（按 Agent 版本分组）
curl -s "http://localhost:8000/metrics/summary?group_by=agent_version"

# 10. 质量门禁
curl -s -X POST http://localhost:8000/quality-gates/check \
  -H "Content-Type: application/json" \
  -d '{"evaluation_id":"eval_xxx","gate_name":"release-gate"}'
```

## 12. 版本与兼容性

- API 版本：`0.1.0`（`/health` 的 `version` 字段）
- 当前未做 URL 版本前缀（无 `/v1`）。理由：项目尚在 0.x，契约允许破坏性变更，变更必须同步更新本文件。
- `attributes`、`result_summary`、`metrics` 为可扩展对象，允许新增键，客户端不得依赖键的穷尽性。
