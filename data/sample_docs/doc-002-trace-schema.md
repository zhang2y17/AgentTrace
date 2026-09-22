---
document_id: doc-002
title: Trace 事件模型与 sequence 语义
tags: [trace, schema, sequence, replay]
---

# Trace 事件模型与 sequence 语义

## 为什么需要 Trace

一次 Agent 运行的黑盒输出只有一个最终答案，无法回答"它怎么走到这里的"。
Trace 是运行的**过程证据**，必须支撑四类问题：

1. **查询** — 这次运行做了什么？每个节点进出时间、状态、耗时？
2. **回放** — 能不能用同样的输入再跑一次并对比？
3. **失败定位** — 失败发生在哪个节点或工具，错误码是什么？
4. **度量** — 工具选对没有、参数对不对、Token 花了多少、钱花了多少？

## 六类事件

| event_type | 触发点 | 数量级 |
|---|---|---|
| `run` | 创建运行 | 每 run 1 条 |
| `node` | 每个节点进入/退出 | 每 run 5~7 条 |
| `tool_call` | 每次工具调用 | 每 run 2~5 条 |
| `model_call` | 每次 LLM 调用 | 每 run 2~4 条 |
| `error` | 任何被捕获的失败 | 0~N 条 |
| `final_result` | 运行终结 | 每 run 1 条 |

## 必需字段

全部 6 类事件共有 12 个必需字段：

`event_id`、`run_id`、`parent_event_id`、`event_type`、`name`、`status`、
`started_at`、`ended_at`、`duration_ms`、`input_summary`、`output_summary`、
`error_code`

其中 `parent_event_id` 为 null 表示根事件。`run` 事件与顶层 `node` 事件
的 `parent_event_id` 都是 null。

## 八个状态

| status | 含义 |
|---|---|
| `pending` | 已创建未开始（节点级当前不使用） |
| `running` | 进行中，`ended_at` 仍为 null |
| `ok` | 正常结束 |
| `failed` | 执行失败 |
| `skipped` | 按策略跳过（如证据已足够，不再放宽检索） |
| `retried` | 本事件是重试产生的 |
| `invalid_arguments` | 工具参数未通过 Pydantic 校验 |
| `handoff` | 需要人工接管，自动流程停止 |

## sequence 解决了什么问题

### 时间戳不够用

`started_at` 的精度是毫秒。同一个 run 内，多个事件完全可能落在**同一毫秒**内——
特别是 `question_parser` 这类几乎不耗时的节点。

如果只用 `started_at` 排序，同一毫秒内的多个事件顺序是不确定的。
数据库在不同时刻可能给出不同的顺序（取决于物理存储、索引选择），
这会直接破坏回放的可复现性。

### 两个解决方案的比较

| 方案 | 是否可用 | 原因 |
|---|---|---|
| 提高时间戳精度到微秒/纳秒 | 否 | 依赖平台时钟精度，跨机器不可靠，且写入延迟本身就不稳定 |
| `sequence` 单调递增整数 | **是** | 由应用层保证，与时钟无关，可被唯一约束校验 |

### sequence 的具体语义

1. **同 run 内从 1 开始连续递增**：`1, 2, 3, ...`，不留空洞；
2. **跨 run 相互独立**：不同 run 的 sequence 都从 1 开始；
3. **由 `max(sequence)+1` 分配**：写入前查询当前最大值；
4. **有唯一约束 `UNIQUE(run_id, sequence)`**：这是关键的保障——
   分配的数值一旦重复，数据库会直接拒绝，而不是静默产生一个歧义顺序。

### 并发下的冲突处理

`max(sequence)+1` 在并发写入同一 run 时会撞唯一约束。处理方式是：

1. 捕获 `IntegrityError`；
2. 回滚当前 flush 的影响；
3. 重新分配 sequence 并重试；
4. 最多重试 3 次，仍失败则抛出异常（暴露问题，不无限重试）。

连续冲突说明存在更严重的并发问题，掩盖它反而更难排查。

### 排序的最终依据

查询事件列表时，排序依据是：

```sql
ORDER BY sequence ASC
```

而**不是** `ORDER BY started_at ASC`。这是刻意的：`sequence` 是应用层
显式分配的因果顺序，比时钟更可信。

## 回放与 sequence

回放会生成一个**新的 run_id** 与新的 Trace，`source_run_id` 指向被回放的原始 run。
原始 run 的记录**绝不被覆盖**。对比两次回放时，用 `(event_type, name)` 对齐事件，
再用 sequence 确定各自内部的顺序。

## degraded 的语义

`final_result` 事件的 `status` 可能是 `degraded` 情形下的标记：
流程完成了，但证据不足。它**不视为成功**，也**不计入错误率**，
而是由独立的 `degraded_rate` 指标度量。

这个区分很重要：把"证据不足但跑完了"算成成功会高估能力，
算成错误又会高估稳定性。
