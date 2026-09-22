# ARCHITECTURE.md — AgentTrace 架构设计

## 1. 架构总览

```mermaid
flowchart TB
    subgraph Client["调用方"]
        CLI["curl / HTTP 客户端"]
        EVALCLI["scripts/run_eval.py"]
    end

    subgraph API["FastAPI 应用 (app/)"]
        R1["GET /health"]
        R2["POST /runs"]
        R3["GET /runs/{id}"]
        R4["GET /runs/{id}/events"]
        R5["POST /runs/{id}/replay"]
        R6["POST /evaluations"]
        R7["GET /evaluations/{id}"]
        R8["GET /metrics/summary"]
        R9["POST /quality-gates/check"]
    end

    subgraph Core["核心层"]
        CFG["core/config.py<br/>env 配置"]
        LOG["core/logging.py<br/>JSON 日志 + 脱敏"]
        ERR["core/errors.py<br/>统一错误响应"]
        IDS["core/ids.py<br/>run_id / event_id"]
        RED["core/redaction.py<br/>输入输出摘要与脱敏"]
    end

    subgraph Agent["Agent 层 (app/agent/)"]
        GRAPH["graph.py<br/>LangGraph StateGraph"]
        N1["question_parser"]
        N2["document_search"]
        N3["evidence_checker"]
        N4["answer_writer"]
        N5["final_validator"]
        MW["middleware.py<br/>Trace 中间件"]
        LLMGW["llm.py<br/>LLM 网关"]
    end

    subgraph Tools["工具层 (app/tools/)"]
        REG["registry.py<br/>统一注册表"]
        TS["search_documents"]
        TG["get_document"]
        TL["calculate_latency_summary"]
        TC["calculate_cost_summary"]
    end

    subgraph Eval["评测层 (app/evaluation/)"]
        LOADER["dataset.py<br/>JSONL 加载"]
        RUNNER["runner.py<br/>评测执行"]
        METRICS["metrics.py<br/>指标计算"]
        GATE["gate.py<br/>质量门禁"]
    end

    subgraph Data["数据层 (app/db/)"]
        MODELS["models.py<br/>SQLAlchemy 2.x"]
        SESS["session.py<br/>引擎与 Session"]
        REPO["repository.py<br/>读写仓储"]
    end

    subgraph Infra["基础设施"]
        PG[("PostgreSQL 16")]
        RD[("Redis 7")]
    end

    CLI --> API
    EVALCLI --> Eval
    API --> Core
    API --> Agent
    API --> Eval
    R2 --> GRAPH
    R5 --> GRAPH
    GRAPH --> N1 --> N2 --> N3 --> N4 --> N5
    N2 --> REG
    REG --> TS & TG & TL & TC
    N1 --> LLMGW
    N4 --> LLMGW
    GRAPH --> MW
    MW --> REPO
    N3 --> MW
    N5 --> MW
    REPO --> MODELS
    MODELS --> SESS
    SESS --> PG
    Agent -.短期任务状态.-> RD
    Eval --> REPO
    GATE --> METRICS
    METRICS --> REPO
```

## 2. 模块划分

