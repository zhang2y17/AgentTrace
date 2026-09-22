# ACCEPTANCE_CHECKLIST.md — AgentTrace 验收清单

## 使用方式

每条检查项都有 **ID**、**验证方式**、**通过判据**、**结果**、**类别**。

| 类别 | 含义 |
|---|---|
| `AUTO` | 由自动化命令判定（pytest / ruff / docker compose config / grep） |
| `MANUAL` | 需人工阅读或手动执行判定 |
| `REAL-INTEGRATION` | 需要真实 PostgreSQL / Redis / 真实 LLM，属集成测试 |

结果列在 S8 阶段填写：`PASS` / `FAIL` / `RISK` / `SKIP`。

---

## A. 环境与安装

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| A-01 | Python 版本符合契约 T1 | `python --version` | ≥ 3.12 | AUTO | |
| A-02 | 依赖可安装 | `pip install -r requirements.txt` | 退出码 0，无冲突 | AUTO | |
| A-03 | `pyproject.toml` 声明 `requires-python` | `grep requires-python pyproject.toml` | 含 `>=3.12` | AUTO | |
| A-04 | `.env.example` 覆盖全部配置项 | 对比 Settings 字段与 `.env.example` | 无遗漏、无多余 | AUTO | |
| A-05 | 无 `.env` 时应用仍可启动 | 删除 `.env` 后 `pytest tests -q` | 全部通过（默认值生效） | AUTO | |

## B. 容器与编排

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| B-01 | compose 文件语法合法 | `docker compose config` | 退出码 0 | AUTO | |
| B-02 | 三服务齐备 | `docker compose config --services` | 含 `api`、`postgres`、`redis` | AUTO | |
| B-03 | 依赖顺序正确 | 阅读 `depends_on` | api 依赖 pg/redis 且 `service_healthy` | MANUAL | |
| B-04 | 服务可启动 | `docker compose up -d` | 三容器 Up | REAL-INTEGRATION | |
| B-05 | 健康检查生效 | `docker compose ps` | api 显示 `healthy` | REAL-INTEGRATION | |
| B-06 | Dockerfile 非 root 运行 | 阅读 Dockerfile | 有 `USER` 指令且非 root | MANUAL | |
| B-07 | `.dockerignore` 存在且排除敏感文件 | `cat .dockerignore` | 排除 `.env`、`.git`、`__pycache__` | AUTO | |

## C. 契约一致性（文档 ↔ 代码）

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| C-01 | 5 个节点名一致 | grep 节点名于 docs 与 `app/agent/nodes/` | 完全一致 | AUTO | |
| C-02 | 4 个工具名一致 | grep 工具名于 docs 与 `app/tools/` | 完全一致 | AUTO | |
| C-03 | 6 类 `event_type` 一致 | 对比 `EventType` 枚举与 TRACE_SCHEMA §2 | 完全一致 | AUTO | |
| C-04 | `trace_event` 12 个必需字段齐备 | 对比模型列与 TRACE_SCHEMA §3 | 无遗漏 | AUTO | |
| C-05 | 8 张表齐备 | 对比 `Base.metadata.tables` 与 DATA_MODEL §2 | 8 张，无多余 | AUTO | |
| C-06 | 10 个 API 端点齐备 | 读 `/openapi.json` 的 paths | 10 条路径全部存在 | AUTO | |
| C-07 | 12 个指标名一致（M1~M10 编号，M6 展开三分位数） | 对比 `metrics.py` 导出与 EVALUATION §2 | 完全一致 | AUTO | |
| C-08 | ID 前缀符合 DATA_MODEL §4 | 检查 `core/ids.py` | 前缀与文档一致 | AUTO | |

