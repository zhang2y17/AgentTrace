# 贡献指南

感谢你的关注。这是一个**个人独立开发项目**，主要目的是作为可公开阅读的
工程实践样本。欢迎 Issue 与 Pull Request，但请先了解下面几条约束 ——
它们不是客套话，而是这个项目的立身之本。

---

## 最重要的一条：项目的边界

**不得引入任何公司代码、公司数据、公司接口或未公开业务信息。**

具体来说，以下内容**不会**被合并：

- 来自你所在公司内部仓库的代码片段（哪怕是你自己写的）；
- 真实的生产数据、用户数据、日志样本、trace 导出；
- 内部系统名、内部接口地址、内部服务名、未公开的架构细节；
- 任何"我们线上大概有 X 万用户"这类无法公开验证的规模描述；
- 真实的 API Key、Token、连接串（**即使是已失效的**）。

演示与测试数据必须是**自建、公开或合成**的。若你新增评测用例或样例文档，
请在 PR 描述里说明数据的来源与生成方式。

## 第二条：不得写无法由测试证明的表述

这个项目的所有数字都必须能被 `pytest` 复现。因此：

- **不要**在 README、docstring 或注释里写"生产可用""企业级""高并发""高性能"；
- **不要**声称本项目有线上用户量、客户数、收入或覆盖率数字；
- **不要**把测试替身跑出的指标描述为模型能力或业务效果；
- 可以写"测试通过""这些边界有测试覆盖""本版本不提供鉴权"，并给出对应测试位置。

如果某个结论无法由测试证明，就把它写成"已知限制"。

## 第三条：替换与真实调用必须严格区分

任何涉及模型调用的改动，都要保证：

1. 默认测试路径**不访问网络、不需要任何 API Key**；
2. 测试替身产生的数据必须标注 `is_test_double: true`；
3. 真实模型集成测试必须放在 `tests/integration` 且默认跳过
   （通过 `ENABLE_REAL_LLM_TESTS=true` 显式开启）。

---

## 开发环境

```bash
git clone https://github.com/zhang2y17/AgentTrace.git agenttrace
cd agenttrace

python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# source .venv/bin/activate        # macOS / Linux

pip install -r requirements.txt
cp .env.example .env               # 按需修改；LLM_PROVIDER 保持 fake 即可
```

Python 版本要求 **3.12+**（见 `pyproject.toml` 的 `requires-python`）。

数据库不是必需的：默认测试会用临时 SQLite 文件，
`tests/conftest.py` 的 `isolated_env` 夹具负责为每个测试准备隔离环境。

## 提交前必须全绿

```bash
ruff check .                 # lint
ruff format --check .        # 格式（保持一致，避免 PR 里混入无关 diff）
python -m pytest tests -q    # 全部测试
python scripts/verify_contract.py   # 代码 ↔ docs/contract.lock.json 一致性
```

CI 会跑同样的四件事，另外加 `docker compose config --quiet` 与密钥扫描。
本地全绿但 CI 失败，通常是因为漏了后两项。

### 关于 `ruff format`

本项目采用 `ruff format` 作为唯一格式标准。提交前请执行 `ruff format .`，
不要手工调整缩进或换行 —— 也不要提交只改格式、不改语义的 PR。

---

## 契约优先

`docs/` 下的文档不是"事后补的说明"，而是**先定契约、后写实现**：

| 文档 | 约束什么 |
|---|---|
| `PROJECT_SPEC.md` | 项目边界、5 个节点、4 个工具 |
| `API_CONTRACT.md` | 端点、状态码、错误码、字段形状 |
| `DATA_MODEL.md` | 8 张表的字段级定义 |
| `TRACE_SCHEMA.md` | 6 类事件、12 个必需字段 |
| `EVALUATION.md` | 指标公式、分母、边界、反模式 |
| `contract.lock.json` | 机器可校验的契约快照 |

**改了契约就要同步改 `contract.lock.json`，并让 `verify_contract.py` 通过。**
如果实现需要偏离契约，先改文档与 lock 文件，再改代码 —— 反过来做的话，
文档会慢慢变成一份和代码无关的说明书。

## 代码约定

- **类型标注**：公开函数都要有完整标注；项目不开 `mypy`，但标注要准确；
- **注释说"为什么"**：代码说"做什么"。解释反直觉的取值、被排除的方案、
  容易踩的边界。不要写 `# 循环遍历列表` 这种复读；
- **中文注释**：项目内注释与文档使用中文，代码标识符用英文；
- **禁止静默失败**：配置错误、未知字段、非法参数一律**报错**，
  不要吞掉或退化成默认值。这个项目里"安静地做了别的事"比"抛异常"危险得多；
- **`None` 与 `0` 不可混用**：空数据返回 `None`（"无从计算"），
  计数型返回 `0`（"确实是零"）。两者含义不同，混淆会让报告说谎。

## 测试约定

- 测试名要能读出**在验什么**，docstring 说明**为什么这条边界重要**
  （尤其是"如果放宽会怎样"）；
- 优先覆盖边界与错误路径，而不是把每个正常分支都跑一遍；
- 新增配置项时同步更新 `.env.example` —— 有测试会检查两者一致
  （`tests/unit/test_config.py::TestEnvExampleCoverage`）；
- 不要为了让测试通过而放宽契约模型（`extra="forbid"`）。
  响应多出未声明字段时，应该在 API 边界收敛，而不是让契约变得模糊。

## 提交信息

用中文或英文都可以，但要说清**做了什么**与**为什么**。
若修的是隐蔽的 bug（尤其是"看起来正常但语义错了"的那类），
请在提交信息里写清症状与根因 —— 这类信息比 diff 本身更有价值。

格式参考：

```
S6: 离线评测与质量门禁

- 新增 data/eval/doc_research_v1.jsonl（14 case）
- ...

修了一个静默失败：thresholds={} 会覆盖默认阈值，导致门禁恒通过
```

## Pull Request

PR 描述里请包含：

1. 解决的问题；
2. 实现方式与**被否决的替代方案**（如果有）；
3. 如何验证（具体命令）；
4. 如果改了契约：指出改了哪些文档与 lock 文件。

## 许可证

提交贡献即表示你同意以 [MIT 许可证](LICENSE) 授权你的贡献。
