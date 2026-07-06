"""
IP Ban Enforcement Middleware.

Redis에 기록된 IP ban을 실제 HTTP 요청 단계에서 강제 적용합니다.

SecurityViolationService._temporary_ip_ban()과 _permanent_ip_ban()이
Redis에 ban을 기록하지만, 후속 요청을 차단하는 미들웨어가 없었음.
is_ip_banned()가 정의만 되어 있고 호출되는 곳이 없었음.

설계:
- FAIL-OPEN: Redis 장애 시 요청 허용 (가용성 우선)
- 헬스체크 경로 면제: /health/, /readiness/, /liveness/ 경로는 ban 대상에서 제외
- IP 추출: baldur.utils.network.extract_client_ip() 재사용 (프로젝트 표준)
- 응답 최소화: 403 응답에 ban_type 미포함 (공격자 정보 노출 방지)

미들웨어 위치 (base.py MIDDLEWARE):
    TieringMiddleware 다음, BaldurMiddleware 이전

Usage in settings.py:
    MIDDLEWARE = [
        ...
        "baldur.api.django.tiering.TieringMiddleware",
        "baldur.api.django.middleware.IPBanMiddleware",
        "baldur.api.django.middleware.BaldurMiddleware",
        ...
    ]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from baldur.utils.network import extract_client_ip

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


class IPBanMiddleware:
    """
    IP Ban 강제 적용 미들웨어.

    SecurityViolationService가 Redis에 기록한 IP ban 정보를 확인하여
    ban된 IP의 요청을 403으로 거부합니다.

    Redis 키 패턴: security:banned_ip:{ip_address}
    Redis 값: {"banned": True, "type": "temporary"|"permanent"}

    Fail-Open: Redis 조회 실패 시 요청을 허용합니다.
    """

    # 헬스체크 경로 면제 (K8s probe, ELB health check 등)
    # /health/는 nginx.conf에서 직접 응답하여 Django 미도달이나, 방어적으로 유지
    EXEMPT_PATH_PREFIXES = (
        "/health/",
        "/readiness/",
        "/liveness/",
    )

    def __init__(self, get_response):
        self.get_response = get_response
        self._cache = None
        self._config = None
        self._initialized = False

    def _lazy_init(self) -> None:
        """Lazy initialization to avoid circular imports at module load."""
        if self._initialized:
            return

        try:
            from baldur.services.security.models import SecurityConfig

            self._config = SecurityConfig.from_settings()
        except Exception as e:
            logger.warning(
                "ip_ban_middleware.config_init_failed",
                error=e,
            )
            self._config = None

        try:
            from baldur.factory import ProviderRegistry

            self._cache = ProviderRegistry.get_cache()
        except Exception as e:
            logger.debug(
                "ip_ban_middleware.cache_init_failed_retry",
                error=e,
            )
            self._cache = None

        self._initialized = True

    def _get_cache(self):
        """Get cache provider, retrying if initial load failed."""
        if self._cache is not None:
            return self._cache

        try:
            from baldur.factory import ProviderRegistry

            self._cache = ProviderRegistry.get_cache()
        except Exception:
            pass

        return self._cache

    def _get_banned_ip_prefix(self) -> str:
        """Get banned IP cache prefix from config.

        CRITICAL: SecurityViolationService._temporary_ip_ban()/_permanent_ip_ban()과
        반드시 동일한 키 프리픽스를 사용해야 함. 변경 시 ban 조회 불가 버그 발생.
        """
        if self._config is not None:
            return str(self._config.banned_ip_cache_prefix)
        # SecurityConfig 기본값과 동일 (models.py banned_ip_cache_prefix 필드)
        return "security:banned_ip:"

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.http import JsonResponse

        self._lazy_init()

        # 헬스체크 경로 면제
        if any(request.path.startswith(prefix) for prefix in self.EXEMPT_PATH_PREFIXES):
            return cast("HttpResponse", self.get_response(request))

        # IP 추출 (프로젝트 표준: baldur.utils.network.extract_client_ip)
        client_ip = extract_client_ip(request, default="unknown") or "unknown"

        # ban 여부 확인
        ban_info = self._check_ip_ban(client_ip)

        if ban_info is not None:
            ban_type = ban_info.get("type", "unknown")
            logger.warning(
                "ip_ban_middleware.blocked_banned_ip",
                ban_type=ban_type,
                request_path=request.path,
            )

            # 보안: ban_type을 응답에 포함하지 않음 (공격자 정보 노출 방지)
            # ban_type은 로그에만 기록
            return JsonResponse(
                {
                    "error": "Access denied",
                    "code": "IP_BANNED",
                },
                status=403,
            )

        return cast("HttpResponse", self.get_response(request))

    def _check_ip_ban(self, ip_address: str) -> dict[str, Any] | None:
        """
        Redis에서 IP ban 정보 조회.

        Returns:
            ban 정보 dict (banned인 경우) or None (미차단/조회실패)

        FAIL-OPEN: Redis 조회 실패 시 None 반환 (요청 허용)
        """
        cache = self._get_cache()
        if cache is None:
            return None

        try:
            prefix = self._get_banned_ip_prefix()
            cache_key = f"{prefix}{ip_address}"
            ban_info = cache.get(cache_key)

            if isinstance(ban_info, dict) and ban_info.get("banned", False):
                return dict(ban_info)

            return None

        except Exception as e:
            # FAIL-OPEN: Redis 장애 시 요청 허용
            logger.debug(
                "ip_ban_middleware.cache_check_failed_fail",
                error=e,
            )
            return None
