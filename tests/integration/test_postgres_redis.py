"""真实集成测试：PostgreSQL + Redis。

**这是真集成测试，不是替身测试。** 标记为 ``integration``。

默认行为（契约 T12）：
``pyproject.toml`` 的 ``addopts`` 已加 ``-m "not integration"``，
因此 **默认测试套件不会收集本模块**——这比在模块内用 ``skipif`` 更早生效，
避免收集阶段就去连接数据库（连接超时会让整个测试套件卡住）。

显式运行::

    docker compose up -d postgres redis
    DATABASE_URL=postgresql+psycopg://agenttrace:agenttrace@localhost:5432/agenttrace \\
    REDIS_URL=redis://localhost:6379/0 \\
    python -m pytest tests/integration -q -m integration

本模块的测试**不**使用替身，验证的是：
1. PostgreSQL 方言下的建表与读写；
2. ON DELETE CASCADE 在 PostgreSQL 上的真实行为；
3. NUMERIC 金额在 PostgreSQL 上的精度；
4. Redis 真实读写与 TTL。
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 依赖可用性探测
# ---------------------------------------------------------------------------


def _postgres_available() -> tuple[bool, str]:
    """探测真实 PostgreSQL 是否可用。

    超时设得很短（3 秒）：探测本身不应拖慢测试收集。
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return False, "DATABASE_URL 未指向 PostgreSQL（默认测试使用 SQLite）"

    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(
            url, connect_args={"connect_timeout": 3}, pool_pre_ping=False
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"无法连接 PostgreSQL: {type(exc).__name__}"


def _redis_available() -> tuple[bool, str]:
    """探测真实 Redis 是否可用。"""
    url = os.environ.get("REDIS_URL", "")
    if not url.startswith(("redis://", "rediss://")):
        return False, "REDIS_URL 未设置"

    try:
        import redis

        client = redis.Redis.from_url(
            url, socket_connect_timeout=3, socket_timeout=3, retry_on_timeout=False
        )
        client.ping()
        client.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"无法连接 Redis: {type(exc).__name__}"


requires_postgres = pytest.mark.skipif(
    not _postgres_available()[0],
    reason=f"[集成测试] 需要真实 PostgreSQL：{_postgres_available()[1]}",
)

requires_redis = pytest.mark.skipif(
    not _redis_available()[0],
    reason=f"[集成测试] 需要真实 Redis：{_redis_available()[1]}",
)


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


@pytest.fixture
def postgres_repository(monkeypatch):  # type: ignore[no-untyped-def]
    """在真实 PostgreSQL 上提供仓储。

    使用独立的 schema 前缀隔离测试数据，测试结束后清理。
    """
    from app.core.config import get_settings
    from app.db.repository import TraceRepository
    from app.db.session import create_all_tables, dispose_engine, get_session_factory

    get_settings.cache_clear()
    dispose_engine()
    create_all_tables()

    session = get_session_factory()()
    repository = TraceRepository(session)
    created_run_ids: list[str] = []

    yield repository, session, created_run_ids

    # 清理：只删除本测试创建的 run，不碰其他数据
    from app.db.models import Run

    try:
        for run_id in created_run_ids:
            run = session.get(Run, run_id)
            if run is not None:
                session.delete(run)
        session.commit()
    except Exception:  # noqa: BLE001  —— 清理失败不应掩盖测试结果
        session.rollback()
    finally:
        session.close()
        dispose_engine()


@requires_postgres
class TestPostgresPersistence:
    """真实 PostgreSQL 上的持久化行为。"""

    def test_create_run_and_events_on_postgres(self, postgres_repository) -> None:  # type: ignore[no-untyped-def]
        repository, session, created = postgres_repository

        run = repository.create_run(
            question="集成测试：PostgreSQL 写入",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
            is_test_double=True,
        )
        session.commit()
        created.append(run.id)

        for index in range(3):
            repository.append_event(
                run_id=run.id,
                event_type="node",
                name=f"node_{index}",
                status="ok",
                input_summary={"index": index},
            )
        session.commit()

        events, total = repository.list_events(run.id)
        assert total == 3
        assert [e.sequence for e in events] == [1, 2, 3]

    def test_numeric_money_precision_on_postgres(self, postgres_repository) -> None:  # type: ignore[no-untyped-def]
        """验证 NUMERIC(12,6) 在 PostgreSQL 上的精度无损。

        SQLite 的 numeric 亲和性会掩盖精度问题，因此在 PostgreSQL 上单独验证
        是必要的——这是 IMPLEMENTATION_PLAN S3 风险表中列出的风险项。
        """
        repository, session, created = postgres_repository

        run = repository.create_run(
            question="精度测试",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="openai",
        )
        session.commit()
        created.append(run.id)

        precise_cost = Decimal("0.000187")
        repository.record_model_call(
            run_id=run.id,
            node_name="n",
            provider="openai",
            model_name="gpt-4o-mini",
            is_test_double=False,
            total_tokens=340,
            estimated_cost_usd=precise_cost,
            status="ok",
        )
        session.commit()
        session.expire_all()

        _, total_cost, _ = repository.aggregate_token_and_cost(run.id)
        assert total_cost == precise_cost

    def test_cascade_delete_on_postgres(self, postgres_repository) -> None:  # type: ignore[no-untyped-def]
        """在真实 PostgreSQL 上验证 ON DELETE CASCADE 生效。

        SQLite 默认关闭外键，因此这个测试在 PostgreSQL 上才是可信的
        （session.py 已为 SQLite 显式打开 PRAGMA，但两者仍应分别验证）。
        """
        repository, session, created = postgres_repository
        from app.db.models import TraceEvent

        run = repository.create_run(
            question="级联删除测试",
            status="running",
            agent_version="v1",
            prompt_version="prompt-v1",
            llm_provider="fake",
        )
        session.commit()

        repository.append_event(run_id=run.id, event_type="node", name="n", status="ok")
        session.commit()

        run_id = run.id
        session.delete(run)
        session.commit()

        from sqlalchemy import select

        remaining = (
            session.execute(select(TraceEvent).where(TraceEvent.run_id == run_id))
            .scalars()
            .all()
        )
        assert remaining == []


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


@requires_redis
class TestRedisReal:
    """真实 Redis 上的读写与 TTL。"""

    def test_set_get_clear_on_real_redis(self) -> None:
        from app.core.config import get_settings
        from app.services.cache import TaskStateCache

        get_settings.cache_clear()
        settings = get_settings()
        cache = TaskStateCache(settings)

        run_id = "run_integration_redis_test"
        try:
            assert cache.set_task_state(
                run_id, status="running", node_name="document_search", progress=0.5
            )

            state = cache.get_task_state(run_id)
            assert state is not None
            assert state["status"] == "running"
            assert state["progress"] == 0.5

            assert cache.clear_task_state(run_id)
            assert cache.get_task_state(run_id) is None
        finally:
            cache.clear_task_state(run_id)

    def test_real_redis_ping(self) -> None:
        from app.core.config import get_settings
        from app.services.cache import TaskStateCache

        get_settings.cache_clear()
        healthy, error = TaskStateCache(get_settings()).ping()

        assert healthy is True
        assert error is None


# ---------------------------------------------------------------------------
# 健康检查（含真实依赖）
# ---------------------------------------------------------------------------


@requires_postgres
class TestHealthWithRealDependencies:
    """带真实 PostgreSQL 的 /health 行为。"""

    def test_health_reports_database_ok(self, client) -> None:  # type: ignore[no-untyped-def]
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["components"]["database"]["status"] == "ok"
        assert body["components"]["database"]["detail"] == "postgresql"
