"""
X-Test-Mode Idempotency Views

멱등성 보장(Idempotency Service) 동작을 X-Test-Mode 환경에서 관찰할 수 있는 API.

Endpoints:
- POST /api/baldur/xtest/idempotency/generate-key/ - 멱등성 키 생성 및 해시 미리보기
- POST /api/baldur/xtest/idempotency/check-duplicate/ - 중복 요청 감지 테스트
- GET  /api/baldur/xtest/idempotency/status/ - 현재 등록된 키 상태 조회
- POST /api/baldur/xtest/idempotency/register/ - 테스트용 키 수동 등록
- POST /api/baldur/xtest/idempotency/clear/ - 테스트 키 삭제

Components:
- IdempotencyKey: 멱등성 키 생성 (entity_type, entity_id, action 조합)
- IdempotencyDomain: 도메인 열거형 (EXTERNAL_SERVICE, ASYNC_TASK 등)
- IdempotencyService: 중복 체크 및 등록 서비스

Security:
- X-Test-Mode: chaos-monkey 헤더 필수
- DEBUG 또는 CHAOS_ENABLED 환경 변수 필요
- production 환경에서는 완전 차단
"""

from typing import Any

import structlog
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin, collect_system_snapshot

logger = structlog.get_logger()


# X-Test-Mode로 생성된 키 식별용 상수
XTEST_SOURCE = "x-test-mode"
XTEST_METADATA_KEY = "idempotency:xtest:keys"  # X-Test에서 생성한 키 추적용
DEFAULT_TTL_SECONDS = 3600  # 1시간 기본 TTL
MAX_STATUS_RESULTS = 50  # 상태 조회 시 최대 결과 수


def _get_xtest_tracked_keys() -> list[str]:
    """X-Test-Mode로 등록된 키 목록 조회."""
    try:
        keys = cache.get(XTEST_METADATA_KEY) or []
        return list(keys)
    except Exception:
        return []


def _track_xtest_key(cache_key: str) -> None:
    """X-Test-Mode로 생성한 키를 추적 목록에 추가."""
    try:
        keys = _get_xtest_tracked_keys()
        if cache_key not in keys:
            keys.append(cache_key)
            cache.set(XTEST_METADATA_KEY, keys, timeout=86400)  # 24시간
    except Exception as e:
        logger.warning(
            "test.idempotency_failed_track",
            error=e,
        )


def _untrack_xtest_key(cache_key: str) -> None:
    """추적 목록에서 키 제거."""
    try:
        keys = _get_xtest_tracked_keys()
        if cache_key in keys:
            keys.remove(cache_key)
            cache.set(XTEST_METADATA_KEY, keys, timeout=86400)
    except Exception as e:
        logger.warning(
            "test.idempotency_failed_untrack",
            error=e,
        )


# =============================================================================
# 멱등성 키 생성 View
# =============================================================================


