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
            # Clear trace ID after request — 예외 발생 시에도 반드시 정리
            clear_trace_id()

    return middleware


# =============================================================================
# Celery Task trace_id 전파 및 복원
# =============================================================================


# =============================================================================
# Celery Task trace_id 표준화
# =============================================================================

# Celery 컨텍스트 저장용 변수
_celery_context_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "celery_context", default=None
)


def generate_celery_trace_id(task_id: str) -> str:
    """
    Celery Task ID를 기반으로 trace_id를 생성합니다.

    OTEL이 활성화된 경우 OTEL의 trace_id를 우선 사용합니다.
    OTEL이 비활성화된 경우 "CELERY_{task_id}" 형식을 사용합니다.

    이 형식을 사용하면:
    - Flower UI에서 task_id로 직접 검색 가능
    - 재시도 시에도 동일한 trace_id 유지
    - Audit 로그에서 Celery Task와 1:1 매칭

    Args:
        task_id: Celery Task ID (예: "7483abc-1234-...")

    Returns:
        str: trace_id (OTEL 형식 또는 "CELERY_{task_id}" 형식)

    Example:
        >>> generate_celery_trace_id("7483abc-1234-5678-90ab-cdef12345678")
        "CELERY_7483abc-1234-5678-90ab-cdef12345678"  # OTEL 비활성화 시
        "req-a1b2c3d4"  # OTEL 활성화 시 (현재 span에서 추출)
    """
    # OTEL 활성화 시 현재 span의 trace_id 우선 사용
    otel_trace_id = _get_trace_id_from_otel()
    if otel_trace_id:
        return otel_trace_id

    if not task_id:
        # Fallback: task_id가 없으면 기존 방식으로 생성
        return f"CELERY_{generate_trace_id()}"
    return f"CELERY_{task_id}"


def get_celery_trace_id_with_otel_context(task_id: str) -> dict[str, str | None]:
    """
    Celery Task의 trace 정보를 OTEL 컨텍스트와 함께 반환합니다.

    OTEL이 활성화된 경우 전체 W3C trace_id와 span_id도 포함합니다.

    Args:
        task_id: Celery Task ID

    Returns:
        dict: {
            "trace_id": 표시용 trace_id (req-xxx 또는 CELERY_xxx),
            "trace_id_full": 전체 W3C trace_id (32자 hex, OTEL 활성화 시),
            "span_id": 현재 span_id (16자 hex, OTEL 활성화 시),
            "celery_task_id": 원본 Celery task_id
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
    현재 Celery Task 컨텍스트를 설정합니다.

    task_prerun 시그널에서 호출되어 Task 실행 동안 유지됩니다.

    Args:
        task_id: Celery Task ID
        task_name: Celery Task 이름 (예: "baldur.adapters.celery.tasks.replay_single_dlq_entry")
        retries: 현재 재시도 횟수
    """
    context = {
        "task_id": task_id,
        "task_name": task_name,
        "retries": retries,
    }
    _celery_context_var.set(context)

    # trace_id도 함께 설정
    trace_id = generate_celery_trace_id(task_id)
    set_trace_id(trace_id)


def get_celery_context() -> dict | None:
    """
    현재 Celery Task 컨텍스트를 반환합니다.

    Returns:
        dict: {"task_id": ..., "task_name": ..., "retries": ...} 또는 None
    """
    return _celery_context_var.get()


def clear_celery_context() -> None:
    """
    Celery Task 컨텍스트를 정리합니다.

    task_postrun 시그널에서 호출되어 Worker 재사용 시 이전 컨텍스트 잔존을 방지합니다.
    """
    _celery_context_var.set(None)
    clear_trace_id()


def is_celery_task() -> bool:
    """
    현재 실행 컨텍스트가 Celery Task 내부인지 확인합니다.

    Returns:
        bool: Celery Task 내부이면 True
    """
    return _celery_context_var.get() is not None


def get_trace_for_celery() -> dict[str, Any]:
    """
    Celery Task에 전달할 trace 정보를 반환합니다.

    HTTP 요청 컨텍스트에서 호출 시 현재 trace_id를 포함하여 반환합니다.
    Celery Task 내에서 restore_trace_from_celery()로 복원할 수 있습니다.

    Returns:
        dict: trace_id와 source 정보를 담은 딕셔너리

    Example:
        # View에서 Task 호출 시
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
    Celery Task에서 trace 컨텍스트를 복원하거나 자체 생성합니다.

    우선순위:
    1. trace_info에 trace_id가 있으면 사용 (HTTP → Celery 전파)
    2. celery_task_id가 있으면 CELERY_{task_id} 생성
    3. 둘 다 없으면 CELERY_{uuid} 생성 (Fallback)

    Note:
        task_prerun 시그널이 활성화되면 이 함수는 더 이상 수동 호출 불필요.
        하위 호환성을 위해 유지됨.

    Args:
        trace_info: HTTP 요청에서 전파된 trace 정보 (optional)
        celery_task_id: Celery Task ID (optional, self.request.id)
        celery_task_name: Celery Task 이름 (optional)

    Yields:
        str: 현재 사용 중인 trace_id
    """
    if trace_info and trace_info.get("trace_id"):
        # HTTP 요청에서 전파된 trace_id 사용
        trace_id = trace_info["trace_id"]
    elif celery_task_id:
        # Celery Task ID 기반 생성
        trace_id = generate_celery_trace_id(celery_task_id)
    else:
        # Fallback: UUID 기반 생성
        trace_id = f"CELERY_{generate_trace_id()}"

    # Celery 컨텍스트 설정 (있는 경우)
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
