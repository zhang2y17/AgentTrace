---
document_id: doc-004
title: 工具治理与参数校验
tags: [tools, validation, pydantic, errors]
---

# 工具治理与参数校验

## 四个工具

| 工具名 | 参数 | 返回 | 性质 |
|---|---|---|---|
| `search_documents` | `query: str`, `top_k: int` | `list[DocumentHit]` | 检索，仅访问项目内样例文档 |
| `get_document` | `document_id: str` | `Document` | 读取，仅访问项目内样例文档 |
| `calculate_latency_summary` | `run_id: str` | `LatencySummary` | **确定性代码**，不使用 LLM |
| `calculate_cost_summary` | `run_id: str` | `CostSummary` | **确定性代码**，不使用 LLM |

## 强制约束

### 1. 参数一律经 Pydantic 校验

校验失败必须产生 `tool_call.status = "invalid_arguments"` 的 Trace 记录，
且**不得**把未校验的参数传给工具实现体。

这条约束是可被测试验证的：测试传入 `top_k=999`（超出 `1..10`），
断言两点 —— 状态是 `invalid_arguments`，并且工具实现体**没有被调用**。

为什么"不传给实现体"很重要：如果校验只是记录日志、然后照样执行，
那校验就是摆设。参数校验必须是一道**闸门**，不是一句备注。

### 2. 统计与成本必须由确定性代码计算

阈值、统计和成本计算**不允许让 LLM 猜测数值**。

原因很直接：LLM 生成的数字不可复现，会被波动采样影响，
而成本和延迟是**验收指标**。用不可复现的数值做验收，等于没有验收。

### 3. 工具通过统一注册表发现

工具在 `app/tools/registry.py` 注册。节点里**禁止**硬编码
"工具名 → 函数"的映射。

这条约束的收益是：新增工具不需要改节点代码；测试可以替换注册表内容；
Trace 记录能统一从注册表拿到版本号。

### 4. 每次调用必须双写

每次工具调用必须同时写入：

- `tool_call` 表（结构化字段，便于聚合统计）；
- 一条 `event_type = "tool_call"` 的 `trace_event`（进入事件时间轴，便于回放）。

两者互补：表适合查询聚合，事件流适合还原时序。

## 工具调用的生命周期

```
1. 节点调用 registry.invoke(tool_name, raw_arguments)
2. 注册表写一条 trace_event（status=running）
3. 注册表按 tool_name 取出 ToolSpec
4. 用 ToolSpec 的 Pydantic 模型校验 raw_arguments
   ├─ 校验失败 → tool_call.status=invalid_arguments，写事件，返回错误
   └─ 校验通过 → 继续
5. 调用实现体，计时
6. 写 tool_call 行（含 arguments 快照、validated=True、duration_ms）
7. 更新 trace_event（status=ok / failed，补 ended_at）
```

## 参数快照的脱敏

`tool_call.arguments` 存的是**已脱敏**的参数快照，
不存未脱敏的原始参数。脱敏规则见 Trace 契约：六类敏感模式
（API Key、Bearer Token、赋值型密钥、URL 凭证、手机号、邮箱）
一律替换为 `[REDACTED_*]` 占位符。

## 重试

工具重试有上限 `TOOL_MAX_RETRIES`（默认 2）。
重试时 `tool_call.retry_count` 递增，且产生 `status = "retried"` 的事件。

重试只对**可重试的错误**进行（超时、临时性故障）。
参数校验失败**不重试** —— 重试同一个非法参数不会有不同结果。

## 错误码

| 场景 | 错误码 |
|---|---|
| 参数未通过校验 | `INVALID_ARGUMENT` |
| 工具执行抛异常 | `TOOL_EXECUTION_FAILED` |
| 工具超时（超过 `TOOL_TIMEOUT_SECONDS`） | 记录为 `timeout` 状态 |

## 注册表的可注入性

注册表接受注入的 `TraceRecorder`。这个设计让单元测试可以：

- 用内存 SQLite 承接写入，不碰真实数据库；
- 替换 `TraceRecorder` 为内存收集器，断言"写了什么事件"而不必查库。

`top_k` 的上限由 `TOOL_SEARCH_MAX_TOP_K`（默认 10）控制。
