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
    - 把工作目录相关的路径指向临时目录，避免测试污染仓库。

    注意：本夹具 ``autouse=True``，保证没有测试会意外读到本机真实配置。
    """
    # 阻止 pytest 从仓库 .env 读到真实配置
    monkeypatch.chdir(tmp_path)
    for key in _ENV_KEYS_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)

    # 显式告知应用处于测试环境
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LLM_PROVIDER", "fake")

    yield

    # 清理配置缓存，避免下一个测试读到上一个测试缓存的 Settings
    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def settings(isolated_env: None):  # type: ignore[no-untyped-def]
    """测试用配置（fake provider，SQLite，无外部依赖）。"""
    from app.core.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def client(settings) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """FastAPI 测试客户端。

    使用 ``with`` 触发 lifespan（日志配置等）。
    """
    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


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
