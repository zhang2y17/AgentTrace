"""Redis 短期任务状态。

**契约 T6 的强约束：Redis 只用于短期任务状态与可选事件队列，绝不缓存最终答案。**

为什么这条约束重要：如果缓存了最终答案，重复评测同一个 case 会直接命中缓存，
导致（1）延迟指标失真（变快）、（2）成本指标失真（变低）、
（3）模型更新后指标不反映真实变化。这三条都会让评测失去意义。

因此本模块只提供：
- 运行进度状态（供长任务的进度查询）
- 短期去重标记

不提供：答案缓存、模型响应缓存、评测结果缓存。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import AgentTraceError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 短期状态的默认存活时间（秒）。短于 1 小时，确保不会长期占用内存。
STATUS_TTL_SECONDS = 900


class CacheUnavailableError(AgentTraceError):
    """Redis 不可用。"""

    error_code = "CACHE_UNAVAILABLE"
    http_status = 503


class TaskStateCache:
    """运行进度状态缓存。

    所有方法在 Redis 不可用时：
    - ``settings.redis_required=False``（默认）：记录警告并**降级为无操作**，
      因为短期状态属于可选能力，不应阻断核心的 Trace 记录与评测功能；
    - ``settings.redis_required=True``：抛出 ``CacheUnavailableError``。
    """

    # 键前缀。只用状态类语义，杜绝出现 "answer:" / "result:" 这类前缀。
    _PREFIX = "agenttrace:task_state:"

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._available: bool | None = None

    # ------------------------------------------------------------ 连接
    @property
    def client(self) -> Any | None:
        """惰性创建 Redis 客户端。

        惰性创建的原因：默认测试路径不启动 Redis，模块导入阶段不应尝试连接。

        超时设置很短（默认 2 秒）：/health 必须快速返回，
        不能因为 Redis 不可达而让健康检查挂住数秒。
        """
        if self._client is not None:
            return self._client

        try:
            import redis

            self._client = redis.Redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=self.settings.redis_connect_timeout_seconds,
                socket_timeout=self.settings.redis_connect_timeout_seconds,
                # 连接失败不重试：健康检查需要快速失败而非等待重试
                retry_on_timeout=False,
                decode_responses=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "redis_client_creation_failed",
                extra={"error_type": type(exc).__name__},
            )
            self._client = None

        return self._client

    def _handle_failure(self, operation: str, exc: Exception) -> None:
        """统一处理 Redis 故障：按配置决定降级还是抛错。"""
        self._available = False
        logger.warning(
            "redis_operation_failed",
            extra={"operation": operation, "error_type": type(exc).__name__},
        )
        if self.settings.redis_required:
            raise CacheUnavailableError(
                "Redis 不可用，且配置要求 Redis 必须可用。",
                details={"operation": operation},
            ) from exc

    # ------------------------------------------------------------ 状态读写
    def set_task_state(
        self,
        run_id: str,
        *,
        status: str,
        node_name: str | None = None,
        progress: float | None = None,
        ttl_seconds: int = STATUS_TTL_SECONDS,
    ) -> bool:
        """写入运行进度状态。

        Returns:
            写入成功为 True；Redis 不可用而降级时为 False。
        """
        client = self.client
        if client is None:
            return False

        payload = {
            "run_id": run_id,
            "status": status,
            "node_name": node_name,
            "progress": progress,
        }

        try:
            client.setex(
                f"{self._PREFIX}{run_id}",
                ttl_seconds,
                json.dumps(payload, ensure_ascii=False),
            )
            self._available = True
            return True
        except Exception as exc:  # noqa: BLE001
            self._handle_failure("set_task_state", exc)
            return False

    def get_task_state(self, run_id: str) -> dict[str, Any] | None:
        """读取运行进度状态。Redis 不可用或键不存在时返回 None。"""
        client = self.client
        if client is None:
            return None

        try:
            raw = client.get(f"{self._PREFIX}{run_id}")
            self._available = True
        except Exception as exc:  # noqa: BLE001
            self._handle_failure("get_task_state", exc)
            return None

        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("redis_task_state_corrupted", extra={"run_id": run_id})
            return None

    def clear_task_state(self, run_id: str) -> bool:
        """删除运行进度状态（运行结束时调用）。"""
        client = self.client
        if client is None:
            return False

        try:
            client.delete(f"{self._PREFIX}{run_id}")
            self._available = True
            return True
        except Exception as exc:  # noqa: BLE001
            self._handle_failure("clear_task_state", exc)
            return False

    # ------------------------------------------------------------ 健康检查
    def ping(self) -> tuple[bool, str | None]:
        """探测 Redis 连通性，供 /health 使用。"""
        client = self.client
        if client is None:
            return False, "redis client unavailable"

        try:
            client.ping()
            self._available = True
            return True, None
        except Exception as exc:  # noqa: BLE001
            self._available = False
            return False, f"{type(exc).__name__}: {exc}"


def check_redis_health(settings: Settings) -> tuple[bool, str | None]:
    """模块级 Redis 健康探测函数，供健康探针注册使用。"""
    return TaskStateCache(settings).ping()


__all__ = [
    "STATUS_TTL_SECONDS",
    "CacheUnavailableError",
    "TaskStateCache",
    "check_redis_health",
]
