"""结构化 JSON 日志。

契约 T10：日志必须是结构化 JSON，每行一个 JSON 对象，便于采集与检索。

契约 B8 / SECURITY：日志中**不得**出现 API Key、完整 Prompt、完整模型响应。
本模块在 ``_serialize`` 阶段统一过一遍脱敏函数，作为最后一道防线；
调用方仍应主动使用 ``app.core.redaction.summarize`` 只传摘要。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.redaction import redact, truncate

# 单条日志消息的字符上限。避免某次意外把完整 Prompt 或者大响应打进日志。
_MAX_LOG_MESSAGE_CHARS = 4000

# LogRecord 的内建属性，序列化时需排除，只保留 extra 中显式传入的字段
_RESERVED_RECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """把 LogRecord 格式化为单行 JSON。

    输出字段：
    - ``ts``: ISO 8601 UTC，毫秒精度
    - ``level``: 日志级别
    - ``logger``: logger 名称
    - ``message``: 日志正文（已脱敏并截断）
    - ``exception``: 异常信息（若存在，已脱敏）
    - 其余通过 ``extra=`` 传入的自定义字段会平铺到顶层
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
        }

        # 取格式化后的消息（已应用 %-style 参数），再统一脱敏截断
        message = record.getMessage()
        payload["message"] = truncate(redact(message), _MAX_LOG_MESSAGE_CHARS)

        # 平铺 extra 字段
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS or key.startswith("_"):
                continue
            payload[key] = _safe_extra_value(value)

        if record.exc_info:
            exc_type, exc_value, _tb = record.exc_info
            payload["exception"] = {
                "type": exc_type.__name__ if exc_type else "Unknown",
                # 只记录异常消息，不记录完整 traceback：traceback 可能包含局部变量值
                "message": truncate(redact(str(exc_value)), _MAX_LOG_MESSAGE_CHARS),
            }

        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_extra_value(value: Any) -> Any:
    """把 extra 字段值转成可 JSON 序列化且已脱敏的形式。"""
    if isinstance(value, str):
        return truncate(redact(value), _MAX_LOG_MESSAGE_CHARS)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _safe_extra_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_safe_extra_value(item) for item in value]
    return truncate(redact(repr(value)), _MAX_LOG_MESSAGE_CHARS)


def configure_logging(level: str = "INFO", *, force: bool = False) -> None:
    """配置根 logger 输出结构化 JSON 到 stdout。

    Args:
        level: 日志级别名。
        force: 为 True 时先移除已有 handler（测试中重复调用时需要）。
    """
    root = logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)

    root.setLevel(level.upper())

    # uvicorn 默认的 access log 是纯文本，会破坏"每行一个 JSON"的约定。
    # 关闭它，改由应用自身的中间件记录结构化访问日志。
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("uvicorn", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(noisy).propagate = True
        logging.getLogger(noisy).handlers = []


def get_logger(name: str) -> logging.Logger:
    """获取 logger。所有模块统一通过此函数获取，便于将来替换实现。"""
    return logging.getLogger(name)


__all__ = ["JsonFormatter", "configure_logging", "get_logger"]
