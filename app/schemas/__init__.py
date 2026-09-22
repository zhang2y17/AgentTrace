"""Pydantic 契约模型。

本包是整个项目的类型契约中心：
- ``common``：枚举与统一错误体
- ``health``：健康检查
- ``runs``：运行与 Trace 事件
- ``tools``：4 个工具的参数与返回值
- ``metrics``：指标汇总
- ``eval``：评测与质量门禁
- ``agents``：Agent 内部数据结构
"""

from app.schemas.agents import (
    AgentFinalResult,
    EvidenceAssessment,
    FinalValidation,
    ParsedTask,
)
from app.schemas.common import (
    DataScope,
    ErrorBody,
    ErrorCode,
    ErrorResponse,
    EvalRunStatus,
    EventStatus,
    EventType,
    GateStatus,
    HealthStatus,
    ModelCallStatus,
    RunStatus,
    ToolCallStatus,
)
from app.schemas.eval import (
    CaseAssertionResult,
    CreateEvaluationRequest,
    EvaluationCaseResult,
    EvaluationResponse,
    GateViolation,
    QualityGateRequest,
    QualityGateResponse,
    ThresholdSpec,
)
from app.schemas.health import ComponentHealth, HealthResponse, LlmProviderHealth
from app.schemas.metrics import (
    DATA_SOURCE_NOTE,
    OFFLINE_EVAL_NOTE,
    MetricsBlock,
    MetricsGroup,
    MetricsSummaryResponse,
    ToolUsageStat,
)
from app.schemas.runs import (
    CreateRunRequest,
    ReplayRequest,
    ResultSummary,
    RunCounts,
    RunDetail,
    TraceEventOut,
    TraceEventPage,
)
from app.schemas.tools import (
    CalculateCostSummaryArgs,
    CalculateLatencySummaryArgs,
    CostSummary,
    Document,
    DocumentHit,
    GetDocumentArgs,
    LatencySummary,
    ModelCostBreakdown,
    RetrievalEvidence,
    RunScopedArgs,
    SearchDocumentsArgs,
    SearchDocumentsResult,
    StageLatency,
    ToolArgsModel,
)

__all__ = [
    "DATA_SOURCE_NOTE",
    "OFFLINE_EVAL_NOTE",
    "AgentFinalResult",
    "CalculateCostSummaryArgs",
    "CalculateLatencySummaryArgs",
    "CaseAssertionResult",
    "ComponentHealth",
    "CostSummary",
    "CreateEvaluationRequest",
    "CreateRunRequest",
    "DataScope",
    "Document",
    "DocumentHit",
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "EvalRunStatus",
    "EvaluationCaseResult",
    "EvaluationResponse",
    "EventStatus",
    "EventType",
    "EvidenceAssessment",
    "FinalValidation",
    "GateStatus",
    "GateViolation",
    "GetDocumentArgs",
    "HealthResponse",
    "HealthStatus",
    "LatencySummary",
    "LlmProviderHealth",
    "MetricsBlock",
    "MetricsGroup",
    "MetricsSummaryResponse",
    "ModelCallStatus",
    "ModelCostBreakdown",
    "ParsedTask",
    "QualityGateRequest",
    "QualityGateResponse",
    "ReplayRequest",
    "ResultSummary",
    "RetrievalEvidence",
    "RunCounts",
    "RunDetail",
    "RunScopedArgs",
    "RunStatus",
    "SearchDocumentsArgs",
    "SearchDocumentsResult",
    "StageLatency",
    "ThresholdSpec",
    "ToolArgsModel",
    "ToolCallStatus",
    "ToolUsageStat",
    "TraceEventOut",
    "TraceEventPage",
]