class GenerateKeyView(XTestModeMixin, APIView):
    """
    멱등성 키 생성 및 해시값 미리보기 API.

    POST /api/baldur/xtest/idempotency/generate-key/

    Request Body:
        {
            "entity_type": "order",
            "entity_id": "123",
            "action": "process",
            "domain": "EXTERNAL_SERVICE"  // 선택, 기본값
        }

    Response:
        {
            "status": "success",
            "key_string": "order:123:process",
            "cache_key": "idempotency:external_service:order:123:process",
            "key_hash": "a1b2c3d4e5f6...",
            "domain": "EXTERNAL_SERVICE",
            "ttl_seconds": 3600,
            "components": {
                "entity_type": "order",
                "entity_id": "123",
                "operation": "process"
            }
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
            get_idempotency_service,
        )

        # 필수 파라미터 검증
        entity_type = request.data.get("entity_type")
        entity_id = request.data.get("entity_id")
        action = request.data.get("action")

        missing_fields = []
        if not entity_type:
            missing_fields.append("entity_type")
        if not entity_id:
            missing_fields.append("entity_id")
        if not action:
            missing_fields.append("action")

        if missing_fields:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_fields",
                    "message": f"Required fields: {', '.join(missing_fields)}",
                    "missing": missing_fields,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 도메인 파싱 (기본값: EXTERNAL_SERVICE)
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        try:
            domain = IdempotencyDomain[domain_str]
        except KeyError:
            valid_domains = [d.name for d in IdempotencyDomain]
            return Response(
                {
                    "status": "error",
                    "error": "invalid_domain",
                    "message": f"Invalid domain: {domain_str}",
                    "valid_domains": valid_domains,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 키 생성
        try:
            # entity_id를 문자열에서 정수로 변환 시도
            try:
                entity_id_int = int(entity_id)
            except (ValueError, TypeError):
                # 변환 실패 시 custom 키 사용
                key = IdempotencyKey.custom(
                    f"{entity_type}:{entity_id}:{action}",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action=action,
                )
                key.domain = domain
            else:
                key = IdempotencyKey.for_operation(
                    entity_type=entity_type,
                    entity_id=entity_id_int,
                    operation=action,
                    domain=domain,
                )

            # TTL 조회
            service = get_idempotency_service()
            ttl = service.cache_ttl

            response_data = {
                "status": "success",
                "key_string": key.key,
                "cache_key": key.cache_key,
                "key_hash": key.hash,
                "domain": domain.name,
                "ttl_seconds": ttl,
                "components": key.components,
                "timestamp": timezone.now().isoformat(),
            }

            # WAL Audit 기록
            self.log_xtest_audit(
                request=request,
                action="generate_key",
                component="idempotency",
                details={"key_string": key.key, "domain": domain.name},
                result="success",
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_key_generation",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "key_generation_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# 중복 감지 시뮬레이션 View
# =============================================================================


class CheckDuplicateView(XTestModeMixin, APIView):
    """
    중복 요청 감지 동작 테스트 API.

    POST /api/baldur/xtest/idempotency/check-duplicate/

    Request Body:
        {
            "key": "order:123:process",
            "domain": "EXTERNAL_SERVICE",  // 선택
            "register": false  // 선택, true면 체크 후 등록
        }

    Response:
        {
            "status": "success",
            "is_duplicate": true,
            "first_seen_at": "2026-01-26T10:00:00Z",  // 중복 시
            "ttl_remaining": 3540,  // 남은 TTL (초)
            "registered": false,  // 등록 수행 여부
            "cache_key": "idempotency:external_service:order:123:process"
        }
    """

    def post(self, request: Request) -> Response:  # noqa: C901
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
            get_idempotency_service,
        )

        # 필수 파라미터 검증
        key_string = request.data.get("key")
        if not key_string:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_fields",
                    "message": "Required field: key",
                    "missing": ["key"],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 도메인 파싱
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        try:
            domain = IdempotencyDomain[domain_str]
        except KeyError:
            valid_domains = [d.name for d in IdempotencyDomain]
            return Response(
                {
                    "status": "error",
                    "error": "invalid_domain",
                    "message": f"Invalid domain: {domain_str}",
                    "valid_domains": valid_domains,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        register = request.data.get("register", False)

        try:
            # IdempotencyKey 생성 (custom 타입 사용)
            key = IdempotencyKey.custom(key_string, raw_key=key_string)
            key.domain = domain
            cache_key = key.cache_key

            # 캐시에서 확인
            cached_value = None
            is_duplicate = False
            first_seen_at = None
            ttl_remaining = None

            try:
                cached_value = cache.get(cache_key)
                if cached_value:
                    is_duplicate = True
                    # 메타데이터에서 first_seen_at 추출
                    if isinstance(cached_value, dict):
                        first_seen_at = cached_value.get("first_seen_at")
                    # TTL 조회 시도 (Django ``BaseCache`` does not expose a
                    # ``ttl`` method; backends that do, such as Redis, surface
                    # it dynamically — fall back to ``None`` when unavailable.)
                    ttl_fn = getattr(cache, "ttl", None)
                    if callable(ttl_fn):
                        try:
                            ttl_remaining = ttl_fn(cache_key)
                        except Exception:
                            ttl_remaining = None
            except Exception as e:
                logger.warning(
                    "test.idempotency_cache_check",
                    error=e,
                )

            # 등록 수행 여부
            registered = False
            if register and not is_duplicate:
                try:
                    service = get_idempotency_service()
                    metadata = {
                        "first_seen_at": timezone.now().isoformat(),
                        "source": XTEST_SOURCE,
                    }
                    cache.set(cache_key, metadata, timeout=service.cache_ttl)
                    _track_xtest_key(cache_key)
                    registered = True
                    ttl_remaining = service.cache_ttl
                except Exception as e:
                    logger.warning(
                        "test.idempotency_registration_failed",
                        error=e,
                    )

            response_data = {
                "status": "success",
                "is_duplicate": is_duplicate,
                "first_seen_at": first_seen_at,
                "ttl_remaining": ttl_remaining,
                "registered": registered,
                "cache_key": cache_key,
                "key_string": key_string,
                "domain": domain.name,
                "timestamp": timezone.now().isoformat(),
            }

            # WAL Audit 기록
            self.log_xtest_audit(
                request=request,
                action="check_duplicate",
                component="idempotency",
                details={
                    "key_string": key_string,
                    "is_duplicate": is_duplicate,
                    "registered": registered,
                },
                result="success",
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_duplicate_check",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "check_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# Idempotency Status Helpers (Complexity Reduction)
# =============================================================================


def _filter_tracked_keys(
    tracked_keys: list[str],
    domain_filter: str,
    prefix_filter: str,
) -> list[str]:
    """도메인 및 프리픽스 필터 적용."""
    from baldur.services.idempotency import IdempotencyDomain

    result = tracked_keys

    if domain_filter:
        try:
            domain = IdempotencyDomain[domain_filter]
            result = [k for k in result if f":{domain.value}:" in k]
        except KeyError:
            pass

    if prefix_filter:
        result = [k for k in result if prefix_filter in k]

    return result


def _aggregate_by_domain(tracked_keys: list[str]) -> dict[str, int]:
    """도메인별 키 집계."""
    from baldur.services.idempotency import IdempotencyDomain

    by_domain: dict[str, int] = {}
    for key in tracked_keys:
        for domain in IdempotencyDomain:
            if f":{domain.value}:" in key:
                by_domain[domain.name] = by_domain.get(domain.name, 0) + 1
                break
    return by_domain


def _get_recent_keys_details(
    tracked_keys: list[str], limit: int
) -> list[dict[str, Any]]:
    """최근 키 상세 정보 조회."""
    recent_keys = []
    for cache_key in tracked_keys[:limit]:
        try:
            cached_value = cache.get(cache_key)
            first_seen_at = None
            if isinstance(cached_value, dict):
                first_seen_at = cached_value.get("first_seen_at")
            recent_keys.append(
                {
                    "cache_key": cache_key,
                    "first_seen_at": first_seen_at,
                    "has_value": cached_value is not None,
                }
            )
        except Exception:
            recent_keys.append(
                {
                    "cache_key": cache_key,
                    "first_seen_at": None,
                    "has_value": False,
                }
            )
    return recent_keys


def _get_cache_backend_name() -> str:
    """캐시 백엔드 이름 조회."""
    from django.conf import settings

    try:
        cache_config = settings.CACHES.get("default", {})
        return str(cache_config.get("BACKEND", "unknown"))
    except Exception:
        return "unknown"


# =============================================================================
# 멱등성 상태 조회 View
# =============================================================================


class IdempotencyStatusView(XTestModeMixin, APIView):
    """
    현재 등록된 Idempotency 키 상태 조회 API.

    GET /api/baldur/xtest/idempotency/status/

    Query Parameters:
        domain: 도메인 필터 (선택)
        prefix: 키 프리픽스 필터 (선택)
        limit: 조회 개수 (기본 50)

    Response:
        {
            "status": "success",
            "total_xtest_keys": 10,
            "by_domain": {"EXTERNAL_SERVICE": 5, "ASYNC_TASK": 5},
            "recent_keys": [
                {
                    "cache_key": "idempotency:external_service:...",
                    "first_seen_at": "2026-01-26T10:00:00Z",
                    "has_value": true
                }
            ],
            "cache_backend": "django.core.cache.backends.redis.RedisCache"
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        # 쿼리 파라미터 파싱
        domain_filter = request.query_params.get("domain", "").upper()
        prefix_filter = request.query_params.get("prefix", "")
        try:
            limit = min(
                int(request.query_params.get("limit", MAX_STATUS_RESULTS)),
                MAX_STATUS_RESULTS,
            )
        except (ValueError, TypeError):
            limit = MAX_STATUS_RESULTS

        try:
            # X-Test로 등록된 키 조회 및 필터링
            tracked_keys = _get_xtest_tracked_keys()
            tracked_keys = _filter_tracked_keys(
                tracked_keys, domain_filter, prefix_filter
            )

            # 도메인별 집계
            by_domain = _aggregate_by_domain(tracked_keys)

            # 최근 키 상세 정보
            recent_keys = _get_recent_keys_details(tracked_keys, limit)

            # 캐시 백엔드 정보
            cache_backend = _get_cache_backend_name()

            response_data = {
                "status": "success",
                "total_xtest_keys": len(tracked_keys),
                "by_domain": by_domain,
                "recent_keys": recent_keys,
                "cache_backend": cache_backend,
                "filters_applied": {
                    "domain": domain_filter or None,
                    "prefix": prefix_filter or None,
                    "limit": limit,
                },
                "snapshot": collect_system_snapshot(),
                "timestamp": timezone.now().isoformat(),
            }

            # WAL Audit 기록
            self.log_xtest_audit(
                request=request,
                action="query_status",
                component="idempotency",
                details={"total_xtest_keys": len(tracked_keys)},
                result="success",
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_status_retrieval",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "status_retrieval_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# 멱등성 키 등록 View
# =============================================================================


class RegisterKeyView(XTestModeMixin, APIView):
    """
    테스트용 Idempotency 키 수동 등록 API.

    POST /api/baldur/xtest/idempotency/register/

    Request Body:
        {
            "key": "order:123:process",
            "domain": "EXTERNAL_SERVICE",  // 선택
            "ttl_seconds": 3600,  // 선택, 기본값
            "result_data": {"order_id": 123}  // 선택, 저장할 데이터
        }

    Response:
        {
            "status": "success",
            "registered": true,
            "cache_key": "idempotency:external_service:order:123:process",
            "expires_at": "2026-01-26T11:00:00Z"
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
            get_idempotency_service,
        )

        # 필수 파라미터 검증
        key_string = request.data.get("key")
        if not key_string:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_fields",
                    "message": "Required field: key",
                    "missing": ["key"],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 도메인 파싱
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        try:
            domain = IdempotencyDomain[domain_str]
        except KeyError:
            valid_domains = [d.name for d in IdempotencyDomain]
            return Response(
                {
                    "status": "error",
                    "error": "invalid_domain",
                    "message": f"Invalid domain: {domain_str}",
                    "valid_domains": valid_domains,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # TTL 설정
        service = get_idempotency_service()
        try:
            ttl_seconds = int(request.data.get("ttl_seconds", service.cache_ttl))
        except (ValueError, TypeError):
            ttl_seconds = service.cache_ttl

        result_data = request.data.get("result_data", {})

        try:
            # 키 생성
            key = IdempotencyKey.custom(key_string, raw_key=key_string)
            key.domain = domain
            cache_key = key.cache_key

            # 캐시에 등록
            now = timezone.now()
            metadata = {
                "first_seen_at": now.isoformat(),
                "source": XTEST_SOURCE,
                "result_data": result_data,
            }

            cache.set(cache_key, metadata, timeout=ttl_seconds)
            _track_xtest_key(cache_key)

            # 만료 시간 계산
            from datetime import timedelta

            expires_at = now + timedelta(seconds=ttl_seconds)

            response_data = {
                "status": "success",
                "registered": True,
                "cache_key": cache_key,
                "key_string": key_string,
                "domain": domain.name,
                "ttl_seconds": ttl_seconds,
                "expires_at": expires_at.isoformat(),
                "metadata": {
                    "source": XTEST_SOURCE,
                    "has_result_data": bool(result_data),
                },
                "timestamp": now.isoformat(),
            }

            # WAL Audit 기록
            self.log_xtest_injection(
                request=request,
                component="idempotency",
                injection_type="register",
                count=1,
                target_ids=[cache_key],
            )

            return Response(response_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception(
                "test.idempotency_registration_failed",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "registration_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# 멱등성 키 삭제 View
# =============================================================================


class ClearKeysView(XTestModeMixin, APIView):
    """
    테스트 키 정리 API.

    POST /api/baldur/xtest/idempotency/clear/

    Request Body:
        {
            "key": "order:123:process",  // 특정 키 삭제
            "domain": "EXTERNAL_SERVICE",  // 키 삭제 시 도메인
            // OR
            "clear_all_xtest": true  // X-Test 생성 키만 전체 삭제
        }

    Response:
        {
            "status": "success",
            "cleared_count": 10,
            "cleared_keys": ["idempotency:..."]  // 삭제된 키 목록
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
        )

        key_string = request.data.get("key")
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        clear_all_xtest = request.data.get("clear_all_xtest", False)

        cleared_keys: list[str] = []
        errors: list[str] = []

        try:
            if clear_all_xtest:
                # X-Test 생성 키 전체 삭제
                tracked_keys = _get_xtest_tracked_keys()
                for cache_key in tracked_keys:
                    try:
                        cache.delete(cache_key)
                        cleared_keys.append(cache_key)
                    except Exception as e:
                        errors.append(f"{cache_key}: {str(e)}")

                # 추적 목록 초기화
                try:
                    cache.delete(XTEST_METADATA_KEY)
                except Exception:
                    pass

            elif key_string:
                # 특정 키 삭제
                try:
                    domain = IdempotencyDomain[domain_str]
                except KeyError:
                    valid_domains = [d.name for d in IdempotencyDomain]
                    return Response(
                        {
                            "status": "error",
                            "error": "invalid_domain",
                            "message": f"Invalid domain: {domain_str}",
                            "valid_domains": valid_domains,
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                key = IdempotencyKey.custom(key_string, raw_key=key_string)
                key.domain = domain
                cache_key = key.cache_key

                try:
                    cache.delete(cache_key)
                    _untrack_xtest_key(cache_key)
                    cleared_keys.append(cache_key)
                except Exception as e:
                    errors.append(f"{cache_key}: {str(e)}")

            else:
                return Response(
                    {
                        "status": "error",
                        "error": "missing_parameters",
                        "message": "Provide either 'key' or 'clear_all_xtest=true'",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            response_data = {
                "status": "success",
                "cleared_count": len(cleared_keys),
                "cleared_keys": cleared_keys[:MAX_STATUS_RESULTS],  # 최대 50개만 표시
                "errors": errors if errors else None,
                "timestamp": timezone.now().isoformat(),
            }

            # WAL Audit 기록
            self.log_xtest_cleanup(
                request=request,
                component="idempotency",
                cleaned_count=len(cleared_keys),
                cleaned_ids=cleared_keys[:20],
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_clear_failed",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "clear_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
