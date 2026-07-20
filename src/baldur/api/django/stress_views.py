"""
Baldur Pool Stress Test Endpoints.

These endpoints intentionally exhaust the DB connection pool.
Test-only — never use them in production!

Note:
- Business logic is split out into StressTestService
  (services/stress_test_service.py)
- Views only handle request/response
"""

import json

import structlog
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from baldur.services.stress_test_service import get_stress_test_service
from baldur.settings.stress_test import get_stress_test_settings

logger = structlog.get_logger()


# =============================================================================
# Backward Compatibility - Re-export get_pool_info
# =============================================================================


def get_pool_info():
    """Look up SQLAlchemy pool info (backward compatibility)."""
    service = get_stress_test_service()
    return service.get_pool_info()


# =============================================================================
# Slow Query Endpoints
# =============================================================================


@require_GET
def slow_query_5s(request):
    """
    Slow query that holds a DB connection for 5 seconds.

    GET /api/baldur/stress/slow-5s/
    """
    service = get_stress_test_service()
    result = service.execute_slow_query(seconds=5)

    if result.status in ("pool_exhausted", "error"):
        return JsonResponse(result.to_dict(), status=503)
    return JsonResponse(result.to_dict())


@require_GET
def slow_query_10s(request):
    """
    Very slow query that holds a DB connection for 10 seconds.

    GET /api/baldur/stress/slow-10s/
    """
    service = get_stress_test_service()
    result = service.execute_slow_query(seconds=10)

    if result.status in ("pool_exhausted", "error"):
        return JsonResponse(result.to_dict(), status=503)
    return JsonResponse(result.to_dict())


@require_GET
def connection_leak_simulation(request):
    """
    Simulation that intentionally "leaks" connections.
    Opens connections and holds them without closing.

    GET /api/baldur/stress/leak/

    ⚠️ Test-only! Never use in production!
    """
    settings = get_stress_test_settings()
    hold_seconds = int(request.GET.get("seconds", settings.default_leak_hold_seconds))

    service = get_stress_test_service()
    result = service.simulate_connection_leak(hold_seconds=hold_seconds)

    if result.status == "error":
        return JsonResponse(result.to_dict(), status=503)
    return JsonResponse(result.to_dict())


@require_GET
def pool_status(request):
    """
    Look up the current connection pool status.
    Returns the real pool state when an SQLAlchemy pool is in use, otherwise
    PostgreSQL statistics.

    GET /api/baldur/stress/pool-status/

    V3 Optimization: Uses multi-tier cache for P95 < 30ms target.
    Query Parameters:
    - nocache: Set to "true" to bypass cache
    """
    # V3: Check cache bypass
    use_cache = request.GET.get("nocache", "").lower() != "true"

    if use_cache:
        try:
            from baldur.services.precomputed_cache import get_cached_pool_status

            data = get_cached_pool_status()

            # Return 503 when the pool is exhausted
            if data.get("status") == "exhausted":
                return JsonResponse(data, status=503)
            return JsonResponse(data)
        except ImportError:
            pass  # Fall through to direct computation

    # Direct computation via service
    service = get_stress_test_service()
    result = service.get_pool_status()

    if result.status in ("exhausted", "error"):
        return JsonResponse(result.to_dict(), status=503)
    return JsonResponse(result.to_dict())


@require_GET
def heavy_concurrent_query(request):
    """
    Heavy query that JOINs several tables.

    GET /api/baldur/stress/heavy-query/
    """
    service = get_stress_test_service()
    result = service.execute_heavy_query()

    if result.status == "error":
        return JsonResponse(result.to_dict(), status=503)
    return JsonResponse(result.to_dict())


# =============================================================================
# Advisory Lock API - non-invasive DB lock testing
# =============================================================================


def _parse_lock_request_body(request) -> dict:
    """Parse lock request body with defaults."""
    settings = get_stress_test_settings()
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    return {
        "lock_id": int(body.get("lock_id", settings.default_lock_id)),
        "hold_seconds": min(
            int(body.get("hold_seconds", settings.default_lock_hold_seconds)),
            settings.max_lock_hold_seconds,
        ),
        "exclusive": body.get("exclusive", True),
        "wait": body.get("wait", True),
    }


