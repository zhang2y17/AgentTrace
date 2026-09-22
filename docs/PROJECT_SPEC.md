# PROJECT_SPEC.md — AgentTrace 项目契约

> 本文件是 AgentTrace 的最高约束文件。所有后续实现、测试、文档都必须与本文件一致。
> 如需变更契约，必须先修改本文件并说明理由，再改代码。

## 0. 项目定位

AgentTrace 是一个**个人独立开发**的开源项目，用于记录、回放、评测和分析 LLM Agent 的执行过程。

它解决的问题是：Agent 跑完一次任务之后，开发者拿不到可靠的过程证据——不知道每个节点花了多久、工具选对没有、参数对不对、Token 花了多少、失败在哪一步。AgentTrace 把这些问题变成可查询、可回放、可量化、可设门禁的工程能力。

### 0.1 边界声明（强制）

| 编号 | 约束 |
|---|---|
| B1 | 不使用任何公司代码、公司数据、公司接口、公司名称或未公开业务信息 |
| B2 | 演示数据仅使用公开数据、合成数据或项目自建数据 |
| B3 | README 必须标注：项目为个人独立开发 |
| B4 | 不编造线上用户量、商业客户、商业收益或无法复现的指标 |
| B5 | 真实 LLM 调用与测试替身（test double）必须在代码和文档中明确区分 |
| B6 | 测试不得用固定成功结果冒充集成测试 |
| B7 | 默认支持 OpenAI-compatible API，同时支持本地 Ollama |
| B8 | API Key 只能从环境变量读取，不得落盘、不得入库、不得进日志 |
| B9 | 任何指标必须标注其数据来源范围（离线评测 / 样例运行 / 测试），不得暗示生产表现 |

### 0.2 示例 Agent 的业务场景

示例 Agent 是**技术文档研究 Agent**（Technical Documentation Research Agent）：
给定一个关于本项目样例文档的技术问题，它检索本地样例文档、校验证据充分性、生成带引用的结构化答案，并做最终校验。

选择这个场景的原因：完全公开、无业务机密、可离线复现、且天然包含"工具选择 + 参数构造 + 证据引用"这三类最值得评测的 Agent 行为。

## 1. 核心能力（业务目标）

| 编号 | 能力 | 验收方式 |
|---|---|---|
| G1 | 执行一个可配置的 LangGraph Agent | `POST /runs` 返回终态结果 |
| G2 | 记录 run / node / tool_call / model_call / error / final_result 事件 | `GET /runs/{run_id}/events` 可查到全部 6 类 |
| G3 | 保存节点输入输出摘要、状态、耗时、错误信息 | trace_event 字段完整（见 TRACE_SCHEMA.md） |
| G4 | 保存工具名称、参数、校验结果、返回状态、耗时 | tool_call 表可查，含 validated 标志 |
| G5 | 保存模型名称、Token 使用量、估算成本 | model_call 表可查，成本由确定性代码计算 |
| G6 | 对一次运行做查询、回放、失败定位 | `GET /runs/{id}`、`POST /runs/{id}/replay` |
| G7 | 用固定评测集计算完成率/工具正确率/参数正确率/延迟/成本 | `POST /evaluations` + `GET /evaluations/{id}` |
| G8 | 对不同 Prompt / 模型 / Agent 版本做回归对比 | 评测结果支持按 3 个维度分组对比 |
| G9 | 指标低于阈值时阻止版本通过 | `POST /quality-gates/check` 返回 `passed=false` |
| G10 | 提供 README、API 文档、样例数据、测试、Docker Compose | 交付物清单见 PROJECT_SPEC §6 |

## 2. 技术约束（强制）

