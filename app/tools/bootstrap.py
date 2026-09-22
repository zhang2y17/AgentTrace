"""内置工具的装配入口。

``registry.register_builtin_tools()`` 要求调用方传入工具实现实例 ——
这是刻意的（测试可以注入指向临时目录的 store）。但应用启动时没人愿意
手动拼装这一串依赖，所以把它收敛到这里：**生产装配只此一处**。

分工：

- ``registry`` 管"注册表本身"（注册、查找、调用、重试）；
- ``bootstrap`` 管"按配置把实现实例造出来并注册"。

``get_document_store()`` 提供进程级缓存：文档目录在进程生命周期内不变，
每次请求都重读 6 个 markdown 文件是纯粹的浪费。缓存的是 **store 实例**，
不是检索结果 —— 缓存答案会破坏"同输入同输出"的可验证性。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# 进程级 store 缓存：key 是文档目录的绝对路径字符串。
# 用路径做 key 而不是单一全局变量，是为了让"换目录"在测试里能立即生效。
_STORE_CACHE: dict[str, Any] = {}

# 已注册标记。四个工具都是进程级单例，重复注册没有必要。
_REGISTERED = False


def get_document_store(docs_dir: str | Path | None = None) -> Any:
    """取得（必要时构造）文档存储。

    Args:
        docs_dir: 文档目录；``None`` 时取配置 ``SAMPLE_DOCS_DIR``。

    Returns:
        ``DocumentStore`` 实例（同一目录返回同一实例）。
    """
    from app.core.config import get_settings
    from app.tools.document_search import DocumentStore

    if docs_dir is None:
        docs_dir = get_settings().sample_docs_dir

    path = Path(docs_dir)
    try:
        key = str(path.resolve())
    except OSError:  # pragma: no cover —— 路径不可解析时退化为原字符串
        key = str(path)

    cached = _STORE_CACHE.get(key)
    if cached is not None:
        return cached

    # DocumentStore 在构造时即完成加载，构造完就可用。
    store = DocumentStore(path)
    _STORE_CACHE[key] = store

    logger.info(
        "document_store_loaded",
        extra={"docs_dir": key, "document_count": len(store.document_ids)},
    )
    return store


def get_analytics_tools() -> Any:
    """构造分析工具。

    分析工具需要 Session 才能查库，但四个工具的契约都声明
    ``writes_database=False`` 且实现体自行管理会话，因此这里
    返回一个"懒绑定 Session 工厂"的实例 —— 构造时不建立连接。
    """
    from app.db.session import session_scope
    from app.tools.analytics import AnalyticsTools

    return AnalyticsTools(session_factory=session_scope)


def register_default_tools(*, force: bool = False) -> list[str]:
    """按默认配置注册四个内置工具。

    Args:
        force: 为 True 时先清空注册表再注册（供测试使用）。

    Returns:
        注册完成后的工具名列表（排序）。
    """
    global _REGISTERED

    from app.tools.registry import (
        clear,
        register_builtin_tools,
        registered_names,
    )

    if force:
        clear()
        _REGISTERED = False

    if _REGISTERED:
        return registered_names()

    from app.tools.document_search import DocumentSearchTools

    document_tools = DocumentSearchTools(get_document_store())
    register_builtin_tools(
        document_tools=document_tools,
        analytics_tools=get_analytics_tools(),
    )
    _REGISTERED = True

    names = registered_names()
    logger.info("builtin_tools_registered", extra={"tools": names})
    return names


def reset_for_tests() -> None:
    """清空进程级缓存与注册状态。**仅供测试使用。**"""
    global _REGISTERED
    _STORE_CACHE.clear()
    _REGISTERED = False


__all__ = [
    "get_analytics_tools",
    "get_document_store",
    "register_default_tools",
    "reset_for_tests",
]
