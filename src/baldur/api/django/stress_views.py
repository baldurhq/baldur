"""
Baldur Pool Stress Test Endpoints.

이 엔드포인트들은 의도적으로 DB Connection Pool을 고갈시킵니다.
테스트 전용이며, 프로덕션에서는 절대 사용하지 마세요!

Note:
- 비즈니스 로직은 StressTestService(services/stress_test_service.py)로 분리됨
- View는 Request/Response 처리만 담당
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
    """SQLAlchemy Pool 정보 조회 (backward compatibility)."""
    service = get_stress_test_service()
    return service.get_pool_info()


# =============================================================================
# Slow Query Endpoints
# =============================================================================


@require_GET
def slow_query_5s(request):
    """
    5초 동안 DB 연결을 점유하는 느린 쿼리.

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
    10초 동안 DB 연결을 점유하는 매우 느린 쿼리.

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
    의도적으로 연결을 '누수'시키는 시뮬레이션.
    연결을 열고 닫지 않은 채로 유지합니다.

    GET /api/baldur/stress/leak/

    ⚠️ 테스트 전용! 프로덕션에서 절대 사용 금지!
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
    현재 Connection Pool 상태 조회.
    SQLAlchemy Pool 사용 시 실제 Pool 상태를, 아니면 PostgreSQL 통계를 반환.

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

            # Pool 고갈 시 503 반환
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
    여러 테이블을 JOIN하는 무거운 쿼리.

    GET /api/baldur/stress/heavy-query/
    """
    service = get_stress_test_service()
    result = service.execute_heavy_query()

    if result.status == "error":
        return JsonResponse(result.to_dict(), status=503)
    return JsonResponse(result.to_dict())


# =============================================================================
# ��� Advisory Lock API - 비침투적 DB 락 테스트
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
    PostgreSQL Advisory Lock 획득 - 비침투적 락 테스트.

    POST /api/baldur/stress/advisory-lock/acquire/

    비즈니스 데이터를 전혀 건드리지 않고, DB 엔진 수준의 락 경합만 발생시킵니다.
    이를 통해 시스템의 락 감지 및 복구 능력을 검증할 수 있습니다.

    Parameters:
        lock_id (int): 락 식별자 (1-1000000). 같은 ID로 다수 요청 시 경합 발생
        hold_seconds (int): 락 유지 시간 (1-60초, 기본값: 5초)
        exclusive (bool): 배타적 락 여부 (기본값: true)
        wait (bool): 락 획득 대기 여부. false면 즉시 실패 반환 (기본값: true)

    Response:
        - 200: 락 획득 성공
        - 409: 락 획득 실패 (다른 세션이 보유 중, wait=false인 경우)
        - 503: DB 오류 또는 타임아웃

    ⚠️ 테스트 전용! 프로덕션에서 절대 사용 금지!
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
    Advisory Lock 경합 시뮬레이션 - 다수의 세션이 동일 락을 놓고 경쟁.

    POST /api/baldur/stress/advisory-lock/contention/

    지정된 시간 동안 동일한 락 ID에 대해 반복적으로 획득/해제를 시도합니다.
    이는 실제 DB 락 경합 상황을 시뮬레이션합니다.

    Parameters:
        lock_id (int): 락 식별자
        duration_seconds (int): 경합 지속 시간 (1-30초, 기본값: 5초)
        lock_hold_ms (int): 각 락 유지 시간 (ms, 기본값: 100ms)

    Response:
        경합 통계 (성공/실패 횟수, 평균 대기 시간 등)
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
    Controlled Burst Failure - "폭풍 전야 -> 시스템 붕괴 -> 자율 복구" 연출.

    POST /api/baldur/stress/burst-failure/

    지정된 시간 동안 극단적인 락 타임아웃과 부하를 발생시켜
    100건 이상의 DLQ 항목을 강제로 생성합니다.

    Parameters:
        lock_id (int): Advisory Lock ID
        lock_timeout_ms (int): 극단적으로 짧은 락 타임아웃 (기본값: 1ms!)
        burst_duration_seconds (int): burst 지속 시간 (기본값: 10초)
        concurrent_locks (int): 동시 락 시도 수 (기본값: 50)

    이 API는 다음을 수행합니다:
    1. lock_timeout을 1ms로 축소
    2. 지정된 시간 동안 동시에 많은 락 획득 시도
    3. 대부분의 요청이 타임아웃으로 실패
    4. 실패한 요청들이 DLQ로 자동 라우팅됨

    ⚠️ 테스트 전용! 시스템에 의도적으로 장애를 발생시킵니다!
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
# ��� Pool Exhaustion API - CB 트리거를 위한 실제 풀 고갈
# =============================================================================


@csrf_exempt
def pool_exhaust(request):
    """
    DB 커넥션 풀을 의도적으로 고갈시켜 CB를 트리거합니다.

    POST /api/baldur/stress/pool-exhaust/

    Parameters:
        connections_to_hold (int): 점유할 커넥션 수 (기본값: 10)
        hold_seconds (int): 커넥션 유지 시간 (기본값: 30초, 최대 60초)

    이 API는:
    1. 여러 개의 DB 커넥션을 열고 유지
    2. 다른 요청들이 커넥션을 얻지 못해 503 에러 발생
    3. BaldurMiddleware가 이 에러를 감지하고 CB를 OPEN으로 전환
    4. 지정된 시간 후 커넥션 반환

    ⚠️ 테스트 전용! 시스템에 의도적으로 장애를 발생시킵니다!
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
    Circuit Breaker를 직접 트리거하기 위한 의도적 실패 엔드포인트.

    POST /api/baldur/stress/trigger-cb-failure/

    Parameters:
        failure_count (int): 연속 실패 횟수 (기본값: 10)
        error_type (str): 에러 유형 - "db_error", "timeout", "exception" (기본값: "db_error")

    이 API는 BaldurMiddleware를 통해 처리되는 실패를 발생시킵니다.
    연속된 실패가 CB threshold를 초과하면 CB가 OPEN 상태로 전환됩니다.

    ⚠️ 테스트 전용!
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

    # 의도적 실패이므로 503 반환하여 CB가 카운트하도록 함
    if result.status == "intentional_failure":
        return JsonResponse(result.to_dict(), status=503)

    return JsonResponse(result.to_dict())
