"""跨方言时间戳处理测试。

**这个文件的存在理由**：SQLAlchemy 的 ``TIMESTAMP(timezone=True)``
在 PostgreSQL 上保留时区，但 SQLite 根本没有时区类型 ——
写进去的 aware datetime 读出来是 naive 的。

于是"写入时 aware、读回时 naive"的差异会在做时间差计算时炸掉::

    TypeError: can't subtract offset-naive and offset-aware datetimes

这个 bug 只在 SQLite 上暴露，命中**每一条事件的耗时计算**与
**每一次 run 终结**，影响面是全部 Trace。因此需要专门的回归测试。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db.base import elapsed_ms, ensure_aware, utcnow

# 别名：下面的注解检查需要按对象身份比较 datetime 类型
_datetime = datetime


class TestEnsureAware:
    """``ensure_aware`` 的归一化语义。"""

    def test_aware_value_is_unchanged(self) -> None:
        value = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        assert ensure_aware(value) == value

    def test_naive_value_gains_utc(self) -> None:
        """naive 值补上 UTC —— 因为我们写入时就用的 UTC，语义是确定的。"""
        naive = datetime(2026, 9, 22, 12, 0, 0)
        result = ensure_aware(naive)
        assert result.tzinfo is not None
        assert result == naive.replace(tzinfo=UTC)

    def test_non_utc_aware_is_converted(self) -> None:
        """带其他时区的值会被转换到 UTC，而不是原样保留。"""
        tz_plus8 = datetime(2026, 9, 22, 20, 0, 0, tzinfo=__import__("datetime").timezone(timedelta(hours=8)))
        result = ensure_aware(tz_plus8)
        assert result.tzinfo == UTC
        assert result.hour == 12

    def test_utcnow_is_aware(self) -> None:
        assert utcnow().tzinfo is not None


class TestElapsedMs:
    """``elapsed_ms`` 的跨方言安全性。"""

    def test_both_aware(self) -> None:
        start = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        end = start + timedelta(milliseconds=250)
        assert elapsed_ms(start, end) == 250

    def test_both_naive(self) -> None:
        start = datetime(2026, 9, 22, 12, 0, 0)
        end = start + timedelta(milliseconds=250)
        assert elapsed_ms(start, end) == 250

    def test_mixed_naive_start_aware_end(self) -> None:
        """**回归测试**：SQLite 读回的 naive start + aware end。

        这正是修复前抛 ``TypeError`` 的组合。
        """
        start = datetime(2026, 9, 22, 12, 0, 0)  # 模拟从 SQLite 读回
        end = datetime(2026, 9, 22, 12, 0, 1, tzinfo=UTC)
        assert elapsed_ms(start, end) == 1000

    def test_mixed_aware_start_naive_end(self) -> None:
        """反向组合同样不能炸。"""
        start = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        end = datetime(2026, 9, 22, 12, 0, 1)
        assert elapsed_ms(start, end) == 1000

    def test_negative_delta_is_clamped_to_zero(self) -> None:
        """时钟回拨产生负耗时，夹到 0 而不是抛错。

        负的耗时会污染下游百分位统计；记录 0 是更诚实的降级。
        """
        start = datetime(2026, 9, 22, 12, 0, 5, tzinfo=UTC)
        end = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        assert elapsed_ms(start, end) == 0

    def test_sub_millisecond_rounds_down(self) -> None:
        start = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        end = start + timedelta(microseconds=999)
        assert elapsed_ms(start, end) == 0


class TestRepositoryDatetimeSafety:
    """仓储层在 SQLite 上的实际行为（而不是只测纯函数）。"""

    def _make_run(self, repository: Any) -> Any:
        return repository.create_run(
            question="跨方言时间测试",
            status="running",
            agent_version="1.0.0",
            prompt_version="1.0.0",
            llm_provider="fake",
            is_test_double=True,
        )

    def test_close_event_works_on_sqlite(self, repository: Any) -> None:
        """**核心回归**：在 SQLite 上关闭事件不得抛 TypeError。

        修复前这里抛 ``can't subtract offset-naive and offset-aware
        datetimes``，且因为每个节点都会产生事件，等于 Trace 记录全废。
        """
        run = self._make_run(repository)
        event = repository.append_event(
            run_id=run.id,
            event_type="node",
            name="question_parser",
            status="running",
        )
        event_id = event.event_id
        # 不显式传 ended_at，走 utcnow() 分支 —— 这正是触发路径
        repository.close_event(event_id, status="ok")

        events, _total = repository.list_events(run.id)
        closed = next(e for e in events if e.event_id == event_id)
        assert closed.status == "ok"
        assert closed.ended_at is not None
        assert closed.duration_ms is not None
        assert closed.duration_ms >= 0

    def test_finalize_run_works_on_sqlite(self, repository: Any) -> None:
        """run 终结同样要能在 SQLite 上算耗时。"""
        run = self._make_run(repository)
        repository.finalize_run(
            run.id,
            status="succeeded",
            result_summary={"status": "succeeded"},
        )
        refreshed = repository.get_run(run.id)
        assert refreshed is not None
        assert refreshed.ended_at is not None
        assert refreshed.total_duration_ms is not None
        assert refreshed.total_duration_ms >= 0

    def test_explicit_ended_at_is_respected(self, repository: Any) -> None:
        """显式传入 ended_at 时，duration 按传入值算（播种脚本依赖这点）。"""
        run = self._make_run(repository)
        start = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
        event = repository.append_event(
            run_id=run.id,
            event_type="node",
            name="x",
            status="running",
            started_at=start,
        )
        event_id = event.event_id
        repository.close_event(
            event_id,
            status="ok",
            ended_at=start + timedelta(milliseconds=123),
        )
        events, _total = repository.list_events(run.id)
        closed = next(e for e in events if e.event_id == event_id)
        assert closed.duration_ms == 123

    def test_timeline_stays_monotonic(self, repository: Any) -> None:
        """连续关闭多个事件后，耗时都应非负。"""
        run = self._make_run(repository)
        for index in range(5):
            event = repository.append_event(
                run_id=run.id,
                event_type="node",
                name=f"node_{index}",
                status="running",
            )
            repository.close_event(event.event_id, status="ok")

        events, _total = repository.list_events(run.id)
        assert len(events) == 5
        for event in events:
            assert event.duration_ms is not None
            assert event.duration_ms >= 0


@pytest.mark.parametrize(
    "naive_offset_ms",
    [0, 1, 500, 1000],
)
def test_close_event_various_durations(repository: Any, naive_offset_ms: int) -> None:
    """不同耗时量级下 duration 计算都要正确。"""
    run = repository.create_run(
        question="耗时参数化测试",
        status="running",
        agent_version="1.0.0",
        prompt_version="1.0.0",
        llm_provider="fake",
        is_test_double=True,
    )
    start = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    event = repository.append_event(
        run_id=run.id,
        event_type="node",
        name="x",
        status="running",
        started_at=start,
    )
    repository.close_event(
        event.event_id,
        status="ok",
        ended_at=start + timedelta(milliseconds=naive_offset_ms),
    )
    events, _total = repository.list_events(run.id)
    closed = next(e for e in events if e.event_id == event.event_id)
    assert closed.duration_ms == naive_offset_ms


class TestUtcTimestampSerialization:
    """契约 API_CONTRACT §0.2 的序列化格式：UTC、毫秒、``Z`` 后缀。

    这是"跨方言时间戳"问题的**出口端**：``ensure_aware`` 保证入库前
    时间带时区，``UtcTimestamp`` 保证出参时格式符合契约。两者缺一，
    同一个时间字段在 SQLite 与 PostgreSQL 下就会产出两种形状。

    契约示例是 ``2026-09-22T04:00:00.000Z`` —— 注意小数部分**始终存在**，
    哪怕微秒为 0。直接声明 ``datetime`` 会把它省略成 ``...T04:00:00Z``，
    客户端按定长解析时两者长度不同。
    """

    def test_zero_microseconds_still_emits_milliseconds(self) -> None:
        from pydantic import BaseModel

        from app.schemas.common import UtcTimestamp

        class _Model(BaseModel):
            t: UtcTimestamp

        value = datetime(2026, 9, 22, 4, 0, 0, tzinfo=UTC)
        assert _Model(t=value).model_dump_json() == '{"t":"2026-09-22T04:00:00.000Z"}'

    def test_nonzero_microseconds_are_truncated_to_milliseconds(self) -> None:
        from pydantic import BaseModel

        from app.schemas.common import UtcTimestamp

        class _Model(BaseModel):
            t: UtcTimestamp

        value = datetime(2026, 9, 22, 4, 0, 0, 412000, tzinfo=UTC)
        assert _Model(t=value).model_dump_json() == '{"t":"2026-09-22T04:00:00.412Z"}'

    def test_naive_value_is_treated_as_utc(self) -> None:
        """SQLite 读回的时间是 naive 的。把它当 UTC 而不是拒绝 ——
        拒绝会让整个 API 在 SQLite 下不可用。
        """
        from pydantic import BaseModel

        from app.schemas.common import UtcTimestamp

        class _Model(BaseModel):
            t: UtcTimestamp

        assert (
            _Model(t=datetime(2026, 9, 22, 4, 0, 0)).model_dump_json()
            == '{"t":"2026-09-22T04:00:00.000Z"}'
        )

    def test_non_utc_offset_is_converted(self) -> None:
        """带 +08:00 偏移的时间必须换算到 UTC —— 契约只声明 UTC。"""
        from datetime import timedelta, timezone

        from pydantic import BaseModel

        from app.schemas.common import UtcTimestamp

        class _Model(BaseModel):
            t: UtcTimestamp

        value = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        assert _Model(t=value).model_dump_json() == '{"t":"2026-09-22T04:00:00.000Z"}'

    def test_optional_none_is_preserved(self) -> None:
        from pydantic import BaseModel

        from app.schemas.common import UtcTimestamp

        class _Model(BaseModel):
            t: UtcTimestamp | None = None

        assert _Model().model_dump_json() == '{"t":null}'

    def test_all_response_timestamp_fields_use_the_alias(self) -> None:
        """防止有人日后新加一个时间字段时直接写 ``datetime``。

        直接声明 ``datetime`` 不会报错，只会静默产出不符契约的格式 ——
        正是那种"测试全绿但客户端解析失败"的问题。

        检查方式是看字段的**元数据**里有没有 ``PlainSerializer``，
        而不是去匹配类型字符串：``Annotated[datetime, ...]`` 的 repr
        本身就含 ``datetime`` 子串，按字符串匹配会把已正确标注的字段
        误判成"裸 datetime"，让这条测试永远无法通过。
        """
        from typing import get_args

        from pydantic import PlainSerializer

        from app.schemas.eval import EvaluationResponse, QualityGateResponse
        from app.schemas.health import HealthResponse
        from app.schemas.runs import RunDetail, TraceEventOut

        def _has_plain_serializer(annotation: object, metadata: object) -> bool:
            """判断字段是否挂了 ``PlainSerializer``。

            Pydantic 对这两种声明的处理不同，必须都覆盖：

            - ``t: UtcTimestamp`` → 元数据被**上提**到 ``field.metadata``，
              此时 ``field.annotation`` 是裸的 ``datetime``；
            - ``t: UtcTimestamp | None`` → 元数据**留在** ``Annotated`` 里，
              而 ``field.metadata`` 是空的。

            只看其中一个会让另一类字段被误判为"用了裸 datetime"。
            """
            if any(isinstance(item, PlainSerializer) for item in (metadata or ())):
                return True
            for item in getattr(annotation, "__metadata__", ()) or ():
                if isinstance(item, PlainSerializer):
                    return True
            # Optional[...] / Annotated 嵌套
            return any(
                _has_plain_serializer(arg, None) for arg in get_args(annotation)
            )

        def _is_annotated_datetime(annotation: object) -> bool:
            """递归判断注解里是否出现 ``datetime`` 类型。"""
            if annotation is _datetime:
                return True
            return any(_is_annotated_datetime(arg) for arg in get_args(annotation))

        offenders: list[str] = []
        checked = 0
        for model in (
            RunDetail,
            TraceEventOut,
            HealthResponse,
            EvaluationResponse,
            QualityGateResponse,
        ):
            for name, field in model.model_fields.items():
                annotation = field.annotation
                if not _is_annotated_datetime(annotation):
                    continue
                checked += 1
                if not _has_plain_serializer(annotation, field.metadata):
                    offenders.append(f"{model.__name__}.{name}: {annotation}")

        # 收集型断言最危险的失效方式是"收集范围为零"：
        # 模型改名之后它照样绿，但什么都没检查。
        assert checked >= 5, f"只匹配到 {checked} 个时间字段，检查范围可能已失效"
        assert offenders == [], (
            f"以下时间字段未使用 UtcTimestamp（缺 PlainSerializer）：{offenders}"
        )

    def test_at_least_one_field_was_actually_checked(self) -> None:
        """防止上面的检查因为"一个字段都没匹配到"而空过。

        这条"零收集"守卫已经内联在上面的 ``checked`` 断言里，
        这里额外用真实的序列化结果确认别名确实在生效 ——
        只看元数据仍可能漏掉"元数据在、但序列化器被覆盖"的情况。
        """
        from pydantic import BaseModel

        from app.schemas.common import UtcTimestamp

        class _Model(BaseModel):
            t: UtcTimestamp | None = None

        # 走真实的 model_dump_json，确认格式而不只是确认装饰器存在。
        assert (
            _Model(t=datetime(2026, 9, 22, 4, 0, 0, tzinfo=UTC)).model_dump_json()
            == '{"t":"2026-09-22T04:00:00.000Z"}'
        )
