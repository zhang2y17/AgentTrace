"""评测服务：编排 ``EvaluationRunner``、落库批次结论、执行质量门禁。

**本层与 runner 的分工**

``EvaluationRunner`` 负责"怎么跑一个 case 并判定它"，
本服务负责"一次评测从请求到落库到可查询的完整生命周期"：

1. 调 runner 跑完整批；
2. 把批次结论写进 ``quality_gate``（若请求带了 thresholds）；
3. 提供 ``GET /evaluations/{id}`` 所需的**重读**能力 ——
   从 ``eval_run`` 表恢复批次结果，而不是把结果缓存在内存里。

第 3 点是关键：契约要求评测结果可查询，而查询发生在
`POST /evaluations` 返回之后的任意时刻（可能是另一个进程、
另一次部署）。因此**评测结论必须落库**，不能只存在于响应里。

**为什么批次元信息从 ``eval_run`` 反推而不是单开一张表**

DATA_MODEL 定了 8 张表，其中没有 ``evaluation`` 批次表 ——
批次 ID 是 ``eval_run`` 上的一个分组列（见 ``EvalRun.evaluation_id``）。
这是刻意的：一次评测的"case 级结果"就是它的全部内容，
单开一张只存聚合值的表会让两者可能不一致。因此这里重读时
**从 case 行重新计算聚合值**，保证报告与实际记录永远自洽。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.errors import EvaluationNotFoundError
from app.core.logging import get_logger
from app.evaluation.assertions import AssertionResult
from app.evaluation.gate import GateResult, check_gate
from app.evaluation.metrics import compute_metrics
from app.evaluation.runner import CaseResult, EvaluationResult, EvaluationRunner

logger = get_logger(__name__)


@dataclass(slots=True)
class StoredEvaluation:
    """从库里恢复出来的评测批次。"""

    evaluation_id: str
    dataset_version: str
    agent_version: str
    prompt_version: str
    model_name: str
    is_test_double: bool
    status: str
    case_results: list[CaseResult]
    metrics: dict[str, Any]
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    llm_provider: str = "fake"

    @property
    def case_count(self) -> int:
        return len(self.case_results)

    @property
    def passed_cases(self) -> int:
        return sum(1 for item in self.case_results if item.passed)

    @property
    def failed_cases(self) -> int:
        return self.case_count - self.passed_cases


class EvaluationService:
    """评测与门禁服务。

    Args:
        run_service: 执行单次运行的服务。
        session_factory: 返回 Session 的可调用对象。
        runner: 可注入的评测执行器（测试用）。
    """

    def __init__(
        self,
        *,
        run_service: Any | None = None,
        session_factory: Any | None = None,
        runner: EvaluationRunner | None = None,
    ) -> None:
        self._session_factory_override = session_factory
        self._run_service = run_service
        self._runner = runner

    # ------------------------------------------------------------------
    # 执行评测
    # ------------------------------------------------------------------

    def create_evaluation(
        self,
        *,
        dataset_version: str,
        agent_version: str = "v1",
        prompt_version: str = "prompt-v1",
        case_keys: list[str] | None = None,
        thresholds: dict[str, Any] | None = None,
    ) -> tuple[EvaluationResult, GateResult | None]:
        """执行一次评测；带 ``thresholds`` 时同时做门禁判定。

        Returns:
            ``(评测结果, 门禁结果或 None)``

        Raises:
            DatasetNotFoundError: 评测集不存在（404）。
            DatasetValidationError: 评测集非法或 case_keys 含未知用例（400）。
        """
        runner = self._resolve_runner()

        result = runner.run_evaluation(
            dataset_version=dataset_version,
            agent_version=agent_version,
            prompt_version=prompt_version,
            case_keys=case_keys,
        )

        gate_result: GateResult | None = None
        if thresholds:
            gate_result = check_gate(
                evaluation_id=result.evaluation_id,
                gate_name="evaluation-inline-gate",
                metrics=result.metrics,
                thresholds=thresholds,
            )
            self.persist_gate(gate_result)

        return result, gate_result

    # ------------------------------------------------------------------
    # 查询评测
    # ------------------------------------------------------------------

    def get_evaluation(
        self,
        evaluation_id: str,
        *,
        include_cases: bool = False,
        only_failures: bool = False,
    ) -> tuple[StoredEvaluation, dict[str, Any]]:
        """按 ``evaluation_id`` 重读评测结果。

        **从 case 行重新计算指标**，而不是存一份聚合快照。
        理由见模块 docstring：两份数据就有一份可能是错的。

        Args:
            evaluation_id: 批次 ID。
            include_cases: 是否内联逐 case 结果。
            only_failures: 仅返回失败 case（需 ``include_cases=true``）。

        Returns:
            ``(批次, 响应体)``

        Raises:
            EvaluationNotFoundError: 批次不存在（404）。
        """
        stored = self._load_evaluation(evaluation_id)

        outcomes = [item.to_outcome() for item in stored.case_results]
        metrics = compute_metrics(outcomes)

        payload: dict[str, Any] = {
            "evaluation_id": stored.evaluation_id,
            "dataset_version": stored.dataset_version,
            "agent_version": stored.agent_version,
            "prompt_version": stored.prompt_version,
            "model_name": stored.model_name,
            "llm_provider": stored.llm_provider,
            "is_test_double": stored.is_test_double,
            "status": stored.status,
            "case_count": stored.case_count,
            "passed_cases": stored.passed_cases,
            "failed_cases": stored.failed_cases,
            # 契约 API_CONTRACT §6 要求返回 run_ids。这里与
            # ``EvaluationResult.to_dict`` 保持同一顺序来源（``case_results``），
            # 否则 POST 与 GET 会给出顺序不同的两份 run_ids，
            # 而它们本应描述同一件事。
            "run_ids": [item.run_id for item in stored.case_results],
            "metrics": metrics,
            "started_at": stored.started_at,
            "ended_at": stored.ended_at,
            "duration_ms": stored.duration_ms,
            "data_source_note": _data_source_note(stored.is_test_double),
            "skipped_metrics": [],
        }

        if include_cases:
            cases = stored.case_results
            if only_failures:
                cases = [item for item in cases if not item.passed]
            payload["cases"] = [item.to_dict() for item in cases]

        return stored, payload

    def _load_evaluation(self, evaluation_id: str) -> StoredEvaluation:
        """从 ``eval_run`` 表恢复一个批次。"""
        factory = self._resolve_session_factory()
        with factory() as session:
            from sqlalchemy import select

            from app.db.models import EvalCase, EvalRun

            statement = (
                select(EvalRun, EvalCase.case_key)
                .join(EvalCase, EvalRun.eval_case_id == EvalCase.id)
                .where(EvalRun.evaluation_id == evaluation_id)
                # 按 case_key 排序而不是插入顺序：ULID 主键虽然时间有序，
                # 但评测集里的 case 顺序才是人阅读报告时的自然顺序。
                .order_by(EvalCase.case_key.asc())
            )

            try:
                rows = list(session.execute(statement).all())
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "evaluation_load_failed",
                    extra={
                        "evaluation_id": evaluation_id,
                        "error_type": type(exc).__name__,
                    },
                )
                raise EvaluationNotFoundError(
                    f"评测批次不存在：{evaluation_id}",
                    details={"evaluation_id": evaluation_id},
                ) from exc

            if not rows:
                raise EvaluationNotFoundError(
                    f"评测批次不存在：{evaluation_id}",
                    details={"evaluation_id": evaluation_id},
                )

            case_results: list[CaseResult] = []
            for eval_run, case_key in rows:
                case_results.append(_to_case_result(eval_run, case_key))

            first = rows[0][0]
            outcomes = [item.to_outcome() for item in case_results]
            metrics = compute_metrics(outcomes)

            # 批次时间窗口取所有 case 行的极值。
            # 一行不存 started_at（DATA_MODEL 没有这个列），
            # 因此用 created_at 作为近似 —— 它是 case 跑完的时刻。
            started_candidates = [
                row[0].created_at for row in rows if row[0].created_at is not None
            ]
            started_at = min(started_candidates) if started_candidates else None
            ended_at = max(started_candidates) if started_candidates else None
            duration_ms = None
            if started_at is not None and ended_at is not None:
                duration_ms = int((ended_at - started_at).total_seconds() * 1000)

            return StoredEvaluation(
                evaluation_id=evaluation_id,
                dataset_version=first.dataset_version,
                agent_version=first.agent_version,
                prompt_version=first.prompt_version,
                model_name=first.model_name,
                is_test_double=bool(first.is_test_double),
                status="succeeded",
                case_results=case_results,
                metrics=metrics,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                llm_provider="fake" if first.is_test_double else "openai",
            )

    # ------------------------------------------------------------------
    # 质量门禁
    # ------------------------------------------------------------------

    def run_gate(
        self,
        *,
        evaluation_id: str,
        gate_name: str = "release-gate",
        thresholds: dict[str, Any] | None = None,
    ) -> GateResult:
        """对指定评测批次执行门禁检查并落库。

        Args:
            evaluation_id: 被检查的评测批次。
            gate_name: 门禁名。
            thresholds: 阈值覆盖；``None`` 用默认阈值。

        Returns:
            ``GateResult``

        Raises:
            EvaluationNotFoundError: 评测批次不存在（404）。
            InvalidArgumentError: 阈值定义非法（400）。
        """
        stored = self._load_evaluation(evaluation_id)

        # 用重算出的指标而不是缓存值：门禁必须检查"库里记录的事实"，
        # 而不是某个请求当时算出的快照。
        metrics = stored.metrics

        result = check_gate(
            evaluation_id=evaluation_id,
            gate_name=gate_name,
            metrics=metrics,
            thresholds=thresholds,
        )
        self.persist_gate(result)
        return result

    def persist_gate(self, result: GateResult) -> None:
        """把门禁结果写入 ``quality_gate`` 表。

        契约 EVALUATION §6.3 第 3 条：必须持久化**阈值快照**，
        保证可复现判断依据。因此 ``thresholds`` 原样入库，
        而不只是存"通过了/没通过"。
        """
        factory = self._resolve_session_factory()
        with factory() as session:
            from app.db.models import QualityGate

            try:
                session.add(
                    QualityGate(
                        id=result.gate_id,
                        evaluation_id=result.evaluation_id,
                        gate_name=result.gate_name,
                        status=result.status,
                        thresholds=result.thresholds,
                        observed_metrics=result.observed_metrics,
                        violations=[item.to_dict() for item in result.violations] or None,
                        skipped_metrics=result.skipped_metrics or None,
                        blocked=result.blocked,
                    )
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                # 门禁结论落库失败**不**让请求失败：结论已经算出来了，
                # 返回给调用方比抛错更有用（调用方能据此阻断 CI）。
                # 但必须留 error 级别日志 —— 少了一条审计记录。
                logger.error(
                    "quality_gate_persist_failed",
                    extra={
                        "gate_id": result.gate_id,
                        "evaluation_id": result.evaluation_id,
                        "error_type": type(exc).__name__,
                    },
                )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve_runner(self) -> EvaluationRunner:
        if self._runner is not None:
            return self._runner

        if self._run_service is None:
            from app.services.run_service import RunService

            self._run_service = RunService()

        return EvaluationRunner(
            run_service=self._run_service,
            session_factory=self._session_factory_override,
        )

    def _resolve_session_factory(self) -> Any:
        if self._session_factory_override is not None:
            return self._session_factory_override
        from app.db.session import session_scope

        return session_scope


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _to_case_result(eval_run: Any, case_key: str) -> CaseResult:
    """把 ``EvalRun`` 行转回 ``CaseResult``。"""
    assertions: list[AssertionResult] = []
    for raw in eval_run.assertion_results or []:
        if not isinstance(raw, dict):
            continue
        assertions.append(
            AssertionResult(
                assertion=str(raw.get("assertion") or ""),
                passed=bool(raw.get("passed")),
                detail=raw.get("detail"),
            )
        )

    cost = eval_run.estimated_cost_usd
    return CaseResult(
        case_key=case_key,
        run_id=eval_run.run_id,
        status=str(eval_run.status),
        task_completed=bool(eval_run.task_completed),
        tool_selection_correct=eval_run.tool_selection_correct,
        tool_argument_correct=eval_run.tool_argument_correct,
        evidence_coverage=(
            float(eval_run.evidence_coverage)
            if eval_run.evidence_coverage is not None
            else None
        ),
        latency_ms=eval_run.latency_ms,
        total_tokens=int(eval_run.total_tokens or 0),
        estimated_cost_usd=(
            cost if isinstance(cost, Decimal) else Decimal(str(cost or 0))
        ),
        failure_reason=eval_run.failure_reason,
        assertion_results=assertions,
        needs_human_review=str(eval_run.status) == "timeout",
        tool_sequence=[],
    )


def _data_source_note(is_test_double: bool) -> str:
    if is_test_double:
        return (
            "离线评测结果（测试替身）：数值由本地测试替身产生，仅用于验证流程与逻辑正确性，"
            "不代表真实模型能力，也不代表任何线上流量或生产环境表现。"
        )
    return (
        "离线评测结果：在固定评测集上跑出，使用真实模型提供方。"
        "指标仅适用于本评测集与本次运行，不代表线上流量或生产环境表现。"
    )


__all__ = [
    "EvaluationService",
    "StoredEvaluation",
]
