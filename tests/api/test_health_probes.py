"""基础设施健康探针与 /health 集成行为的测试。

覆盖 ACCEPTANCE_CHECKLIST：
- 组件探针注册后出现在 /health；
- 数据库不可达时如实报告而非把 /health 打挂；
- Redis 不可用时降级（默认 REQUIRED=false）；
- 响应中不含连接串口令。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.schemas.common import HealthStatus

pytestmark = pytest.mark.api


class TestDatabaseProbe:
    """数据库探针。"""

    def test_database_probe_reports_dialect(self, client: TestClient) -> None:
        """数据库可用时报告方言名（而非连接串）。"""
        body = client.get("/health").json()

        assert "database" in body["components"]
        db = body["components"]["database"]
        assert db["status"] == "ok"
        # detail 只含方言，绝不含连接串
        assert db["detail"] == "sqlite"
        assert "://" not in (db["detail"] or "")

    def test_database_unreachable_reports_error_not_crash(
        self, monkeypatch, settings
    ) -> None:  # type: ignore[no-untyped-def]
        """数据库不可达时 /health 仍返回 200，组件状态为 error。"""
        # 指向一个不可达的 PostgreSQL 地址（端口 1 必然拒绝连接）
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/nodb"
        )
        from app.core.config import get_settings
        from app.db.session import dispose_engine

        get_settings.cache_clear()
        dispose_engine()

        from app.main import create_app

        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == HealthStatus.DEGRADED.value
        assert body["components"]["database"]["status"] == "error"
        assert body["components"]["database"]["error"] is not None

    def test_database_error_message_redacts_password(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """数据库错误信息中的口令必须被脱敏。

        真实场景：驱动异常消息里常内嵌完整连接串（含口令）。
        """
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+psycopg://agenttrace:supersecretpw@127.0.0.1:1/nodb",
        )
        from app.core.config import get_settings
        from app.db.session import dispose_engine

        get_settings.cache_clear()
        dispose_engine()

        from app.main import create_app

        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get("/health")

        assert "supersecretpw" not in response.text


class TestRedisProbe:
    """Redis 探针。"""

    def test_redis_unavailable_degrades_by_default(self, client: TestClient) -> None:
        """默认 REDIS_REQUIRED=false：Redis 不可用 → degraded（非 error）。

        理由：Redis 只承载可选的短期任务状态，不应被视为致命依赖，
        否则本地开发时每次都要先起 Redis 才能看到健康检查变绿。
        """
        body = client.get("/health").json()

        assert "redis" in body["components"]
        redis_component = body["components"]["redis"]
        # 本机测试环境未启动 Redis，应为 degraded 而非 error
        assert redis_component["status"] == HealthStatus.DEGRADED.value

    def test_when_redis_required_unavailable_is_error(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """REDIS_REQUIRED=true 时不可用应报 error，不能假装降级就够了。"""
        monkeypatch.setenv("REDIS_REQUIRED", "true")
        from app.core.config import get_settings
        from app.db.session import dispose_engine

        get_settings.cache_clear()
        dispose_engine()

        from app.main import create_app

        with TestClient(create_app(), raise_server_exceptions=False) as client:
            body = client.get("/health").json()

        assert body["components"]["redis"]["status"] == HealthStatus.ERROR.value


class TestOverallStatusAggregation:
    """整体状态汇总逻辑。"""

    def test_all_ok_gives_ok_status(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """所有组件（含 Redis）都正常时整体为 ok。"""
        from app.api.health import clear_component_probes, register_component_probe
        from app.core.config import get_settings
        from app.main import create_app

        get_settings.cache_clear()

        with TestClient(create_app()) as client:
            # 用两个假探针模拟"全部正常"
            register_component_probe(
                "database", lambda _s: (HealthStatus.OK, "postgresql", None)
            )
            register_component_probe("redis", lambda _s: (HealthStatus.OK, None, None))
            try:
                body = client.get("/health").json()
                assert body["status"] == HealthStatus.OK.value
            finally:
                clear_component_probes()

    def test_multiple_component_probes_all_appear(self, client: TestClient) -> None:
        """注册的探针必须全部出现在 components 中。"""
        body = client.get("/health").json()

        assert {"api", "database", "redis"} <= set(body["components"])

    def test_health_completes_quickly(self, client: TestClient) -> None:
        """健康检查必须快速返回。

        这是真实约束：如果 Redis/PG 不可达时探针等待数十秒，
        容器编排的 healthcheck 会误判并反复重启容器。
        """
        import time

        started = time.perf_counter()
        response = client.get("/health")
        elapsed = time.perf_counter() - started

        assert response.status_code == 200
        # 允许较宽松的上限（CI 环境较慢），但要能拦住"挂住十几秒"的情况
        assert elapsed < 5.0, f"/health 耗时 {elapsed:.2f}s，过慢"
