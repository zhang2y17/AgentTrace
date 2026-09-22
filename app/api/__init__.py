"""HTTP API 层。

分层规则（ARCHITECTURE §2 L1）：本包不允许直接引用 ``app.db.models``，
只能通过 ``app.services`` 或 ``app.db.repository`` 访问数据。
"""

from app.api import evaluations, health, metrics, quality_gates, runs

__all__ = ["evaluations", "health", "metrics", "quality_gates", "runs"]
