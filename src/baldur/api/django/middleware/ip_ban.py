"""
IP Ban Enforcement Middleware.

Enforces Redis-recorded IP bans at the HTTP request boundary.

SecurityViolationService._temporary_ip_ban() and _permanent_ip_ban() record
bans in Redis, and is_ip_banned() can read them back, but without this
middleware nothing consults that state on subsequent requests — the ban is
recorded and never enforced.

Design:
- FAIL-OPEN: allow the request when Redis is unavailable (availability first)
- Health-check paths exempt: /health/, /readiness/, /liveness/ are never banned
- IP extraction: reuses baldur.utils.network.extract_client_ip() (project standard)
- Minimal response: 403 omits ban_type (avoids leaking information to attackers)

Middleware position (base.py MIDDLEWARE):
    After TieringMiddleware, before BaldurMiddleware

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
    IP ban enforcement middleware.

    Checks the IP ban records written to Redis by SecurityViolationService and
    rejects requests from banned IPs with a 403.

    Redis key pattern: security:banned_ip:{ip_address}
    Redis value: {"banned": True, "type": "temporary"|"permanent"}

    Fail-open: allows the request when the Redis lookup fails.
    """

    # Health-check paths are exempt (K8s probes, ELB health checks, etc.).
    # /health/ is normally answered by nginx.conf and never reaches Django;
    # kept here defensively.
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

        CRITICAL: must use the same key prefix as
        SecurityViolationService._temporary_ip_ban()/_permanent_ip_ban().
        Changing one side alone silently breaks ban lookups.
        """
        if self._config is not None:
            return str(self._config.banned_ip_cache_prefix)
        # Same as the SecurityConfig default (models.py banned_ip_cache_prefix)
        return "security:banned_ip:"

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.http import JsonResponse

        self._lazy_init()

        # Health-check paths are exempt
        if any(request.path.startswith(prefix) for prefix in self.EXEMPT_PATH_PREFIXES):
            return cast("HttpResponse", self.get_response(request))

        # IP extraction (project standard: baldur.utils.network.extract_client_ip)
        client_ip = extract_client_ip(request, default="unknown") or "unknown"

        # Check whether the IP is banned
        ban_info = self._check_ip_ban(client_ip)

        if ban_info is not None:
            ban_type = ban_info.get("type", "unknown")
            logger.warning(
                "ip_ban_middleware.blocked_banned_ip",
                ban_type=ban_type,
                request_path=request.path,
            )

            # Security: ban_type is omitted from the response so attackers
            # learn nothing; it is recorded in the log only.
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
        Look up IP ban information in Redis.

        Returns:
            The ban info dict when banned, or None (not banned / lookup failed).

        FAIL-OPEN: returns None when the Redis lookup fails (request allowed).
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
            # FAIL-OPEN: allow the request when Redis is unavailable
            logger.debug(
                "ip_ban_middleware.cache_check_failed_fail",
                error=e,
            )
            return None
