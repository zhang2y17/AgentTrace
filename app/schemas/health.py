"""健康检查响应模型（API_CONTRACT §1）。

契约 SECURITY：本模块的响应**不得**包含任何密钥信息，
数据库/Redis 的 URL 如需回显必须脱敏。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import HealthStatus


class ComponentHealth(BaseModel):
    """单个依赖组件的健康状态。"""

    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    latency_ms: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, description="补充说明，如方言名、provider 名；已脱敏")
    error: str | None = Field(default=None, description="异常信息，已脱敏且截断；正常时为 null")


class LlmProviderHealth(BaseModel):
    """LLM provider 的健康与替身标注。"""

    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    provider: str = Field(description="fake / openai / ollama")
    is_test_double: bool = Field(
        description="契约 B5：为 True 表示当前使用测试替身，不进行真实模型调用"
    )
    model_name: str | None = None
    api_key_configured: bool = Field(
        default=False,
        description="是否已配置密钥的布尔值。**只暴露布尔值，绝不暴露密钥本身。**",
    )


class HealthResponse(BaseModel):
    """``GET /health`` 响应。

    ``status`` 语义：
    - ``ok``：全部必需组件正常
    - ``degraded``：部分组件异常；HTTP 仍为 200，由调用方按组件判断
    """

    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    service: str
    version: str
    components: dict[str, ComponentHealth] = Field(default_factory=dict)
    llm_provider: LlmProviderHealth
    checked_at: datetime


__all__ = ["ComponentHealth", "HealthResponse", "LlmProviderHealth"]
