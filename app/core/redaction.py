"""脱敏与摘要工具。

契约 T10 / TRACE_SCHEMA §7：
1. 任何写入日志或数据库的输入输出，都必须先经过本模块；
2. 六类敏感模式必须被替换为 ``[REDACTED_*]``；
3. 超长内容必须截断，且截断标记中保留原始长度，便于判断"是否被截断"。

本模块是纯函数集合，无副作用、无 IO，因此可以被任何层安全调用。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

# ---------------------------------------------------------------------------
# 脱敏模式（顺序有意义：先替换更具体的模式，再替换更宽泛的模式）
# ---------------------------------------------------------------------------

_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "openai_key",
        re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
        "[REDACTED_API_KEY]",
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
        "[REDACTED_BEARER]",
    ),
    (
        # 赋值/JSON 键值形式的敏感字段。
        #
        # 关键设计点（均来自实际调试发现的问题）：
        #
        # 1. **必须允许分隔符后有任意空白**。JSON 风格是 `"api_key": "value"`，
        #    若正则写成 `[:=]\s*` 之外的形态，带空格的写法完全匹配不到——这是最易漏的一类。
        #
        # 2. **替换题只替换值本身，不替换键与分隔符**。早期实现用 `\1=[REDACTED]`
        #    会把键名与分隔符一起重写，导致 `{"api_key": "abc"}` 变成
        #    `{api_key=[REDACTED]"}`——JSON 结构被破坏，后续解析直接失败。
        #    正确做法是把"值的部分"单独捕获，只替换这一段。
        #
        # 3. **值的字符类不能包含引号**，否则会跨字段贪婪匹配：
        #    `"api_key": "abc", "other": "def"` 中值会吞掉中间所有内容。
        #
        # 4. 用 `lookbehind` 排除 `key` 前紧跟字母数字的情况，避免误伤
        #    `monkey=xxx`、`turnkey=xxx` 之类单词。同时显式列出 `key` 本身，
        #    因为 `key=abcdef123456` 这种裸名写法在实践中很常见。
        #
        # 5. **必须原样保留分隔符**（`:` 或 `=`）。早期实现统一改写成 `=`，
        #    结果把 `{"api_key": "abc"}` 变成 `{"api_key"=[REDACTED]}`，
        #    JSON 直接无法解析。因此分隔符单独捕获并回填。
        "assignment_secret",
        re.compile(
            r"(?i)"
            r"(?P<keyquote>[\"']?)"
            r"(?P<key>(?<![A-Za-z0-9_-])(?:api[_-]?key|access[_-]?token|auth[_-]?token"
            r"|client[_-]?secret|private[_-]?key|key|token|secret|password|passwd|pwd))"
            r"(?P=keyquote)"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?P<valquote>[\"']?)"
            r"(?P<value>[^\s\"',;}\]]{4,})"
        ),
        # 只替换值：键名、引号、分隔符全部原样保留，确保 JSON/文本结构不被破坏
        r"\g<keyquote>\g<key>\g<keyquote>\g<sep>\g<valquote>[REDACTED]",
    ),
    (
        "url_credentials",
        re.compile(r"(?<=://)[^:/@\s]+:[^@/\s]+(?=@)"),
        "[REDACTED_CREDENTIALS]",
    ),
    (
        "cn_phone",
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        "[REDACTED_PHONE]",
    ),
    (
        "email",
        re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
        "[REDACTED_EMAIL]",
    ),
)

REDACTED_PLACEHOLDERS = tuple(repl for _name, _pattern, repl in _PATTERNS)

# 截断后缀中保留原长度，便于事后判断数据是否完整
_TRUNCATION_TEMPLATE = "...[truncated:{original_length}]"


def redact(text: str) -> str:
    """对文本应用全部脱敏模式。

    Args:
        text: 原始文本。

    Returns:
        脱敏后的文本。若未命中任何模式，返回原文本。
    """
    if not text:
        return text
    result = text
    for _name, pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """递归脱敏映射中的键与值。

    处理三类风险：
    1. 键名本身含敏感词（如 ``api_key``）→ 值替换为占位符；
    2. 值中内嵌的密钥字符串 → 正则替换；
    3. 嵌套结构 → 递归。

    第 1 点是必要的：因为 ``{"api_key": "abc123"}`` 中的值 ``abc123``
    可能短于正则要求的长度、或形态完全不像密钥（比如一个普通单词），
    仅靠模式匹配会漏掉。此时"键名敏感 ⇒ 值一律视为敏感"是更安全的选择。
    """
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        safe_key = redact(str(key))
        if _is_sensitive_key(str(key)):
            # 键名敏感时，值整体替换，不做任何展示
            cleaned[safe_key] = _placeholder_for(str(key))
            continue
        cleaned[safe_key] = _redact_value(value)
    return cleaned


# 敏感键名的判定：键名中**包含**任一敏感词即判定为敏感。
# 用包含而非相等，是为了覆盖 `db_password`、`openai_api_key`、`authToken` 等变体。
_SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "apikey",
    "access_token",
    "accesstoken",
    "auth_token",
    "authtoken",
    "client_secret",
    "clientsecret",
    "private_key",
    "privatekey",
    "secret",
    "password",
    "passwd",
    "pwd",
    "token",
    # 裸键名 "key" 需单独判断（不能用子串匹配，否则 monkey/turnkey 会误伤）
    "api_key",
)

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(" + "|".join(re.escape(token) for token in _SENSITIVE_KEY_TOKENS) + r")"
)
# 去掉分隔符后再判断，覆盖 api-key / apiKey / api_key 三种写法
_SENSITIVE_KEY_NORMALIZED = re.compile(r"[^a-z0-9]")


def _is_sensitive_key(key: str) -> bool:
    """判断键名是否表示敏感信息。

    判定顺序：
    1. 去掉分隔符后做子串匹配，覆盖 apiKey / api-key / api_key / dbPassword 等；
    2. 键名恰好是裸 ``key`` 时也判为敏感。
    """
    if not key:
        return False

    lowered = key.lower()
    normalized = _SENSITIVE_KEY_NORMALIZED.sub("", lowered)

    if _SENSITIVE_KEY_PATTERN.search(lowered):
        return True
    if normalized == "key":
        return True
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def _placeholder_for(key: str) -> str:
    """按键名返回合适的占位符，保留语义以便调试。"""
    lowered = key.lower()
    if "key" in lowered:
        return "[REDACTED_API_KEY]"
    if "token" in lowered:
        return "[REDACTED_TOKEN]"
    if "password" in lowered or "passwd" in lowered or "pwd" in lowered:
        return "[REDACTED_PASSWORD]"
    return "[REDACTED_SECRET]"


def _redact_value(value: Any) -> Any:
    """递归脱敏任意值。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, bool | int | float) or value is None:
        return value
    # 其他类型降级为字符串后脱敏，避免意外泄露自定义对象的 repr
    return redact(repr(value))


