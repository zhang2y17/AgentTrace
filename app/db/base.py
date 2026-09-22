"""SQLAlchemy 声明式基类与通用列类型。

跨方言考虑（ARCHITECTURE / IMPLEMENTATION_PLAN S3 风险）：
1. 统一使用 ``sqlalchemy.JSON`` 而非 ``JSONB``——SQLite 不支持 JSONB，
   而 SQLite 是默认测试方言；
2. 时间统一为 ``TIMESTAMP(timezone=True)``，值一律为 aware UTC datetime；
3. 金额统一为 ``Numeric(12, 6)``，Python 侧用 ``Decimal`` 比较，
   避免 float 精度问题（SQLite 会以 numeric 亲和性存储）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import DeclarativeBase, mapped_column

# ---------------------------------------------------------------------------
# 通用类型别名
# ---------------------------------------------------------------------------

# 主键 / 外键：VARCHAR(36)。最长 ID 形如 "agentdef_" + 26 = 35 字符，留 1 位余量。
ID_LENGTH = 36
# 名称类字段
NAME_LENGTH = 120
# 版本类字段
VERSION_LENGTH = 40
# 状态 / 枚举类字段
STATUS_LENGTH = 20
# 错误码
ERROR_CODE_LENGTH = 60
# provider 名
PROVIDER_LENGTH = 20
# 模型名
MODEL_NAME_LENGTH = 80
# 工具名
TOOL_NAME_LENGTH = 80

# 金额精度：6 位小数足以表达单次调用级别的成本（例如 0.000187 USD）
MONEY_PRECISION = 12
MONEY_SCALE = 6


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。

    提供统一的时间戳与 ID 列类型，保证跨模型一致性。
    """

    __abstract__ = True


def utcnow() -> datetime:
    """返回当前 UTC 时间（aware）。

    统一入口，避免各处使用 ``datetime.utcnow()``（它返回 naive datetime，
    与 ``TIMESTAMP(timezone=True)`` 混用会产生时区歧义）。
    """
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """把可能丢失时区信息的 datetime 归一为 aware UTC。

    **为什么必须有这个函数**：SQLAlchemy 的 ``TIMESTAMP(timezone=True)``
    在 PostgreSQL 上会原样保留时区，但 **SQLite 根本没有时区类型** ——
    写进去的 aware datetime 读出来是 naive 的。

    于是"写入时 aware、读回时 naive"这个差异会在做时间差计算时炸掉::

        finished - event.started_at
        TypeError: can't subtract offset-naive and offset-aware datetimes

    这个 bug 只在 SQLite 上暴露、在 PostgreSQL 上不暴露，
    属于典型的"本地测试全绿、换方言就崩"的跨方言陷阱 ——
    而且它命中的是**每一条事件的耗时计算**，影响面是全部 Trace。

    处理策略：naive 值一律**假定为 UTC**（因为我们写入时就是 UTC），
    然后补上 tzinfo。这不是猜测：写入路径唯一，语义是确定的。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def elapsed_ms(started_at: datetime, ended_at: datetime) -> int:
    """计算两个时间点之间的毫秒差，跨方言安全。

    Args:
        started_at: 起始时间（naive 会被视为 UTC）。
        ended_at: 结束时间（naive 会被视为 UTC）。

    Returns:
        毫秒数，负值被夹到 0。

    负值夹到 0 而不是报错：时钟回拨（NTP 校正、容器迁移）会真实发生，
    一个负的耗时会让下游的百分位统计失去意义。记录 0 是更诚实的降级。
    """
    delta = ensure_aware(ended_at) - ensure_aware(started_at)
    return max(0, int(delta.total_seconds() * 1000))


def id_column(*, primary_key: bool = False, foreign_key: str | None = None):  # type: ignore[no-untyped-def]
    """构造 ID 列。

    Args:
        primary_key: 是否为主键。
        foreign_key: 若给定，作为外键目标（如 ``"run.id"``）。
    """
    kwargs: dict[str, object] = {}
    if foreign_key is not None:
        kwargs["ForeignKey"] = foreign_key  # type: ignore[assignment]

    return mapped_column(
        String(ID_LENGTH),
        **({"primary_key": True} if primary_key else {}),
        **({"nullable": False} if not primary_key else {}),
        **kwargs,  # type: ignore[arg-type]
    )


def money_column(default: str = "0"):  # type: ignore[no-untyped-def]
    """构造金额列。

    使用 ``Numeric`` 并在 Python 侧以 ``Decimal`` 处理，
    避免 float 累加误差影响成本统计的可复现性。
    """
    return mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        default=Decimal(default),
        server_default=default,
    )


def created_at_column():  # type: ignore[no-untyped-def]
    """构造 ``created_at`` 列：非空、索引、默认当前 UTC 时间。"""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )


__all__ = [
    "ERROR_CODE_LENGTH",
    "ID_LENGTH",
    "MODEL_NAME_LENGTH",
    "MONEY_PRECISION",
    "MONEY_SCALE",
    "NAME_LENGTH",
    "PROVIDER_LENGTH",
    "STATUS_LENGTH",
    "TOOL_NAME_LENGTH",
    "VERSION_LENGTH",
    "Base",
    "created_at_column",
    "id_column",
    "money_column",
    "utcnow",
]