```
app/
├── main.py                    # FastAPI 应用装配、路由挂载、lifespan
├── api/                       # HTTP 层：只做参数解析、调用服务、错误映射
│   ├── deps.py                # 依赖注入（DB Session、配置、评测服务）
│   ├── health.py              # GET /health
│   ├── runs.py                # POST /runs, GET /runs/{id}, /events, /replay
│   ├── metrics.py             # GET /metrics/summary
│   ├── evaluations.py         # POST /evaluations, GET /evaluations/{id}
│   └── quality_gates.py       # POST /quality-gates/check
├── core/                      # 与业务无关的横切能力
│   ├── config.py              # Pydantic Settings，从 .env 读取
│   ├── logging.py             # 结构化 JSON 日志
│   ├── errors.py              # 领域异常 + 统一错误响应
│   ├── ids.py                 # run_id / event_id 生成
│   └── redaction.py           # 摘要截断与密钥脱敏
├── schemas/                   # Pydantic 契约（API 输入输出、工具参数、评测结果）
│   ├── common.py              # 分页、错误体、状态枚举
│   ├── runs.py
│   ├── events.py
│   ├── tools.py               # 4 个工具的参数与返回模型
│   ├── eval.py
│   ├── gate.py
│   └── agents.py
├── db/                        # 持久化
│   ├── base.py                # DeclarativeBase
│   ├── models.py              # 8 张表
│   ├── session.py             # engine / SessionLocal / init_db
│   └── repository.py          # 仓储：run/event/tool_call/model_call/评测的读写
├── agent/                     # 示例 Agent
│   ├── state.py               # LangGraph 状态定义（TypedDict）
│   ├── graph.py               # 固定 5 节点图 + 条件边
│   ├── middleware.py          # Trace 中间件：包装节点与工具调用
│   ├── llm.py                 # LLM 网关（真实 provider / fake provider）
│   └── nodes/                 # 5 个节点实现
│       ├── question_parser.py
│       ├── document_search.py
│       ├── evidence_checker.py
│       ├── answer_writer.py
│       └── final_validator.py
├── tools/                     # 工具治理
│   ├── registry.py            # 注册表：发现、校验、调用、计时、落库
│   ├── base.py                # ToolSpec / ToolResult 协议
│   ├── document_search.py     # search_documents, get_document
│   └── analytics.py           # calculate_latency_summary, calculate_cost_summary
├── services/                  # 应用服务（跨层编排）
│   ├── run_service.py         # 创建运行、执行、回放
│   ├── cache.py               # Redis 短期任务状态（不缓存最终答案）
│   └── metrics_service.py     # 运行级指标汇总
└── evaluation/                # 离线评测
    ├── dataset.py             # JSONL 加载与校验
    ├── runner.py              # 逐 case 执行、结果落库
    ├── metrics.py             # 10 个指标的计算公式实现
    └── gate.py                # 阈值门禁
```

### 分层规则

| 规则 | 说明 |
|---|---|
| L1 | `api/` 不允许直接引用 `db.models`，只能通过 `services/` 或 `db.repository` |
| L2 | `agent/` 与 `tools/` 不允许直接持有连接或提交事务，必须通过注入的 `TraceRecorder` |
| L3 | `core/` 不依赖任何其他内部模块（除 `core` 内部） |
| L4 | 工具实现体不允许访问网络（样例阶段）与数据库，只能读 `data/sample_docs/` |
| L5 | 指标计算只在 `evaluation/metrics.py` 与 `services/metrics_service.py` 中实现，不允许散落 |

## 3. 请求时序：执行一次 Agent 运行

```mermaid
sequenceDiagram
    participant C as Client
    participant A as POST /runs
    participant RS as RunService
    participant G as LangGraph
    participant MW as TraceMiddleware
    participant T as ToolRegistry
    participant R as Repository
    participant DB as PostgreSQL

    C->>A: {"question": "...", "agent_version": "v1"}
    A->>A: Pydantic 校验 (含 max_length)
    A->>RS: execute(question, config)
    RS->>R: create_run(status="running") -> run_id
    R->>DB: INSERT run
    RS->>G: ainvoke(initial_state)
    loop 5 个节点
        G->>MW: node_enter(node_name, input_summary)
        MW->>R: INSERT trace_event(node)
        G->>T: invoke(tool_name, raw_args)
        T->>T: Pydantic 校验参数
        alt 参数合法
            T->>T: 执行实现体 + 计时
            T->>R: INSERT tool_call(validated=true)
        else 参数非法
            T->>R: INSERT tool_call(status=invalid_arguments)
            T-->>G: ToolResult(error)
        end
        MW->>R: INSERT trace_event(tool_call)
        G->>MW: node_exit(status, output_summary)
        MW->>R: UPDATE trace_event SET ended_at, duration_ms
    end
    G-->>RS: final_state
    RS->>R: finalize_run(status, result_summary)
    R->>DB: UPDATE run
    RS-->>A: RunDetail
    A-->>C: 201 + run_id + status + duration_ms
```

## 4. Trace 写入路径

Trace 写入有两条路径，职责分离：

| 路径 | 触发者 | 写入内容 |
|---|---|---|
| 节点级 | `TraceMiddleware.node_scope()` | 一条 `trace_event`（`event_type=node`），开闭两次写（INSERT + UPDATE 补 `ended_at`/`duration_ms`） |
| 工具级 | `ToolRegistry.invoke()` | 一条 `tool_call` + 一条 `trace_event`（`event_type=tool_call`），后者的 `event_id` 作为 `parent_event_id` 挂在节点事件下 |
| 模型级 | `LLMGateway.complete()` | 一条 `model_call` + 一条 `trace_event`（`event_type=model_call`） |
| 错误级 | `core/errors.py` 的捕获点 | 一条 `trace_event`（`event_type=error`），带 `error_code` |
| 终结 | `RunService._finalize()` | 一条 `trace_event`（`event_type=final_result`） |

