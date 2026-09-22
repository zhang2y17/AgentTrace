---
document_id: doc-003
title: 示例 Agent 的五个节点
tags: [agent, graph, nodes, langgraph]
---

# 示例 Agent 的五个节点

## 用途

示例 Agent 是"技术文档研究 Agent"：针对本仓库自建的样例文档回答技术问题。
它的价值不是"多聪明"，而是**结构完整且可被测试** ——
5 个节点、4 个工具、1 条条件边，足以覆盖成功、重试、降级、失败四条路径。

## 固定 5 个节点（顺序不可变）

| 顺序 | 节点名 | 职责 | 输入状态 | 输出状态 |
|---|---|---|---|---|
| 1 | `question_parser` | 解析问题，生成结构化任务 | `question`, `agent_config` | `parsed_task` |
| 2 | `document_search` | 调用本地样例文档搜索工具 | `parsed_task` | `search_results`, `tool_calls[]` |
| 3 | `evidence_checker` | 判断证据是否充分 | `search_results` | `evidence_sufficient`, `evidence_refs[]` |
| 4 | `answer_writer` | 基于证据生成结构化答案 | `parsed_task`, `evidence_refs[]` | `answer` |
| 5 | `final_validator` | 校验答案含引用与必要字段 | `answer` | `final_result`, `validation_errors[]` |

节点顺序是**契约冻结项**：一旦变更，所有历史评测结果的可比性就消失了。

## 流程分叉规则

`evidence_checker` 判定证据不足时，沿条件边**回到** `document_search`
进行一次**放宽检索**：

- `top_k` 增大（默认 3 → 更大值）；
- 放宽关键词匹配（允许同义词命中）。

最多重试 `MAX_EVIDENCE_RETRIES`（默认 1）次。

重试耗尽后仍不足，则走 `answer_writer` 并在
`final_result.status = "degraded"`。

**禁止伪装成成功**：证据不足却返回 `succeeded` 是本项目明确的反模式。

## 状态传递

节点间通过 LangGraph 的 `StateGraph` 状态字典传递数据。状态用 `TypedDict`
而非 Pydantic 模型，理由：

1. LangGraph 的 reducer 机制依赖 TypedDict 的注解形式；
2. 状态在节点间流动频繁，Pydantic 的校验开销在这个位置没有收益
   （校验发生在**工具参数**和**API 边界**，那才是需要严格把关的地方）。

## 条件边的判定逻辑

`evidence_checker` 输出两个关键字段：

- `evidence_sufficient: bool` — 证据是否达到阈值；
- `evidence_coverage: float` — 证据覆盖率（0~1）。

判定依据是 `EVIDENCE_COVERAGE_THRESHOLD`（默认 0.5）。

条件边的分支：

```
evidence_sufficient == True                    → answer_writer
evidence_sufficient == False 且 重试次数 < 上限  → document_search（放宽检索）
evidence_sufficient == False 且 重试次数已耗尽    → answer_writer（走降级路径）
```

## 节点与 Trace 事件的对应

每个节点进入时写一条 `event_type = "node"` 的事件（`status = running`），
退出时更新为 `ok` / `failed` 并补 `ended_at` 与 `duration_ms`。

因此一次成功的运行通常产生：

- 1 条 `run` 事件；
- 5 条 `node` 事件（重试时 6 条，`document_search` 出现两次）；
- 若干 `tool_call` 与 `model_call` 事件；
- 1 条 `final_result` 事件。

## 重试时的事件标记

放宽检索产生的第二次 `document_search` 节点事件，其
`attributes.attempt` 为 1（从 0 开始计数），并且前一次的检索事件
在证据不足的判定下被标记为 `skipped` 或保留为 `ok`（检索本身成功了，
只是结果不够）。这个区分需要小心：**检索成功 ≠ 证据充分**。