## D. 功能：运行与 Trace

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| D-01 | 一次运行产生完整 Trace | `curl POST /runs` + `GET /runs/{id}/events` | 同时含 `run`/`node`/`tool_call`/`model_call`/`final_result` | AUTO | |
| D-02 | 节点事件带耗时 | 检查 `node` 事件的 `duration_ms` | 非 null | AUTO | |
| D-03 | `parent_event_id` 树形正确 | 校验每个非根事件的父存在且同 run | 无孤儿、无环 | AUTO | |
| D-04 | `sequence` 同 run 内唯一且递增 | `GET /events` 后校验 | 严格递增、无重复 | AUTO | |
| D-05 | 工具调用落库 | 查询 `tool_call` | 含 `tool_name`/`arguments`/`validated`/`duration_ms` | AUTO | |
| D-06 | 模型调用落库含 Token 与成本 | 查询 `model_call` | 三个 token 字段非 null，成本有值 | AUTO | |
| D-07 | 事件类型过滤生效 | `?event_type=node` | 返回结果全为 `node` | AUTO | |
| D-08 | 状态过滤生效 | `?status=failed,retried` | 返回结果状态在集合内 | AUTO | |
| D-09 | 失败运行仍产生完整 Trace | 用非法输入触发失败 | 有 `error` 事件 + run 状态为 `failed` | AUTO | |
| D-10 | 工具参数非法被拦截 | 调用 `top_k=999` | `tool_call.status=invalid_arguments`，实现体未被调用 | AUTO | |
| D-11 | 证据不足触发放宽检索 | 用低覆盖问题 | 出现第二次 `search_documents`，`retry_count=1` | AUTO | |
| D-12 | 重试耗尽标记 `handoff` | 构造无法满足的问题 | 存在 `status=handoff` 的事件，run 终态非 `succeeded` | AUTO | |

## E. 功能：查询与回放

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| E-01 | 回放生成新 `run_id` | `POST /runs/{id}/replay` | 返回 `run_id != 原 run_id` | AUTO | |
| E-02 | 回放保留 `source_run_id` | 同上响应 | `source_run_id == 原 run_id` | AUTO | |
| E-03 | 原记录未被覆盖 | 回放前后对比原 run 行 | 字段完全不变 | AUTO | |
| E-04 | 回放可覆盖 prompt 版本 | 传 `prompt_version=prompt-v2` | 新 run 的 `prompt_version` 为 `prompt-v2` | AUTO | |
| E-05 | 无效 run_id 返回 404 | `GET /runs/run_nonexistent` | 404 + `RUN_NOT_FOUND` | AUTO | |
| E-06 | running 状态不可回放 | 对 running run 调 replay | 409 + `RUN_NOT_REPLAYABLE` | AUTO | |
| E-07 | 超长输入返回 400 | `question` 超 2000 字符 | 400 + `INVALID_ARGUMENT` | AUTO | |
| E-08 | 超时返回 504 | 设 `AGENT_TIMEOUT_SECONDS=0.01` 并运行 | 504 + `AGENT_TIMEOUT`，run 落库为 `timeout` | AUTO | |

## F. 功能：评测与门禁

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| F-01 | 评测集可加载并通过 schema 校验 | `python scripts/run_eval.py --dataset doc_research_v1` | 退出码 0，case 数 = 14 | AUTO | |
| F-02 | 12 个指标全部产出 | 查看评测响应 `metrics` | 12 个契约指标键全部存在且非缺失 | AUTO | |
| F-03 | 指标公式与 EVALUATION §3 一致 | 单测对照手算值 | 误差 < 1e-9 | AUTO | |
| F-04 | 分母为 0 时返回 `null` 而非崩溃 | 空评测集单测 | `null`，无异常 | AUTO | |
| F-05 | 失败 case 保存原因 | `GET /evaluations/{id}?include_cases=true&only_failures=true` | 每条含 `failure_reason` + `assertion_results` | AUTO | |
| F-06 | 失败 case 可关联 Trace | 用返回的 `run_id` 查事件 | 可查到事件 | AUTO | |
| F-07 | 支持三维度分组对比 | `group_by=agent_version/prompt_version/model_name` | 三种都返回 `groups` | AUTO | |
| F-08 | 门禁通过场景 | 用宽松阈值调用 | `passed=true`，`violations=[]` | AUTO | |
| F-09 | 门禁阻断场景 | 用严苛阈值调用 | `passed=false`，`blocked=true`，`violations` 非空 | AUTO | |
| F-10 | 门禁记录阈值快照 | 查 `quality_gate` 行 | `thresholds` 与请求一致 | AUTO | |
| F-11 | CLI 退出码语义正确 | 分别触发通过/阻断/异常 | 依次为 0 / 1 / 2 | AUTO | |
| F-12 | 未知指标名阈值被拒绝 | 传 `{"unknown_metric":{"min":0}}` | 400 + `INVALID_ARGUMENT` | AUTO | |