@csrf_exempt
def advisory_lock_acquire(request):
    """
    Acquire a PostgreSQL advisory lock - non-invasive lock testing.

    POST /api/baldur/stress/advisory-lock/acquire/

    Produces lock contention purely at the DB engine level without touching
    any business data. This verifies the system's lock detection and
    recovery capabilities.

    Parameters:
        lock_id (int): Lock identifier (1-1000000). Concurrent requests with
            the same ID create contention
        hold_seconds (int): Lock hold time (1-60s, default: 5s)
        exclusive (bool): Whether the lock is exclusive (default: true)
        wait (bool): Whether to wait for the lock. If false, fails immediately
            (default: true)

    Response:
        - 200: Lock acquired
        - 409: Lock acquisition failed (held by another session, wait=false)
        - 503: DB error or timeout

    ⚠️ Test-only! Never use in production!
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    params = _parse_lock_request_body(request)

    service = get_stress_test_service()
    result = service.acquire_advisory_lock(
        lock_id=params["lock_id"],
        hold_seconds=params["hold_seconds"],
        exclusive=params["exclusive"],
        wait=params["wait"],
    )

    if result.status == "conflict":
        return JsonResponse(result.to_dict(), status=409)
    if result.status == "lock_timeout":
        return JsonResponse(result.to_dict(), status=423)  # Locked
    if result.status == "error":
        return JsonResponse(result.to_dict(), status=503)

    return JsonResponse(result.to_dict())


@csrf_exempt
def advisory_lock_contention(request):
    """
    Advisory lock contention simulation - many sessions compete for one lock.

    POST /api/baldur/stress/advisory-lock/contention/

    Repeatedly acquires and releases the same lock ID for the given duration.
    This simulates real DB lock contention.

    Parameters:
        lock_id (int): Lock identifier
        duration_seconds (int): Contention duration (1-30s, default: 5s)
        lock_hold_ms (int): Hold time per lock (ms, default: 100ms)

    Response:
        Contention statistics (success/failure counts, average wait time, etc.)
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    settings = get_stress_test_settings()
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    lock_id = int(body.get("lock_id", settings.contention_lock_id))
    duration_seconds = int(
        body.get("duration_seconds", settings.default_contention_duration_seconds)
    )
    lock_hold_ms = int(body.get("lock_hold_ms", settings.default_lock_hold_ms))

    service = get_stress_test_service()
    result = service.run_lock_contention(
        lock_id=lock_id,
        duration_seconds=duration_seconds,
        lock_hold_ms=lock_hold_ms,
    )

    if result.status == "error":
        return JsonResponse(result.to_dict(), status=503)

    return JsonResponse(result.to_dict())


@csrf_exempt
def controlled_burst_failure(request):
    """
    Controlled Burst Failure - stages "calm before the storm -> system
    collapse -> autonomous recovery".

    POST /api/baldur/stress/burst-failure/

    Generates extreme lock timeouts and load for the given duration, forcing
    the creation of 100+ DLQ entries.

    Parameters:
        lock_id (int): Advisory Lock ID
        lock_timeout_ms (int): Extremely short lock timeout (default: 1ms!)
        burst_duration_seconds (int): Burst duration (default: 10s)
        concurrent_locks (int): Concurrent lock attempts (default: 50)

    This API does the following:
    1. Shrinks lock_timeout to 1ms
    2. Attempts many concurrent lock acquisitions for the given duration
    3. Most requests fail with a timeout
    4. The failed requests are automatically routed to the DLQ

    ⚠️ Test-only! Intentionally induces failures in the system!
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    settings = get_stress_test_settings()
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    lock_id = int(body.get("lock_id", settings.burst_lock_id))
    lock_timeout_ms = int(body.get("lock_timeout_ms", settings.default_lock_timeout_ms))
    burst_duration_seconds = int(
        body.get("burst_duration_seconds", settings.default_burst_duration_seconds)
    )
    concurrent_locks = int(
        body.get("concurrent_locks", settings.default_concurrent_locks)
    )

    service = get_stress_test_service()
    result = service.run_controlled_burst_failure(
        lock_id=lock_id,
        lock_timeout_ms=lock_timeout_ms,
        burst_duration_seconds=burst_duration_seconds,
        concurrent_locks=concurrent_locks,
    )

    if result.status == "error":
        return JsonResponse(result.to_dict(), status=503)

    return JsonResponse(result.to_dict())


# =============================================================================
# Pool Exhaustion API - real pool exhaustion to trigger the CB
# =============================================================================


@csrf_exempt
def pool_exhaust(request):
    """
    Intentionally exhausts the DB connection pool to trigger the CB.

    POST /api/baldur/stress/pool-exhaust/

    Parameters:
        connections_to_hold (int): Connections to hold (default: 10)
        hold_seconds (int): Connection hold time (default: 30s, max 60s)

    This API:
    1. Opens and holds several DB connections
    2. Other requests fail with 503 because they cannot get a connection
    3. BaldurMiddleware detects those errors and flips the CB to OPEN
    4. Connections are released after the given time

    ⚠️ Test-only! Intentionally induces failures in the system!
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    settings = get_stress_test_settings()
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    connections_to_hold = int(
        body.get("connections_to_hold", settings.default_connections_to_hold)
    )
    hold_seconds = int(body.get("hold_seconds", settings.default_pool_hold_seconds))

    service = get_stress_test_service()
    result = service.exhaust_pool(
        connections_to_hold=connections_to_hold,
        hold_seconds=hold_seconds,
    )

    if result.status == "error":
        return JsonResponse(result.to_dict(), status=503)

    return JsonResponse(result.to_dict())


@csrf_exempt
def trigger_cb_failure(request):
    """
    Intentional-failure endpoint for directly triggering the Circuit Breaker.

    POST /api/baldur/stress/trigger-cb-failure/

    Parameters:
        failure_count (int): Consecutive failure count (default: 10)
        error_type (str): Error type - "db_error", "timeout", "exception"
            (default: "db_error")

    This API produces failures that are handled through BaldurMiddleware.
    Once consecutive failures exceed the CB threshold, the CB flips to OPEN.

    ⚠️ Test-only!
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    error_type = body.get("error_type", "db_error")

    service = get_stress_test_service()
    result = service.trigger_cb_failure(error_type=error_type)

    # Intentional failure: return 503 so the CB counts it
    if result.status == "intentional_failure":
        return JsonResponse(result.to_dict(), status=503)

    return JsonResponse(result.to_dict())
