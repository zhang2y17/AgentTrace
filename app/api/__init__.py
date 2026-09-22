"""HTTP API 层。

分层规则（ARCHITECTURE §2 L1）：本包不允许直接引用 ``app.db.models``，
只能通过 ``app.services`` 或 ``app.db.repository`` 访问数据。
"""

from app.api import health, metrics, runs

__all__ = ["health", "metrics", "runs"]
