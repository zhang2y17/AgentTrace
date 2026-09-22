---
document_id: doc-006
title: 评测指标与质量门禁
tags: [evaluation, metrics, gate, offline]
---

# 评测指标与质量门禁

## 评测集

- 目录：`data/eval/`
- 文件：`<dataset_version>.jsonl`，一行一个 case
- 当前版本：`doc_research_v1.jsonl`（12 个 case）

评测集中的问题由项目作者针对本仓库自建的样例文档编写，
属于**合成评测集**，不来自任何真实用户日志，不含任何业务数据。
规模小（12 case）是刻意的：目的是让每个 case 都能被人工核对。

## 指标的四个原则

### 原则一：分母是评测集大小，不是成功执行数

分母始终是 $N$（评测集 case 数，含失败的 case）。

如果改用"成功执行数"做分母，失败 case 会同时从分子和分母消失，
指标会**虚高**——这等于用统计口径掩盖失败。

### 原则二：degraded 不计入成功

`run_success_rate` 的分子只统计 `status == "succeeded"`。
`degraded`（流程跑完但证据不足）**不算成功**。

这是刻意的严格定义。否则"证据不足但跑完了"会被算成能力，
与实际表现不符。

同时 `degraded` 也**不计入错误率**（`error_rate` 只统计
`failed` 与 `timeout`）。它有独立的度量口径。

### 原则三：分位数用 nearest-rank，不用线性插值

计算 p50 / p95 时采用 nearest-rank 方法，而不是线性插值。

原因：线性插值会插出**未被观测到**的数值。
例如只有 2 个样本 100ms 和 200ms，插值可能给出 105ms ——
这个延迟从未真实发生过。用未观测值做验收是不诚实的。

nearest-rank 的结果一定是样本中真实存在的某个值。

### 原则四：每个指标必须标注数据来源

所有指标输出都必须带 `scope` 字段，取值：

- `offline_evaluation` — 在固定评测集上跑出的结果；
- `sample_runs` — 从样例运行推导；
- `empty` — 无数据。

允许的表述是"离线评测结果"、"在 doc_research_v1 评测集上"。
**禁止**的表述是"线上成功率"、"生产准确率"。
这个约束由契约校验脚本自动检查。

## 十个指标

| # | 指标 | 类型 | 方向 |
|---|---|---|---|
| M1 | `run_success_rate` | 比率 | ↑ |
| M2 | `task_completion_rate` | 比率 | ↑ |
| M3 | `tool_selection_accuracy` | 比率 | ↑ |
| M4 | `tool_argument_accuracy` | 比率 | ↑ |
| M5 | `evidence_coverage` | 均值 | ↑ |
| M6 | `latency_ms_p50` / `latency_ms_p95` / `latency_ms_mean` | 分位数 | ↓ |
| M7 | `total_tokens` | 计数 | — |
| M8 | `estimated_cost_usd` | 金额 | ↓ |
| M9 | `error_rate` | 比率 | ↓ |
| M10 | `human_review_rate` | 比率 | ↓ |

### M1 与 M2 的关系

$M2 \le M1$ 恒成立：M1 只看状态，M2 还看内容断言。

如果出现 $M2 > M1$，说明断言逻辑有 bug ——
"完成了内容要求却没成功结束"是自相矛盾的。

### M3 的工具选择判定

工具选择是**有序匹配**：实际调用序列与期望序列逐项比较。

仅统计"有工具期望"的 case。若某 case 的 `expected_tools` 为空，
它不参与本指标（分子分母都不计），否则会虚高准确率。

### 工具参数比对语义

比较器集中在一个函数里，按类型区分：

| 类型 | 比较方式 |
|---|---|
| 精确值 | 相等 |
| 区间期望 | 落在区间内即算对 |
| 集合期望 | 忽略顺序 |
| 缺省 | 期望未指定时不参与判定 |

分散在各种 if 里的比较会很快产生不一致，因此集中一处。

### M6 的适用边界

延迟分位数只在**成功**的 case 上有意义。
失败 case 的延迟反映的是失败路径，与正常路径不具可比性，
因此 M6 的分母是成功 case 数。这与 M1~M5 的分母规则不同，
是刻意的例外。

### M8 成本估算的三条限制

1. 基于**静态价目表**（`pricing.json`），不反映实时价格；
2. 测试替身模式下成本记为 0 并标记 `cost_estimation_unavailable`；
3. 仅用于**量级比较**，不能作为账单依据。

这三条必须与指标一同展示，不能只给一个数字。

## 质量门禁

门禁把指标与阈值比较，输出通过与否。

门禁的三条语义边界（必须与结果一同展示）：

1. 门禁**只作用于离线评测集**，不代表线上表现；
2. 阈值是**项目自定**的工程判断，不是行业标准；
3. 门禁通过**不代表**无需人工检查；`human_review_rate` 仍被独立追踪。

### 阈值快照

每次门禁检查都必须把当次使用的 `thresholds` **持久化**到
`quality_gate` 表。

原因：事后无法解释"当时为什么算通过"是常见问题。
存了快照，任何人都能用同样的阈值重算一遍，验证结论。

### 违规项与跳过项

- `violations` — 未达标的指标列表，含 `metric`、`required`、`observed`；
- `skipped_metrics` — 因数据缺失（分母为 0）无法判定的指标。

两者必须区分：无法判定 ≠ 通过。把"没数据"当作"达标"是错误的口径。

## 断言目录

`required_assertions` 支持的断言：

| 断言名 | 判定 |
|---|---|
| `contains_citation` | 答案中至少含 1 处 `[doc-xxx]` 形式引用 |
| `min_citations>=N` | 引用数 ≥ N |
| `has_evidence_section` | 答案含"证据"/"依据"小节 |
| `mentions_<keyword>` | 答案含指定关键词（大小写不敏感） |
| `no_hallucinated_doc` | 引用的所有 `doc-xxx` 都真实存在于 `data/sample_docs/` |
| `status_is_succeeded` | run 终态为 `succeeded` |
| `status_is_failed` | run 终态为 `failed` |

`no_hallucinated_doc` 是最重要的一条：它检查 Agent 是否引用了
**不存在的文档 ID**。这是可确定性验证的，不依赖人工判断，
是唯一能自动兜住"编造引用"的机制。

## 反模式清单

1. 用"成功执行数"做分母；
2. 把 `degraded` 算作成功；
3. 分位数用线性插值；
4. 指标不带数据来源标注；
5. 门禁不存阈值快照；
6. 把"无法判定"当作"通过"；
7. 断言只用一个 `assert True` 形式的占位实现；
8. 用 LLM 生成的数字代替确定性代码计算的成本和延迟。
