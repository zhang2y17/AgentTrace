"""配置模块单元测试。

覆盖 S2 阶段识别出的关键风险：**无 ``.env`` 时应用仍可启动**
（所有字段必须有默认值），以及密钥永不进入 repr 与日志。
"""

from __future__ import annotations

import pytest

from app.core.config import (
    Settings,
    get_settings,
    is_loopback_host,
    redact_database_url,
)

pytestmark = pytest.mark.unit


class TestDefaults:
    """默认值行为。契约 T12：无 .env、无环境变量时必须能构造出配置。"""

    def test_loads_without_any_env_config(self, isolated_env: None) -> None:
        """清空环境后仍能构造 Settings，且关键默认值正确。

        注意：``isolated_env`` 夹具会把 DATABASE_URL 指向临时 SQLite，
        因此这里断言方言为 sqlite。要验证 PostgreSQL 默认值，
        见 :meth:`test_default_database_url_is_postgres`。
        """
        get_settings.cache_clear()
        settings = Settings()  # type: ignore[call-arg]

        assert settings.app_name == "agenttrace"
        assert settings.llm_provider == "fake"
        assert settings.environment == "test"
        # autouse 夹具注入了 SQLite，保证默认测试不依赖外部 PostgreSQL
        assert settings.database_dialect == "sqlite"

    def test_default_database_url_is_postgres(self, monkeypatch) -> None:
        """不读环境变量时，默认 DATABASE_URL 必须指向 PostgreSQL。

        这是"开箱即用指向真实数据库"的契约（契约 T5）。
        注意必须显式删除已被 autouse 夹具注入的 DATABASE_URL，
        否则读到的是测试用的 SQLite。
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert "postgresql+psycopg" in settings.database_url
        assert settings.database_dialect == "postgresql"

    def test_default_provider_is_test_double(self, isolated_env: None) -> None:
        """默认 provider 必须是 fake，保证默认路径不访问网络。"""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.llm_provider == "fake"
        assert settings.is_test_double_mode is True

    def test_default_limits_are_sane(self, isolated_env: None) -> None:
        """上限类配置必须有合理默认值。"""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert settings.max_question_chars == 2000
        assert settings.summary_max_chars == 500
        assert settings.max_evidence_retries == 1
        assert settings.tool_max_retries == 2
        assert 0.0 < settings.evidence_coverage_threshold < 1.0

    def test_real_llm_tests_disabled_by_default(self, isolated_env: None) -> None:
        """真实 LLM 集成测试必须默认关闭（契约 T12）。"""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.enable_real_llm_tests is False


class TestValidation:
    """配置校验。"""

    def test_rejects_unknown_database_driver(self, isolated_env: None) -> None:
        """非 psycopg / pysqlite 驱动的 DATABASE_URL 必须被拒绝。

        理由：误配到未安装的驱动会在运行时才炸，属于可提前拦截的错误。
        """
        with pytest.raises(ValueError, match="DATABASE_URL 必须以"):
            Settings(database_url="mysql+pymysql://u:p@localhost/db")  # type: ignore[call-arg]

    def test_accepts_sqlite_for_tests(self, isolated_env: None) -> None:
        """SQLite 是受支持的测试方言。"""
        settings = Settings(  # type: ignore[call-arg]
            database_url="sqlite+pysqlite:///./test.db"
        )
        assert settings.database_dialect == "sqlite"

    def test_rejects_invalid_redis_url(self, isolated_env: None) -> None:
        with pytest.raises(ValueError, match="REDIS_URL 必须以"):
            Settings(redis_url="http://localhost:6379")  # type: ignore[call-arg]

    def test_rejects_out_of_range_bounds(self, isolated_env: None) -> None:
        """越界数值必须被拒绝，避免配置错误变成运行时行为异常。"""
        with pytest.raises(ValueError):
            Settings(max_question_chars=1)  # type: ignore[call-arg]

        with pytest.raises(ValueError):
            Settings(max_evidence_retries=99)  # type: ignore[call-arg]

        with pytest.raises(ValueError):
            Settings(evidence_coverage_threshold=1.5)  # type: ignore[call-arg]


class TestSecretHandling:
    """契约 B8：密钥不得出现在 repr / 日志 / 序列化结果中。"""

    def test_api_key_not_in_repr(self, isolated_env: None) -> None:
        """SecretStr 必须保证 repr 中不出现密钥明文。"""
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        settings = Settings(llm_api_key=secret)  # type: ignore[call-arg]

        assert secret not in repr(settings)
        assert secret not in str(settings)
        # 但通过显式调用仍可取到，保证功能可用
        assert settings.llm_api_key is not None
        assert settings.llm_api_key.get_secret_value() == secret

    def test_api_key_not_in_model_dump(self, isolated_env: None) -> None:
        """model_dump 输出不得包含密钥明文。"""
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        settings = Settings(llm_api_key=secret)  # type: ignore[call-arg]

        dumped = repr(settings.model_dump())
        assert secret not in dumped

    def test_safe_database_url_redacts_password(self, isolated_env: None) -> None:
        """脱敏后的 URL 必须是 user:***@host 形式。"""
        settings = Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://agenttrace:supersecret@db.internal:5432/agenttrace"
        )
        safe = settings.safe_database_url()

        assert "supersecret" not in safe
        assert "agenttrace:***@db.internal:5432" in safe

    def test_api_key_configured_flag_available_without_leak(self, isolated_env: None) -> None:
        """/health 需要知道"是否配置了密钥"，但不能拿到密钥本身。"""
        settings = Settings(llm_api_key="sk-test")  # type: ignore[call-arg]
        assert settings.llm_api_key is not None
        assert bool(settings.llm_api_key.get_secret_value()) is True


class TestRedactDatabaseUrl:
    """URL 脱敏的边界情况。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "postgresql+psycopg://u:p@h:5432/d",
                "postgresql+psycopg://u:***@h:5432/d",
            ),
            ("redis://localhost:6379/0", "redis://localhost:6379/0"),
            ("sqlite+pysqlite:///./a.db", "sqlite+pysqlite:///./a.db"),
            ("redis://:onlypass@host:6379/0", "redis://:***@host:6379/0"),
            ("nonsense", "nonsense"),
        ],
    )
    def test_redaction_cases(self, raw: str, expected: str) -> None:
        assert redact_database_url(raw) == expected

    def test_idempotent(self) -> None:
        """重复脱敏不应继续变化。"""
        once = redact_database_url("postgresql+psycopg://u:p@h/d")
        assert redact_database_url(once) == once


