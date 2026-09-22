"""通用枚举与响应模型。

契约 DATA_MODEL §1 定义了全部枚举取值。本模块是这些枚举的**唯一**定义处，
其他模块必须从这里导入，不得各自重复定义字符串字面量。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

# 契约 API_CONTRACT §0.2 要求所有时间为 ISO 8601 UTC、带 ``Z`` 后缀、
# **毫秒精度**（``2026-09-22T04:00:00.000Z``）。
#
# 直接声明 ``datetime`` 有两个坑，都会让响应偏离契约：
#   1. ``datetime.isoformat()`` 在微秒为 0 时**省略小数部分**，
#      产出 ``2026-09-22T04:00:00Z`` 而不是 ``...T04:00:00.000Z``；
#      在微秒非 0 时又产出 6 位（``.100000``）。
#   2. 从 SQLite 读回的时间是 naive 的（没有 tzinfo），序列化结果不带 ``Z``，
#      而 PostgreSQL 读回的是 aware —— 同一份契约在两种方言下产出两种格式。
#
# 因此统一走这个别名：入库/构造时补 UTC 时区（naive 一律当 UTC），
# 序列化时收敛到毫秒并带 ``Z``。所有时间字段都必须用它，
# 不允许直接写 ``datetime``。
_ISO_MILLISECONDS = 3


def _coerce_utc(value: Any) -> Any:
    """把时间输入补齐为带 UTC 时区的 ``datetime``。

    契约只声明 UTC 时间，因此 naive 值一律**当作** UTC ——
    而不是拒绝。库里读回的 naive 值是 SQLite 的正常行为，
    拒绝会让整个 API 在 SQLite 下不可用。
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _format_utc(value: datetime) -> str:
    """按契约格式序列化：UTC、毫秒精度、``Z`` 后缀。"""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)

    # ``isoformat(timespec="milliseconds")`` 恰好给出 ``...T04:00:00.000+00:00``，
    # 把 ``+00:00`` 换成 ``Z`` 即为契约形状。
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


UtcTimestamp = Annotated[
    datetime,
    BeforeValidator(_coerce_utc),
    PlainSerializer(_format_utc, return_type=str, when_used="json"),
]
"""契约 API_CONTRACT §0.2 的 UTC 毫秒时间戳。

用法：``started_at: UtcTimestamp`` —— 不要写 ``datetime``。
"""


class RunStatus(StrEnum):
    """run 的终态与中间态（DATA_MODEL §1）。

    注意 ``degraded`` 的语义：流程完成但证据不足或校验有非致命问题。
    它**不视为成功**（EVALUATION §3 M1），这是刻意的严格定义。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED = "degraded"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        """是否为终态（不再变化）。"""
        return self is not RunStatus.PENDING and self is not RunStatus.RUNNING

    @property
    def counts_as_success(self) -> bool:
        """是否计入成功。仅 ``succeeded`` 计入（EVALUATION §3 M1）。"""
        return self is RunStatus.SUCCEEDED

    @property
    def counts_as_error(self) -> bool:
        """是否计入错误率（EVALUATION §3 M9）。``degraded`` 不计入错误。"""
        return self in (RunStatus.FAILED, RunStatus.TIMEOUT)


class EventType(StrEnum):
    """Trace 事件类型（TRACE_SCHEMA §2，固定 6 类）。"""

    RUN = "run"
    NODE = "node"
    TOOL_CALL = "tool_call"
    MODEL_CALL = "model_call"
    ERROR = "error"
    FINAL_RESULT = "final_result"


class EventStatus(StrEnum):
    """Trace 事件状态（TRACE_SCHEMA §4，固定 8 个）。"""

    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRIED = "retried"
    INVALID_ARGUMENTS = "invalid_arguments"
    HANDOFF = "handoff"

    @property
    def is_terminal(self) -> bool:
        """``running`` 与 ``pending`` 之外都是终态。"""
        return self not in (EventStatus.PENDING, EventStatus.RUNNING)


class ToolCallStatus(StrEnum):
    """工具调用结果状态。"""

    OK = "ok"
    ERROR = "error"
    INVALID_ARGUMENTS = "invalid_arguments"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class ModelCallStatus(StrEnum):
    """模型调用结果状态。"""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


class EvalRunStatus(StrEnum):
    """评测执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GateStatus(StrEnum):
    """质量门禁状态。"""

    PASSED = "passed"
    FAILED = "failed"


class HealthStatus(StrEnum):
    """组件与整体健康状态。"""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"


class DataScope(StrEnum):
    """指标数据来源范围（EVALUATION §0，契约 B9 强制标注）。

    任何指标输出都必须带 scope，禁止暗示生产表现。
    """

    OFFLINE_EVALUATION = "offline_evaluation"
    SAMPLE_RUNS = "sample_runs"
    EMPTY = "empty"


class ErrorCode(StrEnum):
    """统一错误码（API_CONTRACT §0.1）。"""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    EVALUATION_NOT_FOUND = "EVALUATION_NOT_FOUND"
    RUN_NOT_REPLAYABLE = "RUN_NOT_REPLAYABLE"
    AGENT_VALIDATION_ERROR = "AGENT_VALIDATION_ERROR"
    TRACE_WRITE_FAILED = "TRACE_WRITE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    LLM_PROVIDER_ERROR = "LLM_PROVIDER_ERROR"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"


# ---------------------------------------------------------------------------
# 统一错误响应体
# ---------------------------------------------------------------------------


from pydantic import BaseModel, ConfigDict, Field  # noqa: E402


class ErrorBody(BaseModel):
    """``{"error": {...}}`` 的内层对象（API_CONTRACT §0.1）。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="错误码，取值见 ErrorCode")
    message: str = Field(description="面向调用方的错误说明，已脱敏")
    details: dict[str, Any] = Field(default_factory=dict, description="机器可读的补充信息")
    request_id: str = Field(description="请求 ID，用于与日志关联")


class ErrorResponse(BaseModel):
    """统一错误响应体。所有非 2xx 响应都使用此结构。"""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


__all__ = [
    "DataScope",
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "EvalRunStatus",
    "EventStatus",
    "EventType",
    "GateStatus",
    "HealthStatus",
    "ModelCallStatus",
    "RunStatus",
    "ToolCallStatus",
    "UtcTimestamp",
]
