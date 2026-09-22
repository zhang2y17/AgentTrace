# 安全说明

AgentTrace 是一个**个人独立开发项目**，用于记录、回放、评测和分析 LLM Agent 的执行过程。
本文说明它的密钥处理、日志与数据边界、已知威胁模型，以及**明确不提供的安全能力**。

> **一句话结论**：本项目按"本地开发与演示工具"设计。
> 它**没有鉴权、没有多租户隔离、没有传输加密**，不应直接暴露在公网。
> 详见 [不提供的安全能力](#不提供的安全能力)。

---

## 1. 密钥处理

### 1.1 唯一来源：环境变量

所有密钥只从环境变量读取，经由 `app/core/config.py` 的 `Settings` 暴露。
代码中**不存在**任何 `os.environ` 直接读取密钥的路径 —— 配置读取只有一个入口。

| 配置项 | 用途 | 敏感 |
|---|---|---|
| `LLM_API_KEY` | 调用真实模型提供方 | **是** |
| `LLM_BASE_URL` | 自定义端点，**可能内嵌凭据**（`https://user:pass@host/v1`） | 可能 |
| `DATABASE_URL` | 数据库连接串，**可能内嵌密码** | 可能 |

`LLM_API_KEY` 用 Pydantic 的 `SecretStr` 承载。这意味着它在被显式
`get_secret_value()` 之前**不会**出现在 `repr()`、日志或错误信息里。
代码中只有出现在真正构造 HTTP 请求头的一处会调用它。

### 1.2 密钥不落库

`tests/api/test_secret_boundary.py` 会向运行中的应用注入真实形态的密钥
（`LLM_API_KEY`、带密码的 `LLM_BASE_URL`），跑完一次完整 Agent 运行后
**扫描数据库所有表的全部单元格**，断言密钥值不出现。

这条测试比"代码里搜不到写密钥的语句"强得多 —— 后者无法覆盖间接写入
（例如某个 summary 恰好带上了整个配置对象）。

### 1.3 密钥不进镜像

- `.dockerignore` 排除 `.env` / `.env.*`（保留 `.env.example`）；
- `Dockerfile` 不含任何密钥的 `ENV` 默认值或构建参数；
- CI 的 `secret-scan` 任务扫描提交内容，并校验 `.env` 未被 `git add`。

### 1.4 用户输入里的密钥：一个刻意的例外

如果用户在 `question` 里粘贴了一个密钥，它会**原样保存在 `run.question` 字段**。

这是有意的设计，不是遗漏：

- `question` 是"当时到底问了什么"的**权威记录**。改写它会让回放
  无法重现原始输入，而回放的可复现性正是本项目评测结论可信的前提；
- 该由脱敏承担的是**派生数据**（Trace 的 `input_summary` / `output_summary`），
  那些字段存在的唯一理由是"为了可观测性额外记录"，没有任何理由承载密钥明文。

两层期望都有测试固定：`TestUserInputVerbatimVsDerived` 断言 `question` 保持原文，
`TestTraceSummariesAreRedacted` 断言派生数据里密钥被替换为 `[REDACTED]` 占位符。

---

## 2. 日志与 Trace 的脱敏

所有写入日志或数据库的输入输出都经过 `app/core/redaction.py`。
该模块在**数据层写入点**（`repository.append_event` / `close_event`）被强制执行，
不是靠各调用方自觉 —— 调用方可以忘记脱敏，但写不进去。

替换的模式：

| 模式 | 匹配内容 | 占位符 |
|---|---|---|
| `openai_key` | `sk-` 开头的密钥 | `[REDACTED_API_KEY]` |
| `bearer_token` | `Bearer <token>` | `[REDACTED_BEARER]` |
| `assignment_secret` | `api_key=...` / `"token": "..."` 等键值形式 | `[REDACTED]`（**只替换值**，保留键与分隔符） |
| `url_credentials` | `https://user:pass@host` 中的凭据段 | `[REDACTED_CREDENTIALS]` |
| `cn_phone` | 中国大陆手机号 | `[REDACTED_PHONE]` |
| `email` | 邮箱地址 | `[REDACTED_EMAIL]` |

另外，所有摘要经 `truncate` 截断（默认 500 字符，`SUMMARY_MAX_CHARS` 可配），
截断标记中保留原始长度（`...[truncated:12345]`），便于判断内容是否完整。

**脱敏的已知局限**：

- 模式匹配而非语义识别。**全新的密钥格式**（例如某家新提供商的短 token）
  在模式更新前不会被命中。若你接入的提供方密钥形态特殊，请自行扩展 `_PATTERNS`；
- 键名判定是子串匹配，可能**误报**（把名为 `monkey` 的字段当作敏感键）。
  误报不只是"多脱敏一点"：替换发生在**写入时且不可逆**，被误判的字段值会
  永久变成占位符。因此误报会影响数据正确性，必须认真对待。
  **已知的一类边界**：`token` 只在**词尾**判定为凭证义（`token` / `access_token` /
  `authToken`），而 `total_tokens` / `prompt_tokens` / `completion_tokens` /
  `max_tokens` 这类**计数**字段一律放过 —— 它们的值必然是数字，脱敏只会毁掉
  Trace 的用量数据。若你新增的字段是"单数 `token` + 计量后缀"命名
  （如 `token_count`），它会被判为不敏感；这是刻意的，请改用更明确的名字而不是放宽规则；
- 不识别图片、音频等非文本内容里的敏感信息（本项目不处理这类输入）。

---

## 3. 数据边界

| 类别 | 内容 | 是否可提交到仓库 |
|---|---|---|
| 演示文档语料 | `data/sample_docs/`，项目自建的合成技术文档 | 可 |
| 评测集 | `data/eval/doc_research_v1.jsonl`，自建问题与期望 | 可 |
| 价目表 | `app/evaluation/pricing.json`，**公开的参考价** | 可 |
| 运行记录 | 数据库中的 `run` / `trace_event` 等 8 张表 | **否**（含用户输入，属本地数据） |
| `.env` | 真实密钥 | **否**（已被 `.gitignore` 与 `.dockerignore` 排除） |

**本项目不使用任何公司代码、公司数据、公司接口或未公开业务信息。**
所有演示数据均为自建/公开/合成数据。若你要用自己的数据进行评测，
请自行确认数据合规性 —— 尤其是 `question` 字段（它按设计原样保存）。

---

## 4. 威胁模型

本项目按**单机、单用户、可信环境**建模。以下是显式接受的风险与理由。

| 威胁 | 现状 | 理由 / 缓解 |
|---|---|---|
| **无鉴权**：任何能访问端口的人都能调用全部接口 | 不缓解 | 契约未要求；面向本地开发与演示。**不要暴露到公网** |
| **无传输加密**：HTTP 明文 | 不缓解 | 同上。如需公网访问，请置于带 TLS 的反向代理之后 |
| **无租户隔离**：所有数据在同一库中 | 不缓解 | 单用户场景下租户概念无意义 |
| **SQL 注入** | 缓解 | 全部查询经 SQLAlchemy 表达式构造，无字符串拼接 SQL |
| **路径穿越**：`dataset_version` 拼进文件路径 | 缓解 | `dataset_path()` 用 `relative_to` 校验解析结果必须落在评测集目录内，并有测试覆盖 |
| **提示词注入**：用户让 Agent 忽略指令 | 部分缓解 | `final_validator` 节点做结构校验（引用真实性等）；但**内容安全不在范围内**，本项目不做输入内容审查 |
| **资源耗尽**：超大输入 / 超长运行 | 缓解 | `MAX_QUESTION_CHARS`（默认 2000）、`AGENT_TIMEOUT_SECONDS`（默认 60）、`TOOL_TIMEOUT_SECONDS`（默认 10）、`LLM_MAX_RETRIES`、`TOOL_MAX_RETRIES` 全部可配且有边界校验 |
| **容器以 root 运行** | 缓解 | `Dockerfile` 创建并切换到非 root 用户（uid 1001） |
| **依赖供应链** | 缓解 | CI 全绿才算通过；但未锁定哈希，也未启用 Dependabot |
| **日志注入**：输入含换行伪造日志行 | 缓解 | 日志为结构化 JSON（一行一个对象），换行被转义，无法伪造独立日志条目 |
| **信息泄露经错误信息** | 缓解 | 统一错误响应体只含 `code` / `message` / `details` / `request_id`；`message` 面向调用方且已脱敏；堆栈只进服务端日志 |

### 明确不做的安全能力

以下能力**本版本不提供**，且不是"忘了做"：

- 用户认证 / 授权 / API Key 鉴权；
- 多租户数据隔离；
- TLS / mTLS；
- 速率限制的服务端强制（依赖前置代理）；
- 输入内容的合规审查；
- 密钥轮换与 KMS 集成；
- 审计日志的防篡改存储。

---

## 5. 报告安全问题

这是一个个人项目，没有安全响应团队。若仓库已启用 GitHub Private
Vulnerability Reporting，请在仓库的 **Security → Advisories → Report a
vulnerability** 中私密报告。若该入口不可用，可以开一个不包含漏洞细节的
GitHub Issue，请维护者建立私密沟通渠道后再提供复现信息。

请在报告中**不要**包含真实密钥、真实用户数据或生产环境信息。
用自造的假数据复现即可。

---

## 6. 部署检查清单

若你打算在受控环境中运行本项目：

- [ ] 用 `.env.example` 生成 `.env`，确认 `.env` 未被提交（`git status` 应无它）；
- [ ] `LLM_API_KEY` 用最小权限的密钥，不要复用其他系统的凭据；
- [ ] 用 `docker compose` 时，把 `POSTGRES_PASSWORD` 从默认的 `agenttrace` 改掉；
- [ ] 确认服务未直接暴露在公网（`docker compose` 默认会映射端口到宿主机）；
- [ ] 生产场景把 `AUTO_CREATE_TABLES` 设为 `false` 并显式跑 `scripts/init_db.py`；
- [ ] 生产场景不要在每次启动时播种数据（`docker-compose.yml` 的注释里说明了改法）；
- [ ] 确认日志的落盘位置与保留策略符合你的要求（日志含 `question` 等用户输入）。
