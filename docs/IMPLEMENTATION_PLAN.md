# IMPLEMENTATION_PLAN.md — AgentTrace 实施计划

## 0. 阶段总览

| 阶段 | 名称 | 交付重点 | 验收命令 | 状态 |
|---|---|---|---|---|
| S1 | 固化项目契约 | 8 份设计文档 | 文档间字段一致性人工核对 | 见 §1 |
| S2 | 创建骨架和配置 | 目录、配置、/health、错误体、CI | `python -m pytest tests -q` | 见 §2 |
| S3 | 数据库和 Trace 存储 | 8 张表、仓储、Compose、脱敏日志 | `docker compose config` + pytest | 见 §3 |
| S4 | 示例 Agent 和工具治理 | LangGraph 5 节点、工具注册表、Trace 中间件 | python -m pytest tests/unit -q | 见 §4 |
| S5 | 运行、查询和回放 API | 5 个端点、过滤、回放 | pytest tests/api tests/smoke | 见 §5 |
| S6 | 离线评测和质量门禁 | JSONL、10 指标、门禁 | scripts/run_eval.py --gate | 见 §6 |
| S7 | 安全和 GitHub 交付 | 脱敏、限流上限、CI、README、SECURITY | CI 全绿 | 见 §7 |
| S8 | 发布前验收 | 只读检查报告 | 按 ACCEPTANCE_CHECKLIST.md | 见 §8 |

## 1. S1 固化项目契约

**输入**：`codex-greenfield-agenttrace.md` 总控提示词
**输出**：`docs/PROJECT_SPEC.md`、`docs/ARCHITECTURE.md`、`docs/DATA_MODEL.md`、`docs/TRACE_SCHEMA.md`、`docs/API_CONTRACT.md`、`docs/EVALUATION.md`、`docs/IMPLEMENTATION_PLAN.md`、`docs/ACCEPTANCE_CHECKLIST.md`

**冻结项**（后续阶段不得擅自变更）：

1. 5 个节点名与顺序；
2. 4 个工具名与参数签名；
3. 6 类 `event_type` 与 8 个 `EventStatus`；
4. `trace_event` 的 12 个必需字段；
5. 10 个 API 端点路径；
6. 12 个指标名与公式（M1~M10 编号，M6 展开为三个分位数）；
7. SQLAlchemy 2.x（非 SQLModel）；
8. ID 前缀与 ULID 规则。

**验收**：逐条核对 8 份文档中出现的节点名/工具名/字段名/端点/指标名是否完全一致（S8 会程序化检查）。

## 2. S2 创建骨架和配置

**范围**：只做骨架，**不实现真实 Agent、数据库访问或 LLM 调用**。

**任务清单**

1. 创建 `app/`、`tests/`、`scripts/`、`data/`、`docs/` 目录；
2. `app/core/config.py`：Pydantic Settings 从 `.env` 读取，字段见 ARCHITECTURE §6；
3. `app/core/logging.py`：结构化 JSON 日志，每行一个 JSON 对象；
4. `app/core/errors.py`：领域异常类 + FastAPI 异常处理器 + 统一错误体；
5. `app/schemas/`：Pydantic 模型（含枚举）；
6. `GET /health`：本阶段只报 `api` 组件，DB/Redis 留占位；
7. `pyproject.toml`：依赖 + ruff + pytest 配置；
8. `.env.example`：全部环境变量；
9. `.github/workflows/ci.yml`：ruff + pytest；
10. `README.md` 骨架含本地启动命令；
11. 测试：配置加载测试、health 测试。

**验收命令**：`python -m pytest tests -q`

**风险**：Pydantic Settings 对 `.env` 缺失字段的处理——必须为所有字段提供默认值，否则 CI 无 `.env` 时会失败。

## 3. S3 数据库和 Trace 存储

**范围**：只做持久化层与依赖状态。

**任务清单**

1. `docker-compose.yml`：`api` + `postgres:16-alpine` + `redis:7-alpine`，含 healthcheck 与 `depends_on: condition: service_healthy`；
2. `Dockerfile`：`python:3.12-slim`，非 root 用户；
3. `app/db/base.py`、`app/db/models.py`：8 张表，字段严格对齐 DATA_MODEL.md；
4. `app/db/session.py`：engine 创建、`SessionLocal`、`init_db()`；SQLite 与 PostgreSQL 的差异处理（`JSON`、`NUMERIC` 兼容）；
5. `app/db/repository.py`：run/trace_event/tool_call/model_call 的增删查；
6. `app/core/ids.py`：ULID 生成 + 前缀；
7. **`sequence` 分配**：`max(sequence)+1`，需处理并发下的唯一约束冲突（冲突则重试一次）；
8. `app/core/redaction.py`：摘要截断 + 脱敏（TRACE_SCHEMA §7）；
9. `scripts/init_db.py`、`scripts/seed_data.py`；
10. `/health` 补齐 `database` 与 `redis` 组件探测；
11. 测试：模型字段测试、仓储 CRUD 测试、`sequence` 单调性测试、模拟写入失败不被吞掉。

