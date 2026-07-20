"""
Distributed Trace ID Management.

Provides request tracing across the audit logging system.
Integrates with OpenTelemetry and common tracing headers.
"""

import contextvars
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import structlog

logger = structlog.get_logger()

# Context variable for async-safe trace ID storage
_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)

# Thread-local fallback for non-async code
_thread_local = threading.local()

# Flag to enable/disable cluster prefix in trace IDs
_cluster_prefix_enabled = True


def generate_trace_id(include_cluster_prefix: bool = True) -> str:
    """
    Generate a new trace ID with optional cluster prefix.

    Format with cluster prefix: "req-{cluster_prefix}-{uuid4_short}"
    Example: "req-seop-a1b2c3d4" (seoul + production)

    Format without cluster prefix: "req-{uuid4_short}"
    Example: "req-a1b2c3d4"

    The cluster prefix includes:
    - First 3 characters of region (or "unk" if not set)
    - First character of environment (or "u" if not set)

    This ensures trace IDs from different clusters are distinguishable,
    reducing collision probability in multi-cluster environments.

    Args:
        include_cluster_prefix: Whether to include cluster prefix (default: True)

    Returns:
        New unique trace ID
    """
    uuid_part = uuid.uuid4().hex[:8]

    if not include_cluster_prefix or not _cluster_prefix_enabled:
        return f"req-{uuid_part}"

    try:
        from baldur.core.cluster_identity import get_cluster_identity

        identity = get_cluster_identity()
        prefix = identity.trace_id_prefix
        return f"req-{prefix}-{uuid_part}"
    except Exception:
        # Fallback to basic format if cluster identity not available
        return f"req-{uuid_part}"


def set_cluster_prefix_enabled(enabled: bool) -> None:
    """
    Enable or disable cluster prefix in trace IDs.

    Args:
        enabled: True to include cluster prefix, False to disable
    """
    global _cluster_prefix_enabled
    _cluster_prefix_enabled = enabled


def get_cluster_prefix_enabled() -> bool:
    """
    Check if cluster prefix is enabled in trace IDs.

    Returns:
        True if cluster prefix is enabled, False otherwise
    """
    return _cluster_prefix_enabled


def get_trace_id() -> str:
    """
    Get the current trace ID.

    Checks in order:
    1. OpenTelemetry span context (if OTEL enabled)
    2. Context variable (async-safe)
    3. Thread-local storage
    4. Generates new if none exists

    Returns:
        Current trace ID
    """
    # Try OpenTelemetry first (if enabled)
    otel_trace_id = _get_trace_id_from_otel()
    if otel_trace_id:
        return otel_trace_id

    # Try context variable (async-safe)
    trace_id = _trace_id_var.get()
    if trace_id:
        return trace_id

    # Try thread-local
    trace_id = getattr(_thread_local, "trace_id", None)
    if trace_id:
        return trace_id

    # Generate new one
    new_id = generate_trace_id()
    set_trace_id(new_id)
    return new_id


def _get_trace_id_from_otel() -> str | None:
    """
    Extract trace ID from OpenTelemetry span context.

    Returns:
        Full W3C trace_id (32 hex chars) prefixed with 'req-' for UI display,
        or None if OTEL is not enabled or no active span.
    """
    try:
        from baldur.observability import (
            get_current_trace_id_from_otel,
            is_otel_enabled,
        )

        if not is_otel_enabled():
            return None

        full_trace_id = get_current_trace_id_from_otel()
        if full_trace_id:
            # Return short format for display compatibility
            # Full ID stored in OTEL context, short for logs
            return f"req-{full_trace_id[:8]}"
    except ImportError:
        pass
    except Exception:
        pass

    return None


def get_trace_id_full() -> str | None:
    """
    Get the full W3C format trace ID (32 hex characters) from OTEL.

    Returns:
        Full 32-character hex trace_id if OTEL enabled, None otherwise.
        Use this for internal storage (Loki/Tempo) to avoid collision.
    """
    try:
        from baldur.observability import (
            get_current_trace_id_from_otel,
            is_otel_enabled,
        )

        if is_otel_enabled():
            return get_current_trace_id_from_otel()
    except ImportError:
        pass
    except Exception:
        pass

    return None