**写入失败处理**：Trace 写入失败**不得静默吞掉**。策略：
- 运行中写入失败 → 抛出 `TracePersistenceError`，把当前 run 标记为 `status="failed"`、`error_code="TRACE_WRITE_FAILED"`，并把异常返回给调用方；
- `finalize` 阶段写入失败 → 记 `ERROR` 级 JSON 日志，并把 run 标记为 `failed`，API 返回 500 且明确错误码。

## 5. 错误模型

统一错误响应体：

```json
{
  "error": {
    "code": "RUN_NOT_FOUND",
    "message": "Run 'abc' was not found.",
    "details": {"run_id": "abc"},
    "request_id": "req_01H..."
  }
}
```

| HTTP | code | 触发场景 |
|---|---|---|
| 400 | `INVALID_ARGUMENT` | Pydantic 校验失败、`max_length` 超限 |
| 404 | `RUN_NOT_FOUND` | run_id 不存在 |
| 404 | `EVALUATION_NOT_FOUND` | evaluation_id 不存在 |
| 409 | `RUN_NOT_REPLAYABLE` | run 仍在 running 状态 |
| 422 | `AGENT_VALIDATION_ERROR` | 请求体结构合法但业务语义非法 |
| 500 | `TRACE_WRITE_FAILED` | Trace 落库失败 |
| 500 | `INTERNAL_ERROR` | 未归类异常 |
| 502 | `LLM_PROVIDER_ERROR` | 真实 LLM 调用失败 |
| 504 | `AGENT_TIMEOUT` | 超过 `AGENT_TIMEOUT_SECONDS` |

## 6. 配置与密钥

| 配置项 | 来源 | 默认 | 说明 |
|---|---|---|---|
| `DATABASE_URL` | env | `postgresql+psycopg://agenttrace:agenttrace@localhost:5432/agenttrace` | 可为 `sqlite+pysqlite:///./agenttrace.db` |
| `REDIS_URL` | env | `redis://localhost:6379/0` | 短期任务状态 |
| `LLM_PROVIDER` | env | `fake` | `fake` / `openai` / `ollama` |
| `LLM_API_KEY` | **仅 env** | 空 | 绝不入库、绝不进日志 |
| `LLM_BASE_URL` | env | 空 | OpenAI-compatible endpoint |
| `LLM_MODEL` | env | `gpt-4o-mini` | 模型名 |
| `AGENT_TIMEOUT_SECONDS` | env | `60` | 单次运行超时 |
| `MAX_QUESTIONS_PER_RUN` | env | `1` | 最大输入长度受 `MAX_QUESTION_CHARS` 控制 |
| `MAX_QUESTION_CHARS` | env | `2000` | 输入长度上限 |
| `TOOL_MAX_RETRIES` | env | `2` | 工具重试上限 |
| `SUMMARY_MAX_CHARS` | env | `500` | Trace 摘要截断 |
| `ENABLE_REAL_LLM_TESTS` | env | `false` | 真实 LLM 集成测试开关 |

`LLM_PROVIDER=fake` 时**不进行任何网络调用**，这是默认测试路径。`openai`/`ollama` 才是真实集成路径，且必须显式设置。

## 7. 部署形态

```mermaid
flowchart LR
    subgraph Compose["docker compose"]
        API["api:8000<br/>python:3.12-slim"]
        PG[("postgres:16-alpine<br/>:5432, volume pgdata")]
        RD[("redis:7-alpine<br/>:6379")]
    end
    HOST["宿主机 :8000"] --> API
    API --> PG
    API --> RD
```

三个服务通过 `depends_on` + healthcheck 串联，API 在 PG/Redis 健康后才启动。

## 8. 并发与性能取舍

| 取舍 | 决策 | 理由 |
|---|---|---|
| 同步 vs 异步 DB | 使用**同步** SQLAlchemy Session，FastAPI 端点用 `def`（线程池执行） | Trace 写入是短事务、追加型；同步栈可读性高、调试容易，且避免 async session 与 LangGraph 事件循环的混用问题 |
| 事件批量写 | 逐条 INSERT | 需要每条事件即时可查（失败定位要求）；批量写会牺牲可观测性 |
| 评测并发 | 串行执行 case | 默认 `LLM_PROVIDER=fake` 时耗时主要在本地，串行更易复现；真实模型模式可后续加并发上限 |
| Redis 用途 | 仅任务状态 | 契约明确禁止缓存最终答案，避免"读到旧答案"污染评测 |