**验收命令**

```bash
docker compose config
docker compose up -d
docker compose ps
python -m pytest tests -q
```

**风险**

| 风险 | 缓解 |
|---|---|
| 本地无 Docker 无法跑集成测试 | 测试默认 SQLite + fakeredis；集成测试标记 `@pytest.mark.integration` 并在无 PG 时 skip 而非 fail |
| SQLite 不支持 `NUMERIC` 精确语义 | 金额用 `NUMERIC(12,6)`，SQLite 下以 `Numeric(asdecimal=True)` 处理，测试断言用 `Decimal` 比较 |
| `JSON` 类型跨方言 | 统一用 `sqlalchemy.JSON`，避免 `JSONB`（SQLite 不支持） |
| `max(sequence)+1` 并发冲突 | 唯一约束 + 捕获 `IntegrityError` 重试 |

## 4. S4 示例 Agent 和工具治理

**范围**：只做 Agent 与工具，**不实现评测仪表盘，不接入公司数据**。

**任务清单**

1. `data/sample_docs/*.md`：6 篇自建合成文档；
2. `app/agent/state.py`：LangGraph 状态 `TypedDict`；
3. `app/agent/llm.py`：`LLMGateway` 抽象 + `FakeLLMProvider`（明确标注测试替身）+ `OpenAICompatibleProvider` + `OllamaProvider`；
4. `app/agent/nodes/*.py`：5 个节点；
5. `app/agent/graph.py`：`StateGraph` 装配 + `evidence_checker` → `document_search` 条件边（最多 1 次放宽检索）；
6. `app/tools/base.py`：`ToolSpec`（名、参数模型、返回模型、实现、版本）；
7. `app/tools/registry.py`：注册、发现、Pydantic 校验、计时、落库、重试；
8. `app/tools/document_search.py`：`search_documents`、`get_document`（只用关键词打分，不引入向量库，避免额外依赖）；
9. `app/tools/analytics.py`：`calculate_latency_summary`、`calculate_cost_summary`（确定性 SQL 聚合）；
10. `app/agent/middleware.py`：`TraceRecorder`（节点包装、事件写入）；
11. 测试：图拓扑测试、节点单测、工具参数校验测试（含非法参数）、失败路径测试（工具异常、证据不足重试、handoff）。

**验收命令**：`python -m pytest tests/unit -q`

**风险**

| 风险 | 缓解 |
|---|---|
| LangGraph 版本 API 变动 | 锁定版本区间；状态用 `TypedDict` 而非 Pydantic，减少耦合 |
| 假 LLM 输出导致节点逻辑失真 | `FakeLLMProvider` 改为**确定性规则实现**（基于关键词匹配产出结构化 JSON），而非随机文本，保证测试稳定 |
| 检索质量太低导致证据永远不足 | 关键词打分 + 同义词表；阈值设为可配置 |
| 工具在测试中写库 | 注册表接受注入的 `TraceRecorder`；单测用内存 SQLite |

## 5. S5 运行、查询和回放 API

**范围**：`POST /runs`、`GET /runs/{id}`、`GET /runs/{id}/events`、`POST /runs/{id}/replay`、`GET /metrics/summary`。

**任务清单**

1. `app/services/run_service.py`：`execute()`、`replay()`、`get_run()`、超时控制（`asyncio.wait_for` 或线程超时）；
2. `app/api/runs.py`：4 个端点，含 `event_type`/`status` 过滤与分页；
3. `app/services/metrics_service.py`：运行级 `run_success_rate` / `error_rate` / 延迟分位 / Token / 成本；
4. `app/api/metrics.py`：`GET /metrics/summary`，含 `group_by`；
5. `app/main.py`：路由挂载、异常处理器、lifespan 建表；
6. 测试：接口测试（成功、404、400、409、超时）、回放语义测试（新 run_id + source_run_id 保留 + 原记录不变）、Smoke Test（一个完整 HTTP 生命周期）。

**验收命令**：`python -m pytest tests/api tests/smoke -q`

**风险**

| 风险 | 缓解 |
|---|---|
| 同步端点在超时场景下难以中断 LangGraph | 用线程执行 + `future.result(timeout)`；超时后标记 run 为 `timeout`，不强杀线程但记录事实 |
| 回放覆盖原始记录 | 仓储层禁止 `update(run.question)`；测试断言原 run 行完全不变（含 `updated_at` 语义） |
| 时间戳时区不一致 | 统一 `datetime.now(timezone.utc)`，序列化统一 `Z` 后缀 |

## 6. S6 离线评测和质量门禁

**任务清单**

