"""ID 生成。

契约 DATA_MODEL §4：所有实体 ID 使用 ``<前缀> + ULID``。

为什么用 ULID 而不是 UUID4：
- ULID 前 48 位是毫秒时间戳，字典序即时间序，Trace 事件按 ID 排序天然得到时间顺序；
- 避免 UUID4 随机分布导致的 B-tree 索引页分裂；
- Crockford Base32 不含易混字符 I/L/O/U，便于人工转录。

本模块是纯函数模块，无 IO，可被任何层调用。
"""

from __future__ import annotations

import re
from typing import Final

from ulid import ULID

# 各实体前缀，与 DATA_MODEL §4 表格严格一致。
# contract.lock.json 的 id_prefixes 与这里必须保持同步（S8 会校验）。
PREFIX_RUN: Final = "run_"
PREFIX_EVENT: Final = "evt_"
PREFIX_TOOL_CALL: Final = "tc_"
PREFIX_MODEL_CALL: Final = "mc_"
PREFIX_AGENT_DEFINITION: Final = "agentdef_"
PREFIX_EVAL_CASE: Final = "case_"
PREFIX_EVAL_RUN: Final = "evalrun_"
PREFIX_QUALITY_GATE: Final = "gate_"
PREFIX_EVALUATION: Final = "eval_"

ALL_PREFIXES: Final[tuple[str, ...]] = (
    PREFIX_RUN,
    PREFIX_EVENT,
    PREFIX_TOOL_CALL,
    PREFIX_MODEL_CALL,
    PREFIX_AGENT_DEFINITION,
    PREFIX_EVAL_CASE,
    PREFIX_EVAL_RUN,
    PREFIX_QUALITY_GATE,
    PREFIX_EVALUATION,
)

# ULID 的规范文本长度（Crockford Base32, 128 bit → 26 字符）
_ULID_LENGTH: Final = 26
_ID_PATTERN: Final = re.compile(
    rf"^(?P<prefix>[a-z]+_)(?P<ulid>[0-9A-HJKMNP-TV-Z]{{{_ULID_LENGTH}}})$"
)


def new_ulid() -> str:
    """生成一个新的 ULID 字符串。"""
    return str(ULID())


def _make(prefix: str) -> str:
    """按前缀生成 ID。"""
    return f"{prefix}{new_ulid()}"


def new_run_id() -> str:
    return _make(PREFIX_RUN)


def new_event_id() -> str:
    return _make(PREFIX_EVENT)


def new_tool_call_id() -> str:
    return _make(PREFIX_TOOL_CALL)


def new_model_call_id() -> str:
    return _make(PREFIX_MODEL_CALL)


def new_agent_definition_id() -> str:
    return _make(PREFIX_AGENT_DEFINITION)


def new_eval_case_id() -> str:
    return _make(PREFIX_EVAL_CASE)


def new_eval_run_id() -> str:
    return _make(PREFIX_EVAL_RUN)


def new_quality_gate_id() -> str:
    return _make(PREFIX_QUALITY_GATE)


def new_evaluation_id() -> str:
    """一次评测批次的 ID（不落表，作为 eval_run 的分组键）。"""
    return _make(PREFIX_EVALUATION)


def has_expected_prefix(value: str, prefix: str) -> bool:
    """校验 ID 是否以期望前缀开头，且整体格式合法。

    用于 API 入参快速拒绝明显非法的 ID（避免无意义地查库）。
    """
    match = _ID_PATTERN.match(value)
    if match is None:
        return False
    return match.group("prefix") == prefix


def split_id(value: str) -> tuple[str, str] | None:
    """拆分 ID 为 (前缀, ULID 部分)；格式非法时返回 None。"""
    match = _ID_PATTERN.match(value)
    if match is None:
        return None
    return match.group("prefix"), match.group("ulid")


__all__ = [
    "ALL_PREFIXES",
    "PREFIX_AGENT_DEFINITION",
    "PREFIX_EVAL_CASE",
    "PREFIX_EVAL_RUN",
    "PREFIX_EVALUATION",
    "PREFIX_EVENT",
    "PREFIX_MODEL_CALL",
    "PREFIX_QUALITY_GATE",
    "PREFIX_RUN",
    "PREFIX_TOOL_CALL",
    "has_expected_prefix",
    "new_agent_definition_id",
    "new_eval_case_id",
    "new_eval_run_id",
    "new_evaluation_id",
    "new_event_id",
    "new_model_call_id",
    "new_quality_gate_id",
    "new_run_id",
    "new_tool_call_id",
    "new_ulid",
    "split_id",
]