## G. 安全与数据边界

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| G-01 | API Key 仅从环境变量读取 | grep 代码中的 key 读取点 | 无硬编码、无文件读取 | AUTO | |
| G-02 | 仓库内无真实密钥 | `grep -riE "sk-[A-Za-z0-9]{16,}"` | 仅命中测试用假串且被标注 | AUTO | |
| G-03 | 日志不输出完整 Prompt | 检查日志调用点 | 只输出摘要/长度 | MANUAL | |
| G-04 | 日志不输出 API Key | 运行一次并查日志 | 无 key 明文 | AUTO | |
| G-05 | Trace 摘要已截断 | 用超长输入运行 | 摘要长度 ≤ `SUMMARY_MAX_CHARS` + 后缀 | AUTO | |
| G-06 | 脱敏覆盖 6 类模式 | 单测逐模式断言 | 全部替换为 `[REDACTED_*]` | AUTO | |
| G-07 | 最大输入长度生效 | 见 E-07 | 400 | AUTO | |
| G-08 | 工具重试有上限 | 构造持续失败工具 | 重试次数 ≤ `TOOL_MAX_RETRIES` | AUTO | |
| G-09 | 无公司名称/内部域名 | `grep -riE "tencent|bytedance|alibaba|internal\.|corp\."` | 无命中 | AUTO | |
| G-10 | `.env` 未被 git 跟踪 | `git check-ignore .env` | 被忽略 | AUTO | |
| G-11 | README 标注个人项目 | 读 README | 含明确声明 | MANUAL | |
| G-12 | 无越权表述 | grep "生产|企业级|线上|客户" | 无未加限定语的此类表述 | MANUAL | |

## H. 测试替身与真实集成边界

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| H-01 | 默认测试不访问网络 | 断网或代理拦截下跑 pytest | 全部通过 | AUTO | |
| H-02 | 默认测试不需要 API Key | 清空 key 环境变量后跑 pytest | 全部通过 | AUTO | |
| H-03 | 替身有明确标注 | grep `is_test_double` / `Fake` 类 docstring | 存在且语义清晰 | AUTO | |
| H-04 | 真实 LLM 测试默认跳过 | 跑 `pytest tests/integration` | skip 而非 fail | AUTO | |
| H-05 | 真实 LLM 测试可显式开启 | `ENABLE_REAL_LLM_TESTS=true` 且配好 key | 尝试真实调用（或明确报缺 key） | REAL-INTEGRATION | |
| H-06 | 无固定成功结果冒充集成测试 | 阅读 integration 测试 | 无硬编码 `assert True` 式假测试 | MANUAL | |
| H-07 | 评测结果标注数据来源 | 查看响应 `scope` / `data_source_note` | 存在且语义正确 | AUTO | |

## I. 交付物完整性

