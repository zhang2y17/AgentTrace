"""指标汇总响应模型（API_CONTRACT §8）。

契约 B9 / EVALUATION §0：任何指标输出**必须**带数据来源标注。
因此 ``MetricsSummaryResponse`` 强制包含 ``scope`` 与 ``data_source_note``。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import DataScope

# 契约 B9：指标口径声明。任何指标响应都必须携带这句话的等价表述。
DATA_SOURCE_NOTE = (
    "基于本实例已记录的运行结果（样例运行 / 离线评测），不代表任何线上流量或生产环境表现。"
)

OFFLINE_EVAL_NOTE = (
    "本结果来自固定评测集上的离线评测运行。"
    "当 LLM_PROVIDER=fake 时数值由测试替身产生，仅用于验证流程与逻辑正确性，"
    "不代表真实模型能力。"
)


class MetricsBlock(BaseModel):
    """10 个指标的容器。

    契约 EVALUATION §3：无数据时返回 ``None`` 而非 0，
    避免把"从未运行"误读为"成功率 0"。
    """

    model_config = ConfigDict(extra="forbid")

    # 成功率类
    run_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    task_completion_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    human_review_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    degraded_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, description="降级率，单独统计（不计入错误率）"
    )

    # 准确性类（仅评测场景有值）
    tool_selection_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    tool_argument_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_coverage: float | None = Field(default=None, ge=0.0, le=1.0)

    # 延迟类
    latency_ms_p50: float | None = Field(default=None, ge=0.0)
    latency_ms_p95: float | None = Field(default=None, ge=0.0)
    latency_ms_mean: float | None = Field(default=None, ge=0.0)

    # 规模类
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    cost_estimation_unavailable: bool = Field(
        default=False,
        description="存在未知模型名导致成本无法完整估算时为 True",
    )

    # 分母透明度（EVALUATION §3 M3/M4 要求）
    tool_selection_eligible_count: int | None = Field(
        default=None,
        ge=0,
        description="有工具期望的 case 数，即 tool_selection_accuracy 的分母",
    )
    case_count: int | None = Field(default=None, ge=0)


class ToolUsageStat(BaseModel):
    """单个工具的使用统计。"""

    model_config = ConfigDict(extra="forbid")

    calls: int = Field(ge=0)
    ok: int = Field(ge=0)
    invalid_arguments: int = Field(ge=0)
    error: int = Field(ge=0)
    mean_duration_ms: float | None = Field(default=None, ge=0.0)


class MetricsGroup(BaseModel):
    """按某个维度分组的指标。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="分组键名，如 agent_version / prompt_version / model_name / day")
    value: str = Field(description="该组的取值")
    run_count: int = Field(ge=0)
    metrics: MetricsBlock


class MetricsSummaryResponse(BaseModel):
    """``GET /metrics/summary`` 响应（API_CONTRACT §8）。"""

    model_config = ConfigDict(extra="forbid")

    scope: DataScope = Field(description="数据来源范围。empty 表示无数据，此时所有指标为 null。")
    data_source_note: str = Field(
        default=DATA_SOURCE_NOTE, description="契约 B9 强制：指标口径声明"
    )
    filters: dict[str, str | None] = Field(default_factory=dict)
    group_by: str | None = None
    run_count: int = Field(ge=0)
    metrics: MetricsBlock = Field(default_factory=MetricsBlock)
    tools: dict[str, ToolUsageStat] = Field(default_factory=dict)
    groups: list[MetricsGroup] = Field(default_factory=list)


__all__ = [
    "DATA_SOURCE_NOTE",
    "OFFLINE_EVAL_NOTE",
    "MetricsBlock",
    "MetricsGroup",
    "MetricsSummaryResponse",
    "ToolUsageStat",
]
