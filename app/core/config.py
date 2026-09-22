"""AgentTrace 配置。

所有配置从环境变量 / `.env` 读取。契约 B8：API Key 只能从环境变量读取，
不得硬编码、不得入库、不得进日志。

设计要点：**每个字段都有默认值**，因此在完全没有 `.env` 的环境（例如 CI）
中应用依然可以启动。这是 S2 阶段识别出的关键风险。
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmProvider = Literal["fake", "openai", "ollama"]

# 项目根目录：``app/core/config.py`` 往上三层。
# 用它把"数据目录"类配置锚定为绝对路径 —— 见下方 _PROJECT_ROOT 的用法说明。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """应用配置。字段与环境变量同名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- 应用
    app_name: str = "agenttrace"
    app_version: str = "0.1.0"
    environment: Literal["local", "ci", "test"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ---------------------------------------------------------------- 数据库
    # 默认指向 Docker Compose 中的 PostgreSQL；
    # 离线测试时可覆盖为 sqlite+pysqlite:///./agenttrace.db
    database_url: str = "postgresql+psycopg://agenttrace:agenttrace@localhost:5432/agenttrace"
    db_echo: bool = False
    # 建连接超时（秒），避免数据库不可达时请求长时间挂起
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    # 启动时自动建表（本地开发便利）。生产应设为 false 并用 scripts/init_db.py。
    auto_create_tables: bool = True

    # ---------------------------------------------------------------- Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_connect_timeout_seconds: int = Field(default=2, ge=1, le=30)
    # Redis 不可用时是否降级继续（短期任务状态属可选能力）
    redis_required: bool = False

    # ---------------------------------------------------------------- LLM
    llm_provider: LlmProvider = "fake"
    # 仅从环境变量读取；SecretStr 保证 repr/日志中不会泄露明文
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = Field(default=30, ge=1, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # ---------------------------------------------------------------- Agent
    agent_version: str = Field(default="v1", max_length=40)
    prompt_version: str = Field(default="prompt-v1", max_length=40)
    agent_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_evidence_retries: int = Field(default=1, ge=0, le=3)
    default_top_k: int = Field(default=3, ge=1, le=10)
    # 证据充分性阈值：覆盖率低于该值判为不足
    evidence_coverage_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    # ---------------------------------------------------------------- 工具
    tool_max_retries: int = Field(default=2, ge=0, le=5)
    tool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    tool_search_max_top_k: int = Field(default=10, ge=1, le=50)

    # ---------------------------------------------------------------- 输入上限
    max_question_chars: int = Field(default=2000, ge=10, le=20000)

    # ---------------------------------------------------------------- Trace
    # Trace 摘要截断长度（TRACE_SCHEMA §7.1）
    summary_max_chars: int = Field(default=500, ge=50, le=10000)

    # ---------------------------------------------------------------- 评测
    # 同样锚定为绝对路径，理由见下方"数据目录"一节。
    eval_dataset_dir: str = str(_PROJECT_ROOT / "data" / "eval")
    # 默认门禁阈值，键名必须与 evaluation/metrics.py 导出的指标名一致
    gate_default_thresholds_json: str = Field(
        default=(
            '{"run_success_rate":{"min":0.90},'
            '"task_completion_rate":{"min":0.85},'
            '"tool_selection_accuracy":{"min":0.90},'
            '"tool_argument_accuracy":{"min":0.85},'
            '"evidence_coverage":{"min":0.70},'
            '"latency_ms_p95":{"max":2000},'
            '"estimated_cost_usd":{"max":0.05},'
            '"error_rate":{"max":0.05},'
            '"human_review_rate":{"max":0.00}}'
        )
    )

    # ---------------------------------------------------------------- 数据目录
    # 默认值锚定到**项目根目录的绝对路径**，而不是相对路径。
    #
    # 为什么不能用相对路径：相对路径按进程的当前工作目录解析，
    # 于是"能读到几个样例文档"取决于进程从哪里启动 ——
    # 测试里 chdir 到临时目录后，同一个配置就会指向一个空目录，
    # 表现为"检索结果为空 → 证据不足 → 全部降级"，
    # 与真实缺陷难以区分。锚定绝对路径让行为与 CWD 无关。
    #
    # 仍可通过环境变量覆盖（例如容器里挂载到别的路径）。
    sample_docs_dir: str = str(_PROJECT_ROOT / "data" / "sample_docs")
    pricing_table_path: str = str(_PROJECT_ROOT / "app" / "evaluation" / "pricing.json")

    # ---------------------------------------------------------------- 测试开关
    # 真实 LLM 集成测试默认关闭。默认测试路径不访问网络。
    enable_real_llm_tests: bool = False

    # ------------------------------------------------------------ 校验器
    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        """只接受 psycopg / pysqlite 驱动，避免误配到不存在的驱动。"""
        allowed_prefixes = (
            "postgresql+psycopg://",
            "sqlite+pysqlite://",
        )
        if not value.startswith(allowed_prefixes):
            raise ValueError(
                "DATABASE_URL 必须以 'postgresql+psycopg://' 或 'sqlite+pysqlite://' 开头，"
                f"当前值的前缀不合法（收到 {urlsplit(value).scheme!r}）"
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL 必须以 'redis://'、'rediss://' 或 'unix://' 开头")
        return value

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> Settings:
        """真实 provider 必须提供 base_url（openai）或明确使用本地 ollama。

        注意：这里**不检查 API Key 是否存在**——那属于运行时校验，放在 LLM 网关里做，
        这样配置层不会因为缺少密钥而无法加载（有利于测试与 /health 探测）。
        """
        if self.llm_provider == "openai" and not self.llm_base_url:
            # 允许留空表示使用 OpenAI 官方端点，这里不报错，仅由网关决定默认值
            pass
        return self

    # ------------------------------------------------------------ 派生属性
    @property
    def is_test_double_mode(self) -> bool:
        """当前是否处于测试替身模式。

        契约 B5/T13：替身模式必须可被明确识别，并体现在 API 响应与落库字段中。
        """
        return self.llm_provider == "fake"

    @property
    def database_dialect(self) -> str:
        """返回数据库方言名，用于 /health 与跨方言分支判断。"""
        return "sqlite" if self.database_url.startswith("sqlite") else "postgresql"

    def safe_database_url(self) -> str:
        """返回脱敏后的数据库 URL，可安全写入日志。"""
        return redact_database_url(self.database_url)

    def safe_redis_url(self) -> str:
        """返回脱敏后的 Redis URL，可安全写入日志。"""
        return redact_database_url(self.redis_url)


def redact_database_url(url: str) -> str:
    """把 URL 中的用户口令替换为 ***，用于日志输出。

    ``postgresql+psycopg://agenttrace:secret@host:5432/db``
    → ``postgresql+psycopg://agenttrace:***@host:5432/db``
    """
    if "@" not in url:
        return url
    scheme_sep = "://"
    if scheme_sep not in url:
        return url
    scheme, rest = url.split(scheme_sep, 1)
    if "@" not in rest:
        return url
    credentials, host_part = rest.rsplit("@", 1)
    if ":" not in credentials:
        return url
    user, _password = credentials.split(":", 1)
    return f"{scheme}{scheme_sep}{user}:***@{host_part}"


def is_loopback_host(url: str) -> bool:
    """判断 URL 主机是否为回环地址。

    用于 /health 输出与日志中的信息分级：本地回环地址可以正常显示，
    非本地地址在日志中会被弱化显示。
    """
    host = urlsplit(url).hostname
    if host is None:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例。

    使用 ``lru_cache`` 而非模块级实例，便于测试中通过
    ``get_settings.cache_clear()`` 重置。
    """
    return Settings()


__all__ = [
    "LlmProvider",
    "Settings",
    "get_settings",
    "is_loopback_host",
    "redact_database_url",
]
