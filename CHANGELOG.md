# Changelog

本项目采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构，并遵循语义化版本号。

## [Unreleased]

### Added

- 项目架构总览、项目介绍与 GitHub 发布准备清单。
- 可复现的离线演示说明、路线图和依赖锁文件。
- GitHub Issue 表单与 Pull Request 模板。
- CI 离线评测质量门禁。

## [0.1.0] - 2026-08-22

### Added

- FastAPI 运行、事件、评测、指标与质量门禁接口。
- LangGraph 五节点文档研究 Agent，以及 Fake、OpenAI-compatible、Ollama 模型适配。
- 节点、工具、模型与最终结果 Trace，支持运行查询和关联重新运行。
- SQLAlchemy 数据模型、PostgreSQL/SQLite 支持与 Redis 短期状态基础能力。
- 14 个离线评测 case、12 个输出指标和可配置质量门禁。
- Docker Compose、本地脚本、自动化测试、契约校验和安全扫描。
