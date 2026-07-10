"""
Sensitive Endpoint Access Logging Middleware.

민감한 엔드포인트(audit, config, chaos)에 대한 접근을 로깅하여
컴플라이언스 감사 증적을 제공합니다.

Features:
- Sensitive endpoints detection
- Log data masking for privacy protection
- FAIL-OPEN design: Logging failure does not block requests
- Fallback logging to stdout
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, cast

import structlog

from baldur.utils.network import extract_client_ip
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

# Sensitive endpoints that require access logging
# Only HIGH sensitivity endpoints are logged (audit, config, chaos schedules)
SENSITIVE_ENDPOINT_PATTERNS = [
    re.compile(r"^/api/baldur/audit/"),
    re.compile(r"^/api/baldur/config/"),
    re.compile(r"^/api/baldur/chaos/schedules/"),
    re.compile(r"^/api/baldur/chaos/config/"),
]


# =============================================================================
# Access Log Entry
# =============================================================================


class AccessLogEntry:
    """Represents an access log entry for sensitive endpoints."""

    def __init__(
        self,
        *,
        timestamp: datetime,
        user: str,
        method: str,
        path: str,
        query_params: str,
        source_ip: str,
        user_agent: str,
        status_code: int | None = None,
        response_time_ms: float | None = None,
    ):
        self.timestamp = timestamp
        self.user = user
        self.method = method
        self.path = path
        self.query_params = query_params
        self.source_ip = source_ip
        self.user_agent = user_agent
        self.status_code = status_code
        self.response_time_ms = response_time_ms

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "user": self.user,
            "method": self.method,
            "path": self.path,
            "query_params": self.query_params,
            "source_ip": self._mask_internal_ip(self.source_ip),
            "user_agent": self.user_agent,
            "status_code": self.status_code,
            "response_time_ms": self.response_time_ms,
        }

    def _mask_internal_ip(self, ip: str) -> str:
        """
        Mask internal IP addresses for privacy.

        FAIL-SECURE: On any error, return "[MASKED]" instead of raw IP.
        """
        if not ip:
            return "[EMPTY]"

        try:
            internal_patterns = [
                re.compile(r"^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$"),
                re.compile(r"^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$"),
                re.compile(r"^192\.168\.\d{1,3}\.\d{1,3}$"),
            ]

            for pattern in internal_patterns:
                if pattern.match(ip):
                    # Show only network portion for internal IPs
                    parts = ip.split(".")
                    return f"{parts[0]}.{parts[1]}.xxx.xxx"

            return ip

        except Exception:
            # FAIL-SECURE: On any error, mask completely
            return "[MASKED]"

    def __str__(self) -> str:
        """Format as log message."""
        return (
            f"[AccessLog] user={self.user} method={self.method} "
            f"path={self.path} ip={self._mask_internal_ip(self.source_ip)} "
            f"status={self.status_code} time_ms={self.response_time_ms}"
        )


# =============================================================================
# Access Logger Service
# =============================================================================


class SensitiveEndpointAccessLogger:
    """
    Service for logging access to sensitive endpoints.

    This provides audit trail for compliance requirements.
    Only logs access to HIGH sensitivity endpoints:
    - /audit/ - System control audit logs
    - /config/* - System configuration
    - /chaos/schedules/* - Chaos experiment schedules
    - /chaos/config/* - Chaos configuration
    """

    def __init__(self, patterns: list[re.Pattern] | None = None):
        """
        Initialize the access logger.

        Args:
            patterns: Custom list of patterns to consider sensitive.
                     Uses default SENSITIVE_ENDPOINT_PATTERNS if None.
        """
        self.patterns = patterns or SENSITIVE_ENDPOINT_PATTERNS
        try:
            from baldur.settings.api_view import get_api_view_settings

            self._access_log_file = get_api_view_settings().access_log_path
        except Exception:
            self._access_log_file = "logs/sensitive_access.log"

    def is_sensitive_endpoint(self, path: str) -> bool:
        """Check if the given path is a sensitive endpoint."""
        return any(pattern.match(path) for pattern in self.patterns)

    def log_if_sensitive(
        self,
        request: HttpRequest,
        response: HttpResponse,
        response_time_ms: float = 0.0,
    ) -> AccessLogEntry | None:
        """
        Log access if the request is to a sensitive endpoint.

        Args:
            request: Django HttpRequest object
            response: Django HttpResponse object
            response_time_ms: Response time in milliseconds

        Returns:
            AccessLogEntry if logged, None otherwise
        """
        path = request.path

        if not self.is_sensitive_endpoint(path):
            return None

        # Extract user info
        user = "anonymous"
        if hasattr(request, "user") and request.user.is_authenticated:
            user = getattr(request.user, "username", str(request.user.id))

        # Get client IP (consider X-Forwarded-For for proxied requests)
        source_ip = self._get_client_ip(request)

        # Create log entry
        entry = AccessLogEntry(
            timestamp=utc_now(),
            user=user,
            method=request.method or "",
            path=path,
            query_params=request.META.get("QUERY_STRING", ""),
            source_ip=source_ip,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            status_code=response.status_code,
            response_time_ms=response_time_ms,
        )

        # Log to both structured logger and access log
        self._write_log(entry)

        return entry

    def _get_client_ip(self, request: HttpRequest) -> str:
        """Extract client IP (canonical resolution: XFF -> X-Real-IP -> REMOTE_ADDR)."""
        return cast(str, extract_client_ip(request, default=""))

    def _write_log(self, entry: AccessLogEntry) -> None:
        """
        Write the access log entry with fallback mechanism.

        FAIL-OPEN with FALLBACK Design:
        - Primary: Structured logger + dedicated access log file
        - Fallback: Standard output (print) if primary fails
        - This ensures we always have SOME record, even if degraded
        """
        primary_success = False

        # Primary: Log to Python logger (for aggregation)
        try:
            logger.info(str(entry))
            primary_success = True
        except Exception:
            # Logger failed, will use fallback
            pass

        # Primary: Also write to dedicated access log file
        try:
            self._append_to_file(entry)
            primary_success = True
        except Exception as e:
            logger.warning(
                "access_log.write_file_failed",
                error=e,
            )

        # FALLBACK: If all primary logging failed, use stdout as last resort
        if not primary_success:
            self._fallback_log(entry)

    def _fallback_log(self, entry: AccessLogEntry) -> None:
        """
        Fallback logging to stdout when primary logging fails.

        This ensures we have at least some audit trail even when:
        - Main logger is misconfigured
        - Log file is inaccessible
        - Redis/DB logging is down

        stdout is captured by container orchestrators (Docker, K8s)
        so it provides a secondary audit trail.
        """
        import json
        import sys

        try:
            fallback_record = {
                "_fallback": True,
                "_reason": "primary_logging_failed",
                **entry.to_dict(),
            }
            # Write directly to stdout, bypassing logging framework
            print(
                f"[FALLBACK_AUDIT_LOG] {json.dumps(fallback_record)}",
                file=sys.stdout,
                flush=True,
            )
        except Exception:
            # Last resort: minimal output
            print(
                f"[FALLBACK_AUDIT_LOG] user={entry.user} path={entry.path} status={entry.status_code}",
                file=sys.stdout,
                flush=True,
            )

    def _append_to_file(self, entry: AccessLogEntry) -> None:
        """Append entry to the access log file."""
        import json
        import os

        # Ensure directory exists
        log_dir = os.path.dirname(self._access_log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        with open(self._access_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")


# =============================================================================
# Django Middleware
# =============================================================================


class SensitiveAccessLoggingMiddleware:
    """
    Django middleware for logging access to sensitive endpoints.

    Add to MIDDLEWARE in settings.py:
        MIDDLEWARE = [
            ...
            'baldur.api.django.middleware.SensitiveAccessLoggingMiddleware',
            ...
        ]

    This middleware logs all GET requests to sensitive endpoints
    (audit, config, chaos schedules) for compliance audit trails.

    FAIL-OPEN Design: Logging failure does not block requests.
    The primary function (serving the request) must not be affected by
    secondary functions (logging).
    """

    def __init__(self, get_response: Callable):
        self.get_response = get_response
        self.access_logger = SensitiveEndpointAccessLogger()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        import time

        start_time = time.time()

        response: HttpResponse = self.get_response(request)

        # Calculate response time
        response_time_ms = (time.time() - start_time) * 1000

        # FAIL-OPEN: Log if sensitive endpoint, but don't block on failure
        try:
            self.access_logger.log_if_sensitive(request, response, response_time_ms)
        except Exception as e:
            # Log error but don't affect response
            logger.exception(
                "access_log.middleware_error_fail_open",
                error=e,
            )

        return response
