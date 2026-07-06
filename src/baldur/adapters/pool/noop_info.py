"""
No-op PoolInfoProvider implementation.

Returns an empty dict for runtimes that have no reachable connection pool.
"""

from __future__ import annotations

from typing import Any

from baldur.interfaces.pool_info import PoolInfoProvider

__all__ = ["NoopPoolInfoProvider"]


class NoopPoolInfoProvider(PoolInfoProvider):
    """PoolInfoProvider that always returns an empty dict."""

    def get_pool_info(self) -> dict[str, Any]:
        return {}
