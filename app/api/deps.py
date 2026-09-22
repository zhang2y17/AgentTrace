"""HTTP 层依赖注入。

本模块集中 HTTP 层需要的所有依赖，避免各路由重复构造。
S3/S5 阶段会在此处补齐数据库 Session 与服务的注入。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]

__all__ = ["SettingsDep"]