def peek_trace_context() -> dict[str, str | None]:
    """
    Non-generating snapshot of the currently-active trace context.

    Unlike :func:`get_trace_id`, this NEVER generates-and-sets a fresh id when
    no trace is active — it returns all-None instead. Use it at capture time
    (e.g. storing a failed operation into the DLQ) to record the origin trace
    without fabricating one on a no-trace capture.

    Layers, in the same precedence as ``get_trace_id``:
    1. OpenTelemetry span context (if OTEL enabled and a span is active)
    2. Context variable (async-safe)
    3. Thread-local storage

    When an OTEL span is active, the full W3C ``trace_id_full`` (32 hex) and
    ``span_id`` (16 hex) are included so a downstream consumer can rebuild a
    full ``SpanContext`` for an OTEL span link. Context-var / thread-local
    sources carry only the display ``trace_id``.

    Returns:
        dict with ``trace_id`` (display id, ``req-xxx`` / ``CELERY_xxx``),
        ``trace_id_full`` (32-hex W3C, OTEL only), and ``span_id`` (16-hex,
        OTEL only) — all None when no trace is active.
    """
    result: dict[str, str | None] = {
        "trace_id": None,
        "trace_id_full": None,
        "span_id": None,
    }

    # OTEL first (matches get_trace_id precedence) — non-generating peek.
    otel_trace_id = _get_trace_id_from_otel()
    if otel_trace_id:
        result["trace_id"] = otel_trace_id
        try:
            from baldur.observability import (
                get_current_span_id_from_otel,
                get_current_trace_id_from_otel,
                is_otel_enabled,
            )

            if is_otel_enabled():
                result["trace_id_full"] = get_current_trace_id_from_otel()
                result["span_id"] = get_current_span_id_from_otel()
        except ImportError:
            pass
        except Exception:
            pass
        return result

    # Context variable (async-safe) then thread-local — display id only.
    trace_id = _trace_id_var.get() or getattr(_thread_local, "trace_id", None)
    if trace_id:
        result["trace_id"] = trace_id

    return result


def extract_origin_trace(metadata: dict | None) -> dict[str, str | None]:
    """
    Read origin-trace keys captured at store time from a stored metadata dict.

    Companion read-side helper to :func:`peek_trace_context` (the capture
    side). Reads the three origin keys via plain ``.get()``; guards ONLY the
    non-dict / None shape, returning all-None there.

    A truncation marker that carries the origin keys still yields them (the
    capture path injects the keys *after* size-cap truncation, so a
    truncated-but-traced entry links), while a marker without origin keys — or
    a pre-existing entry, or a no-trace capture — yields all-None naturally via
    the ``.get()`` miss. Markers are not actively rejected: doing so would
    discard successfully-captured origin keys.

    Args:
        metadata: The stored entry ``metadata`` dict (or None / non-dict).

    Returns:
        dict with ``origin_trace_id``, ``origin_trace_id_full``, and
        ``origin_span_id`` — all None when metadata is None / not a dict or
        carries no origin keys.
    """
    if not isinstance(metadata, dict):
        return {
            "origin_trace_id": None,
            "origin_trace_id_full": None,
            "origin_span_id": None,
        }
    return {
        "origin_trace_id": metadata.get("origin_trace_id"),
        "origin_trace_id_full": metadata.get("origin_trace_id_full"),
        "origin_span_id": metadata.get("origin_span_id"),
    }


def set_trace_id(trace_id: str) -> None:
    """
    Set the current trace ID.

    Sets in both context variable and thread-local for compatibility.

    Args:
        trace_id: The trace ID to set
    """
    _trace_id_var.set(trace_id)
    _thread_local.trace_id = trace_id


def clear_trace_id() -> None:
    """Clear the current trace ID."""
    _trace_id_var.set(None)
    _thread_local.trace_id = None


