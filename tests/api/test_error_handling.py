"""统一错误处理器与请求上下文的接口测试。

覆盖：
1. 未匹配路由的 404 必须是本项目契约结构（而非 FastAPI 默认的 ``{"detail":...}``）；
2. 领域异常经 HTTP 层后仍是契约结构；
3. 未预期异常不透出内部信息；
4. 每个响应都带 ``X-Request-ID``。
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core.errors import RunNotFoundError, TracePersistenceError
from app.main import create_app

pytestmark = pytest.mark.api


@pytest.fixture
def app_with_probe_routes():
    """构造一个额外挂了"会抛异常的路由"的应用，用于验证处理器。

    这些路由只存在于测试中，不进入生产代码路径。
    """
    app = create_app()
    router = APIRouter()

    @router.get("/_test/domain-error")
    def _raise_domain_error() -> None:
        raise RunNotFoundError("Run 不存在。", details={"run_id": "run_missing"})

    @router.get("/_test/unexpected-error")
    def _raise_unexpected() -> None:
        raise RuntimeError("内部实现细节：连接串 postgresql://u:hunter2@db.internal/app 不可达")

    @router.get("/_test/trace-error")
    def _raise_trace_error() -> None:
        raise TracePersistenceError("Trace 写入失败。")

    app.include_router(router)
    return app


@pytest.fixture
def probe_client(app_with_probe_routes) -> TestClient:  # type: ignore[no-untyped-def]
    with TestClient(app_with_probe_routes, raise_server_exceptions=False) as client:
        yield client


class TestContractCompliantErrorShape:
    """所有非 2xx 响应必须是 {"error": {...}} 结构（API_CONTRACT §0.1）。"""

    def test_unknown_route_uses_contract_shape(self, client: TestClient) -> None:
        """FastAPI 默认返回 {"detail": ...}，必须被改写为契约结构。"""
        response = client.get("/definitely-not-a-route")

        assert response.status_code == 404
        body = response.json()
        assert "error" in body
        assert "detail" not in body
        assert set(body["error"]) == {"code", "message", "details", "request_id"}

    def test_domain_error_mapped_with_correct_status(self, probe_client: TestClient) -> None:
        response = probe_client.get("/_test/domain-error")

        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "RUN_NOT_FOUND"
        assert body["error"]["details"] == {"run_id": "run_missing"}
        assert body["error"]["request_id"].startswith("req_")

    def test_trace_persistence_error_returns_500(self, probe_client: TestClient) -> None:
        """契约 ARCHITECTURE §4：Trace 写入失败必须返回 500 且错误码明确。"""
        response = probe_client.get("/_test/trace-error")

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "TRACE_WRITE_FAILED"


class TestUnexpectedErrorHandling:
    """未预期异常不得透出内部信息（连接串、路径、实现细节）。"""

    def test_internal_details_not_exposed(self, probe_client: TestClient) -> None:
        response = probe_client.get("/_test/unexpected-error")

        assert response.status_code == 500
        body = response.json()
        assert body["error"]["code"] == "INTERNAL_ERROR"

        raw = response.text
        # 连接串与口令绝不能出现在响应中
        assert "hunter2" not in raw
        assert "db.internal" not in raw
        assert "RuntimeError" not in raw
        # 但必须给调用方一个可关联日志的 request_id
        assert body["error"]["request_id"].startswith("req_")


class TestRequestIdHeader:
    """每个响应都必须带 X-Request-ID，便于把客户端问题与日志对上。"""

    def test_header_present_on_success(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.headers.get("X-Request-ID", "").startswith("req_")

    def test_header_present_on_error(self, client: TestClient) -> None:
        response = client.get("/definitely-not-a-route")
        assert response.headers.get("X-Request-ID", "").startswith("req_")

    def test_header_matches_body_request_id(self, client: TestClient) -> None:
        """响应头与响应体中的 request_id 必须一致，否则无法关联。"""
        response = client.get("/definitely-not-a-route")
        assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


class TestOpenApiDocs:
    """API_CONTRACT §10：/docs 与 /openapi.json 必须可用。"""

    def test_docs_available(self, client: TestClient) -> None:
        assert client.get("/docs").status_code == 200

    def test_openapi_schema_available(self, client: TestClient) -> None:
        response = client.get("/openapi.json")
        assert response.status_code == 200

        schema = response.json()
        assert schema["info"]["title"] == "AgentTrace API"
        # health 端点已注册
        assert "/health" in schema["paths"]
