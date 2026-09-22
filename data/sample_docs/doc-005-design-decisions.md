---
document_id: doc-005
title: 技术选型理由
tags: [decisions, sqlalchemy, langgraph, pydantic]
---

# 技术选型理由

本文记录几个"看起来可以随便选、实际上会影响可验证性"的决定。

## 为什么用 SQLAlchemy 2.x 而不是 SQLModel

SQLModel 把 Pydantic 与 SQLAlchemy 合成一个类，写起来短。
但这会带来一个具体问题：**API 模型与数据库模型无法独立演化**。

本项目需要两者的字段集合**不同**：

- `RunDetail` 响应需要内联 `events` 数组，但 `run` 表没有这一列；
- `ModelCall` 表有 `cost_estimation_unavailable`，但 API 响应里
  它被折叠进 `cost.note` 字段；
- 工具参数的 Pydantic 模型（`ToolSpec`）与任何表都无关。

如果合成一个类，就得靠 `exclude` 和 `__table__` 注解反复打补丁，
最终比分开写更啰嗦。

选择：**SQLAlchemy 2.x 的 `DeclarativeBase` + `Mapped[]` 注解**，
Pydantic 模型独立放在 `app/schemas/`。两者之间显式转换。

## 为什么用 ULID 而不是 UUID4

ID 格式是 `<前缀> + ULID`，例如 `run_01J8ZQ4K900000000000000001`。

UUID4 是纯随机的，作为 B-tree 主键会导致**索引页分裂**：
新插入的行落在随机位置，页填充率下降，写放大。

ULID 的前 48 位是毫秒时间戳，因此：

1. **字典序即时间序** — 按 ID 排序天然得到按时间排序；
2. **顺序插入** — 新行总是在索引末尾，避免页分裂；
3. **Crookford Base32 不含 I/L/O/U** — 人工转录不会混淆。

前缀的作用是**让 ID 自解释**：看到 `tc_` 就知道是工具调用，
不必查文档或查询数据库。

各实体前缀：

| 实体 | 前缀 |
|---|---|
| run | `run_` |
| trace_event | `evt_` |
| tool_call | `tc_` |
| model_call | `mc_` |
| agent_definition | `agentdef_` |
| eval_case | `case_` |
| eval_run | `evalrun_` |
| quality_gate | `gate_` |
| evaluation | `eval_` |

## 为什么用 LangGraph

可以用一个 for 循环串起 5 个节点，不需要框架。但本项目需要：

1. **显式的条件边** — `evidence_checker` 回到 `document_search` 是图上的边，
   不是藏在 `if` 语句里。这让流程图能直接从代码结构画出；
2. **状态传递的显式声明** — 每个节点声明读什么、写什么；
3. **节点级可观测点** — 框架的节点边界正好是 Trace 事件的天然切分点。

代价是引入一个框架依赖。缓解方式是锁定版本区间，
并且状态用 TypedDict（而不是 Pydantic），减少与框架 API 的耦合。

## 为什么用 JSON 而不是 JSONB

PostgreSQL 的 `JSONB` 支持索引和更高效的查询，性能更好。

但 SQLite **不支持 JSONB**，而 SQLite 是默认测试方言 ——
"本地 pytest 不需要 Docker" 是本项目的一条硬约束。

选择 `sqlalchemy.JSON`：跨方言可用。代价是 PostgreSQL 上少了一些
JSONB 的查询能力。本项目的 JSON 字段（`attributes`、`result_summary`）
只做整体读写，不做内部查询，因此这个代价可以接受。

## 为什么金额用 Numeric 而不是 Float

`float` 是二进制浮点，`0.1 + 0.2 != 0.3`。
成本要累加几十次调用，误差会累积到可见。

选择 `Numeric(12, 6)`，Python 侧统一用 `Decimal` 比较。
6 位小数足以表达单次调用级别的成本（例如 `0.000187` USD）。

## 为什么脱敏只替换值、不替换键

脱敏正则最初把 `{"api_key": "abc123"}` 整体重写成 `{api_key=[REDACTED]"}`。

后果是 **JSON 结构被破坏**，下游 `json.loads()` 直接失败。
正确做法是只捕获"值的部分"并替换，键名、引号、分隔符原样回填。

这个教训写在这里的原因：脱敏的目标是"数据可安全流转"，
如果脱敏本身破坏了数据的可用性，那就是用一个 bug 换另一个 bug。

## 为什么不引入 Alembic

Alembic 的价值在于**增量迁移已发布的 schema**。本项目尚未有那个需求：
数据库是本地开发与演示用的，字段变更时的做法是
删库 → 重新 `init_db.py` → `seed_data.py`。

引入 Alembic 会增加迁移脚本的维护成本，却解决一个还不存在的问题。
这是一个刻意的"不做"，已记录在已知限制中。
