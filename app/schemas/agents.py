"""Agent 内部数据结构（不出现在 HTTP 契约中）。

这些模型描述节点之间流转的任务描述与最终结果。
使用 Pydantic 而非裸 dict，可以在节点边界处尽早暴露类型错误。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import RunStatus


class ParsedTask(BaseModel):
    """``question_parser`` 的产出：结构化任务描述。"""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="意图分类：explain / howto / compare / lookup / other")
    keywords: list[str] = Field(default_factory=list, description="用于检索的关键词")
    needs_search: bool = Field(default=True, description="是否需要进行文档检索")
    normalized_question: str = Field(description="归一化后的问题（去多余空白）")
    parser_confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="解析置信度，0~1")


class EvidenceAssessment(BaseModel):
    """``evidence_checker`` 的产出：证据充分性判断。"""

    model_config = ConfigDict(extra="forbid")

    sufficient: bool
    coverage: float = Field(ge=0.0, le=1.0, description="证据覆盖率")
    accepted_document_ids: list[str] = Field(default_factory=list)
    rejected_reasons: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    threshold_used: float = Field(default=0.5, ge=0.0, le=1.0)


class FinalValidation(BaseModel):
    """``final_validator`` 的产出：答案校验结果。"""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    has_citation: bool
    citation_count: int = Field(default=0, ge=0)
    required_fields_present: bool = Field(default=True)
    errors: list[str] = Field(default_factory=list)
    fatal: bool = Field(default=False, description="为 True 时 run 终态应为 failed 而非 degraded")


class AgentFinalResult(BaseModel):
    """Agent 运行的最终结果。"""

    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    answer: str = Field(default="")
    citations: list[str] = Field(default_factory=list)
    evidence_sufficient: bool = Field(default=False)
    validation_errors: list[str] = Field(default_factory=list)
    node_statuses: dict[str, str] = Field(
        default_factory=dict, description="节点名 → 状态，便于快速定位"
    )
    extra: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AgentFinalResult",
    "EvidenceAssessment",
    "FinalValidation",
    "ParsedTask",
]
