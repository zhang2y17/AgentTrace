"""领域异常与统一错误响应。

契约 API_CONTRACT §0.1：所有非 2xx 响应体必须是
``{"error": {"code", "message", "details", "request_id"}}``。

设计原则：
1. 领域异常携带 ``error_code`` 与 ``http_status``，由异常处理器统一映射；
2. 异常的 ``message`` 面向调用方，必须已脱敏（不允许把原始密钥回显）；
3. 未预期异常一律映射为 ``INTERNAL_ERROR``，**不回显**原始异常消息给调用方，
   只写日志，避免内部实现细节与敏感数据泄漏。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.redaction import redact, truncate

# 错误消息与 details 的长度上限，避免把超长输入回显给调用方
_MAX_ERROR_MESSAGE_CHARS = 500
_MAX_DETAIL_VALUE_CHARS = 300


class AgentTraceError(Exception):
    """AgentTrace 领域异常基类。"""

    error_code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.raw_message = message
        self.details = _sanitize_details(details or {})
        # 对外消息统一脱敏与截断
        self.message = truncate(redact(message), _MAX_ERROR_MESSAGE_CHARS)
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        """构造统一错误响应体。"""
        return {
            "error": {
                "code": self.error_code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id or generate_request_id(),
            }
        }


# ---------------------------------------------------------------------------
# 具体异常（error_code 与 API_CONTRACT §0.1 表格一一对应）
# ---------------------------------------------------------------------------


class InvalidArgumentError(AgentTraceError):
    """请求参数非法：Pydantic 校验失败、字段超长、枚举值不合法。"""

    error_code = "INVALID_ARGUMENT"
    http_status = 400


class AgentValidationError(AgentTraceError):
    """请求体结构合法但业务语义非法（如 question 全为空白）。"""

    error_code = "AGENT_VALIDATION_ERROR"
    http_status = 422


class RunNotFoundError(AgentTraceError):
    """run_id 不存在。"""

    error_code = "RUN_NOT_FOUND"
    http_status = 404


class EvaluationNotFoundError(AgentTraceError):
    """evaluation_id 不存在。"""

    error_code = "EVALUATION_NOT_FOUND"
    http_status = 404


class RunNotReplayableError(AgentTraceError):
    """run 仍在 running/pending 状态，无法回放。"""

    error_code = "RUN_NOT_REPLAYABLE"
    http_status = 409


class TracePersistenceError(AgentTraceError):
    """Trace 落库失败。

    契约 ARCHITECTURE §4：写入失败**不得静默吞掉**，
    必须抛出本异常，由上层把 run 标记为 failed 并返回 500。
    """

    error_code = "TRACE_WRITE_FAILED"
    http_status = 500


class ToolExecutionError(AgentTraceError):
    """工具执行失败。会被代理节点捕获并写入 Trace，通常不直接返回给调用方。"""

    error_code = "TOOL_EXECUTION_FAILED"
    http_status = 500


class LlmProviderError(AgentTraceError):
    """真实 LLM 提供方调用失败。"""

    error_code = "LLM_PROVIDER_ERROR"
    http_status = 502


class AgentTimeoutError(AgentTraceError):
    """Agent 运行超时。"""

    error_code = "AGENT_TIMEOUT"
    http_status = 504


class DatasetNotFoundError(AgentTraceError):
    """评测集文件不存在。"""

    error_code = "EVALUATION_NOT_FOUND"
    http_status = 404


class DatasetValidationError(AgentTraceError):
    """评测集内容不合法。details 中必须包含行号与字段。"""

    error_code = "INVALID_ARGUMENT"
    http_status = 400


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def generate_request_id() -> str:
    """生成请求 ID，用于把调用方看到的错误与日志关联起来。"""
    return f"req_{uuid.uuid4().hex[:16]}"


def _sanitize_details(details: dict[str, Any]) -> dict[str, Any]:
    """脱敏并截断 details，避免回显敏感或超长内容。

    关键点：**值不是字符串时同样需要按键名判定**。
    早期实现只对字符串值做脱敏，导致 ``{"api_key": 123456789}`` 这类
    数字型密钥被原样透出。因此这里统一走 ``redact_mapping``，
    由它按键名判定敏感度并递归处理任意类型。
    """
    from app.core.redaction import redact_mapping, truncate  # 局部导入避免循环引用

    # 先按键名与值做一次递归脱敏（对任意类型生效）
    sanitized = redact_mapping(details)

    # 再做长度截断，避免超大 details 撑爆响应
    def _trim(value: Any) -> Any:
        if isinstance(value, str):
            return truncate(value, _MAX_DETAIL_VALUE_CHARS)
        if isinstance(value, list):
            return [_trim(item) for item in value[:20]]
        if isinstance(value, dict):
            return {k: _trim(v) for k, v in list(value.items())[:20]}
        return value

    return {truncate(str(k), 80): _trim(v) for k, v in sanitized.items()}


__all__ = [
    "AgentTimeoutError",
    "AgentTraceError",
    "AgentValidationError",
    "DatasetNotFoundError",
    "DatasetValidationError",
    "EvaluationNotFoundError",
    "InvalidArgumentError",
    "LlmProviderError",
    "RunNotFoundError",
    "RunNotReplayableError",
    "ToolExecutionError",
    "TracePersistenceError",
    "generate_request_id",
]