| ID | 文件 | 类别 | 结果 |
|---|---|---|---|
| I-01 | `README.md`（含架构图、命令、限制） | MANUAL | |
| I-02 | `LICENSE` | AUTO | |
| I-03 | `SECURITY.md`（密钥/日志/数据边界） | MANUAL | |
| I-04 | `CONTRIBUTING.md` | AUTO | |
| I-05 | `Dockerfile` + `.dockerignore` | AUTO | |
| I-06 | `docker-compose.yml` | AUTO | |
| I-07 | `pyproject.toml` + `requirements.txt` | AUTO | |
| I-08 | `.env.example` | AUTO | |
| I-09 | `docs/PROJECT_SPEC.md` 等 7 份设计文档 | AUTO | |
| I-10 | `scripts/init_db.py` + `scripts/seed_data.py` | AUTO | |
| I-11 | `data/sample_docs/*.md`（≥6 篇） | AUTO | |
| I-12 | `data/eval/doc_research_v1.jsonl`（14 case） | AUTO | |
| I-13 | `tests/unit` + `tests/api` + `tests/smoke` + `tests/integration` | AUTO | |
| I-14 | `docs/diagrams/agent_flow.mmd` + `eval_flow.mmd` | AUTO | |
| I-15 | `.github/workflows/ci.yml`（lint + pytest + compose config） | AUTO | |

## J. CI 与可复现性

| ID | 检查项 | 验证方式 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|---|
| J-01 | CI 含 lint 步骤 | 阅读 workflow | 有 `ruff check` | AUTO | |
| J-02 | CI 含 pytest 步骤 | 阅读 workflow | 有 `pytest` | AUTO | |
| J-03 | CI 含 compose config 步骤 | 阅读 workflow | 有 `docker compose config` | AUTO | |
| J-04 | CI 不依赖密钥 | 检查 workflow env/secrets | 无 secret 依赖 | AUTO | |
| J-05 | README 命令可原样执行 | 逐条执行 README 中的启动/运行/评测命令 | 全部成功 | MANUAL | |
| J-06 | 测试全部通过 | `python -m pytest tests -q` | 0 failed | AUTO | |
| J-07 | Lint 全绿 | `ruff check .` | 退出码 0 | AUTO | |
| J-08 | 无未跟踪的敏感文件 | `git status --porcelain` | 无 `.env`、无 `*.db` | AUTO | |

## K. 诚实性（项目特有，权重最高）

| ID | 检查项 | 通过判据 | 类别 | 结果 |
|---|---|---|---|---|
| K-01 | 无编造的用户量/客户/收益 | 全文 grep 无此类数据 | MANUAL | |
| K-02 | 每个指标标注数据来源 | 所有指标响应含 `scope` 或 `data_source_note` | AUTO | |
| K-03 | 已知限制章节存在且具体 | README 有 ≥5 条具体限制 | MANUAL | |
| K-04 | 不声明"生产可用" | grep 无未经限定的此类表述 | MANUAL | |
| K-05 | 样例数据来源声明 | README/EVALUATION 说明为自建合成数据 | MANUAL | |
| K-06 | 成本估算限制已声明 | EVALUATION §3 M8 三条限制可见 | MANUAL | |
| K-07 | 门禁语义边界已声明 | EVALUATION §6.3 三条可见 | MANUAL | |

---

## 最终交付命令清单

```bash
# 1. 安装
pip install -r requirements.txt

# 2. 配置
cp .env.example .env

# 3. 启动依赖（PostgreSQL + Redis）
docker compose up -d postgres redis

# 4. 初始化数据库
python scripts/init_db.py
python scripts/seed_data.py

# 5. 启动 API（本地开发）
uvicorn app.main:app --reload --port 8000

# 6. 或整体容器化启动
docker compose up -d --build

# 7. 健康检查
curl -s http://localhost:8000/health

# 8. 运行一次 Agent
curl -s -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"question":"AgentTrace 如何记录一次工具调用？"}'

# 9. 测试
python -m pytest tests -q

# 10. Lint
ruff check .

# 11. 离线评测 + 质量门禁
python scripts/run_eval.py --dataset doc_research_v1 --gate
echo "退出码: $?"
```
