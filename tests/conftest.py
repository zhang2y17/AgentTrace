"""pytest 全局夹具。

契约 T12/T13：默认测试路径**不需要网络与 API Key**，并且所有替身必须被明确标注。

本文件的夹具分三类：
1. 环境隔离：``isolated_env`` 清除可能干扰测试的环境变量；
2. 配置：``settings`` 提供测试专用配置；
3. 客户端：``client`` 提供 FastAPI TestClient。

标记说明见 pyproject.toml 的 ``markers``。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 测试期间必须屏蔽的环境变量：避免本机 `.env` 或 shell 环境干扰测试结果
_ENV_KEYS_TO_CLEAR = (
    "DATABASE_URL",
    "REDIS_URL",
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "ENABLE_REAL_LLM_TESTS",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """为每个测试提供隔离的环境。

    - 清除可能影响配置的外部环境变量；
    - 把工作目录切到临时目录，避免测试污染仓库；
    - 把数据库指向临时 SQLite 文件，**保证默认测试不需要外部 PostgreSQL**；
    - 禁用启动时自动建表，由各个测试自行按需建表。

    注意：本夹具 ``autouse=True``，保证没有测试会意外读到本机真实配置。
    """
    monkeypatch.chdir(tmp_path)
    for key in _ENV_KEYS_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("AUTO_CREATE_TABLES", "false")

    yield

    # 清理全局状态：配置缓存与数据库引擎
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    get_settings.cache_clear()
    dispose_engine()


@pytest.fixture
def settings(isolated_env: None):  # type: ignore[no-untyped-def]
    """测试用配置（fake provider，SQLite，无外部依赖）。"""
    from app.core.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def db_session(settings) -> Iterator[object]:  # type: ignore[no-untyped-def]
    """提供已建表的 SQLite 会话。

    每个测试独立建表与销毁，保证测试之间不相互污染。
    """
    from app.db.session import create_all_tables, dispose_engine, get_session_factory

    dispose_engine()
    create_all_tables()

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
        dispose_engine()


@pytest.fixture
def repository(db_session):  # type: ignore[no-untyped-def]
    """提供 TraceRepository 实例。"""
    from app.db.repository import TraceRepository

    return TraceRepository(db_session)


@pytest.fixture
def client(settings) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """FastAPI 测试客户端。

    使用 ``with`` 触发 lifespan（日志配置、探针注册、工具注册）。
    建表不在 lifespan 里做 —— 本夹具把 ``AUTO_CREATE_TABLES`` 显式设为
    true，让应用自身的建表路径被真实执行（而不是测试另起一套建表逻辑），
    这样"应用能自己把表建起来"这件事每次都被验证。
    """
    import os

    from app.core.config import get_settings
    from app.db.session import dispose_engine
    from app.main import create_app

    previous = os.environ.get("AUTO_CREATE_TABLES")
    os.environ["AUTO_CREATE_TABLES"] = "true"
    get_settings.cache_clear()
    dispose_engine()

    app = create_app()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            os.environ.pop("AUTO_CREATE_TABLES", None)
        else:
            os.environ["AUTO_CREATE_TABLES"] = previous
        get_settings.cache_clear()
        dispose_engine()


def pytest_report_header(config: pytest.Config) -> list[str]:  # noqa: ARG001
    """在测试报告头部声明当前测试的替身边界，避免误读测试结果。"""
    provider = os.environ.get("LLM_PROVIDER", "fake")
    real_llm = os.environ.get("ENABLE_REAL_LLM_TESTS", "false").lower() in {"1", "true", "yes"}
    return [
        "AgentTrace 测试范围声明：",
        f"  LLM_PROVIDER={provider}（fake 表示使用测试替身，不访问网络）",
        f"  ENABLE_REAL_LLM_TESTS={real_llm}（false 表示跳过真实模型集成测试）",
        "  默认测试路径不需要网络与 API Key。",
    ]
