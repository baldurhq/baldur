"""
DLQ List Operations Mixin.

Provides methods for listing and getting DLQ entries.
Uses Repository pattern for domain-free architecture.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

__all__ = ["ListOperationsMixin"]


class ListOperationsMixin:
    """Mixin providing DLQ list operations using Repository pattern."""

    def list_entries(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Get paginated list of DLQ entries.

        Delegates pagination to the repository-native ``find``/``count``
        primitive: storage-layer offset/limit instead of
        load-everything-then-slice, and a no-status filter spans ALL statuses
        (escalated/terminal entries are visible by default), not a hardcoded
        subset.

        Args:
            filters: Dictionary with filter conditions
                - status: Filter by status
                - domain: Filter by domain
                - failure_type: Filter by failure type
            page: Page number (default 1, clamped to >= 1)
            page_size: Items per page (default 20, max 100)

        Returns:
            Dict with entries and pagination info:
                - results: List of entry dicts
                - page: Current page
                - page_size: Items per page
                - total_pages: Total number of pages
                - total_count: Total number of items
                - has_next: Whether there's a next page
                - has_previous: Whether there's a previous page
        """
        filters = filters or {}
        # Clamp BEFORE the offset computation: a zero/negative page would make
        # offset negative, which Redis zrevrange reads as from-the-end and SQL
        # OFFSET rejects (541 D2). Keep the existing 100 page_size upper bound.
        page_size = min(page_size, 100)
        page = max(page, 1)

        status_filter = filters.get("status") or None
        domain_filter = filters.get("domain") or None
        failure_type_filter = filters.get("failure_type") or None

        offset = (page - 1) * page_size
        limit = page_size

        try:
            total_count = self.repository.count(
                status=status_filter,
                domain=domain_filter,
                failure_type=failure_type_filter,
            )
            entries = self.repository.find(
                status=status_filter,
                domain=domain_filter,
                failure_type=failure_type_filter,
                offset=offset,
                limit=limit,
            )

            total_pages = max(1, (total_count + page_size - 1) // page_size)

            results = []
            for entry in entries:
                results.append(
                    {
                        "id": entry.id,
                        "domain": entry.domain,
                        "failure_type": entry.failure_type,
                        "status": entry.status,
                        "retry_count": entry.retry_count,
                        "created_at": (
                            entry.created_at.isoformat() if entry.created_at else None
                        ),
                        "resolved_at": (
                            entry.resolved_at.isoformat() if entry.resolved_at else None
                        ),
                    }
                )

            return {
                "results": results,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_count": total_count,
                "has_next": page < total_pages,
                "has_previous": page > 1,
            }

        except Exception as e:
            logger.exception(
                "dlq.list_query_failed",
                error=e,
            )
            return {
                "results": [],
                "page": page,
                "page_size": page_size,
                "total_pages": 0,
                "total_count": 0,
                "has_next": False,
                "has_previous": False,
            }