| 编号 | 约束 | 实现选择 |
|---|---|---|
| T1 | Python 3.12 | `pyproject.toml` 声明 `requires-python = ">=3.12"`；Docker 镜像 `python:3.12-slim` |
| T2 | FastAPI 提供 HTTP API | `app/main.py` |
| T3 | LangGraph 实现示例 Agent 工作流 | `app/agent/graph.py`，5 个固定节点 |
| T4 | Pydantic 定义配置、事件、工具参数、评测结果 | `app/schemas/` 全部模型 |
| T5 | PostgreSQL 保存运行记录、事件、评测集、评测结果 | `app/db/`，兼容 SQLite 以便离线测试 |
| T6 | Redis 用于短期任务状态与可选事件队列；**不缓存最终答案** | `app/services/cache.py` |
| T7 | SQLAlchemy 2.x —— **本项目明确选择 SQLAlchemy 2.x（同步 Session），不使用 SQLModel** | 理由见下 |
| T8 | Docker Compose 启动 API + PostgreSQL + Redis | `docker-compose.yml` |
| T9 | pytest 编写单元测试、接口测试、Smoke Test | `tests/` 三层目录 |
| T10 | 结构化 JSON 日志 | `app/core/logging.py`，每行一个 JSON 对象 |
| T11 | 核心功能不依赖付费 SaaS | 全部依赖均为开源 |
| T12 | 默认测试不需要网络和 API Key | `tests/conftest.py` 注入假模型 |
| T13 | 测试替身必须在代码和 README 中标注 | 命名前缀 `Fake*` + 模块 docstring 标注 |

### T7 选型理由（SQLAlchemy 2.x 而非 SQLModel）

1. 需要显式控制索引、唯一约束、复合索引与外键级联行为，SQLAlchemy 2.x 的 `mapped_column` 表达力更强；
2. Trace 数据写入是高频、追加型（append-only）路径，需要精确控制 flush/commit 时机，声明式 Session 更直接；
3. LangGraph 节点在工具调用中会写入 trace，需要能显式传入 Session，避免隐式 session 管理带来的连接泄漏；
4. 用 `Mapped[...]` 类型注解 + `mypy` 可获得静态类型检查收益，这比 SQLModel 的运行时校验更贴合本项目需求。

## 3. 示例 Agent 契约

### 3.1 节点（固定 5 个，顺序不可变）

| 顺序 | 节点名 | 职责 | 输入状态 | 输出状态 |
|---|---|---|---|---|
| 1 | `question_parser` | 解析问题，生成结构化任务 | `question`, `agent_config` | `parsed_task` |
| 2 | `document_search` | 调用本地样例文档搜索工具 | `parsed_task` | `search_results`, `tool_calls[]` |
| 3 | `evidence_checker` | 判断证据是否充分 | `search_results` | `evidence_sufficient`, `evidence_refs[]` |
| 4 | `answer_writer` | 基于证据生成结构化答案 | `parsed_task`, `evidence_refs[]` | `answer` |
| 5 | `final_validator` | 校验答案是否含引用和必要字段 | `answer` | `final_result`, `validation_errors[]` |

**流程分叉规则**：`evidence_checker` 判定证据不足时，沿条件边回到 `document_search` 进行一次**放宽检索**（`top_k` 增大、放宽关键词匹配），最多重试 `MAX_EVIDENCE_RETRIES`（默认 1）次；重试耗尽后仍不足，则走 `answer_writer` 并在 `final_result.status = "degraded"`，禁止伪装成成功。

### 3.2 工具（至少 4 个）

| 工具名 | 参数（Pydantic 校验） | 返回 | 性质 |
|---|---|---|---|
| `search_documents` | `query: str`, `top_k: int` | `list[DocumentHit]` | 检索，仅访问项目内样例文档 |
| `get_document` | `document_id: str` | `Document` | 读取，仅访问项目内样例文档 |
| `calculate_latency_summary` | `run_id: str` | `LatencySummary` | **确定性代码**，不使用 LLM |
| `calculate_cost_summary` | `run_id: str` | `CostSummary` | **确定性代码**，不使用 LLM |

### 3.3 强制约束

1. 工具参数一律由 Pydantic 模型校验；校验失败必须产生 `tool_call.status = "invalid_arguments"` 的 trace 记录，且**不得**把未校验参数传给工具实现体。
2. 统计、阈值和成本计算必须由确定性代码完成，**不允许让 LLM 猜测数值**。
3. 工具通过统一注册表（`app/tools/registry.py`）发现，禁止在节点里硬编码工具名到函数的映射。
4. 每次工具调用必须写入 `tool_call` 表与一条 `event_type = "tool_call"` 的 trace_event。

## 4. 核心数据模型

至少包含 8 张表：`agent_definition`、`run`、`trace_event`、`tool_call`、`model_call`、`eval_case`、`eval_run`、`quality_gate`。

