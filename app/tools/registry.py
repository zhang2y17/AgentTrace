"""工具注册表：发现、校验、计时、落库、重试。

契约 PROJECT_SPEC §3.3 的四条强制约束在这里落地：

1. **参数经 Pydantic 校验** —— 校验失败写 ``invalid_arguments``，
   且实现体**不被调用**（结构性保证：实现体只接收已校验的模型实例）；
2. **确定性计算** —— 由 ``ToolSpec.uses_llm`` 标注，注册表不改变这一点；
3. **统一注册表发现** —— 节点通过名字调用，不持有函数引用；
4. **每次调用双写** —— ``tool_call`` 表 + ``tool_call`` 类型的 trace_event。

重试策略：只对**可重试的错误**重试（超时、临时故障）。
参数校验失败**不重试** —— 同一个非法参数重试不会有不同结果。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import ToolExecutionError
from app.core.logging import get_logger
from app.core.redaction import summarize
from app.tools.base import (
    TOOL_NAMES,
    ToolSpec,
)

logger = get_logger(__name__)

# 工具名 → ToolSpec 的注册表。模块级字典：注册在 import 时完成，
# 进程生命周期内不变。这个"不可变"是刻意的 ——
# 若允许运行时替换，评测的可复现性就没了。
_REGISTRY: dict[str, ToolSpec] = {}


class InvalidToolArgumentsError(Exception):
    """参数未通过校验。

    与 ``InvalidArgumentError`` 的区别：后者是 API 层错误（返回 400），
    本异常是**工具层**的执行结果，会被记录为 ``invalid_arguments`` 状态
    并继续流程（而不是让整个 run 失败）。
    """

    def __init__(self, tool_name: str, errors: list[dict[str, Any]]) -> None:
        self.tool_name = tool_name
        self.errors = errors
        super().__init__(f"工具 {tool_name} 的参数校验失败")


class ToolNotFoundError(Exception):
    """请求的工具未注册。"""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"工具未注册：{tool_name}")


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register(spec: ToolSpec) -> None:
    """注册一个工具。

    Raises:
        ValueError: 工具名重复（静默覆盖会导致"改了工具却没生效"的错觉）。
    """
    if spec.name in _REGISTRY:
        raise ValueError(f"工具已注册，不允许覆盖：{spec.name}")
    _REGISTRY[spec.name] = spec
    logger.debug(
        "tool_registered",
        extra={
            "tool_name": spec.name,
            "version": spec.version,
            "uses_llm": spec.uses_llm,
        },
    )


def get(tool_name: str) -> ToolSpec:
    """按名字取工具定义。

    Raises:
        ToolNotFoundError: 未注册。
    """
    spec = _REGISTRY.get(tool_name)
    if spec is None:
        raise ToolNotFoundError(tool_name)
    return spec


def is_registered(tool_name: str) -> bool:
    """工具是否已注册。"""
    return tool_name in _REGISTRY


def registered_names() -> list[str]:
    """已注册工具名（排序后，确定性）。"""
    return sorted(_REGISTRY)


def clear() -> None:
    """清空注册表。仅供测试使用。"""
    _REGISTRY.clear()


def register_builtin_tools(
    *,
    document_tools: Any,
    analytics_tools: Any,
    session_factory: Callable[[], Any] | None = None,
) -> None:
    """注册四个内置工具。

    由应用启动时调用一次。参数是"工具实现实例"而不是模块级单例，
    这样测试可以注入不同的实现（例如指向临时文档目录的 store）。

    Args:
        document_tools: ``DocumentSearchTools`` 实例。
        analytics_tools: ``AnalyticsTools`` 实例。
        session_factory: 预留给需要 Session 的工具；当前四个工具的实现
            都已持有自己的依赖，此参数保持兼容以便扩展。
    """
    from app.tools.base import (
        TOOL_CALCULATE_COST_SUMMARY,
        TOOL_CALCULATE_LATENCY_SUMMARY,
        TOOL_GET_DOCUMENT,
        TOOL_SEARCH_DOCUMENTS,
        CalculateCostSummaryArgs,
        CalculateLatencySummaryArgs,
        GetDocumentArgs,
        SearchDocumentsArgs,
    )

    specs = (
        ToolSpec(
            name=TOOL_SEARCH_DOCUMENTS,
            version="1.0.0",
            description="在项目内样例文档中做关键词检索，返回按相关度排序的命中列表。",
            args_model=SearchDocumentsArgs,
            implementation=document_tools.search_documents,
            writes_database=False,
            uses_llm=False,
            tags=("retrieval", "docs"),
        ),
        ToolSpec(
            name=TOOL_GET_DOCUMENT,
            version="1.0.0",
            description="按文档 ID 读取一篇完整样例文档。",
            args_model=GetDocumentArgs,
            implementation=document_tools.get_document,
            writes_database=False,
            uses_llm=False,
            tags=("retrieval", "docs"),
        ),
        ToolSpec(
            name=TOOL_CALCULATE_LATENCY_SUMMARY,
            version="1.0.0",
            description="按 run_id 确定性聚合延迟统计（含 nearest-rank 分位数）。",
            args_model=CalculateLatencySummaryArgs,
            implementation=analytics_tools.calculate_latency_summary,
            writes_database=False,
            uses_llm=False,
            tags=("analytics", "deterministic"),
        ),
        ToolSpec(
            name=TOOL_CALCULATE_COST_SUMMARY,
            version="1.0.0",
            description="按 run_id 确定性聚合 Token 与成本。",
            args_model=CalculateCostSummaryArgs,
            implementation=analytics_tools.calculate_cost_summary,
            writes_database=False,
            uses_llm=False,
            tags=("analytics", "deterministic"),
        ),
    )

    for spec in specs:
        if is_registered(spec.name):
            continue
        register(spec)


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------


def invoke(
    tool_name: str,
    raw_arguments: dict[str, Any],
    *,
    run_id: str,
    node_name: str,
    recorder: Any | None = None,
    max_retries: int | None = None,
) -> Any:
    """调用一个工具，含校验、计时、落库与重试。

    Args:
        tool_name: 注册表中的工具名。
        raw_arguments: **未校验**的原始参数。校验在本函数内完成。
        run_id: 所属 run，用于写 Trace。
        node_name: 调用方节点名。
        recorder: ``TraceRecorder``（或兼容对象）。为 None 时跳过落库，
            便于纯逻辑单测。
        max_retries: 重试上限；默认取配置 ``TOOL_MAX_RETRIES``。

    Returns:
        工具实现体的返回值。

    Raises:
        InvalidToolArgumentsError: 参数校验失败。**实现体未被调用。**
        ToolNotFoundError: 工具未注册。
        ToolExecutionError: 实现体抛异常且重试耗尽。
    """
    spec = get(tool_name)

    if max_retries is None:
        max_retries = _default_max_retries()

    # ---------------------------------------------------------- 1. 校验
    # 这一步必须在任何副作用之前。校验失败时立刻返回，
    # 因此实现体在结构上不可能收到非法参数。
    try:
        validated = spec.validate_arguments(raw_arguments)
    except ValidationError as exc:
        errors = [
            {
                "location": ".".join(str(part) for part in err.get("loc", ())),
                "type": err.get("type", "unknown"),
                "message": err.get("msg", ""),
            }
            for err in exc.errors()[:20]
        ]
        logger.warning(
            "tool_arguments_invalid",
            extra={"tool_name": tool_name, "error_count": len(errors), "run_id": run_id},
        )

        if recorder is not None:
            recorder.record_tool_call(
                run_id=run_id,
                node_name=node_name,
                tool_name=tool_name,
                tool_version=spec.version,
                arguments=_safe_arguments(raw_arguments),
                validated=False,
                validation_error=summarize(errors, 300),
                status="invalid_arguments",
                error_code="INVALID_ARGUMENT",
                duration_ms=0,
            )
        raise InvalidToolArgumentsError(tool_name, errors) from exc

    # ---------------------------------------------------------- 2. 执行（含重试）
    started = time.perf_counter()
    attempt = 0
    last_error: Exception | None = None

    while attempt <= max_retries:
        try:
            result = spec.implementation(validated)
        except Exception as exc:  # noqa: BLE001 —— 工具内部任何异常都需转为领域错误
            last_error = exc
            attempt += 1
            if attempt > max_retries:
                break
            logger.warning(
                "tool_call_retrying",
                extra={
                    "tool_name": tool_name,
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "error_type": type(exc).__name__,
                },
            )
            continue

        duration_ms = int((time.perf_counter() - started) * 1000)

        if recorder is not None:
            recorder.record_tool_call(
                run_id=run_id,
                node_name=node_name,
                tool_name=tool_name,
                tool_version=spec.version,
                arguments=_safe_arguments(raw_arguments),
                validated=True,
                status="ok",
                result_summary=summarize_from_value(result),
                result_count=_result_count(result),
                retry_count=attempt,
                duration_ms=duration_ms,
            )
        return result

    # ---------------------------------------------------------- 3. 重试耗尽
    duration_ms = int((time.perf_counter() - started) * 1000)
    error_code = _error_code_for(last_error)

    if recorder is not None:
        recorder.record_tool_call(
            run_id=run_id,
            node_name=node_name,
            tool_name=tool_name,
            tool_version=spec.version,
            arguments=_safe_arguments(raw_arguments),
            validated=True,
            status="error",
            error_code=error_code,
            result_summary=summarize_text(f"{type(last_error).__name__}: {last_error}"),
            retry_count=max_retries,
            duration_ms=duration_ms,
        )

    logger.warning(
        "tool_call_failed",
        extra={
            "tool_name": tool_name,
            "attempts": max_retries + 1,
            "error_type": type(last_error).__name__,
            "run_id": run_id,
        },
    )
    raise ToolExecutionError(
        f"工具 {tool_name} 执行失败（已重试 {max_retries} 次）。",
        details={"tool_name": tool_name, "error_type": type(last_error).__name__},
    ) from last_error


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _default_max_retries() -> int:
    """从配置读取重试上限。配置不可用时回退到 2。"""
    try:
        from app.core.config import get_settings

        return get_settings().tool_max_retries
    except Exception:  # noqa: BLE001 —— 配置读取失败不应阻断工具调用
        return 2


def _safe_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    """把原始参数转为可安全落库的字典。

    仓储层还会再过一次脱敏，这里是第一道：
    过滤掉非 JSON 可序列化的值，避免写入时抛错。
    """
    from app.core.redaction import redact_mapping

    serializable: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str | int | float | bool) or value is None:
            serializable[str(key)] = value
        elif isinstance(value, list | tuple):
            serializable[str(key)] = [item for item in value if isinstance(item, str | int | float | bool)]
        else:
            serializable[str(key)] = f"<{type(value).__name__}>"
    return redact_mapping(serializable)


def summarize_from_value(value: Any) -> str:
    """把工具返回值摘要成可落库的短字符串。"""
    if isinstance(value, list):
        return summarize(
            {"count": len(value), "items": [_brief(item) for item in value[:3]]},
            400,
        )
    return summarize(_brief(value), 400)


def summarize_text(text: str) -> str:
    """截断文本用于落库。"""
    return summarize(text, 400)


def _brief(value: Any) -> Any:
    """把对象压成简短的可序列化形式。"""
    if hasattr(value, "model_dump"):
        data = value.model_dump(mode="json")
        # 只保留少量关键字段，避免把整篇文档写进摘要
        keep = ("document_id", "title", "score", "run_id", "total_tokens", "total_duration_ms")
        return {key: data[key] for key in keep if key in data}
    if isinstance(value, str):
        return value[:200]
    return value


def _result_count(value: Any) -> int | None:
    """推断返回结果条数。"""
    if isinstance(value, list | tuple):
        return len(value)
    return None


def _error_code_for(error: Exception | None) -> str:
    """按异常类型决定错误码。

    ``KeyError`` 表示"查不到目标"，语义上更接近 INVALID_ARGUMENT
    （调用方给的 ID 不对），而不是执行故障。
    """
    if isinstance(error, KeyError):
        return "INVALID_ARGUMENT"
    if isinstance(error, SQLAlchemyError):
        return "TOOL_EXECUTION_FAILED"
    return "TOOL_EXECUTION_FAILED"


__all__ = [
    "TOOL_NAMES",
    "InvalidToolArgumentsError",
    "ToolNotFoundError",
    "clear",
    "get",
    "invoke",
    "is_registered",
    "register",
    "register_builtin_tools",
    "registered_names",
]
