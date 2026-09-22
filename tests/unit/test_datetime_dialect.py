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