字段级定义见 [DATA_MODEL.md](./DATA_MODEL.md)。`trace_event` 必需字段：
`event_id`、`run_id`、`parent_event_id`、`event_type`、`name`、`status`、`started_at`、`ended_at`、`duration_ms`、`input_summary`、`output_summary`、`error_code`。

## 5. API 契约

10 个端点，见 [API_CONTRACT.md](./API_CONTRACT.md)：`GET /health`、`POST /runs`、`GET /runs/{run_id}`、`GET /runs/{run_id}/events`、`POST /runs/{run_id}/replay`、`POST /evaluations`、`GET /evaluations/{evaluation_id}`、`GET /metrics/summary`、`POST /quality-gates/check`、`GET /docs`。

## 6. 交付物清单

| 类别 | 文件 |
|---|---|
| 应用代码 | `app/**` |
| 容器 | `Dockerfile`、`docker-compose.yml`、`.dockerignore` |
| 依赖 | `pyproject.toml`、`requirements.txt` |
| 环境 | `.env.example` |
| 数据库 | `scripts/init_db.py`、`scripts/seed_data.py` |
| 样例文档 | `data/sample_docs/*.md` |
| 评测集 | `data/eval/doc_research_v1.jsonl` |
| 测试 | `tests/unit/**`、`tests/api/**`、`tests/smoke/**`、`tests/integration/**` |
| 文档 | `README.md`、`docs/ARCHITECTURE.md`、`docs/EVALUATION.md`、`docs/DATA_MODEL.md`、`docs/API_CONTRACT.md`、`docs/TRACE_SCHEMA.md`、`docs/PROJECT_SPEC.md`、`docs/IMPLEMENTATION_PLAN.md`、`docs/ACCEPTANCE_CHECKLIST.md`、`SECURITY.md`、`CONTRIBUTING.md`、`LICENSE` |
| 图 | `docs/diagrams/agent_flow.mmd`、`docs/diagrams/eval_flow.mmd` |
| CI | `.github/workflows/ci.yml` |

## 7. 工作规则

1. 第一轮只分析和规划，不创建业务代码；
2. 后续每轮只完成一个阶段；
3. 每次修改前查看目录、设计文件和 `git diff`；
4. 每阶段必须有测试或可执行验收命令；
5. 测试失败时先分析根因，再做最小修改；
6. 每轮报告修改文件、命令、结果和未完成内容；
7. 真实集成测试和纯逻辑测试必须分别标注；
8. 每阶段完成后提交 Git commit，但**不替用户 push 到远程仓库**。

### 目录修改权限（每阶段允许修改的目录）

| 阶段 | 允许修改 |
|---|---|
| S1 契约 | `docs/**` |
| S2 骨架 | `app/main.py`、`app/core/**`、`app/schemas/**`、`pyproject.toml`、`tests/{unit,api}/**`、`.github/**`、`README.md`、`.env.example` |
| S3 数据层 | `app/db/**`、`app/services/cache.py`、`scripts/**`、`docker-compose.yml`、`Dockerfile`、`tests/{unit,integration}/**` |
| S4 Agent | `app/agent/**`、`app/tools/**`、`data/sample_docs/**`、`tests/unit/**` |
| S5 运行/回放 API | `app/api/**`、`app/schemas/**`、`tests/{api,smoke}/**` |
| S6 评测/门禁 | `app/evaluation/**`、`app/schemas/**`、`data/eval/**`、`tests/unit/**`、`docs/EVALUATION.md` |
| S7 安全/交付 | 全仓库（仅新增与安全、交付相关改动） |
| S8 验收 | **只读**，不修改任何文件 |

## 8. 已知限制（必须如实写入 README）

1. 本项目**没有**生产环境部署验证，Docker Compose 面向本地开发与演示；
2. 评测指标基于**离线评测集与样例运行**，不代表任何线上流量表现；
3. 样例文档为项目自建合成数据，规模很小（约 6 篇），检索质量不代表真实文档库；
4. 默认测试使用测试替身（Fake Model），不连接真实 LLM；真实 LLM 集成测试需自行配置 API Key 并显式开启；
5. 成本估算基于内置价目表，价格会变动，估算值仅供横向对比，不等于真实账单；
6. 单机 SQLite 模式仅用于测试，不支持并发写入，生产语义依赖 PostgreSQL；
7. 未实现鉴权与多租户（见 SECURITY.md 的威胁模型）。
