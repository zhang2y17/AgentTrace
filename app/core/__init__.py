"""核心横切能力：配置、日志、错误、ID、脱敏。

本包不依赖任何其他内部模块（ARCHITECTURE §2 分层规则 L3）。
"""

from app.core.config import Settings, get_settings
from app.core.errors import AgentTraceError
from app.core.ids import new_event_id, new_run_id
from app.core.logging import configure_logging, get_logger

__all__ = [
    "AgentTraceError",
    "Settings",
    "configure_logging",
    "get_logger",
    "get_settings",
    "new_event_id",
    "new_run_id",
]
