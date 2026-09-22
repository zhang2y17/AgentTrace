"""契约一致性校验脚本（S8 验收使用）。

读取 ``docs/contract.lock.json``，校验代码与设计文档是否一致。
这是把 ACCEPTANCE_CHECKLIST 中 C-01 ~ C-08 从"人工核对"变成"可执行检查"的手段。

用法::

    python scripts/verify_contract.py            # 校验，失败返回 1
    python scripts/verify_contract.py --verbose  # 打印全部检查项

退出码：
    0 = 全部通过
    1 = 存在不一致

本脚本在 CI 的 contract-check job 中运行。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOCK_FILE = PROJECT_ROOT / "docs" / "contract.lock.json"


class CheckResult:
    """单条检查结果。"""

    def __init__(self, check_id: str, description: str, passed: bool, detail: str = "") -> None:
        self.check_id = check_id
        self.description = description
        self.passed = passed
        self.detail = detail


class ContractVerifier:
    """契约校验器。"""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.results: list[CheckResult] = []
        self.lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- 工具
    def add(self, check_id: str, description: str, passed: bool, detail: str = "") -> None:
        self.results.append(CheckResult(check_id, description, passed, detail))

    def _read(self, relative_path: str) -> str:
        path = PROJECT_ROOT / relative_path
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # ---------------------------------------------------------------- 检查项
    def check_event_types(self) -> None:
        """C-03：EventType 枚举与契约一致。"""
        from app.schemas.common import EventType

        expected = set(self.lock["event_types"])
        actual = {t.value for t in EventType}
        self.add(
            "C-03",
            "6 类 event_type 与 TRACE_SCHEMA §2 一致",
            expected == actual,
            f"缺失={sorted(expected - actual)} 多余={sorted(actual - expected)}",
        )

    def check_event_statuses(self) -> None:
        """8 个事件状态与契约一致。"""
        from app.schemas.common import EventStatus

        expected = set(self.lock["event_statuses"])
        actual = {s.value for s in EventStatus}
        self.add(
            "C-03b",
            "8 个 event status 与 TRACE_SCHEMA §4 一致",
            expected == actual,
            f"缺失={sorted(expected - actual)} 多余={sorted(actual - expected)}",
        )

    def check_run_statuses(self) -> None:
        """run 状态与契约一致。"""
        from app.schemas.common import RunStatus

        expected = set(self.lock["run_statuses"])
        actual = {s.value for s in RunStatus}
        self.add(
            "C-03c",
            "run status 与 DATA_MODEL §1 一致",
            expected == actual,
            f"缺失={sorted(expected - actual)} 多余={sorted(actual - expected)}",
        )

    def check_trace_event_fields(self) -> None:
        """C-04：trace_event 的 12 个必需字段必须齐备。"""
        required = set(self.lock["trace_event_required_fields"])

        # 从模型定义中提取列名（S3 之后才有 ORM 模型；在此之前退化为检查 Schema）
        found_orm = False
        try:
            from app.db.models import TraceEvent  # type: ignore[import-not-found]

            columns = {c.name for c in TraceEvent.__table__.columns}
            found_orm = True
        except ImportError:
            from app.schemas.runs import TraceEventOut

            columns = set(TraceEventOut.model_fields)

        missing = required - columns
        self.add(
            "C-04",
            f"trace_event 的 {len(required)} 个必需字段齐备（来源={'ORM 模型' if found_orm else 'Pydantic Schema'}）",
            not missing,
            f"缺失字段={sorted(missing)}",
        )

    def check_tables(self) -> None:
        """C-05：8 张表齐备。"""
        try:
            from app.db.base import Base
        except ImportError as exc:
            self.add("C-05", "8 张表齐备", False, f"无法导入 Base（S3 未完成？）: {exc}")
            return

        import app.db.models  # noqa: F401  —— 触发模型注册

        expected = set(self.lock["tables"])
        actual = set(Base.metadata.tables)
        self.add(
            "C-05",
            f"{len(expected)} 张表齐备（DATA_MODEL §2）",
            expected == actual,
            f"缺失={sorted(expected - actual)} 多余={sorted(actual - expected)}",
        )

    def check_api_endpoints(self) -> None:
        """C-06：10 个 API 端点齐备。"""
        try:
            from app.main import create_app

            app = create_app()
            schema = app.openapi()
            actual_paths = set(schema["paths"])
        except Exception as exc:  # noqa: BLE001
            self.add("C-06", "10 个 API 端点齐备", False, f"无法生成 OpenAPI: {exc}")
            return

        missing: list[str] = []
        for endpoint in self.lock["api_endpoints"]:
            path = endpoint["path"]
            method = endpoint["method"].lower()
            if path not in actual_paths:
                missing.append(f"{endpoint['method']} {path}")
            elif method not in schema["paths"][path]:
                missing.append(f"{endpoint['method']} {path}（路径存在但方法缺失）")

        self.add(
            "C-06",
            f"{len(self.lock['api_endpoints'])} 个 API 端点齐备（API_CONTRACT）",
            not missing,
            f"缺失={missing}",
        )

    def check_nodes(self) -> None:
        """C-01：5 个节点名一致。"""
        expected = self.lock["agent_definition"]["nodes"]

        nodes_dir = PROJECT_ROOT / "app" / "agent" / "nodes"
        if not nodes_dir.exists():
            self.add("C-01", "5 个 Agent 节点齐备（S4 未完成）", False, "app/agent/nodes 不存在")
            return

        actual = {path.stem for path in nodes_dir.glob("*.py") if path.stem != "__init__"}
        missing = set(expected) - actual
        self.add(
            "C-01",
            f"{len(expected)} 个 Agent 节点齐备（PROJECT_SPEC §3.1）",
            not missing,
            f"缺失={sorted(missing)}",
        )

    def check_tools(self) -> None:
        """C-02：4 个工具名一致。"""
        expected = {tool["name"] for tool in self.lock["tools"]}

        try:
            from app.tools.bootstrap import register_default_tools
            from app.tools.registry import registered_names

            # 通过正式的装配入口注册，检查的是**真实生效的注册表内容**，
            # 而不是模块里写了哪些常量。两者背离才是真正要抓的问题。
            register_default_tools()
            actual = set(registered_names())
        except Exception as exc:  # noqa: BLE001 —— 任何导入/装配失败都算未通过
            self.add(
                "C-02",
                "4 个工具齐备（S4 未完成）",
                False,
                f"工具注册表不可用：{type(exc).__name__}: {exc}",
            )
            return

        missing = expected - actual
        extra = actual - expected
        self.add(
            "C-02",
            f"{len(expected)} 个工具注册齐备（PROJECT_SPEC §3.2）",
            not missing and not extra,
            f"缺失={sorted(missing)} 多出={sorted(extra)}",
        )

    def check_metrics(self) -> None:
        """C-07：指标名与契约一致。"""
        try:
            from app.evaluation import metrics as metrics_module  # type: ignore[import-not-found]
        except ImportError:
            self.add("C-07", "指标模块可用（S6 未完成）", False, "app.evaluation.metrics 不可用")
            return

        exported = getattr(metrics_module, "METRIC_NAMES", None)
        if exported is None:
            self.add("C-07", "指标模块导出 METRIC_NAMES", False, "缺少 METRIC_NAMES 常量")
            return

        expected = set(self.lock["metrics"])
        actual = set(exported)
        self.add(
            "C-07",
            f"{len(expected)} 个指标名与 EVALUATION §2 一致",
            expected == actual,
            f"缺失={sorted(expected - actual)} 多余={sorted(actual - expected)}",
        )

    def check_id_prefixes(self) -> None:
        """C-08：ID 前缀与 DATA_MODEL §4 一致。"""
        from app.core import ids as ids_module

        expected = self.lock["id_prefixes"]
        mismatches: list[str] = []

        for entity, prefix in expected.items():
            factory_name = {
                "run": "new_run_id",
                "trace_event": "new_event_id",
                "tool_call": "new_tool_call_id",
                "model_call": "new_model_call_id",
                "agent_definition": "new_agent_definition_id",
                "eval_case": "new_eval_case_id",
                "eval_run": "new_eval_run_id",
                "quality_gate": "new_quality_gate_id",
                "evaluation": "new_evaluation_id",
            }.get(entity)

            if factory_name is None:
                mismatches.append(f"{entity}: 无对应工厂函数")
                continue

            factory = getattr(ids_module, factory_name, None)
            if factory is None:
                mismatches.append(f"{entity}: 缺少 {factory_name}")
                continue

            if not factory().startswith(prefix):
                mismatches.append(f"{entity}: {factory_name}() 未以 {prefix!r} 开头")

        self.add(
            "C-08",
            f"{len(expected)} 个 ID 前缀与 DATA_MODEL §4 一致",
            not mismatches,
            f"不一致={mismatches}",
        )

    def check_error_codes(self) -> None:
        """错误码与契约一致。"""
        from app.schemas.common import ErrorCode

        expected = set(self.lock["error_codes"])
        actual = {c.value for c in ErrorCode}

        # TOOL_EXECUTION_FAILED 是内部异常码，不进入 HTTP 契约表，允许存在
        extra_allowed = {"TOOL_EXECUTION_FAILED"}
        unexpected = actual - expected - extra_allowed

        self.add(
            "C-09",
            "错误码与 API_CONTRACT §0.1 一致",
            not unexpected and not (expected - actual),
            f"缺失={sorted(expected - actual)} 未在契约中声明={sorted(unexpected)}",
        )

    def check_forbidden_claims(self) -> None:
        """K-04：文档不得出现无证据的越权表述。

        注意：**禁止性语境不算违规**。例如契约 B4 写的是
        "不编造线上用户量、商业客户、商业收益"——这句话正是为了禁止这些说法，
        必须放行。因此这里除了检查本行，还检查相邻上下行是否构成禁止语境。
        """
        forbidden = self.lock["forbidden_claims"]
        # 上下文关键词：出现即说明该行处于"禁止/限制/否认"语义中
        #
        # 注意 `不代表` / `不用于` 这类**否定前缀**也必须算作放行语境：
        # README 的免责声明正是"不代表……商业收益"，若不放行会被误判为越权表述。
        # 这是该检查最容易产生假阳性的地方——免责声明与越权声称在字面上都含同一个词。
        allow_context = re.compile(
            r"禁止|不得|不要写|不要|forbidden|反模式|已知限制|不等于|不编造|不声明|不可|无法|"
            r"无真实|没有|声明|限制|边界|避免|拒绝|跳过|约束|"
            r"不代表|不用于|不承诺|不暗示|不构成|不视为|非"
        )

        offenders: list[str] = []
        for doc in ("README.md", "docs/EVALUATION.md", "docs/PROJECT_SPEC.md", "SECURITY.md"):
            content = self._read(doc)
            if not content:
                continue

            lines = content.splitlines()
            for index, line in enumerate(lines):
                hits = [claim for claim in forbidden if claim in line]
                if not hits:
                    continue

                # 检查本行与上下各一行是否处于禁止语境
                window = " ".join(lines[max(0, index - 1) : min(len(lines), index + 2)])
                if allow_context.search(window):
                    continue

                offenders.append(f"{doc}:{index + 1}: {'/'.join(hits)} → {line.strip()[:70]}")

        self.add(
            "K-04",
            "文档中无未加限定的越权表述（禁止性语境放行）",
            not offenders,
            "；".join(offenders[:5]),
        )

    def check_org_denylist(self) -> None:
        """G-09：仓库内不得出现公司名称或内部域名。"""
        denylist = self.lock["org_denylist"]
        offenders: list[str] = []

        scan_globs = ("*.py", "*.md", "*.yml", "*.yaml", "*.json", "*.toml", "*.mmd")
        # 允许出现的位置：本脚本自身、CI 的扫描规则、SECURITY 的说明
        allow_files = {
            "scripts/verify_contract.py",
            ".github/workflows/ci.yml",
            "SECURITY.md",
            "docs/ACCEPTANCE_CHECKLIST.md",
            "CONTRIBUTING.md",
        }

        for pattern in scan_globs:
            for path in PROJECT_ROOT.rglob(pattern):
                rel = path.relative_to(PROJECT_ROOT).as_posix()
                if (
                    rel in allow_files
                    or rel.startswith(".venv/")
                    or rel.startswith("docs/contract")
                ):
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                lowered = content.lower()
                for term in denylist:
                    if term.lower() in lowered:
                        lineno = next(
                            (
                                i
                                for i, line in enumerate(content.splitlines(), start=1)
                                if term.lower() in line.lower()
                            ),
                            0,
                        )
                        offenders.append(f"{rel}:{lineno}: {term}")

        self.add(
            "G-09",
            "仓库内无公司名称 / 内部域名",
            not offenders,
            "；".join(offenders[:5]),
        )

    def check_data_source_notes(self) -> None:
        """K-02：指标响应必须带数据来源标注。"""
        try:
            from app.schemas.metrics import MetricsSummaryResponse
        except ImportError as exc:
            self.add("K-02", "指标响应含数据来源标注", False, str(exc))
            return

        fields = set(MetricsSummaryResponse.model_fields)
        has_scope = "scope" in fields
        has_note = "data_source_note" in fields

        self.add(
            "K-02",
            "指标响应强制包含 scope 与 data_source_note",
            has_scope and has_note,
            f"scope={has_scope} data_source_note={has_note}",
        )

    def check_test_double_marking(self) -> None:
        """H-03：替身必须有明确标注字段。"""
        from app.schemas.eval import EvaluationResponse
        from app.schemas.health import LlmProviderHealth
        from app.schemas.runs import RunDetail

        checks = {
            "RunDetail.is_test_double": "is_test_double" in RunDetail.model_fields,
            "LlmProviderHealth.is_test_double": "is_test_double" in LlmProviderHealth.model_fields,
            "EvaluationResponse.is_test_double": "is_test_double"
            in EvaluationResponse.model_fields,
        }
        failed = [name for name, ok in checks.items() if not ok]

        self.add(
            "H-03",
            "替身标注字段存在于运行/健康/评测响应",
            not failed,
            f"缺失={failed}",
        )

    def check_settings_env_example(self) -> None:
        """A-04：.env.example 覆盖全部配置项。"""
        from app.core.config import Settings

        env_example = self._read(".env.example")
        if not env_example:
            self.add("A-04", ".env.example 覆盖全部配置项", False, ".env.example 不存在")
            return

        documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=", env_example, flags=re.MULTILINE))
        settings_fields = {name.upper() for name in Settings.model_fields}

        missing = settings_fields - documented
        # 派生属性不是配置项，无需出现在 .env.example
        self.add(
            "A-04",
            f".env.example 覆盖 {len(settings_fields)} 个配置项",
            not missing,
            f"未文档化={sorted(missing)}",
        )

    def check_deliverable_scripts(self) -> None:
        """I-10：``init_db.py`` 与 ``seed_data.py`` 必须存在且可被导入。

        只检查"存在 + 语法可解析"（``ast.parse``），**不执行**脚本——
        执行会连数据库、产生副作用，不适合放进只读的契约检查。
        但"能被解析"能捕获语法错误这类最容易犯的交付问题。
        """
        required = ("scripts/init_db.py", "scripts/seed_data.py")
        missing: list[str] = []
        unparsable: list[str] = []

        import ast

        for rel_path in required:
            abs_path = PROJECT_ROOT / rel_path
            if not abs_path.exists():
                missing.append(rel_path)
                continue
            try:
                ast.parse(abs_path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                unparsable.append(f"{rel_path}: {exc}")

        self.add(
            "I-10",
            f"交付脚本齐备且语法合法（{len(required)} 个）",
            not missing and not unparsable,
            f"缺失={missing} 语法错误={unparsable}",
        )

    # ---------------------------------------------------------------- 主流程
    def run_all(self) -> list[CheckResult]:
        checks = [
            self.check_event_types,
            self.check_event_statuses,
            self.check_run_statuses,
            self.check_trace_event_fields,
            self.check_tables,
            self.check_api_endpoints,
            self.check_nodes,
            self.check_tools,
            self.check_metrics,
            self.check_id_prefixes,
            self.check_error_codes,
            self.check_forbidden_claims,
            self.check_org_denylist,
            self.check_data_source_notes,
            self.check_test_double_marking,
            self.check_settings_env_example,
            self.check_deliverable_scripts,
        ]
        for check in checks:
            try:
                check()
            except Exception as exc:  # noqa: BLE001  —— 单条检查失败不应中断整体
                self.add(
                    check.__name__,
                    f"检查 {check.__name__} 执行异常",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
        return self.results


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 AgentTrace 代码与契约文档的一致性")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印全部检查项（含通过项）")
    args = parser.parse_args()

    verifier = ContractVerifier(verbose=args.verbose)
    results = verifier.run_all()

    print("=" * 78)
    print("AgentTrace 契约一致性检查")
    print("=" * 78)
    print(f"契约来源：{LOCK_FILE.relative_to(PROJECT_ROOT)}")
    print()

    passed = 0
    failed: list[CheckResult] = []
    for result in results:
        if result.passed:
            passed += 1
            if args.verbose:
                print(f"  [PASS] {result.check_id}  {result.description}")
        else:
            failed.append(result)
            print(f"  [FAIL] {result.check_id}  {result.description}")
            if result.detail:
                print(f"         {result.detail}")

    print()
    print(f"结果：{passed} 通过 / {len(failed)} 失败 / 共 {len(results)} 项")
    print()

    if failed:
        print("未通过项：")
        for result in failed:
            print(f"  - {result.check_id}: {result.description}")
            if result.detail:
                print(f"      {result.detail}")
        print()
        print("提示：未通过的检查项可能对应尚未完成的阶段。")
        print("      请对照 docs/IMPLEMENTATION_PLAN.md 确认当前进度。")
        return 1

    print("全部契约检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
