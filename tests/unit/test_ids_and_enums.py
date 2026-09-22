"""ID 生成与枚举语义的单元测试。

覆盖契约 DATA_MODEL §4（ID 前缀与 ULID 格式）
与 EVALUATION §3 的枚举语义（``degraded`` 不算成功、不计入错误率）。
"""

from __future__ import annotations

import pytest

from app.core.ids import (
    ALL_PREFIXES,
    PREFIX_AGENT_DEFINITION,
    PREFIX_EVAL_CASE,
    PREFIX_EVAL_RUN,
    PREFIX_EVALUATION,
    PREFIX_EVENT,
    PREFIX_MODEL_CALL,
    PREFIX_QUALITY_GATE,
    PREFIX_RUN,
    PREFIX_TOOL_CALL,
    has_expected_prefix,
    new_agent_definition_id,
    new_eval_case_id,
    new_eval_run_id,
    new_evaluation_id,
    new_event_id,
    new_model_call_id,
    new_quality_gate_id,
    new_run_id,
    new_tool_call_id,
    split_id,
)
from app.schemas.common import DataScope, EventStatus, EventType, RunStatus

pytestmark = pytest.mark.unit


class TestIdPrefixes:
    """ID 前缀必须与 DATA_MODEL §4 表格一致（S8 会用 contract.lock.json 校验）。"""

    @pytest.mark.parametrize(
        ("factory", "prefix"),
        [
            (new_run_id, PREFIX_RUN),
            (new_event_id, PREFIX_EVENT),
            (new_tool_call_id, PREFIX_TOOL_CALL),
            (new_model_call_id, PREFIX_MODEL_CALL),
            (new_agent_definition_id, PREFIX_AGENT_DEFINITION),
            (new_eval_case_id, PREFIX_EVAL_CASE),
            (new_eval_run_id, PREFIX_EVAL_RUN),
            (new_quality_gate_id, PREFIX_QUALITY_GATE),
            (new_evaluation_id, PREFIX_EVALUATION),
        ],
    )
    def test_factory_uses_expected_prefix(self, factory, prefix: str) -> None:
        generated = factory()
        assert generated.startswith(prefix)

    def test_all_prefixes_are_unique(self) -> None:
        assert len(set(ALL_PREFIXES)) == len(ALL_PREFIXES)


class TestIdFormat:
    """ULID 格式与唯一性。"""

    def test_length_is_prefix_plus_26(self) -> None:
        """ULID 规范文本长度是 26 字符。"""
        run_id = new_run_id()
        prefix, ulid_part = split_id(run_id)  # type: ignore[misc]
        assert prefix == PREFIX_RUN
        assert len(ulid_part) == 26

    def test_ulid_charset_is_crockford_base32(self) -> None:
        """Crockford Base32 不含 I/L/O/U，便于人工转录。"""
        ulid_part = new_run_id().removeprefix(PREFIX_RUN)
        assert not set(ulid_part) & set("ILOU")

    def test_ids_are_unique(self) -> None:
        ids = {new_run_id() for _ in range(200)}
        assert len(ids) == 200

    def test_ids_are_monotonic_lexicographically(self) -> None:
        """ULID 字典序即时间序——这是选择 ULID 而非 UUID4 的核心理由。

        注意：同一毫秒内生成的两个 ULID，其后 80 位是**随机**的，
        因此只有跨毫秒比较才能保证字典序单调。这一点必须如实反映在测试中，
        而不是假设"任何两次调用都单调"——那个假设是错的。
        """
        import time

        first = new_run_id()
        # 跨过毫秒边界，确保时间戳部分不同
        time.sleep(0.005)
        second = new_run_id()

        assert first < second
        # 前 10 个字符是时间戳部分（48 bit → 10 个 Base32 字符），必须递增
        assert first.removeprefix(PREFIX_RUN)[:10] < second.removeprefix(PREFIX_RUN)[:10]

    def test_same_millisecond_ids_are_unique_though_not_ordered(self) -> None:
        """同一毫秒内 ULID 不保证有序，但必须唯一。

        记录这个事实，避免将来有人误以为"ULID 永远有序"而写出脆弱代码。
        """
        batch = [new_run_id() for _ in range(50)]
        assert len(set(batch)) == 50

    def test_split_id_on_invalid_input(self) -> None:
        assert split_id("not-an-id") is None
        assert split_id("") is None
        assert split_id("run_short") is None

    def test_has_expected_prefix(self) -> None:
        run_id = new_run_id()
        assert has_expected_prefix(run_id, PREFIX_RUN) is True
        # 前缀不匹配必须为 False，避免把 tool_call id 当 run_id 使用
        assert has_expected_prefix(run_id, PREFIX_EVENT) is False
        assert has_expected_prefix("garbage", PREFIX_RUN) is False


class TestRunStatusSemantics:
    """EVALUATION §3 M1/M9 的枚举语义。"""

    def test_only_succeeded_counts_as_success(self) -> None:
        """契约：``degraded`` 不视为成功，这是刻意的严格定义。"""
        assert RunStatus.SUCCEEDED.counts_as_success is True
        for status in (
            RunStatus.DEGRADED,
            RunStatus.FAILED,
            RunStatus.TIMEOUT,
            RunStatus.RUNNING,
            RunStatus.PENDING,
        ):
            assert status.counts_as_success is False, status

    def test_degraded_not_counted_as_error(self) -> None:
        """降级有独立的 degraded_rate，不计入 error_rate。"""
        assert RunStatus.FAILED.counts_as_error is True
        assert RunStatus.TIMEOUT.counts_as_error is True
        assert RunStatus.DEGRADED.counts_as_error is False
        assert RunStatus.SUCCEEDED.counts_as_error is False

    def test_terminal_states(self) -> None:
        assert RunStatus.SUCCEEDED.is_terminal is True
        assert RunStatus.FAILED.is_terminal is True
        assert RunStatus.DEGRADED.is_terminal is True
        assert RunStatus.TIMEOUT.is_terminal is True
        assert RunStatus.RUNNING.is_terminal is False
        assert RunStatus.PENDING.is_terminal is False


class TestEventTypeAndStatus:
    """TRACE_SCHEMA §2/§4 固定取值。"""

    def test_six_event_types(self) -> None:
        assert {t.value for t in EventType} == {
            "run",
            "node",
            "tool_call",
            "model_call",
            "error",
            "final_result",
        }

    def test_eight_event_statuses(self) -> None:
        assert {s.value for s in EventStatus} == {
            "pending",
            "running",
            "ok",
            "failed",
            "skipped",
            "retried",
            "invalid_arguments",
            "handoff",
        }

    def test_running_and_pending_are_not_terminal(self) -> None:
        assert EventStatus.RUNNING.is_terminal is False
        assert EventStatus.PENDING.is_terminal is False
        for status in (
            EventStatus.OK,
            EventStatus.FAILED,
            EventStatus.SKIPPED,
            EventStatus.RETRIED,
            EventStatus.INVALID_ARGUMENTS,
            EventStatus.HANDOFF,
        ):
            assert status.is_terminal is True, status


class TestDataScope:
    """契约 B9：指标必须标注数据来源范围。"""

    def test_scope_values(self) -> None:
        assert {s.value for s in DataScope} == {
            "offline_evaluation",
            "sample_runs",
            "empty",
        }
