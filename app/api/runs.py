"""运行相关端点：``POST /runs``、``GET /runs/{id}``、``GET /runs/{id}/events``、
``POST /runs/{id}/replay``。

契约 API_CONTRACT §2 ~ §5。

**本层职责边界**：只做参数解析、服务调用、错误映射。
编排逻辑在 ``app/services/run_service.py``，数据访问在 ``app/db/repository.py``。

一处需要注意的契约细节：``POST /runs`` 对空白 ``question`` 要求返回
**422 AGENT_VALIDATION_ERROR**，而 FastAPI 的 ``RequestValidationError``
统一被主应用的处理器映射为 400。因此空白校验放在服务层做，由路由层
捕获后转成 422 —— 见 ``_validate_question_or_422``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Path, Query, Response, status

from app.api.deps import RunServiceDep
from app.core.errors import (
    AgentTraceError,
    InvalidArgumentError,
    RunNotFoundError,
)
from app.core.logging import get_logger
from app.schemas.common import EventStatus, EventType
from app.schemas.runs import (
    CreateRunRequest,
    ReplayRequest,
    ResultSummary,
    RunCounts,
    RunDetail,
    TraceEventOut,
    TraceEventPage,
)
from app.services.run_service import RunExecutionResult

logger = get_logger(__name__)

router = APIRouter(tags=["runs"])

# 依赖用 ``RunServiceDep``（函数包装），**不要**写成 ``Depends(RunService)``。
#
# 原因：直接 ``Depends(类)`` 会让 FastAPI 去内省该类的 ``__init__`` 签名，
# 把它当成一个"子依赖"来解析 —— 于是 ``__init__`` 里的
# ``settings: Settings | None``（Pydantic 模型）被当成了**请求体字段**，
# ``session_factory`` / ``graph_builder`` 被当成了**查询参数**。
# 结果是 requestBody 变成 ``{"payload": ..., "settings": ...}``，
# 与契约完全不符，调用方按文档发请求会被判 400。
# 用函数包装后，依赖是一个无参调用，内省问题自然消失。
ServiceDep = RunServiceDep

# 事件查询的分页上限。上限存在的意义是防止一次拉取把整库读进内存。
_MAX_EVENT_LIMIT = 1000


# ---------------------------------------------------------------------------
# 请求校验辅助
# ---------------------------------------------------------------------------


def _split_csv(raw: str | None) -> list[str] | None:
    """把逗号分隔的查询参数拆成列表。

    返回 ``None``（而非空列表）表示"未传该过滤条件"——
    这两者在契约里语义不同：未传 = 不过滤，传了空值 = 传参错误。
    """
    if raw is None:
        return None
    parts = [item.strip() for item in raw.split(",")]
    return [item for item in parts if item]


def _validate_enum_filter(
    values: list[str] | None, allowed: type, param_name: str
) -> list[str] | None:
    """校验枚举型过滤参数，非法值抛 ``InvalidArgumentError``（400）。

    契约要求 ``event_type`` / ``status`` 含非法值时返回 400 INVALID_ARGUMENT，
    而不是静默忽略 —— 静默忽略会让"筛了但没生效"变成难以察觉的错误结论。
    """
    if values is None:
        return None
    allowed_values = {member.value for member in allowed}
    invalid = [item for item in values if item not in allowed_values]
    if invalid:
        raise InvalidArgumentError(
            f"{param_name} 含非法值。",
            details={
                "invalid": invalid,
                "allowed": sorted(allowed_values),
            },
        )
    return values


def _validate_question_or_422(question: str) -> None:
    """空白问题 → 422 ``AGENT_VALIDATION_ERROR``（契约 API_CONTRACT §2）。

    Raises:
        AgentTraceError: 带上 422 状态码与 AGENT_VALIDATION_ERROR 错误码。

    状态码取 422。Starlette 的 ``HTTP_422_UNPROCESSABLE_ENTITY`` 已被
    重命名为 ``HTTP_422_UNPROCESSABLE_CONTENT``（数值相同，仍是 422），
    新名字在旧版 Starlette 上不存在，因此直接用数值 422 ——
    这比 try/except 两个常量名或用会告警的旧名都干净。
    """
    from app.schemas.common import ErrorCode

    if not question or not question.strip():
        error = InvalidArgumentError("question 不能为空白字符串。")
        error.error_code = ErrorCode.AGENT_VALIDATION_ERROR.value
        error.http_status = 422
        raise error


# ---------------------------------------------------------------------------
# 响应组装
# ---------------------------------------------------------------------------


def _to_run_detail(
    result: RunExecutionResult, *, events: list[TraceEventOut] | None = None
) -> RunDetail:
    """把服务层结果组装成 API 契约模型。

    这层转换是刻意的：服务层不需要知道 API 的字段名与嵌套结构，
    契约变动只需要改这里。
    """
    return RunDetail(
        run_id=result.run_id,
        status=result.status,
        source_run_id=result.source_run_id,
        question=result.question,
        agent_version=result.agent_version,
        prompt_version=result.prompt_version,
        llm_provider=result.llm_provider,
        model_name=result.model_name,
        is_test_double=result.is_test_double,
        duration_ms=result.duration_ms,
        total_tokens=result.total_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        cost_estimation_unavailable=result.cost_estimation_unavailable,
        started_at=result.started_at,
        ended_at=result.ended_at,
        error_code=result.error_code,
        error_message=result.error_message,
        result_summary=ResultSummary(
            answer=result.result_summary.get("answer"),
            answer_chars=int(result.result_summary.get("answer_chars") or 0),
            citations=list(result.result_summary.get("citations") or []),
            evidence_sufficient=bool(result.result_summary.get("evidence_sufficient", False)),
            final_status=str(result.result_summary.get("final_status") or ""),
            validation_errors=list(result.result_summary.get("validation_errors") or []),
        ),
        counts=RunCounts(
            trace_events=int(result.counts.get("trace_events", 0)),
            tool_calls=int(result.counts.get("tool_calls", 0)),
            model_calls=int(result.counts.get("model_calls", 0)),
        ),
        events=events,
    )


def _to_event_out(event: Any) -> TraceEventOut:
    """ORM 事件 → API 模型。"""
    return TraceEventOut(
        event_id=event.event_id,
        run_id=event.run_id,
        parent_event_id=event.parent_event_id,
        event_type=event.event_type,
        name=event.name,
        status=event.status,
        sequence=event.sequence,
        started_at=event.started_at,
        ended_at=event.ended_at,
        duration_ms=event.duration_ms,
        input_summary=event.input_summary,
        output_summary=event.output_summary,
        error_code=event.error_code,
        attributes=event.attributes or {},
    )


def _load_events(run_id: str, *, session_factory: Any | None = None) -> list[Any]:
    """读取 run 的原始事件行（用于 ``include_events=true``）。"""
    from app.db.repository import TraceRepository
    from app.db.session import session_scope

    factory = session_factory or session_scope
    with factory() as session:
        events, _total = TraceRepository(session).list_events(run_id, limit=_MAX_EVENT_LIMIT)
        return events


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post(
    "/runs",
    response_model=RunDetail,
    status_code=status.HTTP_201_CREATED,
    summary="提交并同步执行一次 Agent 运行",
)
def create_run(
    payload: Annotated[CreateRunRequest, Body(description="运行请求体")],
    response: Response,
    service: ServiceDep,
) -> RunDetail:
    """执行一次运行。

    同步等待 Agent 完成，最长 ``AGENT_TIMEOUT_SECONDS``；
    超时返回 504 且 run 状态落库为 ``timeout``。

    **注意这里刻意不接收 ``settings`` 参数**：``Settings`` 继承自
    ``BaseSettings``，是 Pydantic 模型。FastAPI 的参数分析规则是
    "Pydantic 模型类型 → 请求体字段"，因此哪怕写成
    ``Annotated[Settings, Depends(get_settings)]``，它依然会被算作
    一个 body 参数 —— 于是 requestBody 变成
    ``{"payload": {...}, "settings": {...}}``，与契约
    （裸 ``CreateRunRequest``）不符，调用方按文档发请求会被拒。
    ``RunService`` 内部已经持有配置，这里不需要它。
    """
    _validate_question_or_422(payload.question)

    result = service.execute(
        question=payload.question,
        agent_version=payload.agent_version,
        prompt_version=payload.prompt_version,
        top_k=payload.top_k,
        metadata=payload.metadata,
    )
    # Location 头指向新资源，符合 201 的语义
    response.headers["Location"] = f"/runs/{result.run_id}"
    return _to_run_detail(result)


@router.get(
    "/runs/{run_id}",
    response_model=RunDetail,
    summary="查询单次运行详情",
)
def get_run(
    service: ServiceDep,
    run_id: Annotated[str, Path(description="run ID（run_ 前缀）")],
    include_events: Annotated[bool, Query(description="为 true 时内联 events 数组")] = False,
) -> RunDetail:
    """查询运行详情。

    ``RunDetail`` 由持久化的 run 行组装，因此**不需要重跑 Agent** ——
    这正是把结果落库的意义。
    """
    record = service.get_run_record(run_id)
    if record is None:
        raise RunNotFoundError(f"run 不存在：{run_id}")

    events: list[TraceEventOut] | None = None
    if include_events:
        events = [_to_event_out(event) for event in _load_events(run_id)]

    summary = record.result_summary or {}
    counts = service.get_run_counts(run_id)

    return RunDetail(
        run_id=record.id,
        status=record.status,
        source_run_id=record.source_run_id,
        question=record.question,
        agent_version=record.agent_version,
        prompt_version=record.prompt_version,
        llm_provider=record.llm_provider,
        model_name=record.model_name,
        is_test_double=bool(record.is_test_double),
        duration_ms=record.total_duration_ms,
        total_tokens=int(record.total_tokens or 0),
        estimated_cost_usd=float(record.estimated_cost_usd or 0),
        started_at=record.started_at,
        ended_at=record.ended_at,
        error_code=record.error_code,
        error_message=record.error_message,
        result_summary=ResultSummary(
            answer=summary.get("answer"),
            answer_chars=int(summary.get("answer_chars") or 0),
            citations=list(summary.get("citations") or []),
            evidence_sufficient=bool(summary.get("evidence_sufficient", False)),
            final_status=str(summary.get("final_status") or ""),
            validation_errors=list(summary.get("validation_errors") or []),
        ),
        counts=RunCounts(
            trace_events=counts.get("trace_events", 0),
            tool_calls=counts.get("tool_calls", 0),
            model_calls=counts.get("model_calls", 0),
        ),
        events=events,
    )


@router.get(
    "/runs/{run_id}/events",
    response_model=TraceEventPage,
    summary="查询运行的全部 Trace 事件",
)
def list_run_events(
    run_id: Annotated[str, Path(description="run ID（run_ 前缀）")],
    service: ServiceDep,
    event_type: Annotated[
        str | None, Query(description="逗号分隔的事件类型，如 node,tool_call")
    ] = None,
    status_filter: Annotated[
        str | None, Query(alias="status", description="逗号分隔的状态，如 failed,retried")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_EVENT_LIMIT)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TraceEventPage:
    """按 ``sequence`` 升序列出事件。

    先确认 run 存在再查事件：对不存在的 run 返回空列表会让调用方
    误以为"这个 run 没有事件"，而正确的结论是"这个 run 不存在"。
    """
    record = service.get_run_record(run_id)
    if record is None:
        raise RunNotFoundError(f"run 不存在：{run_id}")

    types = _validate_enum_filter(_split_csv(event_type), EventType, "event_type")
    statuses = _validate_enum_filter(_split_csv(status_filter), EventStatus, "status")

    from app.db.repository import TraceRepository
    from app.db.session import session_scope

    with session_scope() as session:
        events, total = TraceRepository(session).list_events(
            run_id,
            event_types=types,
            statuses=statuses,
            limit=limit,
            offset=offset,
        )
        items = [_to_event_out(event) for event in events]

    return TraceEventPage(
        run_id=run_id,
        count=len(items),
        total=total,
        limit=limit,
        offset=offset,
        filters={
            "event_type": types or [],
            "status": statuses or [],
        },
        events=items,
    )


@router.post(
    "/runs/{run_id}/replay",
    response_model=RunDetail,
    status_code=status.HTTP_201_CREATED,
    summary="以原始运行的输入重新执行一次",
)
def replay_run(
    service: ServiceDep,
    response: Response,
    run_id: Annotated[str, Path(description="被回放的原始 run ID")],
    payload: Annotated[ReplayRequest | None, Body()] = None,
) -> RunDetail:
    """回放一次运行。

    **绝不覆盖原始记录**：返回的 ``run_id`` 是新 ID，
    ``source_run_id`` 指向被回放的原 run。
    """
    request = payload or ReplayRequest()

    result = service.replay(
        run_id,
        question=request.question,
        agent_version=request.agent_version,
        prompt_version=request.prompt_version,
        top_k=request.top_k,
        note=request.note,
    )
    response.headers["Location"] = f"/runs/{result.run_id}"
    return _to_run_detail(result)


# 让 AgentTraceError 的子类能被本模块的异常处理器识别
__all__ = ["AgentTraceError", "create_run", "get_run", "list_run_events", "replay_run", "router"]
