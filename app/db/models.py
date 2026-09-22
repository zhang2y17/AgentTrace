"""ORM 模型：8 张表（DATA_MODEL §2）。

字段定义严格对齐 ``docs/DATA_MODEL.md``。任何字段变更必须先改文档。

表清单：
1. ``agent_definition``  Agent 版本定义，用于评测分组
2. ``run``               一次 Agent 运行
3. ``trace_event``       Trace 事件主轴（12 个必需字段）
4. ``tool_call``         工具调用治理记录
5. ``model_call``        模型调用记录
6. ``eval_case``         评测用例
7. ``eval_run``          单个 case 的评测结果
8. ``quality_gate``      门禁检查记录

跨方言约束（见 base.py 的说明）：
- ``JSON`` 而非 ``JSONB``
- ``DateTime(timezone=True)`` 而非 naive datetime
- ``Numeric`` 而非 Float 存金额
- 枚举以 VARCHAR 存储，不用数据库原生 ENUM（便于 SQLite/PostgreSQL 迁移）
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON as JsonType
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    ERROR_CODE_LENGTH,
    ID_LENGTH,
    MODEL_NAME_LENGTH,
    MONEY_PRECISION,
    MONEY_SCALE,
    NAME_LENGTH,
    PROVIDER_LENGTH,
    STATUS_LENGTH,
    TOOL_NAME_LENGTH,
    VERSION_LENGTH,
    Base,
    created_at_column,
)

# ---------------------------------------------------------------------------
# 1. agent_definition
# ---------------------------------------------------------------------------


class AgentDefinition(Base):
    """Agent 版本的定义。

    ``graph_definition`` 保存节点列表与边，用于回溯"这次评测跑的是哪个工作流"。
    三元组 ``(name, agent_version, prompt_version)`` 唯一。
    """

    __tablename__ = "agent_definition"
    __table_args__ = (
        UniqueConstraint(
            "name", "agent_version", "prompt_version", name="uq_agent_definition_triple"
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_LENGTH), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)
    graph_definition: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    runs: Mapped[list[Run]] = relationship(back_populates="agent_definition")

    def __repr__(self) -> str:
        return (
            f"<AgentDefinition id={self.id!r} name={self.name!r} "
            f"agent_version={self.agent_version!r} prompt_version={self.prompt_version!r}>"
        )


# ---------------------------------------------------------------------------
# 2. run
# ---------------------------------------------------------------------------


class Run(Base):
    """一次 Agent 运行。

    ``source_run_id`` 是回放语义的核心：非空表示这是一次回放，
    值为被回放的原始 run。契约要求回放**绝不覆盖**原始记录，
    因此这里不做任何唯一性约束，每次回放产生新行。
    """

    __tablename__ = "run"
    __table_args__ = (
        Index("ix_run_status_started_at", "status", "started_at"),
        Index("ix_run_source_run_id", "source_run_id"),
        Index("ix_run_version_dimensions", "agent_version", "prompt_version", "model_name"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    agent_definition_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("agent_definition.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_run_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("run.id", ondelete="SET NULL"),
        nullable=True,
    )

    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)

    # 冗余存储版本维度：评测分组对比时无需 join agent_definition
    agent_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(MODEL_NAME_LENGTH), nullable=True)
    llm_provider: Mapped[str] = mapped_column(String(PROVIDER_LENGTH), nullable=False)
    is_test_double: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_LENGTH), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        default=Decimal("0"),
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    # 关系：删 run 时事件与调用记录级联删除（DATA_MODEL §3）
    agent_definition: Mapped[AgentDefinition | None] = relationship(back_populates="runs")
    source_run: Mapped[Run | None] = relationship(remote_side=[id], back_populates="replays")
    replays: Mapped[list[Run]] = relationship(back_populates="source_run")
    trace_events: Mapped[list[TraceEvent]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="TraceEvent.sequence",
    )
    tool_calls: Mapped[list[ToolCall]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    model_calls: Mapped[list[ModelCall]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    eval_runs: Mapped[list[EvalRun]] = relationship(back_populates="run")

    def __repr__(self) -> str:
        return (
            f"<Run id={self.id!r} status={self.status!r} "
            f"agent_version={self.agent_version!r} replay_of={self.source_run_id!r}>"
        )


# ---------------------------------------------------------------------------
# 3. trace_event
# ---------------------------------------------------------------------------


class TraceEvent(Base):
    """Trace 事件。

    契约 TRACE_SCHEMA §3 规定 12 个必需字段，本模型全部覆盖：
    event_id / run_id / parent_event_id / event_type / name / status /
    started_at / ended_at / duration_ms / input_summary / output_summary / error_code

    ``sequence`` 是同一 run 内的单调递增整数，用途是让回放按确定顺序对齐，
    避免时间戳毫秒精度相同时的顺序抖动（TRACE_SCHEMA §6）。
    """

    __tablename__ = "trace_event"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_trace_event_run_sequence"),
        Index("ix_trace_event_run_type", "run_id", "event_type"),
        Index("ix_trace_event_run_status", "run_id", "status"),
        Index("ix_trace_event_parent", "parent_event_id"),
    )

    event_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_event_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        # 删父事件不删子事件（DATA_MODEL §3）
        ForeignKey("trace_event.event_id", ondelete="SET NULL"),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_LENGTH), nullable=True)

    attributes: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    run: Mapped[Run] = relationship(back_populates="trace_events")

    def __repr__(self) -> str:
        return (
            f"<TraceEvent id={self.event_id!r} run={self.run_id!r} "
            f"type={self.event_type!r} name={self.name!r} status={self.status!r} "
            f"seq={self.sequence}>"
        )


# ---------------------------------------------------------------------------
# 4. tool_call
# ---------------------------------------------------------------------------


class ToolCall(Base):
    """工具调用记录。

    契约 PROJECT_SPEC §3.3：参数必须经 Pydantic 校验。
    ``validated=False`` 时 ``status`` 必为 ``invalid_arguments``，
    且实现体不应被调用——这是可被测试验证的强约束。
    """

    __tablename__ = "tool_call"
    __table_args__ = (Index("ix_tool_call_run_tool", "run_id", "tool_name"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("trace_event.event_id", ondelete="SET NULL"),
        nullable=True,
    )

    node_name: Mapped[str] = mapped_column(String(NAME_LENGTH), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(TOOL_NAME_LENGTH), nullable=False, index=True)
    tool_version: Mapped[str] = mapped_column(
        String(STATUS_LENGTH), nullable=False, default="1.0.0"
    )

    # 已脱敏的参数快照。不存未脱敏的原始参数。
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_LENGTH), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="tool_calls")

    def __repr__(self) -> str:
        return (
            f"<ToolCall id={self.id!r} tool={self.tool_name!r} "
            f"status={self.status!r} validated={self.validated}>"
        )


# ---------------------------------------------------------------------------
# 5. model_call
# ---------------------------------------------------------------------------


class ModelCall(Base):
    """模型调用记录。

    契约 T13 / EVALUATION §7：``is_test_double`` 是**强制标注**字段。
    为 True 表示这次调用由本地替身产生，其数值不能用于说明模型能力。
    """

    __tablename__ = "model_call"
    __table_args__ = (Index("ix_model_call_run_model", "run_id", "model_name"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("trace_event.event_id", ondelete="SET NULL"),
        nullable=True,
    )

    node_name: Mapped[str] = mapped_column(String(NAME_LENGTH), nullable=False)
    provider: Mapped[str] = mapped_column(String(PROVIDER_LENGTH), nullable=False)
    model_name: Mapped[str] = mapped_column(String(MODEL_NAME_LENGTH), nullable=False, index=True)
    is_test_double: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        default=Decimal("0"),
    )
    cost_estimation_unavailable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_LENGTH), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    run: Mapped[Run] = relationship(back_populates="model_calls")

    def __repr__(self) -> str:
        return (
            f"<ModelCall id={self.id!r} model={self.model_name!r} "
            f"tokens={self.total_tokens} test_double={self.is_test_double}>"
        )


# ---------------------------------------------------------------------------
# 6. eval_case
# ---------------------------------------------------------------------------


class EvalCase(Base):
    """评测集中的一个用例（从 JSONL 载入）。

    ``case_key`` 在 JSONL 中稳定且唯一，是跨版本对齐 case 的键。
    """

    __tablename__ = "eval_case"
    __table_args__ = (Index("ix_eval_case_dataset", "dataset_version"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    case_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    dataset_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_tools: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    expected_arguments: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    required_assertions: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    required_citations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expect_success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tags: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    eval_runs: Mapped[list[EvalRun]] = relationship(
        back_populates="eval_case", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<EvalCase key={self.case_key!r} dataset={self.dataset_version!r}>"


# ---------------------------------------------------------------------------
# 7. eval_run
# ---------------------------------------------------------------------------


class EvalRun(Base):
    """一个 case 在一次评测批次中的结果。

    ``evaluation_id`` 是批次 ID（多行共享），用于把一次评测的所有 case 结果聚起来。
    """

    __tablename__ = "eval_run"
    __table_args__ = (
        Index("ix_eval_run_evaluation", "evaluation_id"),
        Index(
            "ix_eval_run_dimensions",
            "agent_version",
            "prompt_version",
            "model_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    eval_case_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("eval_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        # run 被清理后仍保留评测数值（DATA_MODEL §3）
        ForeignKey("run.id", ondelete="SET NULL"),
        nullable=True,
    )

    dataset_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(VERSION_LENGTH), nullable=False)
    model_name: Mapped[str] = mapped_column(String(MODEL_NAME_LENGTH), nullable=False)
    is_test_double: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)

    # case 级判定结果。tool_* 为 None 表示"该 case 无工具期望，不参与该指标"，
    # 这与 False（参与了但不正确）语义不同，必须区分（EVALUATION §3 M3/M4）。
    task_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tool_selection_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tool_argument_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evidence_coverage: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        default=Decimal("0"),
    )

    # 契约 EVALUATION §5.1：失败 case 必须能回答"为什么失败"
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    assertion_results: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType, nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    eval_case: Mapped[EvalCase] = relationship(back_populates="eval_runs")
    run: Mapped[Run | None] = relationship(back_populates="eval_runs")

    def __repr__(self) -> str:
        return (
            f"<EvalRun id={self.id!r} evaluation={self.evaluation_id!r} "
            f"case={self.eval_case_id!r} status={self.status!r} "
            f"completed={self.task_completed}>"
        )


# ---------------------------------------------------------------------------
# 8. quality_gate
# ---------------------------------------------------------------------------


class QualityGate(Base):
    """门禁检查记录。

    契约 EVALUATION §6.3：必须持久化 ``thresholds`` 快照，
    保证判断依据可复现——否则事后无法解释"当时为什么算通过"。
    """

    __tablename__ = "quality_gate"
    __table_args__ = (Index("ix_quality_gate_evaluation", "evaluation_id"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    gate_name: Mapped[str] = mapped_column(String(80), nullable=False)

    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    observed_metrics: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    violations: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType, nullable=True)
    skipped_metrics: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:
        return (
            f"<QualityGate id={self.id!r} name={self.gate_name!r} "
            f"status={self.status!r} blocked={self.blocked}>"
        )


__all__ = [
    "AgentDefinition",
    "EvalCase",
    "EvalRun",
    "ModelCall",
    "QualityGate",
    "Run",
    "ToolCall",
    "TraceEvent",
]