class TestLoopbackDetection:
    """回环地址识别，用于日志分级。"""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("redis://localhost:6379/0", True),
            ("redis://127.0.0.1:6379/0", True),
            ("redis://[::1]:6379/0", True),
            ("redis://redis-host:6379/0", False),
        ],
    )
    def test_loopback(self, url: str, expected: bool) -> None:
        assert is_loopback_host(url) is expected


class TestEnvExampleCoverage:
    """``.env.example`` 必须与 ``Settings`` 的字段集完全一致。

    这条约束的价值在"新加配置项时"体现：漏写文档不会让任何测试失败，
    只会让下一个使用者不知道有这个开关存在 —— 于是它形同虚设，
    而作者以为它可配置。反向的漂移更糟：``.env.example`` 里留着一个
    已被重命名的旧键，抄过去之后**不报错也不生效**。
    """

    def _documented_keys(self) -> set[str]:
        import re
        from pathlib import Path

        # 从测试文件位置向上找到项目根，避免依赖当前工作目录
        # （``isolated_env`` 夹具会把 cwd 切到 tmp_path）。
        root = Path(__file__).resolve().parents[2]
        text = (root / ".env.example").read_text(encoding="utf-8")
        return {match.group(1) for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=.*$", text, re.M)}

    def test_no_undocumented_setting(self) -> None:
        """每个 ``Settings`` 字段都要在样例里出现。"""
        from app.core.config import Settings

        undocumented = sorted(
            {name.upper() for name in Settings.model_fields} - self._documented_keys()
        )
        assert undocumented == [], f"以下配置项未写入 .env.example：{undocumented}"

    def test_no_stale_entry_in_env_example(self) -> None:
        """样例里不能有代码不认的键。"""
        from app.core.config import Settings

        stale = sorted(self._documented_keys() - {name.upper() for name in Settings.model_fields})
        assert stale == [], f".env.example 含已失效的配置项：{stale}"

    def test_compose_services_match_expectation(self) -> None:
        """CI 会校验 compose 含 api/postgres/redis 三个服务。

        这里把同样的期望放在单测里，让本地改坏编排时能立刻发现，
        而不必等 CI 跑完。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        text = (root / "docker-compose.yml").read_text(encoding="utf-8")
        for service in ("api:", "postgres:", "redis:"):
            assert f"  {service}" in text, f"docker-compose.yml 缺少服务 {service}"
