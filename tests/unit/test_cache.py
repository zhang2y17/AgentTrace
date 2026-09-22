"""Redis 短期任务状态缓存测试。

使用 ``fakeredis`` 作为替身（契约 T13：替身必须明确标注），
因此本模块全部标记为 ``test_double``，**不需要真实 Redis**。

重点验证契约 T6 的强约束：Redis **不缓存最终答案**。
"""

from __future__ import annotations

import pytest

from app.services.cache import STATUS_TTL_SECONDS, TaskStateCache

pytestmark = [pytest.mark.unit, pytest.mark.test_double]


@pytest.fixture
def fake_redis_client():
    """fakeredis 替身客户端。标记为 test_double。"""
    fakeredis = pytest.importorskip("fakeredis", reason="fakeredis 未安装")
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def cache(settings, fake_redis_client):  # type: ignore[no-untyped-def]
    """使用替身客户端的缓存实例。"""
    return TaskStateCache(settings, client=fake_redis_client)


class TestContractT6NoAnswerCaching:
    """契约 T6 的强制约束：Redis 不缓存最终答案。

    这不是风格问题，而是评测有效性的前提：若缓存答案，
    延迟与成本指标都会失真（变快、变低），模型更新后指标也不反映真实变化。
    """

    def test_cache_api_exposes_no_answer_methods(self) -> None:
        """公开 API 中不得存在答案/结果缓存方法。"""
        public_methods = {name for name in dir(TaskStateCache) if not name.startswith("_")}

        forbidden = {
            "set_answer",
            "get_answer",
            "cache_answer",
            "set_result",
            "get_result",
            "cache_result",
            "set_model_response",
            "get_model_response",
            "set_completion",
            "get_completion",
        }
        overlap = public_methods & forbidden
        assert not overlap, f"缓存类暴露了答案/结果缓存方法: {sorted(overlap)}"

    def test_key_prefix_is_task_state_only(self) -> None:
        """键前缀必须只体现"任务状态"语义。"""
        assert TaskStateCache._PREFIX == "agenttrace:task_state:"
        assert "answer" not in TaskStateCache._PREFIX
        assert "result" not in TaskStateCache._PREFIX
        assert "completion" not in TaskStateCache._PREFIX

    def test_module_docstring_states_the_constraint(self) -> None:
        """模块 docstring 必须明确写出该约束，避免后来者误加缓存。"""
        import app.services.cache as cache_module

        doc = cache_module.__doc__ or ""
        assert "不缓存最终答案" in doc or "绝不缓存最终答案" in doc


class TestTaskStateReadWrite:
    """状态读写的基本行为。"""

    def test_set_and_get_task_state(self, cache) -> None:  # type: ignore[no-untyped-def]
        ok = cache.set_task_state(
            "run_test", status="running", node_name="document_search", progress=0.4
        )
        assert ok is True

        state = cache.get_task_state("run_test")
        assert state is not None
        assert state["run_id"] == "run_test"
        assert state["status"] == "running"
        assert state["node_name"] == "document_search"
        assert state["progress"] == 0.4

    def test_get_missing_state_returns_none(self, cache) -> None:  # type: ignore[no-untyped-def]
        assert cache.get_task_state("run_never_seen") is None

    def test_clear_task_state(self, cache) -> None:  # type: ignore[no-untyped-def]
        cache.set_task_state("run_x", status="running")
        assert cache.get_task_state("run_x") is not None

        assert cache.clear_task_state("run_x") is True
        assert cache.get_task_state("run_x") is None

    def test_state_has_ttl(self, cache, fake_redis_client) -> None:  # type: ignore[no-untyped-def]
        """状态必须自动过期，避免长期占用内存。"""
        cache.set_task_state("run_ttl", status="running")

        ttl = fake_redis_client.ttl("agenttrace:task_state:run_ttl")
        assert 0 < ttl <= STATUS_TTL_SECONDS

    def test_custom_ttl_respected(self, cache, fake_redis_client) -> None:  # type: ignore[no-untyped-def]
        cache.set_task_state("run_short", status="running", ttl_seconds=30)

        ttl = fake_redis_client.ttl("agenttrace:task_state:run_short")
        assert 0 < ttl <= 30

    def test_ping_success(self, cache) -> None:  # type: ignore[no-untyped-def]
        healthy, error = cache.ping()
        assert healthy is True
        assert error is None


class TestGracefulDegradation:
    """Redis 不可用时的降级行为。

    默认 ``redis_required=False``：短期状态属可选能力，不应阻断核心功能。
    """

    def test_operations_return_false_when_client_missing(self, settings) -> None:  # type: ignore[no-untyped-def]
        """无客户端时写入返回 False，读取返回 None，**不抛异常**。

        通过子类覆盖 ``client`` 属性来稳定模拟"连接不可用"，
        而不依赖真实的连接超时（那会让测试变慢且不确定）。
        """

        class NoClientCache(TaskStateCache):
            @property
            def client(self):  # type: ignore[no-untyped-def]
                return None

        degraded = NoClientCache(settings)

        assert degraded.set_task_state("r", status="running") is False
        assert degraded.get_task_state("r") is None
        assert degraded.clear_task_state("r") is False

        healthy, error = degraded.ping()
        assert healthy is False
        assert error is not None

    def test_raises_when_redis_required(self, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """``redis_required=True`` 时必须改为抛错，不能静默降级。"""
        from app.services.cache import CacheUnavailableError

        class FailingClient:
            def setex(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                raise ConnectionError("redis down")

        monkeypatch.setattr(settings, "redis_required", True)
        cache = TaskStateCache(settings, client=FailingClient())

        with pytest.raises(CacheUnavailableError):
            cache.set_task_state("r", status="running")

    def test_corrupted_state_returns_none(self, cache, fake_redis_client) -> None:  # type: ignore[no-untyped-def]
        """存储值损坏时返回 None 而非抛错，避免污染调用方逻辑。"""
        fake_redis_client.set("agenttrace:task_state:run_bad", "这不是 JSON")

        assert cache.get_task_state("run_bad") is None