1. `data/eval/doc_research_v1.jsonl`：14 个 case，覆盖正常、参数错误、证据不足、失败路径；
2. `app/evaluation/dataset.py`：加载 + schema 校验（含行号报错）；
3. `app/evaluation/assertions.py`：7 个断言实现；
4. `app/evaluation/metrics.py`：12 个指标公式（严格按 EVALUATION.md §3）；
5. `app/evaluation/pricing.py`：价目表 + `estimate_cost`；
6. `app/evaluation/runner.py`：逐 case 执行、判定、落库；
7. `app/evaluation/gate.py`：阈值校验、违规项生成、阈值快照；
8. `app/api/evaluations.py`、`app/api/quality_gates.py`；
9. `scripts/run_eval.py`：CLI，退出码 0/1/2；
10. 测试：指标公式单测（含边界：N=0、单样本、分母为 0）、门禁判定单测、评测端到端测试。

**验收命令**

```bash
python -m pytest tests -q
python scripts/run_eval.py --dataset doc_research_v1 --gate
```

**风险**

| 风险 | 缓解 |
|---|---|
| 指标边界条件遗漏（除零） | 统一 `_safe_ratio(n, d)` 辅助函数，`d == 0` 返回 `None`；每条公式都有 N=0 单测 |
| 期望参数比较语义不一致 | 比较器集中在 `_compare_arg()` 一个函数，按 EVALUATION.md §3 M4 的表实现 |
| 评测集与 Agent 能力不匹配导致指标全 0 | 先跑一次看基线，把 case 调到合理难度；若门禁永远不通过，说明阈值或 case 需要校准（这是校准问题，不是"改指标") |

## 7. S7 补齐安全和 GitHub 交付

**任务清单**

1. 审查所有 `os.environ` / Settings 读取点，确认 Key 只从 env 且不落库；
2. 脱敏函数覆盖所有日志与 Trace 写入点；
3. 超时、重试上限、最大输入长度全部可配置且有测试；
4. `Dockerfile`、`docker-compose.yml`、`.env.example` 完善；
5. `.github/workflows/ci.yml`：lint + pytest + `docker compose config`；
6. `README.md`：架构图、启动方式、样例命令、测试方式、评测方式、已知限制；
7. `SECURITY.md`：密钥、日志、数据边界、威胁模型；
8. `LICENSE`（MIT）、`CONTRIBUTING.md`；
9. **不写**无法由测试证明的"生产可用"或"企业级"表述；
10. **不 push** 到远程仓库。

**验收命令**：`ruff check .` + `python -m pytest tests -q` + `docker compose config`

## 8. S8 发布前验收

只读，不修改代码。按 `ACCEPTANCE_CHECKLIST.md` 逐项执行并输出通过/失败/风险/需人工验证四类结论，附最终启动命令、运行命令、评测命令。

## 9. 依赖清单（预期）

| 包 | 用途 | 版本约束 |
|---|---|---|
| `fastapi` | HTTP | `>=0.115,<1` |
| `uvicorn[standard]` | ASGI 服务 | `>=0.30,<1` |
| `pydantic` | 契约 | `>=2.7,<3` |
| `pydantic-settings` | 配置 | `>=2.3,<3` |
| `sqlalchemy` | ORM | `>=2.0,<3` |
| `psycopg[binary]` | PG 驱动 | `>=3.1,<4` |
| `redis` | Redis 客户端 | `>=5.0,<6` |
| `langgraph` | Agent 图 | `>=0.2,<0.4` |
| `httpx` | LLM HTTP | `>=0.27,<1` |
| `python-ulid` | ID 生成 | `>=2.7,<3` |
| `pytest`、`pytest-asyncio`、`httpx` | 测试 | — |
| `fakeredis` | Redis 替身 | `>=2.23,<3` |
| `ruff` | lint | — |

**说明**：`langgraph` 是本项目唯一较重的依赖。它被选中的理由在契约 T3 已固定：需要显式的工作流拓扑（节点 + 条件边）而非自由循环，这是评测"工具选择是否正确"的前提——图的边定义了**允许**的工具调用范围。

## 10. 明确不做（本版本范围外）

| 项 | 理由 |
|---|---|
| Alembic 迁移 | 尚无已发布 schema（见 DATA_MODEL §5） |
| 鉴权 / 多租户 | 契约未要求；已在 README 已知限制声明 |
| 前端仪表盘 | 契约只要求 API；图表用 Mermaid 文档替代 |
| 异步任务队列 | 同步执行 + 超时已满足契约 G1；异步化需额外引入 Celery/RQ |
| 向量检索 | 会引入 embedding 依赖与成本；关键词检索足以支撑评测 |
| 人工接管 UI | `handoff` 状态已记录，UI 属产品功能 |
| 线上指标接入 | 契约 B4 明确禁止不可复现指标 |
