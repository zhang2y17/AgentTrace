# GitHub 发布准备清单

本清单依据当前仓库文件与代码整理。基础开源文件、展示材料和协作模板已经齐全，公开仓库地址为 <https://github.com/zhang2y17/AgentTrace>。下列建议不是 GitHub 上传仓库的强制要求。

## 已有文件，不必重复添加

| 文件或目录 | 用途与现状 |
|---|---|
| `README.md` | 项目说明、启动命令、API 示例、限制与文档导航 |
| `LICENSE` | MIT 许可证，作者名与本地 Git 配置一致 |
| `.gitignore`、`.dockerignore` | 排除环境文件、本地数据库、缓存和本机工作区数据 |
| `.env.example` | 可公开的配置模板 |
| `pyproject.toml`、`requirements.txt`、`requirements.lock` | Python 元数据、兼容范围与已验证环境的精确版本 |
| `Dockerfile`、`docker-compose.yml` | 本地部署与依赖服务 |
| `.github/workflows/ci.yml` | Lint、默认测试、Compose、契约、离线质量门禁及基础敏感信息扫描 |
| `CONTRIBUTING.md`、`SECURITY.md` | 贡献约定与安全边界 |
| `tests/`、`scripts/`、`data/` | 测试、初始化与评测脚本、合成样例 |
| `docs/` | 架构、介绍、演示、API、数据模型、Trace、评测及机器契约 |
| `.github/ISSUE_TEMPLATE/`、`.github/pull_request_template.md` | Bug、功能建议和 PR 信息模板 |
| `CHANGELOG.md`、`ROADMAP.md` | 版本变化与后续计划 |

本次补充了项目介绍、可复现 Demo、本清单、架构图源、路线图、变更记录、依赖锁文件和 GitHub 协作模板，并将离线质量门禁接入 CI。

## 首次公开前优先完成

- [x] 已创建公开 GitHub 仓库，并在 `pyproject.toml`、README 和贡献指南中加入真实 URL。
- [x] 已启用 GitHub Private Vulnerability Reporting，`SECURITY.md` 描述的私密报告入口可用。
- [x] 作者占位已按本地 Git 配置替换为 `zy`。
- [x] README 与贡献指南不再包含虚构仓库 URL。
- [x] 已在 [项目介绍的实现边界](PROJECT_OVERVIEW.md#当前实现边界) 中说明版本标签、Redis 基础类与 Fake LLM 门禁的范围。
- [x] 已增加真实本地验证结果的 [Demo](DEMO.md)，并明确数据来源与适用范围。
- [x] 已把确定性的离线评测门禁接入 CI。
- [ ] 执行现有质量检查，并确认实际 GitHub CI 运行结果。仅有 workflow 文件不代表远程 CI 已通过。
- [ ] 确认暂存文件及待公开历史不含密钥、真实业务数据和本地数据库；`.gitignore` 不会清理历史提交。

当前 Git 索引只跟踪 `.env.example`，未跟踪检查范围内的 `.db` 和 `.workbuddy-ai` 文件。这是当前索引检查结果，不等于已审计全部 Git 历史。

## 已完成的建议文件

| 文件 | 当前用途 |
|---|---|
| `docs/DEMO.md` | 展示一次实际评测，以及 API、Trace 和回放操作 |
| `requirements.lock` | 固定当前经过验证的开发与 CI 依赖版本 |
| `CHANGELOG.md` | 记录 0.1.0 与未发布变更 |
| `.github/ISSUE_TEMPLATE/*.yml` | 收集 Bug 环境与功能使用场景 |
| `.github/pull_request_template.md` | 提醒贡献者完成验证、契约与数据检查 |
| `ROADMAP.md` | 区分近期、中期计划和暂不承诺的能力 |

若面向英文读者或研究引用，可以再增加 `README.en.md` 或 `CITATION.cff`，它们不是当前发布的必要条件。截图建议在 GitHub 页面最终样式确定后再补充。

展示图和评测报告应取自可复现的实际运行。不要使用虚构的效果数字或提前添加尚未通过的 CI 徽章。

## 本地检查命令

在已经安装依赖的环境中执行：

```bash
ruff check .
ruff format --check .
python -m pytest tests -q
python scripts/verify_contract.py
docker compose config --quiet
git diff --check
git status --short
```

默认 pytest 配置排除 `integration`。需要真实 PostgreSQL / Redis 时，准备对应测试环境后使用 `python -m pytest tests/integration -q -m integration`，否则只指定目录仍会受到默认 marker 过滤。真实模型验证应另行配置并确认存在相应测试用例。

CI 已执行 `scripts/run_eval.py --dataset doc_research_v1 --gate` 并传播退出码。该门禁使用 Fake LLM，只覆盖确定性流程和合成用例；真实模型评测仍需单独的受控环境与密钥策略。
