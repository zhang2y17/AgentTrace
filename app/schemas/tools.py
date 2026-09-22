"""工具参数与返回模型。

契约 PROJECT_SPEC §3.2 + §3.3：
1. 工具参数**必须**由 Pydantic 校验；校验失败不得调用实现体；
2. 统计与成本计算必须由确定性代码完成，不允许让 LLM 猜测数值。

本模块定义 4 个工具的输入输出契约，是工具注册表与 API 的共同依赖。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolArgsModel(BaseModel):
    """所有工具参数模型的基类。

    ``extra="forbid"`` 是关键：LLM 经常产生多余参数，静默忽略会让
    "参数正确率"指标失真，因此这里选择显式拒绝。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# search_documents
# ---------------------------------------------------------------------------


class SearchDocumentsArgs(ToolArgsModel):
    """``search_documents(query, top_k)`` 的参数。"""

    query: str = Field(
        min_length=1,
        max_length=500,
        description="检索词。允许空格分隔的关键词组合。",
    )
    top_k: int = Field(
        ge=1,
        le=10,
        description="返回条数上限，1~10。",
    )

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, value: str) -> str:
        """拒绝全空白查询。"""
        if not value.strip():
            raise ValueError("query 不能为空白字符串")
        return value


class DocumentHit(BaseModel):
    """一条检索命中。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="文档 ID，如 doc-trace-schema")
    title: str = Field(description="文档标题")
    score: float = Field(ge=0.0, le=1.0, description="相关性得分，归一化到 0~1")
    snippet: str = Field(description="命中片段摘要")
    matched_terms: list[str] = Field(default_factory=list, description="本次命中匹配到的关键词")


class SearchDocumentsResult(BaseModel):
    """``search_documents`` 的返回值。"""

    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int
    hits: list[DocumentHit] = Field(default_factory=list)
    total_matched: int = Field(default=0, description="在截断到 top_k 之前匹配到的文档总数")


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


class GetDocumentArgs(ToolArgsModel):
    """``get_document(document_id)`` 的参数。"""

    document_id: str = Field(
        min_length=1,
        max_length=120,
        description="文档 ID，必须存在于样例文档目录中。",
    )


class Document(BaseModel):
    """一篇完整文档。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    content: str
    char_count: int = Field(ge=0)
    headings: list[str] = Field(default_factory=list, description="文档中的标题列表")


# ---------------------------------------------------------------------------
# calculate_latency_summary
# ---------------------------------------------------------------------------


class RunScopedArgs(ToolArgsModel):
    """以 run_id 为唯一参数的工具的公共基类。"""

    run_id: str = Field(
        min_length=8,
        max_length=64,
        description="run ID，必须以 'run_' 开头。",
    )

    @field_validator("run_id")
    @classmethod
    def _validate_run_id_prefix(cls, value: str) -> str:
        """校验 ID 前缀，避免把 tool_call id 误当 run_id 传进来。"""
        if not value.startswith("run_"):
            raise ValueError("run_id 必须以 'run_' 开头")
        return value


class CalculateLatencySummaryArgs(RunScopedArgs):
    """``calculate_latency_summary(run_id)`` 的参数。"""


class StageLatency(BaseModel):
    """单个阶段的耗时统计。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="节点名或工具名")
    kind: str = Field(description="node / tool_call / model_call")
    call_count: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    max_duration_ms: int = Field(ge=0)
    mean_duration_ms: float = Field(ge=0.0)


class LatencySummary(BaseModel):
    """``calculate_latency_summary`` 的返回值。

    全部数值由确定性 SQL 聚合得出（契约 §3.3），不经过 LLM。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    total_duration_ms: int = Field(ge=0, description="run 表记录的全流程耗时")
    event_count: int = Field(ge=0)
    slowest_event_name: str | None = None
    slowest_event_duration_ms: int | None = Field(default=None, ge=0)
    stages: list[StageLatency] = Field(default_factory=list)
    computed_by: str = Field(
        default="deterministic_sql_aggregation",
        description="标注计算方式：确定性代码，非 LLM",
    )


# ---------------------------------------------------------------------------
# calculate_cost_summary
# ---------------------------------------------------------------------------


class CalculateCostSummaryArgs(RunScopedArgs):
    """``calculate_cost_summary(run_id)`` 的参数。"""


class ModelCostBreakdown(BaseModel):
    """按模型维度的成本拆分。"""

    model_config = ConfigDict(extra="forbid")

    model_name: str
    provider: str
    call_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0.0)


class CostSummary(BaseModel):
    """``calculate_cost_summary`` 的返回值。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0.0)
    breakdown: list[ModelCostBreakdown] = Field(default_factory=list)
    cost_estimation_unavailable: bool = Field(
        default=False,
        description=(
            "为 True 时表示存在未知模型名，其成本未能估算。"
            "契约要求：不得静默把未知模型成本当 0 处理（EVALUATION §3 M8）。"
        ),
    )
    computed_by: str = Field(default="deterministic_pricing_table")
    is_test_double: bool = Field(
        default=False, description="是否来源于测试替身模型（fake provider）"
    )


# ---------------------------------------------------------------------------
# 运行上下文（供节点与工具共享）
# ---------------------------------------------------------------------------


class RetrievalEvidence(BaseModel):
    """一条被采纳为证据的检索结果。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    score: float = Field(ge=0.0, le=1.0)
    excerpt: str
    retrieved_at: datetime | None = None


__all__ = [
    "CalculateCostSummaryArgs",
    "CalculateLatencySummaryArgs",
    "CostSummary",
    "Document",
    "DocumentHit",
    "GetDocumentArgs",
    "LatencySummary",
    "ModelCostBreakdown",
    "RetrievalEvidence",
    "RunScopedArgs",
    "SearchDocumentsArgs",
    "SearchDocumentsResult",
    "StageLatency",
    "ToolArgsModel",
]
