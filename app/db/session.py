"""数据库引擎与会话管理。

跨方言设计（ARCHITECTURE §8 / IMPLEMENTATION_PLAN S3 风险表）：

| 关注点 | PostgreSQL | SQLite（测试） |
|---|---|---|
| 外键约束 | 默认启用 | **默认关闭**，必须显式 ``PRAGMA foreign_keys=ON`` |
| 连接池 | 正常使用 | 应避免跨线程复用连接 |
| 超时 | ``connect_timeout`` 参数 | ``timeout`` 参数 |
| 建表 | ``create_all`` | ``create_all`` |

SQLite 的外键默认关闭是一个**真实的坑**：如果不打开，
``ON DELETE CASCADE`` 不会生效，测试会通过但在 PostgreSQL 上行为不同。
因此本模块在 SQLite 连接上强制打开外键。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.base import Base

logger = get_logger(__name__)

# 全局引擎与会话工厂（由 init_engine 初始化）
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(settings: Settings) -> Engine:
    """按方言构造引擎。

    SQLite 与 PostgreSQL 的连接参数不同，这里显式分支而不是靠 SQLAlchemy
    自动处理，因为静默忽略不支持的参数会导致"配了但没生效"的假象。
    """
    url = settings.database_url

    if settings.database_dialect == "sqlite":
        engine = create_engine(
            url,
            echo=settings.db_echo,
            future=True,
            # SQLite 默认禁止跨线程使用连接；FastAPI 的同步端点跑在线程池里，
            # 因此必须关闭该检查。测试场景下这是安全的。
            connect_args={"check_same_thread": False, "timeout": settings.db_connect_timeout_seconds},
        )

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            """SQLite 默认不启用外键，必须显式打开，否则级联删除静默失效。"""
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_engine(
        url,
        echo=settings.db_echo,
        future=True,
        pool_pre_ping=True,  # 连接被中间件断开后自动重建，避免使用失效连接
        pool_size=5,
        max_overflow=10,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )


def init_engine(settings: Settings | None = None, *, force: bool = False) -> Engine:
    """初始化全局引擎与会话工厂。

    Args:
        settings: 配置；默认取全局配置。
        force: 为 True 时重建引擎（测试中切换数据库时需要）。
    """
    global _engine, _session_factory

    if _engine is not None and not force:
        return _engine

    settings = settings or get_settings()
    _engine = _build_engine(settings)
    _session_factory = sessionmaker(
        bind=_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,  # 提交后仍可读取属性，避免已分离实例触发额外查询
    )

    logger.info(
        "database_engine_initialized",
        extra={
            "dialect": settings.database_dialect,
            "database_url": settings.safe_database_url(),
        },
    )
    return _engine


def get_engine() -> Engine:
    """获取全局引擎，未初始化时自动初始化。"""
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """获取全局会话工厂。"""
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


def dispose_engine() -> None:
    """释放全局引擎。用于测试清理与进程退出。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


# ---------------------------------------------------------------------------
# 会话上下文
# ---------------------------------------------------------------------------


@contextmanager
def session_scope() -> Iterator[Session]:
    """提供事务性会话上下文。

    正常退出时 commit，异常时 rollback 并重新抛出。
    **不吞异常**——契约要求写入失败必须显式暴露（ARCHITECTURE §4）。

    用法::

        with session_scope() as session:
            session.add(obj)
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。

    与 ``session_scope`` 的区别：不自动 commit（由服务层决定事务边界），
    异常时 rollback。这符合"服务层掌握事务边界"的分层原则。
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all_tables(engine: Engine | None = None) -> None:
    """建表。等价于 ``Base.metadata.create_all``。

    注意：本项目不使用 Alembic（见 DATA_MODEL §5 已知限制），
    字段变更需要删库重建。
    """
    import app.db.models  # noqa: F401  —— 触发所有模型注册到 metadata

    target = engine or get_engine()
    Base.metadata.create_all(bind=target)
    logger.info(
        "database_tables_created",
        extra={"table_count": len(Base.metadata.tables), "dialect": target.dialect.name},
    )


def drop_all_tables(engine: Engine | None = None) -> None:
    """删除所有表。仅用于测试。"""
    import app.db.models  # noqa: F401

    target = engine or get_engine()
    Base.metadata.drop_all(bind=target)


def check_database_health(settings: Settings) -> tuple[bool, str | None]:
    """探测数据库连通性，供 /health 使用。

    Returns:
        ``(是否健康, 错误信息)``。错误信息已脱敏（由调用方再过一次）。
    """
    try:
        engine = init_engine(settings)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 —— 探针需返回状态而非抛出
        return False, f"{type(exc).__name__}: {exc}"


__all__ = [
    "check_database_health",
    "create_all_tables",
    "dispose_engine",
    "drop_all_tables",
    "get_engine",
    "get_session",
    "get_session_factory",
    "init_engine",
    "session_scope",
]
