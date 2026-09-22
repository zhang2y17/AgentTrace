"""ORM 模型与数据层测试。

覆盖 ACCEPTANCE_CHECKLIST：
- C-04：trace_event 的 12 个必需字段齐备
- C-05：8 张表齐备
- D-02/D-04：节点事件带耗时、sequence 唯一且递增
- D-05/D-06：工具调用与模型调用字段完整

标记：``unit``（SQLite 内存/临时文件，无外部服务，无网络）
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.ids import PREFIX_EVENT, PREFIX_RUN, split_id
from app.db.base import Base
from app.db.models import AgentDefinition, ModelCall, Run, ToolCall, TraceEvent

pytestmark = pytest.mark.unit

# DATA_MODEL §2.3 / TRACE_SCHEMA §3：trace_event 的 12 个必需字段
TRACE_EVENT_REQUIRED_FIELDS = {
    "event_id",
    "run_id",
    "parent_event_id",
    "event_type",
    "name",
    "status",
    "started_at",
    "ended_at",
    "duration_ms",
    "input_summary",
    "output_summary",
    "error_code",
}


class TestSchemaCompleteness:
    """契约 C-04 / C-05。"""

    def test_eight_tables_exist(self) -> None:
        import app.db.models  # noqa: F401

        expected = {
            "agent_definition",
            "run",
            "trace_event",
            "tool_call",
            "model_call",
            "eval_case",
            "eval_run",
            "quality_gate",
        }
        assert set(Base.metadata.tables) == expected

    def test_trace_event_has_all_required_fields(self) -> None:
        """trace_event 的 12 个必需字段必须齐备。"""
        columns = {c.name for c in TraceEvent.__table__.columns}
        missing = TRACE_EVENT_REQUIRED_FIELDS - columns
        assert not missing, f"trace_event 缺少必需字段: {sorted(missing)}"

    def test_run_has_replay_semantics_columns(self) -> None:
        """回放语义依赖 source_run_id（契约 G6/E-02）。"""
        columns = {c.name for c in Run.__table__.columns}
        assert "source_run_id" in columns

    def test_test_double_marking_columns(self) -> None:
        """契约 T13：替身标注必须落库，才能事后区分。"""
        assert "is_test_double" in {c.name for c in Run.__table__.columns}
        assert "is_test_double" in {c.name for c in ModelCall.__table__.columns}

    def test_tool_call_has_validation_columns(self) -> None:
        """契约 §3.3：参数校验结果必须可查。"""
        columns = {c.name for c in ToolCall.__table__.columns}
        assert {"validated", "validation_error", "arguments", "retry_count"} <= columns

    def test_money_columns_use_numeric(self) -> None:
        """金额必须用 Numeric，不能用 Float（避免累加误差）。"""
        from sqlalchemy import Numeric

        for model in (Run, ModelCall):
            col = model.__table__.columns["estimated_cost_usd"]
            assert isinstance(col.type, Numeric), f"{model.__name__} 的金额列不是 Numeric"

    def test_trace_event_unique_constraint_on_run_sequence(self) -> None:
        """TRACE_SCHEMA §6：UNIQUE(run_id, sequence)。"""
        constraint_names = {c.name for c in TraceEvent.__table__.constraints if c.name}
        assert "uq_trace_event_run_sequence" in constraint_names


class TestRunPersistence:
    """run 的写入与读取。"""

    def test_create_and_get_run(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        run = repository.create_run(
            question="AgentTrace 如何记录工具调用？",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
            model_name="fake-model",
            is_test_double=True,
        )
        db_session.commit()

        assert run.id.startswith(PREFIX_RUN)
        assert run.status == "running"
        assert run.is_test_double is True

        fetched = repository.get_run(run.id)
        assert fetched is not None
        assert fetched.question == "AgentTrace 如何记录工具调用？"
        assert fetched.id == run.id

    def test_finalize_run_sets_terminal_state_and_duration(
        self, repository, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        run = repository.create_run(
            question="test",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        repository.finalize_run(
            run.id,
            status="succeeded",
            result_summary={"answer": "答案", "citations": ["doc-a"]},
            total_tokens=340,
            estimated_cost_usd=Decimal("0.000187"),
        )
        db_session.commit()

        assert run.status == "succeeded"
        assert run.ended_at is not None
        assert run.total_duration_ms is not None
        assert run.total_duration_ms >= 0
        assert run.total_tokens == 340
        assert run.estimated_cost_usd == Decimal("0.000187")

    def test_finalize_nonexistent_run_raises(self, repository) -> None:  # type: ignore[no-untyped-def]
        """契约：写入失败不得静默吞掉。"""
        from app.core.errors import TracePersistenceError

        with pytest.raises(TracePersistenceError):
            repository.finalize_run("run_doesnotexist", status="failed")

    def test_get_nonexistent_run_returns_none(self, repository) -> None:  # type: ignore[no-untyped-def]
        assert repository.get_run("run_missing") is None


class TestTraceEventPersistence:
    """Trace 事件写入。"""

    def _make_run(self, repository, db_session):  # type: ignore[no-untyped-def]
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()
        return run

    def test_sequence_starts_at_one_and_increments(
        self, repository, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        """D-04：sequence 严格递增、无重复。"""
        run = self._make_run(repository, db_session)

        sequences = []
        for index in range(5):
            event = repository.append_event(
                run_id=run.id,
                event_type="node",
                name=f"node_{index}",
                status="ok",
            )
            sequences.append(event.sequence)
        db_session.commit()

        assert sequences == [1, 2, 3, 4, 5]

    def test_append_event_generates_prefixed_event_id(
        self, repository, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        run = self._make_run(repository, db_session)
        event = repository.append_event(
            run_id=run.id, event_type="run", name="run", status="running"
        )
        db_session.commit()

        assert event.event_id.startswith(PREFIX_EVENT)
        prefix, ulid_part = split_id(event.event_id)  # type: ignore[misc]
        assert prefix == PREFIX_EVENT
        assert len(ulid_part) == 26

    def test_parent_child_relationship_persists(
        self, repository, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        """D-03：父子关系可正确落库与读回。"""
        run = self._make_run(repository, db_session)

        parent = repository.append_event(
            run_id=run.id, event_type="run", name="run", status="ok"
        )
        child = repository.append_event(
            run_id=run.id,
            event_type="node",
            name="question_parser",
            status="ok",
            parent_event_id=parent.event_id,
        )
        db_session.commit()

        assert child.parent_event_id == parent.event_id
        assert child.run_id == parent.run_id

    def test_close_event_sets_duration_and_status(
        self, repository, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        """D-02：节点事件必须带耗时。"""
        run = self._make_run(repository, db_session)
        event = repository.append_event(
            run_id=run.id, event_type="node", name="question_parser", status="running"
        )
        db_session.commit()

        assert event.ended_at is None
        assert event.duration_ms is None

        repository.close_event(
            event.event_id, status="ok", output_summary={"intent": "explain"}
        )
        db_session.commit()

        assert event.ended_at is not None
        assert event.duration_ms is not None
        assert event.duration_ms >= 0
        assert event.status == "ok"
        assert "explain" in (event.output_summary or "")

    def test_input_output_summaries_are_truncated(
        self, repository, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        """G-05：摘要必须被截断，不能把完整内容写进库。"""
        run = self._make_run(repository, db_session)
        event = repository.append_event(
            run_id=run.id,
            event_type="node",
            name="big_node",
            status="ok",
            input_summary="x" * 5000,
        )
        db_session.commit()

        assert event.input_summary is not None
        assert len(event.input_summary) <= 500
        assert "truncated:5000" in event.input_summary

    def test_summaries_are_redacted(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """G-06：写入库的摘要必须已脱敏。"""
        run = self._make_run(repository, db_session)
        event = repository.append_event(
            run_id=run.id,
            event_type="node",
            name="leaky_node",
            status="ok",
            input_summary={"api_key": "abcdef123456", "safe": "keep"},
        )
        db_session.commit()

        assert "abcdef123456" not in (event.input_summary or "")
        assert "keep" in (event.input_summary or "")

    def test_list_events_ordered_by_sequence(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        run = self._make_run(repository, db_session)
        for index in range(4):
            repository.append_event(
                run_id=run.id, event_type="node", name=f"n{index}", status="ok"
            )
        db_session.commit()

        events, total = repository.list_events(run.id)

        assert total == 4
        assert [e.sequence for e in events] == [1, 2, 3, 4]

    def test_list_events_filter_by_type(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """D-07：按事件类型过滤。"""
        run = self._make_run(repository, db_session)
        repository.append_event(run_id=run.id, event_type="node", name="a", status="ok")
        repository.append_event(run_id=run.id, event_type="tool_call", name="b", status="ok")
        repository.append_event(run_id=run.id, event_type="node", name="c", status="ok")
        db_session.commit()

        events, total = repository.list_events(run.id, event_types=["node"])

        assert total == 2
        assert all(e.event_type == "node" for e in events)

    def test_list_events_filter_by_status(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """D-08：按状态过滤。"""
        run = self._make_run(repository, db_session)
        repository.append_event(run_id=run.id, event_type="node", name="a", status="ok")
        repository.append_event(run_id=run.id, event_type="node", name="b", status="failed")
        repository.append_event(run_id=run.id, event_type="node", name="c", status="retried")
        db_session.commit()

        events, total = repository.list_events(run.id, statuses=["failed", "retried"])

        assert total == 2
        assert {e.status for e in events} == {"failed", "retried"}

    def test_list_events_pagination(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        run = self._make_run(repository, db_session)
        for index in range(10):
            repository.append_event(
                run_id=run.id, event_type="node", name=f"n{index}", status="ok"
            )
        db_session.commit()

        page, total = repository.list_events(run.id, limit=3, offset=2)

        assert total == 10
        assert len(page) == 3
        assert [e.sequence for e in page] == [3, 4, 5]


class TestToolCallPersistence:
    """工具调用记录（D-05）。"""

    def test_record_tool_call_with_all_fields(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        call = repository.record_tool_call(
            run_id=run.id,
            node_name="document_search",
            tool_name="search_documents",
            arguments={"query": "trace", "top_k": 3},
            validated=True,
            status="ok",
            result_summary="命中 3 篇文档",
            result_count=3,
            duration_ms=43,
        )
        db_session.commit()

        assert call.tool_name == "search_documents"
        assert call.validated is True
        assert call.result_count == 3
        assert call.duration_ms == 43
        assert call.arguments == {"query": "trace", "top_k": 3}

    def test_invalid_arguments_recorded_with_flag(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """D-10：参数非法时 validated=False 且状态为 invalid_arguments。"""
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        call = repository.record_tool_call(
            run_id=run.id,
            node_name="document_search",
            tool_name="search_documents",
            arguments={"query": "trace", "top_k": 999},
            validated=False,
            validation_error="top_k 必须 <= 10",
            status="invalid_arguments",
        )
        db_session.commit()

        assert call.validated is False
        assert call.status == "invalid_arguments"
        assert call.validation_error is not None

    def test_tool_call_arguments_are_redacted(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        call = repository.record_tool_call(
            run_id=run.id,
            node_name="n",
            tool_name="t",
            arguments={"api_key": "abcdef123456", "query": "keep-me"},
            status="ok",
        )
        db_session.commit()

        assert "abcdef123456" not in str(call.arguments)
        assert call.arguments is not None
        assert call.arguments["query"] == "keep-me"

    def test_list_tool_calls_ordered_by_started_at(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """M3 依赖：工具序列必须按时间顺序推导。"""
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        for name in ("search_documents", "get_document", "calculate_cost_summary"):
            repository.record_tool_call(
                run_id=run.id, node_name="n", tool_name=name, status="ok"
            )
        db_session.commit()

        calls = repository.list_tool_calls(run.id)

        assert [c.tool_name for c in calls] == [
            "search_documents",
            "get_document",
            "calculate_cost_summary",
        ]


class TestModelCallPersistence:
    """模型调用记录（D-06）。"""

    def test_record_model_call_with_tokens_and_cost(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
            is_test_double=True,
        )
        db_session.commit()

        call = repository.record_model_call(
            run_id=run.id,
            node_name="question_parser",
            provider="fake",
            model_name="fake-model",
            is_test_double=True,
            prompt_tokens=78,
            completion_tokens=42,
            total_tokens=120,
            estimated_cost_usd=Decimal("0"),
            status="ok",
            latency_ms=75,
        )
        db_session.commit()

        assert call.prompt_tokens == 78
        assert call.completion_tokens == 42
        assert call.total_tokens == 120
        assert call.is_test_double is True
        assert call.latency_ms == 75

    def test_aggregate_token_and_cost(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """确定性聚合（契约 §3.3：不允许 LLM 估算）。"""
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        for tokens, cost in ((120, "0.000060"), (220, "0.000127")):
            repository.record_model_call(
                run_id=run.id,
                node_name="n",
                provider="fake",
                model_name="fake-model",
                is_test_double=True,
                total_tokens=tokens,
                estimated_cost_usd=Decimal(cost),
                status="ok",
            )
        db_session.commit()

        total_tokens, total_cost, unavailable = repository.aggregate_token_and_cost(run.id)

        assert total_tokens == 340
        assert total_cost == Decimal("0.000187")
        assert unavailable is False

    def test_aggregate_flags_unavailable_cost(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """契约 M8：未知模型成本不可用时必须打标，不能静默记 0。"""
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="openai",
        )
        db_session.commit()

        repository.record_model_call(
            run_id=run.id,
            node_name="n",
            provider="openai",
            model_name="unknown-model-xyz",
            is_test_double=False,
            total_tokens=100,
            estimated_cost_usd=Decimal("0"),
            cost_estimation_unavailable=True,
            status="ok",
        )
        db_session.commit()

        _, _, unavailable = repository.aggregate_token_and_cost(run.id)

        assert unavailable is True

    def test_aggregate_empty_run_returns_zeros(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """无模型调用时聚合应返回 0 而非报错。"""
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        total_tokens, total_cost, unavailable = repository.aggregate_token_and_cost(run.id)

        assert total_tokens == 0
        assert total_cost == Decimal("0")
        assert unavailable is False


class TestCascadeBehavior:
    """级联删除（DATA_MODEL §3）。"""

    def test_delete_run_cascades_to_events_and_calls(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """SQLite 下必须显式打开外键才能让 CASCADE 生效（session.py 已处理）。"""
        run = repository.create_run(
            question="q",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        repository.append_event(run_id=run.id, event_type="node", name="n", status="ok")
        repository.record_tool_call(
            run_id=run.id, node_name="n", tool_name="t", status="ok"
        )
        repository.record_model_call(
            run_id=run.id,
            node_name="n",
            provider="fake",
            model_name="m",
            is_test_double=True,
            status="ok",
        )
        db_session.commit()

        db_session.delete(run)
        db_session.commit()

        assert db_session.execute(select(TraceEvent)).scalars().all() == []
        assert db_session.execute(select(ToolCall)).scalars().all() == []
        assert db_session.execute(select(ModelCall)).scalars().all() == []

    def test_replay_does_not_delete_original(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """E-03：回放保留 source_run_id，且删除回放不影响原始 run。"""
        original = repository.create_run(
            question="原问题",
            status="succeeded",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        db_session.commit()

        replay = repository.create_run(
            question="原问题",
            status="succeeded",
            agent_version="v1",
            prompt_version="prompt-v2",
            llm_provider="fake",
            source_run_id=original.id,
        )
        db_session.commit()

        assert replay.source_run_id == original.id
        assert replay.id != original.id

        db_session.delete(replay)
        db_session.commit()

        # 原始 run 必须仍然存在
        survivor = repository.get_run(original.id)
        assert survivor is not None
        assert survivor.prompt_version == "prompt-v1"

    def test_agent_definition_uniqueness_constraint(self, repository, db_session) -> None:  # type: ignore[no-untyped-def]
        """三元组 (name, agent_version, prompt_version) 唯一。"""
        from sqlalchemy.exc import IntegrityError

        from app.core.ids import new_agent_definition_id

        for _ in range(1):
            db_session.add(
                AgentDefinition(
                    id=new_agent_definition_id(),
                    name="doc-research",
                    agent_version="v1",
                    prompt_version="prompt-v1",
                    graph_definition={"nodes": []},
                )
            )
        db_session.commit()

        db_session.add(
            AgentDefinition(
                id=new_agent_definition_id(),
                name="doc-research",
                agent_version="v1",
                prompt_version="prompt-v1",
                graph_definition={"nodes": []},
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()
