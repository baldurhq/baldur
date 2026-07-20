"""
Cascade Auditor shared helpers.

Unifies repeated patterns such as index access and backend lookup.
"""

from __future__ import annotations

from typing import Any


def get_index_ids(backend: Any, index_key: str) -> list[str]:
    """
    Unified helper that extracts the ID list from an index.

    Unifies the pattern that was repeated six times across the existing code:
        index_data if isinstance(index_data, list) else index_data.get("ids", [])

    Args:
        backend: State backend instance
        index_key: Index Redis key

    Returns:
        List of cascade IDs
    """
    index_data = backend.get(index_key)
    if not index_data:
        return []
    return index_data if isinstance(index_data, list) else index_data.get("ids", [])
