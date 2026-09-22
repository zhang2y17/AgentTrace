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

    def test_database_unreachable_reports_error_not_crash(self, monkeypatch, settings) -> None:  # type: ignore[no-untyped-def]
        """数据库不可达时 /health 仍返回 200，组件状态为 error。"""
        # 指向一个不可达的 PostgreSQL 地址（端口 1 必然拒绝连接）
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/nodb")
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
    """Redis 探针。

    这两个用例**必须屏蔽真实网络探测**。

    它们最初的写法是"直接请求 /health，然后断言 Redis 是 degraded"，
    隐含前提是"本机没起 Redis"。这个前提只在干净的 CI 容器里成立 ——
    开发者一旦在本地 `docker compose up redis`，端口 6379 变得可达，
    探针就返回 ok，用例随即失败，而代码其实毫无问题。

    2026 年 9 月的一次验收就撞上了：环境里恰好有一个无关的 redis 容器
    在 6379 上监听，两个用例双双报 `assert 'ok' == 'degraded'`。
    在未改动的 HEAD 上复现同样失败，确认是环境耦合而非回归。

    因此改为 monkeypatch ``check_redis_health``，把"Redis 可达性"变成
    一个**显式输入**：要测降级就把探测结果设成不可用，不再寄望于
    环境恰好配合。这也让 `REDIS_REQUIRED=true` 的分支可以被稳定触发。
    """

    def test_redis_unavailable_degrades_by_default(self, client: TestClient) -> None:
        """Redis 组件出现在 /health 的 components 里，且默认口径是"可选"。

        这里**不断言具体状态值** —— 那取决于本机 6379 是否恰好有服务在听
        （见类 docstring 说明的环境耦合）。具体状态由下面三条用显式输入覆盖。
        """
        body = client.get("/health").json()

        assert "redis" in body["components"]
        redis_component = body["components"]["redis"]
        # 无论可达与否，都必须是这两个值之一，绝不能是 error（默认非必需）。
        assert redis_component["status"] in {
            HealthStatus.OK.value,
            HealthStatus.DEGRADED.value,
        }
        # 默认非必需时不允许把可选依赖报成致命错误。
        assert redis_component["status"] != HealthStatus.ERROR.value

    @pytest.fixture
    def redis_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """强制让 Redis 探针认为 Redis 不可达。"""
        from app.services import cache

        monkeypatch.setattr(
            cache,
            "check_redis_health",
            lambda _settings: (False, "ConnectionError: 测试替身，Redis 不可达"),
        )

    def test_unavailable_is_degraded_when_not_required(
        self, redis_down: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REDIS_REQUIRED=false + 不可达 → degraded。"""
        monkeypatch.setenv("REDIS_REQUIRED", "false")
        from app.core.config import get_settings
        from app.db.session import dispose_engine

        get_settings.cache_clear()
        dispose_engine()

        from app.main import create_app

        with TestClient(create_app(), raise_server_exceptions=False) as client:
            body = client.get("/health").json()

        assert body["components"]["redis"]["status"] == HealthStatus.DEGRADED.value

    def test_when_redis_required_unavailable_is_error(
        self, redis_down: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_available_reports_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redis 可达 → ok。

        这条是上面两条的对照：证明"探针结果确实驱动了状态"，
        而不是无论探测结果如何都恒定输出 degraded。
        没有它，把探针写死成 degraded 也能让上面两条通过。
        """
        from app.services import cache

        monkeypatch.setattr(cache, "check_redis_health", lambda _settings: (True, None))
        from app.core.config import get_settings
        from app.db.session import dispose_engine

        get_settings.cache_clear()
        dispose_engine()

        from app.main import create_app

        with TestClient(create_app(), raise_server_exceptions=False) as client:
            body = client.get("/health").json()

        assert body["components"]["redis"]["status"] == HealthStatus.OK.value


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
            register_component_probe("database", lambda _s: (HealthStatus.OK, "postgresql", None))
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