def extract_trace_id_from_request(request) -> str | None:
    """
    Extract trace ID from a Django request.

    Checks in order:
    1. OpenTelemetry span context (if OTEL enabled and has active span)
    2. X-Request-ID header
    3. X-Trace-ID header
    4. X-Correlation-ID header
    5. traceparent (W3C Trace Context)
    6. X-Amzn-Trace-Id (AWS X-Ray)

    Args:
        request: Django HttpRequest object

    Returns:
        Trace ID if found, None otherwise
    """
    # Try OpenTelemetry first (if Django instrumentation created a span)
    otel_trace_id = _get_trace_id_from_otel()
    if otel_trace_id:
        return otel_trace_id

    # Fallback to header-based extraction
    headers_to_check = [
        "HTTP_X_REQUEST_ID",
        "HTTP_X_TRACE_ID",
        "HTTP_X_CORRELATION_ID",
        "HTTP_TRACEPARENT",
        "HTTP_X_AMZN_TRACE_ID",
    ]

    meta = getattr(request, "META", {})

    for header in headers_to_check:
        value = meta.get(header)
        if value:
            # For traceparent, extract the trace-id portion
            if header == "HTTP_TRACEPARENT":
                # Format: version-trace_id-parent_id-flags
                parts = value.split("-")
                if len(parts) >= 2:
                    return f"req-{parts[1][:8]}"
            # For AWS X-Ray, extract the trace ID
            elif header == "HTTP_X_AMZN_TRACE_ID":
                # Format: Root=1-xxx-yyy;Parent=zzz;Sampled=1
                if "Root=" in value:
                    root = value.split("Root=")[1].split(";")[0]
                    return f"req-{root[-8:]}"
            else:
                return value

    return None


