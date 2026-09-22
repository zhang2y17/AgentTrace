"""``GET /health`` 接口测试（API_CONTRACT §1）。

重点验证：
1. HTTP 状态码语义（组件异常仍为 200，但 status=degraded）；
2. 响应中**绝不含**密钥信息；
3. 测试替身必须有明确标注。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.schemas.common import HealthStatus

pytestmark = [pytest.mark.api, pytest.mark.test_double]


class TestHealthBasic:
    """基础行为。"""

    def test_returns_200_and_expected_shape(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()

        assert body["service"] == "agenttrace"
        assert body["version"]
        assert body["status"] in {"ok", "degraded"}
        assert "checked_at" in body
        assert "api" in body["components"]
        assert body["components"]["api"]["status"] == "ok"

    def test_api_component_has_latency(self, client: TestClient) -> None:
        """组件必须报告探测耗时，便于排查依赖变慢。"""
        body = client.get("/health").json()
        assert body["components"]["api"]["latency_ms"] is not None
        assert body["components"]["api"]["latency_ms"] >= 0

    def test_timestamps_are_utc_iso8601(self, client: TestClient) -> None:
        """时间格式统一为 ISO 8601 UTC（API_CONTRACT §0.2）。"""
        body = client.get("/health").json()
        checked_at = body["checked_at"]
        assert "T" in checked_at
        assert checked_at.endswith("Z") or "+00:00" in checked_at


class TestHealthLlmProvider:
    """LLM provider 状态与替身标注（契约 B5）。"""

    def test_fake_provider_marked_as_test_double(self, client: TestClient) -> None:
        """默认 provider 是 fake，必须明确标注为测试替身。"""
        body = client.get("/health").json()
        provider = body["llm_provider"]

        assert provider["provider"] == "fake"
        assert provider["is_test_double"] is True
        assert provider["status"] == "ok"

    def test_real_provider_without_key_is_degraded(self, monkeypatch) -> None:
        """配置真实 provider 但缺密钥时必须报 degraded，不能假装健康。"""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        get_settings.cache_clear()

        with TestClient(create_app()) as client:
            body = client.get("/health").json()

        provider = body["llm_provider"]
        assert provider["provider"] == "openai"
        assert provider["api_key_configured"] is False
        assert provider["status"] == HealthStatus.DEGRADED.value
        # 整体状态因此降级
        assert body["status"] == HealthStatus.DEGRADED.value

    def test_ollama_provider_does_not_require_key(self, monkeypatch) -> None:
        """本地 Ollama 不需要密钥，不应因**缺密钥**而报 degraded。

        注意：整体 ``status`` 还可能因其他组件（如本机未启动的 Redis）而降级，
        因此这里只断言 ``llm_provider`` 组件本身的判定，
        避免把"Redis 没起"误判成"Ollama 配置有问题"。
        """
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        get_settings.cache_clear()

        with TestClient(create_app()) as client:
            body = client.get("/health").json()

        provider = body["llm_provider"]
        assert provider["provider"] == "ollama"
        assert provider["api_key_configured"] is False
        # 关键断言：不因缺密钥而降级
        assert provider["status"] == "ok"


class TestHealthNoSecretLeak:
    """契约 B8/SECURITY：健康检查响应绝不能泄露密钥。"""

    def test_api_key_never_appears_in_response(self, monkeypatch) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", secret)
        get_settings.cache_clear()

        with TestClient(create_app()) as client:
            response = client.get("/health")

        raw = response.text
        assert secret not in raw
        # 只暴露布尔值
        assert response.json()["llm_provider"]["api_key_configured"] is True

    def test_database_password_never_appears(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+psycopg://agenttrace:supersecretpw@localhost:5432/agenttrace",
        )
        get_settings.cache_clear()

        with TestClient(create_app()) as client:
            response = client.get("/health")

        assert "supersecretpw" not in response.text


class TestComponentProbeMechanism:
    """探针注册机制。

    S2 阶段只注册 api 探针；S3 会注册 database 与 redis。
    这里用假探针验证机制本身可用，包括"探针抛异常不能打挂 /health"。
    """

    def test_registered_probe_appears_in_components(self, client: TestClient) -> None:
        from app.api.health import clear_component_probes, register_component_probe

        register_component_probe(
            "fake_dependency",
            lambda _settings: (HealthStatus.OK, "fake-ok", None),
        )
        try:
            body = client.get("/health").json()
            assert "fake_dependency" in body["components"]
            assert body["components"]["fake_dependency"]["status"] == "ok"
            assert body["components"]["fake_dependency"]["detail"] == "fake-ok"
        finally:
            clear_component_probes()

    def test_error_probe_degrades_overall_status_without_crashing(self, client: TestClient) -> None:
        """组件挂掉时 HTTP 仍为 200（便于编排系统区分进程与依赖）。"""
        from app.api.health import clear_component_probes, register_component_probe

        register_component_probe(
            "broken_dependency",
            lambda _settings: (HealthStatus.ERROR, None, "connection refused"),
        )
        try:
            response = client.get("/health")

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "degraded"
            assert body["components"]["broken_dependency"]["status"] == "error"
            assert body["components"]["broken_dependency"]["error"] == "connection refused"
        finally:
            clear_component_probes()

    def test_probe_raising_exception_is_contained(self, client: TestClient) -> None:
        """探针自身抛异常绝不能让 /health 返回 500。"""
        from app.api.health import clear_component_probes, register_component_probe

        def exploding_probe(_settings):  # type: ignore[no-untyped-def]
            raise RuntimeError("探针内部错误")

        register_component_probe("exploding", exploding_probe)
        try:
            response = client.get("/health")

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "degraded"
            assert body["components"]["exploding"]["status"] == "error"
            assert "RuntimeError" in body["components"]["exploding"]["error"]
        finally:
            clear_component_probes()

    def test_probe_error_message_is_sanitized(self, client: TestClient) -> None:
        """探针错误信息中的密钥必须被脱敏。"""
        from app.api.health import clear_component_probes, register_component_probe

        register_component_probe(
            "leaky",
            lambda _settings: (
                HealthStatus.ERROR,
                None,
                "auth failed with api_key=abcdef123456",
            ),
        )
        try:
            body = client.get("/health").json()
            assert "abcdef123456" not in str(body)
        finally:
            clear_component_probes()
