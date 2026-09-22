# AgentTrace 可复现演示

本演示使用 `LLM_PROVIDER=fake` 和合成文档，不访问网络模型，也不代表真实模型能力。以下验证于 2026-09-22 在 Python 3.12 环境完成；运行 ID 和延迟每次都会变化。

## 1. 运行离线评测与质量门禁

```bash
python scripts/run_eval.py --dataset doc_research_v1 --gate
```

一次本地验证的关键结果：

| 项目 | 结果 |
|---|---:|
| case | 14/14 通过 |
| run_success_rate | 1.0 |
| task_completion_rate | 1.0 |
| tool_selection_accuracy | 1.0 |
| tool_argument_accuracy | 0.9167（12 个适用 case） |
| evidence_coverage | 1.0 |
| error_rate | 0.0 |
| human_review_rate | 0.0 |
| 质量门禁 | 通过，退出码 0 |

延迟取决于机器和当前负载，因此不把某次本地耗时作为固定项目指标。完整机器可读结果可用以下命令生成：

```bash
python scripts/run_eval.py \
  --dataset doc_research_v1 \
  --gate \
  --json > evaluation-result.json
```

`evaluation-result.json` 是本地产物，提交前应确认其中不含不希望公开的信息。

## 2. 启动 API 并创建运行

```bash
cp .env.example .env
docker compose up -d --build
curl -s http://localhost:8000/health
```

创建一次文档研究运行：

```bash
curl -s -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"question":"AgentTrace 如何记录工具调用？","top_k":3}'
```

响应包含 `run_id`、状态、执行摘要以及 `is_test_double: true`。将返回的 ID 代入后续命令：

```bash
curl -s "http://localhost:8000/runs/RUN_ID?include_events=true"
curl -s "http://localhost:8000/runs/RUN_ID/events?event_type=node"
curl -s -X POST "http://localhost:8000/runs/RUN_ID/replay" \
  -H "Content-Type: application/json" \
  -d '{"prompt_version":"prompt-v2","note":"演示关联重新运行"}'
```

重新运行会创建新的 `run_id`，并通过 `source_run_id` 关联原运行。这里的 `prompt_version` 当前是记录和分组标签，仅修改标签不会切换实际提示词模板。

## 3. 验证 Trace

一次正常运行应能看到：

- `question_parser`、`document_search`、`evidence_checker`、`answer_writer`、`final_validator` 节点事件；
- `search_documents` 与一个或多个 `get_document` 工具调用；
- 问题解析和答案生成对应的模型调用；
- 一条最终结果事件，以及工具和模型事件指向所属节点的 `parent_event_id`。

Swagger UI 位于 <http://localhost:8000/docs>，适合逐个查看请求与响应模型。
