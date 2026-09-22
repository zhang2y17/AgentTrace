"""HTTP 层依赖注入。

本模块集中 HTTP 层需要的所有依赖，避免各路由重复构造。

**为什么这里用"函数包装"而不是直接 ``Depends(SomeClass)``**

``RunService`` / ``MetricsService`` 的构造签名每个参数都有默认值，
FastAPI 原则上可以自己实例化它们。但仍然以显式函数暴露，
理由是：服务构造与配置来源的关系变得可读，
将来服务需要额外依赖时只改这里，不必动每个路由签名。

**一条硬约束**：不要声明 ``Annotated[Settings, Depends(get_settings)]``
形式的路由参数。``Settings`` 是 Pydantic 模型，FastAPI 会把
"Pydantic 模型类型"判为**请求体字段** —— 于是 OpenAPI 里凭空多出一个
``settings`` body 字段，与契约不符，调用方按文档发请求会被拒。
配置一律在服务层内部获取。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.services.metrics_service import MetricsService
from app.services.run_service import RunService


def _get_settings() -> Settings:
    """配置依赖（仅供服务构造函数内部使用）。"""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(_get_settings)]


def get_run_service() -> RunService:
    """构造运行服务。

    配置由 ``RunService`` 自己在构造函数里取（``settings or get_settings()``），
    不经过 HTTP 层传参 —— 避免 ``Settings`` 泄漏成请求体字段。
    """
    return RunService()


def get_metrics_service() -> MetricsService:
    """构造指标服务。"""
    return MetricsService()


RunServiceDep = Annotated[RunService, Depends(get_run_service)]
MetricsServiceDep = Annotated[MetricsService, Depends(get_metrics_service)]

__all__ = [
    "MetricsServiceDep",
    "RunServiceDep",
    "SettingsDep",
    "get_metrics_service",
    "get_run_service",
]
