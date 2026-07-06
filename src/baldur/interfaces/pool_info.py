"""
Pool Info Provider Interface.

Abstract interface for SQLAlchemy/dj-db-conn-pool connection-pool stats
discovery. Each implementation pulls pool state from a different source
(Django ``connections["default"]``, a directly-passed SQLAlchemy engine,
or nothing), so the ABC + concrete-subclass split is preserved here
rather than collapsed via callable injection (different algorithms, not
just different cursor acquisition).

Replaces the bare ``baldur.adapters.sqlalchemy_pool.get_pool_info`` Django-
hard-coupled discovery path.

Note:
    The dict shape returned by :meth:`PoolInfoProvider.get_pool_info` is
    distinct from ``baldur_pro.services.pool_monitor.PoolStats`` (the
    dataclass-shaped PRO contract). Different consumers, different shapes
    — the two registries coexist intentionally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["PoolInfoProvider"]


class PoolInfoProvider(ABC):
    """Abstract interface for connection-pool stats discovery (OSS dict shape).

    Concrete implementations return the existing OSS dict shape with keys:
    ``pool_type``, ``pool_size``, ``max_overflow``, ``checkedin``,
    ``checkedout``, ``overflow``, ``total_capacity``, ``available``,
    ``pool_exhausted``. An empty dict signals "no pool detected".
    """

    @abstractmethod
    def get_pool_info(self) -> dict[str, Any]:
        """Return pool stats as a dict (empty when no pool is reachable)."""
        ...
