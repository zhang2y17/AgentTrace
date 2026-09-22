"""FastAPI 应用装配。

职责（ARCHITECTURE §2）：
1. 创建 app、配置日志、注册路由；
2. 注册统一异常处理器（API_CONTRACT §0.1）；
3. 记录请求级结构化日志（不记录请求体，避免泄露输入）。

契约 SECURITY：访问日志只记录方法、路径、状态码、耗时、请求 ID，
**不记录**查询参数与请求体，避免把用户问题或密钥写进日志。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import health
from app.core.config import get_settings
from app.core.errors import (
    AgentTraceError,
    InvalidArgumentError,
    generate_request_id,
)
from app.core.logging import configure_logging, get_logger
from app.schemas.common import ErrorBody, ErrorCode, ErrorResponse

logger = get_logger(__name__)

# 请求 ID 在 state 中的键名，供异常处理器与日志复用
_REQUEST_ID_STATE_KEY = "agenttrace_request_id"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期。

    S2 阶段只做日志配置。S3 阶段会在此处初始化数据库连接与 Redis 客户端，
    S5 阶段会在此处建表（本地开发便利），生产场景应改用 init_db.py。
    """
    settings = get_settings()
    configure_logging(settings.log_level, force=True)
    logger.info(
        "application_starting",
        extra={
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "environment": settings.environment,
            "llm_provider": settings.llm_provider,
            "is_test_double_mode": settings.is_test_double_mode,
            # 使用脱敏后的 URL，绝不打明文口令
            "database_url": settings.safe_database_url(),
            "redis_url": settings.safe_redis_url(),
        },
    )
    yield
    logger.info("application_stopping", extra={"app_name": settings.app_name})


def _error_response(exc: AgentTraceError, request_id: str) -> JSONResponse:
    """把领域异常转成统一错误响应。"""
    payload = exc.to_payload(request_id)
    return JSONResponse(status_code=exc.http_status, content=payload)


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    settings = get_settings()

    app = FastAPI(
        title="AgentTrace API",
        version=settings.app_version,
        description=(
            "AgentTrace —— LLM Agent 执行过程的记录、回放、评测与质量门禁平台。\n\n"
            "**个人独立开发项目。** 所有指标均来自离线评测或样例运行，"
            "不代表线上流量或生产环境表现。\n\n"
            "默认 `LLM_PROVIDER=fake` 使用测试替身，不进行任何真实模型调用。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # ------------------------------------------------------------ 中间件
    @app.middleware("http")
    async def _request_context_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """为每个请求分配 request_id 并记录结构化访问日志。

        刻意**不**记录 query string 与 body：它们可能含用户问题或密钥。
        """
        request_id = generate_request_id()
        setattr(request.state, _REQUEST_ID_STATE_KEY, request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    # ------------------------------------------------------------ 异常处理器
    @app.exception_handler(AgentTraceError)
    async def _handle_agenttrace_error(request: Request, exc: AgentTraceError) -> JSONResponse:
        """处理领域异常。"""
        request_id = getattr(request.state, _REQUEST_ID_STATE_KEY, None) or generate_request_id()
        logger.warning(
            "domain_error",
            extra={
                "request_id": request_id,
                "error_code": exc.error_code,
                "path": request.url.path,
                # 这里记录细节，便于排障；details 已在异常构造时脱敏
                "details": exc.details,
            },
        )
        return _error_response(exc, request_id)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """把 Pydantic 校验错误映射为 400 INVALID_ARGUMENT。

        只回显字段路径与错误类型，不回显提交的值——提交值可能是敏感数据。
        """
        request_id = getattr(request.state, _REQUEST_ID_STATE_KEY, None) or generate_request_id()
        field_errors = [
            {
                "location": ".".join(str(part) for part in err.get("loc", ())),
                "type": err.get("type", "unknown"),
                "message": err.get("msg", ""),
            }
            for err in exc.errors()[:20]
        ]
        error = InvalidArgumentError(
            "请求体校验失败。",
            details={"field_errors": field_errors},
        )
        logger.warning(
            "validation_error",
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "field_error_count": len(field_errors),
            },
        )
        return _error_response(error, request_id)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """处理框架层 HTTPException（如未匹配路由的 404）。

        框架默认响应体是 ``{"detail": ...}``，不符合本项目契约，
        因此这里统一改写为 ``{"error": {...}}`` 结构。
        """
        request_id = getattr(request.state, _REQUEST_ID_STATE_KEY, None) or generate_request_id()
        code = {
            404: ErrorCode.RUN_NOT_FOUND.value if "/runs/" in request.url.path else "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR.value)

        # 404 不区分具体路径时给出通用 code，避免误导调用方以为是 run 不存在
        if exc.status_code == 404:
            code = "NOT_FOUND"

        body = ErrorResponse(
            error=ErrorBody(
                code=code,
                message=str(exc.detail),
                details={"path": request.url.path},
                request_id=request_id,
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理器：未预期异常一律返回 500，**不回显**内部异常消息。

        原因：未预期异常的 message 可能包含连接串、文件路径等内部信息。
        只写日志，调用方拿到固定的通用消息与 request_id，用于关联日志。
        """
        request_id = getattr(request.state, _REQUEST_ID_STATE_KEY, None) or generate_request_id()
        logger.exception(
            "unhandled_exception",
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "exception_type": type(exc).__name__,
            },
        )
        body = ErrorResponse(
            error=ErrorBody(
                code=ErrorCode.INTERNAL_ERROR.value,
                message="服务器内部错误。请使用 request_id 联系维护者并提供日志。",
                details={},
                request_id=request_id,
            )
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    # ------------------------------------------------------------ 路由
    app.include_router(health.router)

    return app


app = create_app()


__all__ = ["app", "create_app", "lifespan"]
