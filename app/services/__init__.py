"""应用服务层。

职责（ARCHITECTURE §2）：跨层编排，掌握事务边界。
HTTP 层只做参数解析与错误映射，数据访问统一经由此层。
"""

from app.services.cache import TaskStateCache, check_redis_health

# 注意：``run_service`` 与 ``metrics_service`` 刻意**不**在这里 eager import。
# 它们间接依赖 app.agent / app.tools / app.db，而 ``app.services.cache``
# 是 /health 的依赖项 —— 若在此处把它们拉进来，任何一次健康检查
# 都会连带加载整条 Agent 依赖链，让"看看服务活着没"这种轻量探针
# 变得沉重且容易因无关模块的导入错误而失败。
# 调用方按需 ``from app.services.run_service import RunService`` 即可。

__all__ = ["TaskStateCache", "check_redis_health"]
