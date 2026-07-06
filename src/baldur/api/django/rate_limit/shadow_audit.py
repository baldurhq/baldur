"""
Shadow Audit Logger — Rate limit event forensic logging.

Logs emergency mode access events for forensic analysis,
writing to both AuditService and local fallback files.

Extracted from api/django/rate_limit.py as part of 358 rate_limit package split.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from baldur.api.django.rate_limit.config import FALLBACK_LOG_PATH
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = structlog.get_logger()

__all__ = ["ShadowAuditLogger"]


class ShadowAuditLogger:
    """Rate limit event shadow audit logging.

    Writes to both:
    1. AuditService (if available)
    2. Local fallback file (always)
    """

    def log_rate_limit_event(
        self,
        request: HttpRequest,
        is_allowed: bool,
        emergency_limit: int,
        client_ip: str,
        reason: str = "Redis failure",
    ) -> None:
        """
        Log emergency mode access for forensic analysis.

        Args:
            request: HTTP request
            is_allowed: Whether the request was allowed
            emergency_limit: Current emergency rate limit
            client_ip: Client IP address
            reason: Reason for emergency mode
        """
        self._log_to_file(request, is_allowed, emergency_limit, client_ip, reason)
        self._log_to_audit_service(
            request, is_allowed, emergency_limit, client_ip, reason
        )

    def _log_to_file(
        self,
        request: HttpRequest,
        is_allowed: bool,
        emergency_limit: int,
        client_ip: str,
        reason: str,
    ) -> None:
        """Write to local fallback file (always works)."""
        try:
            FALLBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

            entry = {
                "timestamp": utc_now().isoformat(),
                "event": "rate_limit_emergency",
                "mode": "REDIS_FAILURE_BYPASS",
                "allowed": is_allowed,
                "path": request.path,
                "method": request.method,
                "client_ip": client_ip,
                "emergency_limit": emergency_limit,
                "reason": reason,
            }

            with open(FALLBACK_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        except Exception as e:
            logger.exception(
                "rate_limit.write_fallback_log_failed",
                error=e,
            )

    def _log_to_audit_service(
        self,
        request: HttpRequest,
        is_allowed: bool,
        emergency_limit: int,
        client_ip: str,
        reason: str,
    ) -> None:
        """Write to AuditService (best-effort)."""
        try:
            from baldur.audit import log_config_change

            log_config_change(
                config_type="rate_limit_emergency",
                config_key="mode",
                old_value="normal",
                new_value="REDIS_FAILURE_BYPASS",
                user="system",
                reason=f"Rate limit operating in emergency mode: {reason}",
                metadata={
                    "severity": "critical",
                    "tag": "REDIS_FAILURE_BYPASS",
                    "allowed": is_allowed,
                    "path": request.path,
                    "client_ip": client_ip,
                    "emergency_limit": emergency_limit,
                },
            )
        except Exception as e:
            logger.debug(
                "rate_limit.shadow_audit_skipped",
                error=e,
            )
