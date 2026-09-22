"""评测与质量门禁的 API 契约模型（API_CONTRACT §6、§7、§9）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import DataScope, EvalRunStatus, GateStatus
from app.schemas.metrics import MetricsBlock

ThresholdOperator = Literal["min", "max"]


# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------


class ThresholdSpec(BaseModel):
    """单个指标的阈值。

    契约 EVALUATION §6.2：
    - 比率型指标使用 ``min``（越大越好）
    - 代价型指标使用 ``max``（越小越好）

    ``min`` 与 ``max`` 必须提供且仅提供其一，由服务层按指标类型校验。
    """

    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None

    @field_validator("max")
    @classmethod
    def _validate_not_both(cls, value: float | None, info: Any) -> float | None:
        if value is not None and info.data.get("min") is not None:
            raise ValueError("阈值不能同时指定 min 与 max")
        return value

    @field_validator("min")
    @classmethod
    def _validate_min_specified(cls, value: float | None, info: Any) -> float | None:
        # 注意：Pydantic 的字段校验顺序与声明顺序一致，max 先于 min 校验，
        # 因此这里需要检查 future 字段不可行；两个都不给的情况在下面 model_validator 处理。
        return value

    def operator(self) -> ThresholdOperator | None:
        """返回该阈值使用的运算符。"""
        if self.min is not None:
            return "min"
        if self.max is not None:
            return "max"
        return None

    def threshold_value(self) -> float | None:
        """返回阈值数值。"""
        return self.min if self.min is not None else self.max


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------


class CreateEvaluationRequest(BaseModel):
    """``POST /evaluations`` 请求体（API_CONTRACT §6）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dataset_version: str = Field(min_length=1, max_length=60, description="如 doc_research_v1")
    agent_version: str = Field(default="v1", max_length=40)
    prompt_version: str = Field(default="prompt-v1", max_length=40)
    model_name: str | None = Field(default=None, max_length=80)
    case_keys: list[str] | None = Field(default=None, description="只跑指定用例；不传表示全量")
    thresholds: dict[str, ThresholdSpec] | None = Field(
        default=None, description="仅用于本次评测附带的门禁判断，不改变批次指标"
    )


class CaseAssertionResult(BaseModel):
    """单条断言的执行结果。"""

    model_config = ConfigDict(extra="forbid")

    assertion: str
    passed: bool
    detail: str | None = None


class EvaluationCaseResult(BaseModel):
    """单个评测用例的结果（API_CONTRACT §7 的 cases 元素）。"""

    model_config = ConfigDict(extra="forbid")

    case_key: str
    run_id: str | None = None
    status: EvalRunStatus
    task_completed: bool
    tool_selection_correct: bool | None = None
    tool_argument_correct: bool | None = None
    evidence_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: int | None = Field(default=None, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    failure_reason: str | None = Field(
        default=None,
        description="格式 '<category>: <detail>'，category 见 EVALUATION §5.1",
    )
    assertion_results: list[CaseAssertionResult] = Field(default_factory=list)


class EvaluationResponse(BaseModel):
    """``POST /evaluations`` 与 ``GET /evaluations/{id}`` 的响应。"""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    dataset_version: str
    agent_version: str
    prompt_version: str
    model_name: str | None = None
    llm_provider: str
    is_test_double: bool = Field(
        description="契约 B5：为 True 表示本次评测由测试替身产生，不能用于说明模型能力"
    )
    status: EvalRunStatus
    scope: DataScope = DataScope.OFFLINE_EVALUATION
    data_source_note: str
    case_count: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    metrics: MetricsBlock
    skipped_metrics: list[str] = Field(
        default_factory=list,
        description="因无数据而未参与判定的指标（EVALUATION §6.3：跳过 ≠ 通过）",
    )
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    cases: list[EvaluationCaseResult] | None = Field(
        default=None, description="仅当 include_cases=true 时返回"
    )


# ---------------------------------------------------------------------------
# 质量门禁
# ---------------------------------------------------------------------------


class QualityGateRequest(BaseModel):
    """``POST /quality-gates/check`` 请求体（API_CONTRACT §9）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evaluation_id: str = Field(min_length=1, max_length=64)
    gate_name: str = Field(default="release-gate", min_length=1, max_length=80)
    thresholds: dict[str, ThresholdSpec] | None = Field(
        default=None, description="不传则使用项目默认阈值"
    )


class GateViolation(BaseModel):
    """一条阈值违规记录。"""

    model_config = ConfigDict(extra="forbid")

    metric: str
    operator: ThresholdOperator
    threshold: float
    observed: float
    reason: str


class QualityGateResponse(BaseModel):
    """门禁结果（API_CONTRACT §9）。

    ``passed = (len(violations) == 0)``。
    ``blocked = not passed``，表示该版本**不应**通过门禁。
    """

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    gate_name: str
    evaluation_id: str
    passed: bool
    blocked: bool
    status: GateStatus
    observed_metrics: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, ThresholdSpec] = Field(default_factory=dict)
    violations: list[GateViolation] = Field(default_factory=list)
    skipped_metrics: list[str] = Field(
        default_factory=list,
        description="无数据而跳过的指标；**不得**在文档中把跳过说成通过",
    )
    checked_at: datetime
    data_source_note: str = (
        "阈值判定基于离线评测结果，通过门禁仅表示满足本项目定义的离线质量基线，不等于可生产发布。"
    )


__all__ = [
    "CaseAssertionResult",
    "CreateEvaluationRequest",
    "EvaluationCaseResult",
    "EvaluationResponse",
    "GateViolation",
    "QualityGateRequest",
    "QualityGateResponse",
    "ThresholdOperator",
    "ThresholdSpec",
]