class TraceContext:
    """
    Context manager for trace ID scoping.

    Usage:
        with TraceContext("req-12345"):
            # All audit logs in this block will have this trace ID
            do_something()

        # Or auto-generate:
        with TraceContext() as trace_id:
            print(f"Using trace: {trace_id}")
    """

    def __init__(self, trace_id: str | None = None):
        """
        Initialize trace context.

        Args:
            trace_id: Optional trace ID to use (auto-generated if not provided)
        """
        self.trace_id = trace_id or generate_trace_id()
        self._previous_trace_id: str | None = None

    def __enter__(self) -> str:
        """Enter context and set trace ID."""
        self._previous_trace_id = _trace_id_var.get()
        set_trace_id(self.trace_id)
        return self.trace_id

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context and restore previous trace ID."""
        if self._previous_trace_id:
            set_trace_id(self._previous_trace_id)
        else:
            clear_trace_id()


def trace_id_middleware(get_response):
    """
    Django middleware for automatic trace ID handling.

    Extracts or generates trace ID for each request and adds it to response.

    Usage in settings.py:
        MIDDLEWARE = [
            'baldur.audit.trace.trace_id_middleware',
            # ... other middleware
        ]
    """

    def middleware(request):
        # Extract or generate trace ID
        trace_id = extract_trace_id_from_request(request) or generate_trace_id()
        set_trace_id(trace_id)

        # Store on request for easy access
        request.trace_id = trace_id

        try:
            # Process request
            response = get_response(request)

            # Add trace ID to response headers
            response["X-Request-ID"] = trace_id

            return response
        finally:
            # Clear trace ID after request — always clean up, even on exception
            clear_trace_id()

    return middleware


# =============================================================================
# Celery Task trace_id propagation and restoration
# =============================================================================


# =============================================================================
# Celery Task trace_id standardization
# =============================================================================

# Variable holding the Celery context
_celery_context_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "celery_context", default=None
)


def generate_celery_trace_id(task_id: str) -> str:
    """
    Generate a trace_id from a Celery Task ID.

    When OTEL is enabled, OTEL's trace_id takes precedence.
    When OTEL is disabled, the "CELERY_{task_id}" format is used.

    This format gives:
    - Direct search by task_id in the Flower UI
    - The same trace_id preserved across retries
    - 1:1 matching between audit logs and Celery Tasks

    Args:
        task_id: Celery Task ID (e.g. "7483abc-1234-...")

    Returns:
        str: trace_id (OTEL format or "CELERY_{task_id}" format)

    Example:
        >>> generate_celery_trace_id("7483abc-1234-5678-90ab-cdef12345678")
        "CELERY_7483abc-1234-5678-90ab-cdef12345678"  # OTEL disabled
        "req-a1b2c3d4"  # OTEL enabled (extracted from the current span)
    """
    # When OTEL is enabled, prefer the current span's trace_id
    otel_trace_id = _get_trace_id_from_otel()
    if otel_trace_id:
        return otel_trace_id

    if not task_id:
        # Fallback: no task_id — generate the conventional way
        return f"CELERY_{generate_trace_id()}"
    return f"CELERY_{task_id}"


def get_celery_trace_id_with_otel_context(task_id: str) -> dict[str, str | None]:
    """
    Return a Celery Task's trace info together with the OTEL context.

    When OTEL is enabled, the full W3C trace_id and span_id are included too.

    Args:
        task_id: Celery Task ID

    Returns:
        dict: {
            "trace_id": display trace_id (req-xxx or CELERY_xxx),
            "trace_id_full": full W3C trace_id (32 hex chars, OTEL enabled),
            "span_id": current span_id (16 hex chars, OTEL enabled),
            "celery_task_id": original Celery task_id
        }
    """
    result: dict[str, str | None] = {
        "trace_id": generate_celery_trace_id(task_id),
        "trace_id_full": None,
        "span_id": None,
        "celery_task_id": task_id,
    }

    try:
        from baldur.observability import (
            get_current_span_id_from_otel,
            get_current_trace_id_from_otel,
            is_otel_enabled,
        )

        if is_otel_enabled():
            result["trace_id_full"] = get_current_trace_id_from_otel()
            result["span_id"] = get_current_span_id_from_otel()
    except ImportError:
        pass
    except Exception:
        pass

    return result


def set_celery_context(
    task_id: str,
    task_name: str,
    retries: int = 0,
) -> None:
    """
    Set the current Celery Task context.

    Called from the task_prerun signal and kept for the Task's execution.

    Args:
        task_id: Celery Task ID
        task_name: Celery Task name
            (e.g. "baldur.adapters.celery.tasks.replay_single_dlq_entry")
        retries: Current retry count
    """
    context = {
        "task_id": task_id,
        "task_name": task_name,
        "retries": retries,
    }
    _celery_context_var.set(context)

    # Set the trace_id as well
    trace_id = generate_celery_trace_id(task_id)
    set_trace_id(trace_id)


def get_celery_context() -> dict | None:
    """
    Return the current Celery Task context.

    Returns:
        dict: {"task_id": ..., "task_name": ..., "retries": ...} or None
    """
    return _celery_context_var.get()


def clear_celery_context() -> None:
    """
    Clear the Celery Task context.

    Called from the task_postrun signal to prevent a previous context from
    lingering when a Worker is reused.
    """
    _celery_context_var.set(None)
    clear_trace_id()


def is_celery_task() -> bool:
    """
    Check whether the current execution context is inside a Celery Task.

    Returns:
        bool: True if inside a Celery Task
    """
    return _celery_context_var.get() is not None


def get_trace_for_celery() -> dict[str, Any]:
    """
    Return the trace info to hand off to a Celery Task.

    When called from an HTTP request context, the current trace_id is
    included. Inside the Celery Task it can be restored with
    restore_trace_from_celery().

    Returns:
        dict: dictionary carrying the trace_id and source info

    Example:
        # Calling a Task from a View
        from baldur.audit.trace import get_trace_for_celery

        replay_single_dlq_entry.delay(
            dlq_id=pk,
            trace_info=get_trace_for_celery(),
        )
    """
    current_trace_id = _trace_id_var.get() or getattr(_thread_local, "trace_id", None)

    return {
        "trace_id": current_trace_id,
        "source": "celery_propagated",
    }


@contextmanager
def restore_trace_from_celery(
    trace_info: dict[str, Any] | None = None,
    celery_task_id: str | None = None,
    celery_task_name: str | None = None,
) -> Generator[str, None, None]:
    """
    Restore — or self-generate — the trace context inside a Celery Task.

    Precedence:
    1. Use trace_info's trace_id if present (HTTP → Celery propagation)
    2. Generate CELERY_{task_id} if celery_task_id is present
    3. Generate CELERY_{uuid} if neither is present (Fallback)

    Note:
        Once the task_prerun signal is enabled, this function no longer needs
        to be called manually. Kept for backward compatibility.

    Args:
        trace_info: Trace info propagated from the HTTP request (optional)
        celery_task_id: Celery Task ID (optional, self.request.id)
        celery_task_name: Celery Task name (optional)

    Yields:
        str: The trace_id currently in use
    """
    if trace_info and trace_info.get("trace_id"):
        # Use the trace_id propagated from the HTTP request
        trace_id = trace_info["trace_id"]
    elif celery_task_id:
        # Generate from the Celery Task ID
        trace_id = generate_celery_trace_id(celery_task_id)
    else:
        # Fallback: generate from a UUID
        trace_id = f"CELERY_{generate_trace_id()}"

    # Set the Celery context (when available)
    if celery_task_id:
        set_celery_context(
            task_id=celery_task_id,
            task_name=celery_task_name or "unknown",
            retries=0,
        )

    try:
        with TraceContext(trace_id) as active_trace_id:
            yield active_trace_id
    finally:
        if celery_task_id:
            clear_celery_context()