def truncate(text: str, max_chars: int) -> str:
    """把文本截断到 ``max_chars`` 以内，并在末尾保留原始长度。

    Args:
        text: 原始文本。
        max_chars: 允许的最大长度（含截断后缀）。

    Returns:
        长度不超过 ``max_chars`` 的文本。

    Raises:
        ValueError: ``max_chars`` 小于截断后缀的最小长度时。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正整数")
    if len(text) <= max_chars:
        return text

    suffix = _TRUNCATION_TEMPLATE.format(original_length=len(text))
    # 后缀本身长于预算时，退化为硬截断，避免返回超长字符串或抛错
    if len(suffix) >= max_chars:
        return text[:max_chars]

    return text[: max_chars - len(suffix)] + suffix


def summarize(value: Any, max_chars: int = 500) -> str:
    """把任意值转为紧凑的、已脱敏、已截断的摘要字符串。

    这是 Trace 与日志写入前**唯一**应该调用的入口（TRACE_SCHEMA §7.1）。

    Args:
        value: 任意可序列化值；字符串按原样处理。
        max_chars: 摘要最大长度。

    Returns:
        紧凑 JSON 字符串（字典/列表）或原始字符串（str），已脱敏并截断。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value
    elif isinstance(value, bytes):
        raw = f"<{len(value)} bytes>"
    else:
        try:
            raw = json.dumps(
                redact_mapping(value) if isinstance(value, Mapping) else _redact_value(value),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            # 无法序列化时退化为 repr，仍然过脱敏
            raw = redact(repr(value))
    return truncate(redact(raw), max_chars)


def count_chars(value: Any) -> int:
    """统计值的字符规模，用于日志中替代完整内容输出。"""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes | str):
        return sum(count_chars(item) for item in value)
    if isinstance(value, Mapping):
        return sum(count_chars(k) + count_chars(v) for k, v in value.items())
    return len(repr(value))


__all__ = [
    "REDACTED_PLACEHOLDERS",
    "count_chars",
    "redact",
    "redact_mapping",
    "summarize",
    "truncate",
]
