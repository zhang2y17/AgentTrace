#!/usr/bin/env python
"""离线评测 CLI（契约 EVALUATION §6.4）。

用法::

    python scripts/run_eval.py --dataset doc_research_v1
    python scripts/run_eval.py --dataset doc_research_v1 --gate
    python scripts/run_eval.py --dataset doc_research_v1 --gate \\
        --threshold 'run_success_rate=min:0.9' --json

退出码（EVALUATION §6.4，CI 依赖它）：

| 码 | 含义 |
|---|---|
| 0 | 评测完成且门禁通过（未加 ``--gate`` 时=评测完成） |
| 1 | 评测完成但门禁未通过（**阻断**） |
| 2 | 评测执行本身出错（数据集问题、落库失败等） |

这三者的区分是刻意的：CI 需要能分辨"质量不达标"（回去改 Agent）
与"评测根本没跑起来"（修环境）—— 把两者都报成 1
会让流水线给出误导性的结论。

**本脚本不需要启动 HTTP 服务**：直接调用服务层，
与 ``POST /evaluations`` 走同一套代码（``EvaluationService``）。
这样"脚本跑出的数字"与"接口跑出的数字"不可能不一致。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 允许从仓库根目录直接执行（``python scripts/run_eval.py``）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description="AgentTrace 离线评测与质量门禁",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/run_eval.py --dataset doc_research_v1 --gate\n"
            "  python scripts/run_eval.py --dataset doc_research_v1 --cases doc-001,doc-012\n"
            "  python scripts/run_eval.py --dataset doc_research_v1 --gate "
            "--threshold 'latency_ms_p95=max:5000'\n"
        ),
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="评测集版本，对应 data/eval/<version>.jsonl",
    )
    parser.add_argument("--agent-version", default="v1", help="Agent 版本标签（默认 v1）")
    parser.add_argument(
        "--prompt-version", default="prompt-v1", help="Prompt 版本标签（默认 prompt-v1）"
    )
    parser.add_argument(
        "--cases",
        default=None,
        help="逗号分隔的 case_key，只跑指定用例（便于快速验证）",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="评测后执行质量门禁；未通过时以退出码 1 结束",
    )
    parser.add_argument("--gate-name", default="release-gate", help="门禁名称（默认 release-gate）")
    parser.add_argument(
        "--threshold",
        action="append",
        default=None,
        metavar="METRIC=OP:VALUE",
        help=(
            "覆盖单个阈值，可重复。例如 --threshold 'run_success_rate=min:0.9' "
            "或 --threshold 'latency_ms_p95=max:3000'"
        ),
    )
    parser.add_argument(
        "--sqlite",
        default="sqlite+pysqlite:///./agenttrace.db",
        help="SQLite 数据库 URL（默认 ./agenttrace.db）",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="以 JSON 输出完整结果（便于机器解析）",
    )
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="文本模式下只打印失败 case",
    )
    return parser


def parse_thresholds(raw_items: list[str] | None) -> dict[str, dict[str, float]] | None:
    """解析 ``--threshold METRIC=OP:VALUE`` 形式的阈值覆盖。

    Raises:
        SystemExit: 格式非法（退出码 2，属于"评测执行出错"）。
    """
    if not raw_items:
        return None

    mapping: dict[str, dict[str, float]] = {}
    for raw in raw_items:
        if "=" not in raw:
            _die(f"--threshold 格式非法：{raw!r}，应为 METRIC=OP:VALUE。")
        metric, _, spec = raw.partition("=")
        if ":" not in spec:
            _die(f"--threshold 的 OP:VALUE 部分非法：{spec!r}，应为 min:0.9 或 max:2000。")
        operator, _, value = spec.partition(":")
        operator = operator.strip()
        if operator not in {"min", "max"}:
            _die(f"--threshold 的运算符非法：{operator!r}，只支持 min / max。")
        try:
            mapping.setdefault(metric.strip(), {})[operator] = float(value)
        except ValueError:
            _die(f"--threshold 的阈值不是数值：{value!r}")
    return mapping


def configure_environment(sqlite_url: str) -> None:
    """为脚本执行准备环境变量。

    两件事：

    1. 指向指定的 SQLite 库（默认本地文件），让脚本**不依赖 Docker**；
    2. 确保建表开关打开 —— 首次在空库上跑评测时表还不存在。
       ``AUTO_CREATE_TABLES`` 默认就是 ``true``，这里显式设置是为了
       不让"用户 shell 里恰好导出了 false"这种环境差异
       变成一次莫名其妙的评测失败。
    """
    os.environ.setdefault("DATABASE_URL", sqlite_url)
    os.environ["AUTO_CREATE_TABLES"] = "true"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_environment(args.sqlite)

    # 配置用 lru_cache 缓存，环境变量必须在 import 应用代码**之前**设好；
    # 这里再清一次缓存，防止调用方在进程内先导入了应用代码。
    from app.core.config import get_settings

    get_settings.cache_clear()

    case_keys = (
        [item.strip() for item in args.cases.split(",") if item.strip()] if args.cases else None
    )

    try:
        thresholds = parse_thresholds(args.threshold)
    except SystemExit as exc:  # argparse 风格：格式错误 → 退出码 2
        return int(exc.code or EXIT_ERROR)

    from app.core.errors import AgentTraceError
    from app.services.evaluation_service import EvaluationService

    # 建表。**必须显式做**：应用启动时的自动建表挂在 FastAPI 的 lifespan 上
    # （app/main.py），而本脚本直接调服务层、不启动 HTTP 服务 ——
    # 依赖 lifespan 会让"在空库上首次跑评测"以 OperationalError: no such table
    # 失败，而报错位置在"case 落库"这一层，与真实原因（表没建）隔得很远。
    #
    # create_all 是幂等的：表已存在时不做任何事。
    try:
        from app.db.session import create_all_tables

        create_all_tables()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[错误] 建表失败（{type(exc).__name__}）：{exc}\n"
            f"       请确认 DATABASE_URL 可连接：{args.sqlite}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        service = EvaluationService()
        result, _inline_gate = service.create_evaluation(
            dataset_version=args.dataset,
            agent_version=args.agent_version,
            prompt_version=args.prompt_version,
            case_keys=case_keys,
            # 内联门禁不在这里做：本脚本要落的是带 --gate-name 的
            # 正式门禁记录，而不是一个额外的临时门禁。
            thresholds=None,
        )
    except AgentTraceError as exc:
        # 领域异常（数据集不存在/非法）→ 退出码 2
        print(f"[错误] {exc.error_code}: {exc.message}", file=sys.stderr)
        if exc.details:
            print(f"[详情] {json.dumps(exc.details, ensure_ascii=False)}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 —— 未预期错误也归为"评测执行出错"
        print(f"[错误] 评测执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    gate_result = None
    if args.gate:
        try:
            gate_result = service.run_gate(
                evaluation_id=result.evaluation_id,
                gate_name=args.gate_name,
                thresholds=thresholds,
            )
        except AgentTraceError as exc:
            print(f"[错误] 门禁执行失败：{exc.error_code}: {exc.message}", file=sys.stderr)
            return EXIT_ERROR

    if args.as_json:
        payload = {
            "evaluation": result.to_dict(include_cases=True),
            "gate": gate_result.to_dict() if gate_result else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(result, gate_result, failures_only=args.failures_only)

    if gate_result is not None and not gate_result.passed:
        return EXIT_GATE_FAILED
    return EXIT_OK


def _print_report(result, gate_result, *, failures_only: bool) -> None:  # type: ignore[no-untyped-def]
    """打印人类可读的评测报告。"""
    line = "-" * 72

    print(line)
    print(f"评测批次 {result.evaluation_id}")
    print(line)
    print(f"  评测集        {result.dataset_version}")
    print(f"  Agent / Prompt {result.agent_version} / {result.prompt_version}")
    print(f"  模型           {result.model_name}（provider={result.llm_provider}）")
    print(f"  测试替身       {'是' if result.is_test_double else '否'}")
    print(
        f"  case 数        {result.case_count}"
        f"（通过 {result.passed_cases} / 失败 {result.failed_cases}）"
    )
    print(f"  耗时           {result.duration_ms} ms")

    print()
    print("指标")
    print(line)
    metrics = result.metrics
    rows = [
        ("run_success_rate", metrics.get("run_success_rate")),
        ("task_completion_rate", metrics.get("task_completion_rate")),
        (
            "tool_selection_accuracy",
            f"{_fmt_metric(metrics.get('tool_selection_accuracy'))}"
            f"（分母 {metrics.get('tool_selection_eligible_count')}）",
        ),
        (
            "tool_argument_accuracy",
            f"{_fmt_metric(metrics.get('tool_argument_accuracy'))}"
            f"（分母 {metrics.get('tool_argument_eligible_count')}）",
        ),
        ("evidence_coverage", metrics.get("evidence_coverage")),
        ("error_rate", metrics.get("error_rate")),
        ("degraded_rate", metrics.get("degraded_rate")),
        ("human_review_rate", metrics.get("human_review_rate")),
        ("latency_ms_p50", metrics.get("latency_ms_p50")),
        (
            "latency_ms_p95",
            f"{metrics.get('latency_ms_p95')}"
            + (
                f"（样本 {metrics.get('latency_sample_size')}，样本过小须谨慎解读）"
                if metrics.get("latency_p95_small_sample")
                else ""
            ),
        ),
        ("total_tokens", metrics.get("total_tokens")),
        ("estimated_cost_usd", metrics.get("estimated_cost_usd")),
    ]
    for name, value in rows:
        print(f"  {name:26} {_fmt_metric(value)}")

    print()
    print("逐 case")
    print(line)
    for case in result.case_results:
        if failures_only and case.passed:
            continue
        flag = "PASS" if case.passed else "FAIL"
        print(f"  [{flag}] {case.case_key:10} status={case.status:10} {case.latency_ms or 0:>6} ms")
        if case.failure_reason:
            print(f"         原因：{case.failure_reason}")
        for assertion in case.assertion_results:
            if not assertion.passed:
                print(f"         断言未过：{assertion.assertion} — {assertion.detail}")

    print()
    if gate_result is not None:
        print("质量门禁")
        print(line)
        print(f"  门禁名   {gate_result.gate_name}")
        print(f"  结论     {'通过' if gate_result.passed else '未通过（blocked）'}")
        if gate_result.violations:
            print("  违规项：")
            for violation in gate_result.violations:
                print(f"    - {violation.reason}")
        if gate_result.skipped_metrics:
            # 契约 EVALUATION §6.3：跳过 ≠ 通过，必须显式列出。
            print(f"  跳过项   {gate_result.skipped_metrics}（无数据，未参与判定）")
        print(f"  {gate_result.data_source_note}")
        print()

    print(line)
    print("数据来源声明")
    print(line)
    print(f"  {result.data_source_note}")
    print(line)


def _fmt_metric(value) -> str:  # type: ignore[no-untyped-def]
    """格式化指标值。``None`` 显示为 ``null（无数据）`` 而不是 0。"""
    if value is None:
        return "null（无数据）"
    if isinstance(value, float):
        return f"{value}"
    return str(value)


def _die(message: str) -> None:
    """打印错误并以退出码 2 结束（评测执行出错）。"""
    print(f"[错误] {message}", file=sys.stderr)
    raise SystemExit(EXIT_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
