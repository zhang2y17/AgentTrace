"""运行与 Trace 事件的 API 契约模型。

对应 API_CONTRACT §2、§3、§4、§5（runs 与 events 部分）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import EventStatus, EventType, RunStatus, UtcTimestamp

# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class CreateRunRequest(BaseModel):
    """``POST /runs`` 请求体。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(
        # 注意这里**不设 min_length**。空白问题在契约里要返回
        # 422 AGENT_VALIDATION_ERROR，而 Pydantic 的校验失败会被
        # 统一映射成 400 —— 若在 schema 层用 min_length=1 拦截，
        # 空白问题就永远拿不到契约要求的 422。因此把"非空白"这一步
        # 交给路由层与服务层判定（见 app/api/runs.py）。
        max_length=2000,
        description="提交给 Agent 的问题。不能为空白，长度上限由 MAX_QUESTION_CHARS 控制。",
    )
    agent_version: str = Field(default="v1", max_length=40)
    prompt_version: str = Field(default="prompt-v1", max_length=40)
    top_k: int = Field(default=3, ge=1, le=10, description="检索条数")
    metadata: dict[str, Any] = Field(default_factory=dict, description="自由标签，仅存于运行上下文")


class ReplayRequest(BaseModel):
    """``POST /runs/{run_id}/replay`` 请求体。

    所有字段可选：不传则沿用原始运行的输入。传了则用于做变体对比。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str | None = Field(default=None, max_length=2000)
    agent_version: str | None = Field(default=None, max_length=40)
    prompt_version: str | None = Field(default=None, max_length=40)
    top_k: int | None = Field(default=None, ge=1, le=10)
    note: str | None = Field(default=None, max_length=200, description="回放备注")


# ---------------------------------------------------------------------------
# 响应体
# ---------------------------------------------------------------------------


class ResultSummary(BaseModel):
    """run 的结果摘要。

    契约：只存摘要，不存完整 Prompt 与完整模型响应。
    """

    model_config = ConfigDict(extra="forbid")

    answer: str | None = Field(default=None, description="答案文本（已截断）")
    answer_chars: int = Field(default=0, ge=0)
    citations: list[str] = Field(default_factory=list, description="答案中引用的文档 ID 列表")
    evidence_sufficient: bool = Field(default=False)
    final_status: str = Field(default="", description="节点层的最终判定状态")
    validation_errors: list[str] = Field(default_factory=list)


class RunCounts(BaseModel):
    """一次运行的产物计数，便于调用方快速判断 Trace 是否完整。"""

    model_config = ConfigDict(extra="forbid")

    trace_events: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)


class RunDetail(BaseModel):
    """run 详情。``POST /runs``、``GET /runs/{id}``、replay 共用此结构。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    source_run_id: str | None = Field(
        default=None,
        description="非 null 表示这是一次回放，值为被回放的原始 run_id",
    )
    question: str
    agent_version: str
    prompt_version: str
    llm_provider: str
    model_name: str | None = None
    is_test_double: bool = Field(default=False, description="为 True 表示数值由测试替身产生")
    duration_ms: int | None = Field(default=None, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    cost_estimation_unavailable: bool = False
    started_at: UtcTimestamp | None = None
    ended_at: UtcTimestamp | None = None
    error_code: str | None = None
    error_message: str | None = None
    result_summary: ResultSummary = Field(default_factory=ResultSummary)
    counts: RunCounts = Field(default_factory=RunCounts)
    events: list[TraceEventOut] | None = Field(
        default=None,
        description="仅当 include_events=true 时返回",
    )


class TraceEventOut(BaseModel):
    """单条 Trace 事件（API_CONTRACT §4）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    run_id: str
    parent_event_id: str | None = None
    event_type: EventType
    name: str
    status: EventStatus
    sequence: int = Field(ge=1)
    started_at: UtcTimestamp
    ended_at: UtcTimestamp | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    input_summary: str | None = None
    output_summary: str | None = None
    error_code: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceEventPage(BaseModel):
    """``GET /runs/{run_id}/events`` 响应（API_CONTRACT §4）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    count: int = Field(ge=0, description="本次返回的事件数")
    total: int = Field(ge=0, description="过滤后的总事件数")
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    filters: dict[str, list[str]] = Field(default_factory=dict, description="实际生效的过滤器")
    events: list[TraceEventOut] = Field(default_factory=list)


# 解决 RunDetail 中前向引用 TraceEventOut 的顺序问题
RunDetail.model_rebuild()


__all__ = [
    "CreateRunRequest",
    "ReplayRequest",
    "ResultSummary",
    "RunCounts",
    "RunDetail",
    "TraceEventOut",
    "TraceEventPage",
]
