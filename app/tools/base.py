"""工具契约：``ToolSpec`` 与返回模型。

契约 PROJECT_SPEC §3.2 / §3.3：

1. 工具参数一律由 Pydantic 模型校验；校验失败必须产生
   ``tool_call.status = "invalid_arguments"`` 的记录，
   且**不得**把未校验参数传给实现体；
2. 统计与成本计算必须由确定性代码完成，不允许让 LLM 猜测数值；
3. 工具通过统一注册表发现，节点里禁止硬编码"工具名 → 函数"映射；
4. 每次调用必须写 ``tool_call`` 表与一条 ``tool_call`` 类型的 trace_event。

本模块只定义**结构与数据模型**，不含 IO、不含注册表、不含 Trace 写入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------


class SearchDocumentsArgs(BaseModel):
    """``search_documents`` 的参数。

    ``top_k`` 的上下限来自契约文档（``1..10``）。上限是**契约值**而非实现细节：
    它同时被 ``contract.lock.json`` 与 ``TOOL_SEARCH_MAX_TOP_K`` 配置约束。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500, description="检索关键词")
    top_k: int = Field(default=3, ge=1, le=10, description="返回条数，1~10")


class GetDocumentArgs(BaseModel):
    """``get_document`` 的参数。

    ``pattern`` 约束文档 ID 的形态，让明显非法的输入（如路径穿越字符串）
    在**校验层**就被拒绝，而不是等到文件读取时才失败。
    """

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(
        pattern=r"^doc-\d{3}$",
        description="文档 ID，形如 doc-001",
    )


class CalculateLatencySummaryArgs(BaseModel):
    """``calculate_latency_summary`` 的参数。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=64, description="要统计的 run ID")


class CalculateCostSummaryArgs(BaseModel):
    """``calculate_cost_summary`` 的参数。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=64, description="要统计的 run ID")


# ---------------------------------------------------------------------------
# 返回模型
# ---------------------------------------------------------------------------


class DocumentHit(BaseModel):
    """检索命中的单条结果。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="文档 ID")
    title: str = Field(description="文档标题")
    score: float = Field(description="关键词打分，越大越相关")
    snippet: str = Field(description="命中片段，已截断")
    matched_terms: list[str] = Field(default_factory=list, description="命中的关键词")


class Document(BaseModel):
    """一篇完整文档。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    content: str = Field(description="正文（不含 front matter）")
    char_count: int = Field(description="正文字符数")


class LatencySummary(BaseModel):
    """延迟统计结果。**由确定性代码计算**（契约 §3.3 第 2 条）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    total_duration_ms: int | None = Field(description="run 总耗时，未知时为 null")
    node_count: int = Field(description="node 事件数")
    tool_call_count: int
    model_call_count: int
    slowest_node: str | None = Field(default=None, description="最慢节点名")
    slowest_node_ms: int | None = Field(default=None, description="最慢节点耗时")
    p50_node_ms: int | None = Field(default=None, description="节点耗时 p50（nearest-rank）")
    p95_node_ms: int | None = Field(default=None, description="节点耗时 p95（nearest-rank）")
    data_source_note: str = Field(
        default="由 trace_event 与 run 表确定性聚合得出，不使用 LLM 估算。",
        description="数据来源说明",
    )


class CostSummary(BaseModel):
    """成本统计结果。**由确定性代码计算**。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: Decimal = Field(description="估算成本（USD）")
    model_call_count: int
    cost_estimation_unavailable: bool = Field(
        description="是否至少有一次调用无法估算成本（如测试替身）"
    )
    pricing_note: str = Field(
        default="成本基于静态价目表估算，不反映实时价格，仅用于量级比较。",
        description="成本估算限制说明",
    )


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个工具的定义。

    ``args_model`` 是**闸门**：注册表必须先用它校验，再调用 ``implementation``。
    这是契约"不得把未校验参数传给实现体"的结构性保证 ——
    实现体的签名只接收已校验的模型实例，无法绕过。
    """

    name: str
    version: str
    description: str
    args_model: type[BaseModel]
    implementation: Callable[..., Any]
    # 是否会写数据库。只读工具在评测可重复性上更安全，
    # 这个标记让注册表与评测可以据此区分。
    writes_database: bool = False
    # 是否使用 LLM。契约要求统计/成本类工具为 False。
    uses_llm: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    def validate_arguments(self, raw: dict[str, Any]) -> BaseModel:
        """校验原始参数。

        Raises:
            pydantic.ValidationError: 参数非法。调用方（注册表）负责把它
                转成 ``invalid_arguments`` 状态，而不是让它冒泡成 500。
        """
        return self.args_model.model_validate(raw)


# 四个工具名，与 contract.lock.json 的 ``tools`` 段严格一致。
# 这里定义常量是为了让注册表与测试共用同一份来源，避免字符串散落各处。
TOOL_SEARCH_DOCUMENTS: Final = "search_documents"
TOOL_GET_DOCUMENT: Final = "get_document"
TOOL_CALCULATE_LATENCY_SUMMARY: Final = "calculate_latency_summary"
TOOL_CALCULATE_COST_SUMMARY: Final = "calculate_cost_summary"

TOOL_NAMES: Final[tuple[str, ...]] = (
    TOOL_SEARCH_DOCUMENTS,
    TOOL_GET_DOCUMENT,
    TOOL_CALCULATE_LATENCY_SUMMARY,
    TOOL_CALCULATE_COST_SUMMARY,
)


__all__ = [
    "TOOL_CALCULATE_COST_SUMMARY",
    "TOOL_CALCULATE_LATENCY_SUMMARY",
    "TOOL_GET_DOCUMENT",
    "TOOL_NAMES",
    "TOOL_SEARCH_DOCUMENTS",
    "CalculateCostSummaryArgs",
    "CalculateLatencySummaryArgs",
    "CostSummary",
    "Document",
    "DocumentHit",
    "GetDocumentArgs",
    "LatencySummary",
    "SearchDocumentsArgs",
    "ToolSpec",
]
