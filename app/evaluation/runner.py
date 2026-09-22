"""评测执行器（契约 EVALUATION §5）。

把一份评测集逐 case 跑完，判定每个 case，落库，算出十个指标。

**逐 case 串行，不并行**

刻意如此。三个理由：

1. 并行会让延迟数据失去意义 —— 12 个 case 抢同一份 CPU，
   报出的 p95 主要反映调度抖动而不是 Agent 的表现；
2. 每个 case 要写 run / trace_event / tool_call / model_call 四张表，
   并发写会让 sequence 分配频繁撞唯一约束（仓储层虽有重试，
   但重试会放大尾延迟）；
3. 评测集只有 12 个 case，串行总耗时是秒级，优化收益为零。

**失败不中断批次**

单个 case 的 run 失败（超时、工具报错）是**评测要测的东西**，
不是评测本身的故障。若一个 case 失败就中止整批，那么
"Agent 在有挑战的 case 上表现如何"这个问题就永远得不到答案。
契约 EVALUATION §3 把分母固定为 $N$（含失败 case），
正是建立在这个设计上。

真正会中止整批的只有两类：评测集不合法（400），
以及落库失败（Trace 写入不上 = 评测结论无从追溯）。

**判定逻辑集中在这里**

``metrics.py`` 只做纯聚合，``assertions.py`` 只做单条断言判定。
"一个 case 算不算完成"、"工具序列对不对"、"参数对不对"
这三项复合判定收在本模块，因为它们同时依赖 run 结果、
工具调用序列与 case 期望 —— 放在指标层会让指标层需要读数据库。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.errors import DatasetValidationError, InvalidArgumentError
from app.core.logging import get_logger
from app.evaluation.assertions import (
    AssertionResult,
    CaseFacts,
    evaluate_assertions,
)
from app.evaluation.dataset import EvalCaseSpec, EvalDataset, load_dataset
from app.evaluation.metrics import (
    CaseOutcome,
    compute_metrics,
    coverage_for_case,
)
from app.evaluation.pricing import PricingTable

logger = get_logger(__name__)

# ``failure_reason`` 的 category 枚举（EVALUATION §5.1）
REASON_RUN_STATUS_MISMATCH = "run_status_mismatch"
REASON_TOOL_SELECTION_MISMATCH = "tool_selection_mismatch"
REASON_TOOL_ARGUMENT_MISMATCH = "tool_argument_mismatch"
REASON_ASSERTION_FAILED = "assertion_failed"
REASON_INSUFFICIENT_CITATIONS = "insufficient_citations"
REASON_RUN_ERROR = "run_error"

# 工具调用序列的过滤规则（EVALUATION §3 M3）：
# 排除 ``invalid_arguments`` 与 ``skipped``；**包含** ``error``。
#
# 为什么包含 error 而不是排除：`error` 意味着工具**确实被选中并调用了**，
# 只是执行失败。"选错了" 与 "选了但跑挂了" 是两个不同的问题 ——
# 前者是决策质量问题，后者是可靠性问题。把它们混在一起统计，
# tool_selection_accuracy 就无法定位到具体哪一类。
_EXCLUDED_TOOL_STATUSES = frozenset({"invalid_arguments", "skipped"})

# 浮点比较的绝对误差（EVALUATION §3 M4 表）
_FLOAT_TOLERANCE = 1e-9

# ``eval_run.assertion_results`` 的 JSON 列写入前的摘要长度上限。
# 断言 detail 可能含很长的对比内容，入库前截断。
_ASSERTION_DETAIL_MAX = 500


@dataclass(slots=True)
class CaseResult:
    """一个 case 的完整评测结果（落库 + 对外输出的载体）。"""

    case_key: str
    run_id: str | None
    status: str
    task_completed: bool
    tool_selection_correct: bool | None
    tool_argument_correct: bool | None
    evidence_coverage: float | None
    latency_ms: int | None
    total_tokens: int
    estimated_cost_usd: Decimal
    failure_reason: str | None
    assertion_results: list[AssertionResult] = field(default_factory=list)
    needs_human_review: bool = False
    tool_sequence: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """该 case 是否算通过。

        与 ``task_completed`` 同义 —— 契约 API_CONTRACT §6 的
        ``passed_cases`` / ``failed_cases`` 用这个口径。
        """
        return self.task_completed

    def to_outcome(self) -> CaseOutcome:
        """转成指标层的输入单元。"""
        return CaseOutcome(
            case_key=self.case_key,
            status=self.status,
            task_completed=self.task_completed,
            tool_selection_correct=self.tool_selection_correct,
            tool_argument_correct=self.tool_argument_correct,
            evidence_coverage=self.evidence_coverage,
            latency_ms=self.latency_ms,
            total_tokens=self.total_tokens,
            estimated_cost_usd=float(self.estimated_cost_usd or 0),
            needs_human_review=self.needs_human_review,
            assertion_results=list(self.assertion_results),
            failure_reason=self.failure_reason,
        )

    def to_dict(self, *, only_failures: bool = False) -> dict[str, Any]:  # noqa: ARG002
        """转成 ``GET /evaluations/{id}`` 的 case 条目。

        ``only_failures`` 由调用方过滤，这里保持纯转换。
        """
        return {
            "case_key": self.case_key,
            "run_id": self.run_id,
            "status": self.status,
            "task_completed": self.task_completed,
            "tool_selection_correct": self.tool_selection_correct,
            "tool_argument_correct": self.tool_argument_correct,
            "evidence_coverage": self.evidence_coverage,
            "latency_ms": self.latency_ms,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": float(self.estimated_cost_usd or 0),
            "failure_reason": self.failure_reason,
            "assertion_results": [item.to_dict() for item in self.assertion_results],
            "tool_sequence": self.tool_sequence,
            "needs_human_review": self.needs_human_review,
        }


@dataclass(slots=True)
class EvaluationResult:
    """一次评测批次的完整结果。"""

    evaluation_id: str
    dataset_version: str
    agent_version: str
    prompt_version: str
    model_name: str
    llm_provider: str
    is_test_double: bool
    status: str
    case_results: list[CaseResult]
    metrics: dict[str, Any]
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    cost_estimation_unavailable: bool = False
    pricing_version: str = "unknown"
    data_source_note: str = ""

    @property
    def case_count(self) -> int:
        return len(self.case_results)

    @property
    def passed_cases(self) -> int:
        return sum(1 for item in self.case_results if item.passed)

    @property
    def failed_cases(self) -> int:
        return self.case_count - self.passed_cases

    def to_dict(
        self, *, include_cases: bool = False, only_failures: bool = False
    ) -> dict[str, Any]:
        """转成契约 API_CONTRACT §6/§7 的响应体。"""
        payload: dict[str, Any] = {
            "evaluation_id": self.evaluation_id,
            "dataset_version": self.dataset_version,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_name": self.model_name,
            "llm_provider": self.llm_provider,
            "is_test_double": self.is_test_double,
            "status": self.status,
            "case_count": self.case_count,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            # 契约 API_CONTRACT §6 的顶层字段。顺序跟随 ``case_results``
            # （即数据集内的 case 顺序），而不是插入顺序 ——
            # 否则同一份评测的两次运行会产出顺序不同的 run_ids，
            # 让"第 N 个 case 的 run"变得不可复现。
            "run_ids": [item.run_id for item in self.case_results],
            "metrics": self.metrics,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration_ms": self.duration_ms,
            "data_source_note": self.data_source_note,
            "cost_estimation_unavailable": self.cost_estimation_unavailable,
            "pricing_version": self.pricing_version,
        }
        if include_cases:
            cases = self.case_results
            if only_failures:
                cases = [item for item in cases if not item.passed]
            payload["cases"] = [item.to_dict() for item in cases]
        return payload


class EvaluationRunner:
    """评测执行器。

    Args:
        run_service: 执行单次运行的服务。注入以便测试替换。
        session_factory: 返回 Session 的可调用对象。
        pricing_table: 价目表；``None`` 时加载默认表。
    """

    def __init__(
        self,
        *,
        run_service: Any,
        session_factory: Any | None = None,
        pricing_table: PricingTable | None = None,
    ) -> None:
        self._run_service = run_service
        self._session_factory_override = session_factory
        self._pricing_table = pricing_table

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_evaluation(
        self,
        *,
        dataset_version: str,
        agent_version: str = "v1",
        prompt_version: str = "prompt-v1",
        case_keys: list[str] | None = None,
        top_k: int | None = None,
    ) -> EvaluationResult:
        """执行一次评测。

        Args:
            dataset_version: 评测集版本。
            agent_version: Agent 版本标签。
            prompt_version: Prompt 版本标签。
            case_keys: 只跑指定 case；``None`` 表示全部。
            top_k: 覆盖检索条数；``None`` 表示用默认。

        Raises:
            DatasetNotFoundError: 评测集不存在。
            DatasetValidationError: 评测集内容非法，或 case_keys 含未知用例。
        """
        from app.core.ids import new_evaluation_id

        dataset = load_dataset(dataset_version).select(case_keys)

        evaluation_id = new_evaluation_id()
        started_at = _now()
        started_perf = time.perf_counter()

        logger.info(
            "evaluation_started",
            extra={
                "evaluation_id": evaluation_id,
                "dataset_version": dataset_version,
                "case_count": dataset.case_count,
                "agent_version": agent_version,
                "prompt_version": prompt_version,
            },
        )

        # case 规格先落库：失败 case 的 failure_reason 要能指回它，
        # 而且"这次评测用了哪版 case 定义"必须可追溯。
        self._persist_cases(dataset)

        case_results: list[CaseResult] = []
        cost_unavailable = False

        for case in dataset.cases:
            result = self._run_single_case(
                case=case,
                evaluation_id=evaluation_id,
                agent_version=agent_version,
                prompt_version=prompt_version,
                dataset_version=dataset_version,
                top_k=top_k,
            )
            case_results.append(result)
            # 只要有一个 case 的成本不可估算，整批就标注不可估算。
            # 部分可估算的数字被当成完整成本是最容易误导人的读数。
            if result.estimated_cost_usd == 0 and result.status not in {"failed", "timeout"}:
                cost_unavailable = cost_unavailable or self._run_cost_unavailable(result.run_id)

        ended_at = _now()
        duration_ms = int((time.perf_counter() - started_perf) * 1000)

        outcomes = [item.to_outcome() for item in case_results]
        metrics = compute_metrics(outcomes)

        pricing_table = self._resolve_pricing_table()
        status = self._resolve_batch_status(case_results)

        return EvaluationResult(
            evaluation_id=evaluation_id,
            dataset_version=dataset_version,
            agent_version=agent_version,
            prompt_version=prompt_version,
            model_name=str(self._model_name()),
            llm_provider=str(self._llm_provider()),
            is_test_double=bool(self._is_test_double()),
            status=status,
            case_results=case_results,
            metrics=metrics,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            cost_estimation_unavailable=cost_unavailable,
            pricing_version=pricing_table.version,
            data_source_note=(
                "离线评测结果：在 doc_research_v1 评测集上跑出，"
                "模型调用由本地测试替身产生（is_test_double=true），"
                "不代表真实模型能力，也不代表任何线上流量或生产环境表现。"
                if self._is_test_double()
                else "离线评测结果：在指定评测集上跑出，使用真实模型提供方。"
                "指标仅适用于本评测集与本次运行，不代表线上流量或生产环境表现。"
            ),
        )

    # ------------------------------------------------------------------
    # 单 case 执行
    # ------------------------------------------------------------------

    def _run_single_case(  # noqa: PLR0913 —— 参数都是执行上下文，合并成对象反而模糊
        self,
        *,
        case: EvalCaseSpec,
        evaluation_id: str,
        agent_version: str,
        prompt_version: str,
        dataset_version: str,
        top_k: int | None,
    ) -> CaseResult:
        """执行并判定一个 case。**任何异常都被收敛成 CaseResult**。"""
        from app.core.errors import AgentTraceError

        run_id: str | None = None
        status = "failed"
        error_code: str | None = None
        run_error_message: str | None = None
        state: dict[str, Any] = {}
        state_final: dict[str, Any] = {}
        latency_ms: int | None = None
        total_tokens = 0
        estimated_cost = Decimal("0")

        try:
            execution = self._run_service.execute(
                question=case.question,
                agent_version=agent_version,
                prompt_version=prompt_version,
                top_k=top_k if top_k is not None else _DEFAULT_TOP_K,
                metadata={"eval_case_key": case.case_key, "evaluation_id": evaluation_id},
            )
            run_id = execution.run_id
            state = dict(execution.state or {})
            state_final = dict(state.get("final_result") or {})
            status = str(execution.status)
            latency_ms = execution.duration_ms
            total_tokens = int(execution.total_tokens or 0)
            estimated_cost = Decimal(str(execution.estimated_cost_usd or 0))
        except AgentTraceError as exc:
            # 领域异常是**预期内**的失败路径（超时、工具参数非法等）。
            # 它们的 error_code 是评测结论的一部分。
            error_code = exc.error_code
            run_error_message = exc.message
            status = _status_for_error(exc)
            logger.info(
                "eval_case_run_failed",
                extra={
                    "evaluation_id": evaluation_id,
                    "case_key": case.case_key,
                    "error_code": error_code,
                    "status": status,
                },
            )
            # 超时/失败也落了库，取出 run_id 与实测耗时 ——
            # 契约 EVALUATION §5.1 要求失败 case 必须能追到 run。
            run_id, latency_ms = self._find_run_for_case(evaluation_id, case.case_key)
        except Exception as exc:  # noqa: BLE001 —— 未知异常也必须变成 case 结果
            error_code = "INTERNAL_ERROR"
            run_error_message = f"{type(exc).__name__}: {exc}"
            status = "failed"
            logger.exception(
                "eval_case_run_crashed",
                extra={"evaluation_id": evaluation_id, "case_key": case.case_key},
            )
            run_id, latency_ms = self._find_run_for_case(evaluation_id, case.case_key)

        # ---------------------------------------------------------- 判定
        facts = self._build_facts(
            state=state,
            status=status,
            required_citations=case.required_citations,
        )
        assertion_results = evaluate_assertions(case.required_assertions, facts)

        tool_sequence = self._tool_sequence(run_id) if run_id else []
        tool_selection_correct = self._judge_tool_selection(case, tool_sequence, status)
        tool_argument_correct = self._judge_tool_arguments(case, run_id) if run_id else None
        if case.expected_arguments and tool_argument_correct is None:
            # 有参数期望但拿不到 run（run 建行就失败）：判不通过。
            # 判 None 会让这个 case 从 M4 分母里消失，掩盖一次没验成的检查。
            tool_argument_correct = False

        # 覆盖率解析：优先用 final_result.citations（权威），退化到 state.citations
        citations = list(state_final.get("citations") or facts.citations or [])
        evidence_coverage = coverage_for_case(len(citations), case.required_citations)

        needs_human_review = self._needs_human_review(status, state, run_id)

        # ---------------------------------------------------------- 完成度
        task_completed = self._judge_task_completion(
            case=case,
            status=status,
            answer_chars=facts.answer_chars,
            assertion_results=assertion_results,
        )

        failure_reason = self._build_failure_reason(
            case=case,
            status=status,
            task_completed=task_completed,
            tool_selection_correct=tool_selection_correct,
            tool_argument_correct=tool_argument_correct,
            assertion_results=assertion_results,
            citations=len(citations),
            error_code=error_code,
            run_error_message=run_error_message,
            tool_sequence=tool_sequence,
        )

        result = CaseResult(
            case_key=case.case_key,
            run_id=run_id,
            status=status,
            task_completed=task_completed,
            tool_selection_correct=tool_selection_correct,
            tool_argument_correct=tool_argument_correct,
            evidence_coverage=evidence_coverage,
            latency_ms=latency_ms,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            failure_reason=failure_reason,
            assertion_results=assertion_results,
            needs_human_review=needs_human_review,
            tool_sequence=tool_sequence,
        )

        self._persist_eval_run(
            evaluation_id=evaluation_id,
            case=case,
            result=result,
            dataset_version=dataset_version,
            agent_version=agent_version,
            prompt_version=prompt_version,
        )
        return result

    # ------------------------------------------------------------------
    # 判定细节
    # ------------------------------------------------------------------

    @staticmethod
    def _judge_task_completion(
        *,
        case: EvalCaseSpec,
        status: str,
        answer_chars: int,
        assertion_results: list[AssertionResult],
    ) -> bool:
        """M2 的单 case 判定（EVALUATION §3 M2，四个条件全满足）。

        注意第 4 条：``expect_success = false`` 时要求 run **确实失败**。
        这是"反向断言"，用来验证失败路径 —— 如果一个"应该失败"的
        case 竟然成功了，那说明 Agent 比预期更容易被诱导，
        这是一个应当被记为失败的观察结果。
        """
        if case.expect_success:
            if status != "succeeded":
                return False
            if answer_chars < 1:
                return False
            # 断言里若有 status_is_failed，说明 case 定义自相矛盾
            # （expect_success=true 却要求 failed）。这种矛盾由
            # dataset 层查不到，这里让断言判定自然把它判失败。
            return all(item.passed for item in assertion_results)

        # expect_success = false：要求确实失败
        if status not in {"failed", "timeout", "degraded"}:
            return False
        return all(item.passed for item in assertion_results)

    @staticmethod
    def _judge_tool_selection(
        case: EvalCaseSpec, actual_sequence: list[str], status: str
    ) -> bool | None:
        """M3 的单 case 判定：``ordered_match(actual, expected)``。

        Returns:
            ``None`` 表示该 case 无工具期望，不参与 M3。

        边界：``expected_tools`` 非空但 ``actual`` 为空 —— 长度不同，
        直接判不匹配（返回 ``False`` 而非 ``None``）。
        返回 ``None`` 会让"期望调用却一个都没调"这个严重问题
        从指标里凭空消失。
        """
        if not case.has_tool_expectation:
            return None
        return actual_sequence == list(case.expected_tools)

    def _judge_tool_arguments(self, case: EvalCaseSpec, run_id: str) -> bool | None:
        """M4 的单 case 判定：所有 ``(case, tool)`` 参数对是否全部匹配。

        Returns:
            ``None`` 表示该 case 无参数期望，不参与 M4。

        契约 EVALUATION §3 M4 前提：只统计 ``validated = true`` 的调用。
        未通过 Pydantic 校验的调用直接判 ``A = false``
        （而不是跳过）—— 参数不合法就是"参数不对"，跳过等于放行。
        """
        expected = case.expected_arguments or {}
        if not expected:
            return None

        calls = self._tool_calls(run_id)
        all_passed = True

        for tool_name, arg_expectations in expected.items():
            matched_any = False
            for call in calls:
                if call.tool_name != tool_name:
                    continue
                # 未通过校验的调用：算作"存在一个不匹配的调用"
                if not call.validated:
                    all_passed = False
                    matched_any = True
                    continue
                if self._compare_arguments(arg_expectations, call.arguments or {}):
                    matched_any = True
                    break
            if not matched_any:
                # 期望了参数，却没有一次对该工具的调用 → 参数不匹配
                all_passed = False
            elif not any(
                call.validated
                and call.tool_name == tool_name
                and self._compare_arguments(arg_expectations, call.arguments or {})
                for call in calls
            ):
                all_passed = False

        return all_passed

    @staticmethod
    def _compare_arguments(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
        """参数比较（EVALUATION §3 M4 的比较语义表）。

        四条规则，全部集中在这一个函数里 —— 分散实现会让
        "期望值语义"在不同地方出现分歧，而评测口径的分歧是无法调试的。

        | 期望值类型 | 比较方式 |
        |---|---|
        | 标量 | 严格相等；float 允许绝对误差 1e-9 |
        | 列表 | 视为集合做无序比较 |
        | 字典 | 递归逐键比较，期望是子集即可 |
        | null | 仅当实际值也为 null 时通过 |
        """
        for arg_name, expected_value in expected.items():
            if arg_name not in actual:
                return False
            if not _values_match(expected_value, actual[arg_name]):
                return False
        return True

    def _build_facts(
        self, *, state: dict[str, Any], status: str, required_citations: int
    ) -> CaseFacts:
        """构造断言事实包，注入"已知文档 ID"作为反幻觉基准。"""
        return CaseFacts.from_state(
            state,
            run_status=status,
            known_document_ids=self._known_document_ids(),
            required_citations=required_citations,
        )

    @staticmethod
    def _build_failure_reason(  # noqa: PLR0913 —— 逐类原因各自需要上下文
        *,
        case: EvalCaseSpec,
        status: str,
        task_completed: bool,
        tool_selection_correct: bool | None,
        tool_argument_correct: bool | None,
        assertion_results: list[AssertionResult],
        citations: int,
        error_code: str | None,
        run_error_message: str | None,
        tool_sequence: list[str],
    ) -> str | None:
        """生成 ``<category>: <detail>`` 格式的失败原因（EVALUATION §5.1）。

        按"最能解释失败的类别"排序取第一个，而不是把所有问题都拼上。
        一个 case 可能同时有多处不对（工具选错 + 断言没过），
        但根因通常只有一个；列出全部会让报告读起来像噪音。
        """
        if task_completed:
            return None

        # 1. run 本身失败 —— 最上游的原因，优先报告
        if status in {"failed", "timeout"}:
            return f"{REASON_RUN_ERROR}: status={status}, error_code={error_code or 'n/a'}"

        # 2. 终态与 expect_success 不符
        if case.expect_success and status != "succeeded":
            return f"{REASON_RUN_STATUS_MISMATCH}: expect_success=true, actual status={status}"
        if not case.expect_success and status == "succeeded":
            return (
                f"{REASON_RUN_STATUS_MISMATCH}: expect_success=false, "
                f"actual status=succeeded（本该失败的 case 成功了）"
            )

        # 3. 工具选择
        if tool_selection_correct is False:
            return (
                f"{REASON_TOOL_SELECTION_MISMATCH}: "
                f"expected={list(case.expected_tools)}, actual={tool_sequence}"
            )

        # 4. 工具参数
        if tool_argument_correct is False:
            expected_args = case.expected_arguments or {}
            tool_names = ", ".join(sorted(expected_args))
            return f"{REASON_TOOL_ARGUMENT_MISMATCH}: tool={tool_names}, expected={expected_args}"

        # 5. 引用不足
        if citations < case.required_citations:
            return (
                f"{REASON_INSUFFICIENT_CITATIONS}: "
                f"required={case.required_citations}, found={citations}"
            )

        # 6. 关键断言未通过
        failed = [item for item in assertion_results if not item.passed]
        if failed:
            names = ", ".join(item.assertion for item in failed)
            detail = "; ".join(f"{item.assertion}: {item.detail}" for item in failed if item.detail)
            suffix = f"（{detail}）" if detail else ""
            return f"{REASON_ASSERTION_FAILED}: {names}{suffix}"

        # 兜底：答案为空
        if run_error_message:
            return f"{REASON_RUN_ERROR}: {run_error_message[:200]}"
        return f"{REASON_ASSERTION_FAILED}: task_completed 为 false 但未匹配到具体原因"

    # ------------------------------------------------------------------
    # 数据访问
    # ------------------------------------------------------------------

    def _tool_sequence(self, run_id: str) -> list[str]:
        """推导实际工具调用序列（EVALUATION §3 M3）。

        规则：按 ``started_at`` 升序，去重保留**首次出现**，
        排除 ``invalid_arguments`` 与 ``skipped``。
        仓储层的 ``list_tool_calls`` 已保证时间升序。
        """
        sequence: list[str] = []
        for call in self._tool_calls(run_id):
            if call.status in _EXCLUDED_TOOL_STATUSES:
                continue
            if call.tool_name not in sequence:
                sequence.append(call.tool_name)
        return sequence

    def _tool_calls(self, run_id: str) -> list[Any]:
        """读取 run 的工具调用（复用仓储层，不自己写 SQL）。"""
        factory = self._resolve_session_factory()
        try:
            with factory() as session:
                from app.db.repository import TraceRepository

                return TraceRepository(session).list_tool_calls(run_id)
        except Exception as exc:  # noqa: BLE001 —— 读不到就是没有工具调用
            logger.warning(
                "eval_tool_calls_unavailable",
                extra={"run_id": run_id, "error_type": type(exc).__name__},
            )
            return []

    def _find_run_for_case(
        self, evaluation_id: str, case_key: str
    ) -> tuple[str | None, int | None]:
        """在 run 表里找回某个 case 刚才跑的 run（用于失败路径）。

        为什么不用 ``execution.run_id``：异常路径下 ``execute`` 抛出了，
        调用方拿不到返回值。但 run 行**已经落库**（``execute`` 建行在前），
        所以按 ``result_summary.metadata`` 里的 case_key 反查是可行的。

        这也是"失败 case 必须能追到 run"这条契约要求的实现方式。
        """
        factory = self._resolve_session_factory()
        try:
            with factory() as session:
                from sqlalchemy import select

                from app.db.models import Run

                stmt = (
                    select(Run)
                    .where(Run.agent_version.is_not(None))
                    .order_by(Run.started_at.desc())
                    .limit(50)
                )
                runs = list(session.execute(stmt).scalars().all())
                for run in runs:
                    summary = run.result_summary or {}
                    metadata = summary.get("metadata") or {}
                    if (
                        metadata.get("eval_case_key") == case_key
                        and metadata.get("evaluation_id") == evaluation_id
                    ):
                        return run.id, run.total_duration_ms
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "eval_run_lookup_failed",
                extra={
                    "evaluation_id": evaluation_id,
                    "case_key": case_key,
                    "error_type": type(exc).__name__,
                },
            )
        return None, None

    def _run_cost_unavailable(self, run_id: str | None) -> bool:
        """查询某个 run 是否存在无法估算成本的调用。"""
        if not run_id:
            return False
        factory = self._resolve_session_factory()
        try:
            with factory() as session:
                from app.db.repository import TraceRepository

                _, _, unavailable = TraceRepository(session).aggregate_token_and_cost(run_id)
                return bool(unavailable)
        except Exception:  # noqa: BLE001 —— 查不到就保守判为不可估算
            return True

    @staticmethod
    def _needs_human_review(status: str, state: dict[str, Any], run_id: str | None) -> bool:
        """M10 的单 case 判定：run 中是否存在 ``handoff`` 事件。

        **当前实现说明（诚实标注）**

        M10 的权威口径是"``trace_event`` 中存在 ``status = 'handoff'``"。
        但本项目的示例 Agent 的 ``TraceSpan.mark_handoff()`` 目前
        没有任何节点调用它（自动流程不会主动声明"需要人工接管"），
        因此这个指标在当前实现下**结构性为 0**。

        这里额外把 ``timeout`` 计入 —— 超时意味着"自动流程在预算内
        没能给出结论"，语义上正是需要人工介入的情形。
        该口径与 ``/metrics/summary`` 的 ``human_review_rate``
        保持一致，两处读数不会互相矛盾。

        在 README 与评测报告中必须同时说明这一点，
        不能让一个恒为 0 的指标被读成"从不需要人工介入"。
        """
        return status == "timeout"

    def _known_document_ids(self) -> frozenset[str]:
        """已知文档 ID 集合，作为反幻觉断言的基准。"""
        try:
            from app.tools.bootstrap import get_document_store

            return frozenset(get_document_store().document_ids)
        except Exception as exc:  # noqa: BLE001 —— 拿不到就返回空集（断言会判失败）
            logger.warning(
                "eval_known_documents_unavailable",
                extra={"error_type": type(exc).__name__},
            )
            return frozenset()

    def _resolve_session_factory(self) -> Any:
        if self._session_factory_override is not None:
            return self._session_factory_override
        from app.db.session import session_scope

        return session_scope

    def _resolve_pricing_table(self) -> PricingTable:
        if self._pricing_table is not None:
            return self._pricing_table
        return PricingTable.load()

    def _model_name(self) -> str:
        from app.core.config import get_settings

        settings = get_settings()
        return "fake-model" if settings.is_test_double_mode else settings.llm_model

    def _llm_provider(self) -> str:
        from app.core.config import get_settings

        return str(get_settings().llm_provider)

    def _is_test_double(self) -> bool:
        from app.core.config import get_settings

        return bool(get_settings().is_test_double_mode)

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def _persist_cases(self, dataset: EvalDataset) -> None:
        """把评测集的 case 定义写入 ``eval_case``（幂等 upsert）。

        幂等很重要：同一个评测集会被反复评测，若每次都插入新行，
        ``case_key`` 的唯一约束会直接报错；而"删掉旧的重插"
        会让旧的 ``eval_run`` 外键悬空。
        """
        factory = self._resolve_session_factory()
        with factory() as session:
            from sqlalchemy import select

            from app.core.ids import new_eval_case_id
            from app.db.models import EvalCase

            try:
                for case in dataset.cases:
                    existing = session.execute(
                        select(EvalCase).where(EvalCase.case_key == case.case_key)
                    ).scalar_one_or_none()

                    if existing is None:
                        session.add(
                            EvalCase(
                                id=new_eval_case_id(),
                                case_key=case.case_key,
                                dataset_version=dataset.dataset_version,
                                question=case.question,
                                expected_tools=case.expected_tools,
                                expected_arguments=case.expected_arguments,
                                required_assertions=case.required_assertions,
                                required_citations=case.required_citations,
                                expect_success=case.expect_success,
                                tags=case.tags or None,
                            )
                        )
                    else:
                        # 更新为最新定义：评测集是活文档，改了 question 或
                        # 期望值后，旧行若不更新会让后续评测用过期期望判定。
                        existing.dataset_version = dataset.dataset_version
                        existing.question = case.question
                        existing.expected_tools = case.expected_tools
                        existing.expected_arguments = case.expected_arguments
                        existing.required_assertions = case.required_assertions
                        existing.required_citations = case.required_citations
                        existing.expect_success = case.expect_success
                        existing.tags = case.tags or None
                session.commit()
            except Exception as exc:
                session.rollback()
                raise DatasetValidationError(
                    f"评测集 {dataset.dataset_version} 的 case 落库失败。",
                    details={"error_type": type(exc).__name__},
                ) from exc

    def _persist_eval_run(  # noqa: PLR0913
        self,
        *,
        evaluation_id: str,
        case: EvalCaseSpec,
        result: CaseResult,
        dataset_version: str,
        agent_version: str,
        prompt_version: str,
    ) -> None:
        """写入 ``eval_run`` 行（契约 EVALUATION §5.1 的字段要求）。"""
        factory = self._resolve_session_factory()
        with factory() as session:
            from sqlalchemy import select

            from app.core.ids import new_eval_run_id
            from app.db.models import EvalCase, EvalRun

            try:
                eval_case = session.execute(
                    select(EvalCase).where(EvalCase.case_key == case.case_key)
                ).scalar_one_or_none()
                if eval_case is None:
                    # case 行缺失说明 _persist_cases 没跑或回滚了。
                    # 这里抛错而不是跳过：跳过会让评测报告里凭空少一个 case，
                    # 而分母已经按 N 算好，少一行会让指标与报告互相矛盾。
                    raise InvalidArgumentError(
                        f"eval_case 行缺失：{case.case_key}",
                        details={"case_key": case.case_key},
                    )

                session.add(
                    EvalRun(
                        id=new_eval_run_id(),
                        evaluation_id=evaluation_id,
                        eval_case_id=eval_case.id,
                        run_id=result.run_id,
                        dataset_version=dataset_version,
                        agent_version=agent_version,
                        prompt_version=prompt_version,
                        model_name=str(self._model_name()),
                        is_test_double=bool(self._is_test_double()),
                        status=result.status,
                        task_completed=result.task_completed,
                        tool_selection_correct=result.tool_selection_correct,
                        tool_argument_correct=result.tool_argument_correct,
                        evidence_coverage=(
                            Decimal(str(result.evidence_coverage))
                            if result.evidence_coverage is not None
                            else None
                        ),
                        latency_ms=result.latency_ms,
                        total_tokens=result.total_tokens,
                        estimated_cost_usd=Decimal(str(result.estimated_cost_usd or 0)),
                        failure_reason=result.failure_reason,
                        assertion_results=[item.to_dict() for item in result.assertion_results]
                        or None,
                    )
                )
                session.commit()
            except InvalidArgumentError:
                session.rollback()
                raise
            except Exception as exc:
                session.rollback()
                logger.error(
                    "eval_run_persist_failed",
                    extra={
                        "evaluation_id": evaluation_id,
                        "case_key": case.case_key,
                        "error_type": type(exc).__name__,
                    },
                )
                raise DatasetValidationError(
                    f"eval_run 落库失败：{case.case_key}",
                    details={"error_type": type(exc).__name__},
                ) from exc

    @staticmethod
    def _resolve_batch_status(case_results: list[CaseResult]) -> str:
        """批次状态。

        只要有 case 完成就算 ``succeeded`` —— 批次是"评测跑完了"，
        不是"所有 case 都通过了"。把 case 成败混进批次状态会让
        ``POST /evaluations`` 的 201/非 201 变得取决于 Agent 表现，
        CI 里就分不清"评测跑挂了"和"评测跑通了但质量不达标"。
        （后者由质量门禁的 ``blocked`` 表达。）
        """
        if not case_results:
            return "failed"
        return "succeeded"


# ---------------------------------------------------------------------------
# 模块级辅助
# ---------------------------------------------------------------------------

_DEFAULT_TOP_K = 3


def _values_match(expected: Any, actual: Any) -> bool:
    """按 EVALUATION §3 M4 的比较语义判断单个值是否匹配。"""
    # null：仅当实际值也为 null
    if expected is None:
        return actual is None

    # 布尔要排在数值之前：Python 里 True == 1，不先挡掉的话
    # 期望 True 会被 1 匹配上。
    if isinstance(expected, bool):
        return isinstance(actual, bool) and expected is actual

    # 数值：int/float 互相可比较，float 允许 1e-9 绝对误差
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        if isinstance(expected, float) or isinstance(actual, float):
            return abs(float(expected) - float(actual)) <= _FLOAT_TOLERANCE
        return int(expected) == int(actual)

    # 列表：视为集合无序比较（参数是标量列表时顺序无语义）
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        remaining = list(actual)
        for item in expected:
            for index, candidate in enumerate(remaining):
                if _values_match(item, candidate):
                    remaining.pop(index)
                    break
            else:
                return False
        return True

    # 字典：递归逐键比较，期望是子集即可（允许实际有额外键）
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _values_match(value, actual[key]) for key, value in expected.items()
        )

    return expected == actual


def _status_for_error(exc: Any) -> str:
    """把领域异常映射成 run 级状态。

    超时是独立终态（``timeout``），不是 ``failed`` —— 契约
    EVALUATION §3 M9 把两者都计入错误率，但它们的**处置方式不同**：
    超时要放宽预算或优化性能，失败要查缺陷。
    在 status 上就区分开，后续才能分组统计。
    """
    code = getattr(exc, "error_code", "")
    if code == "AGENT_TIMEOUT":
        return "timeout"
    return "failed"


def _now() -> datetime:
    from app.db.base import utcnow

    return utcnow()


def _iso(value: datetime) -> str:
    """转成契约要求的 ISO 8601（带 Z 后缀）格式。"""
    if value is None:
        return ""
    if value.tzinfo is not None:
        return value.isoformat().replace("+00:00", "Z")
    return value.isoformat() + "Z"


__all__ = [
    "CaseResult",
    "EvaluationResult",
    "EvaluationRunner",
]
