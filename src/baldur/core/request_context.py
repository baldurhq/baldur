"""
Request Context for Graceful Shutdown

Provides context manager and utilities for request tracking.
Framework adapters can use this for integration.
"""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from .shutdown_coordinator import RequestTracker, TrackedRequest


class RequestLifecycleContext:
    """
    Context for tracking a single request lifecycle.

    Usage:
        tracker = RequestTracker()

        with RequestLifecycleContext(tracker, endpoint="/api/orders") as ctx:
            # Process request
            result = process_order()
            ctx.set_metadata("order_id", result.id)
    """

    def __init__(
        self,
        tracker: RequestTracker,
        request_id: str | None = None,
        endpoint: str = "",
        method: str = "",
        metadata: dict[str, Any] | None = None,
    ):
        self._tracker = tracker
        self._request_id = request_id or str(uuid.uuid4())
        self._endpoint = endpoint
        self._method = method
        self._metadata = metadata or {}
        self._tracked: TrackedRequest | None = None
        self._success = True

    @property
    def request_id(self) -> str:
        return self._request_id

    def set_metadata(self, key: str, value: Any) -> None:
        """Add metadata to the request"""
        self._metadata[key] = value
        if self._tracked:
            self._tracked.metadata[key] = value

    def mark_failed(self) -> None:
        """Mark the request as failed"""
        self._success = False

    def __enter__(self) -> "RequestLifecycleContext":
        self._tracked = self._tracker.start_request(
            request_id=self._request_id,
            endpoint=self._endpoint,
            method=self._method,
            metadata=self._metadata,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            self._success = False

        self._tracker.end_request(
            request_id=self._request_id,
            success=self._success,
        )


@contextmanager
def track_request(
    tracker: RequestTracker,
    request_id: str | None = None,
    endpoint: str = "",
    method: str = "",
) -> Generator[RequestLifecycleContext, None, None]:
    """
    Context manager for request tracking.

    Usage:
        with track_request(tracker, endpoint="/api/pay") as ctx:
            process_payment()
    """
    ctx = RequestLifecycleContext(
        tracker=tracker,
        request_id=request_id,
        endpoint=endpoint,
        method=method,
    )
    with ctx:
        yield ctx
