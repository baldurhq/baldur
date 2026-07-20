"""
Security Violation Service.

Handles security violations that should NEVER self-heal.
Security incidents are immediately blocked and routed to the security team.

Audit integration:
- Security violation handling: log_security_violation_audit
- IP blocking: log_security_violation_audit (action="block_ip")
- Session invalidation: log_security_violation_audit (action="invalidate_session")
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_security_violation_audit
from baldur.audit.masking import mask_ip
from baldur.notification.helpers import notify_incident
from baldur.services.security.models import (
    SecurityConfig,
    SecurityViolationResult,
)
from baldur.services.security.types import (
    SEVERITY_BY_VIOLATION_TYPE,
    Severity,
    ViolationType,
)

if TYPE_CHECKING:
    from baldur.interfaces.cache_provider import CacheProviderInterface
    from baldur.interfaces.repositories import SecurityIncidentRepository

logger = structlog.get_logger()


class SecurityViolationService:
    """
    Service for handling security violations.

    Security violations are NEVER auto-recovered. They are:
    1. Immediately blocked
    2. Logged with full forensic context
    3. Routed to security team for investigation

    Usage:
        service = SecurityViolationService()
        result = service.handle_violation(
            violation_type=ViolationType.SIGNATURE_INVALID,
            request_info={"ip": "1.2.3.4", "user_agent": "..."},
            description="Signature validation failed",
        )

    For testing with mock repository:
        mock_repo = Mock(spec=SecurityIncidentRepository)
        service = SecurityViolationService(repository=mock_repo)
    """

    def __init__(
        self,
        config: SecurityConfig | None = None,
        repository: SecurityIncidentRepository | None = None,
        cache: CacheProviderInterface | None = None,
    ):
        """
        Initialize the security violation service.

        Args:
            config: Optional configuration, loads from settings if None
            repository: Optional repository for DI, uses default adapter if None
            cache: Optional cache provider for DI, uses default if None
        """
        self.config = config or SecurityConfig.from_settings()
        self._repository = repository
        self._cache = cache

    @property
    def repository(self) -> SecurityIncidentRepository:
        """Get the repository, creating default adapter if needed."""
        if self._repository is None:
            from baldur.factory import ProviderRegistry

            try:
                self._repository = ProviderRegistry.get_security_repo()
            except (ValueError, ImportError):
                from baldur.adapters.memory import (
                    InMemorySecurityIncidentRepository,
                )

                self._repository = InMemorySecurityIncidentRepository()
        return self._repository

    @property
    def cache(self) -> CacheProviderInterface:
        """Get the cache provider, creating default if needed."""
        if self._cache is None:
            from baldur.factory import ProviderRegistry

            try:
                self._cache = ProviderRegistry.get_cache()
            except (ValueError, ImportError):
                from baldur.adapters.cache.memory_adapter import (
                    InMemoryCacheAdapter,
                )

                self._cache = InMemoryCacheAdapter()
        return self._cache

    def handle_violation(
        self,
        violation_type: str | ViolationType,
        request_info: dict[str, Any] | None = None,
        user_id: int | None = None,
        entity_refs: dict[str, int] | None = None,
        description: str = "",
        raw_request_data: dict[str, Any] | None = None,
    ) -> SecurityViolationResult:
        """
        Handle a security violation.

        This method:
        1. Creates a SecurityIncident record via repository
        2. Takes immediate protective action based on violation type
        3. Triggers security team notification
        4. Returns result with action taken

        Args:
            violation_type: Type of security violation
            request_info: Request info dict with 'ip', 'user_agent' keys
            user_id: Associated user ID (if authenticated)
            entity_refs: Related entity references (e.g., {"order_id": 123})
            description: Detailed description of the violation
            raw_request_data: Sanitized request data for forensics

        Returns:
            SecurityViolationResult with incident ID and action taken
        """
        violation_type_str = (
            violation_type.value
            if isinstance(violation_type, ViolationType)
            else violation_type
        )

        try:
            # Extract request information
            source_ip = request_info.get("ip") if request_info else None
            user_agent = request_info.get("user_agent", "") if request_info else ""

            # Determine severity
            severity = SEVERITY_BY_VIOLATION_TYPE.get(
                violation_type_str, Severity.MEDIUM
            )

            # Create incident record via repository
            incident = self.repository.create(
                incident_type=violation_type_str,
                severity=severity.value,
                description=description,
                source_ip=source_ip,
                user_agent=user_agent,
                user_id=user_id,
                entity_refs=entity_refs or {},
                raw_payload=self._sanitize_request_data(raw_request_data),
            )

            # Take immediate protective action
            action_taken = self._take_protective_action(
                violation_type=violation_type_str,
                incident_id=incident.id,
                user_id=user_id,
                source_ip=source_ip,
            )

            # Log the violation
            logger.warning(
                "security.violation",
                violation_type_str=violation_type_str,
                severity=severity.value,
                source_ip=source_ip,
                user_id=user_id,
                incident=incident.id,
                action_taken=action_taken,
            )

            # === Audit record: security violation handling (85_AUDIT_INTEGRATION Phase 1) ===
            log_security_violation_audit(
                violation_type=violation_type_str,
                action="handle_violation",
                target=(
                    f"ip:{source_ip}"
                    if source_ip
                    else f"user:{user_id}"
                    if user_id
                    else "unknown"
                ),
                result="success",
                severity=severity.value,
                operator="system",
                incident_id=incident.id,
                source_ip=source_ip,
                user_id=user_id,
                details={
                    "action_taken": action_taken,
                    "description": description,
                },
            )

            # Trigger notification (async if possible)
            try:
                self._send_security_notification(
                    incident.id, violation_type_str, severity.value
                )
            except Exception as e:
                logger.exception(
                    "security.violation_notification_failed",
                    error=e,
                )

            # EventBus integration on CRITICAL security violations
            if severity == Severity.CRITICAL:
                self._emit_critical_violation_event(
                    violation_type=violation_type_str,
                    incident_id=incident.id,
                    source_ip=source_ip,
                    user_id=user_id,
                )

            return SecurityViolationResult.handled(
                incident_id=incident.id,
                action=action_taken,
            )

        except Exception as e:
            logger.exception(
                "security.violation_failed_handle",
                error=e,
            )
            return SecurityViolationResult.failed(str(e))

    def record_violation(
        self,
        violation_type: str,
        details: dict[str, Any] | None = None,
        request_info: dict[str, Any] | None = None,
        user_id: int | None = None,
    ) -> SecurityViolationResult:
        """
        Simplified interface for recording a violation.

        This is a convenience method that wraps handle_violation for cases
        like CorruptionShield where simpler parameter passing is needed.

        Args:
            violation_type: Type of violation (e.g., "corruption_injection_attempt")
            details: Violation details dict (becomes description + raw_request_data)
            request_info: Optional request info with 'ip', 'user_agent'
            user_id: Optional associated user ID

        Returns:
            SecurityViolationResult with incident ID and action taken
        """
        # Build description from details
        description = ""
        if details:
            layer = details.get("layer", "unknown")
            message = details.get("message", "")
            field = details.get("field", "")
            description = f"[{layer}] {message}"
            if field:
                description += f" (field: {field})"

        return self.handle_violation(
            violation_type=violation_type,
            request_info=request_info,
            user_id=user_id,
            description=description,
            raw_request_data=details,
        )

    def _take_protective_action(  # noqa: C901, PLR0912
        self,
        violation_type: str,
        incident_id: int,
        user_id: int | None,
        source_ip: str | None,
    ) -> str:
        """
        Take immediate protective action based on violation type.

        Args:
            violation_type: Type of violation
            incident_id: The created incident ID
            user_id: Associated user ID
            source_ip: Source IP address

        Returns:
            Description of action taken
        """
        action_taken = ""

        if violation_type == ViolationType.TOKEN_FORGED.value:
            if user_id:
                action_taken = self._invalidate_user_sessions(user_id)
            else:
                action_taken = "Token forged but no user associated"

        elif violation_type == ViolationType.SIGNATURE_INVALID.value:
            if source_ip:
                action_taken = self._log_suspicious_ip(source_ip)
            else:
                action_taken = "Invalid signature logged"

        elif violation_type == ViolationType.RATE_LIMIT_ABUSE.value:
            if source_ip:
                action_taken = self._temporary_ip_ban(
                    source_ip, hours=self.config.temporary_ban_hours
                )
            else:
                action_taken = "Rate limit abuse detected but no IP"

        elif violation_type == ViolationType.DATA_TAMPERED.value:
            action_taken = "Request blocked, entity frozen for investigation"

        elif violation_type == ViolationType.UNAUTHORIZED_ACCESS.value:
            if user_id:
                action_taken = f"Access blocked for user {user_id}"
            else:
                action_taken = "Unauthorized access attempt logged"

        elif violation_type == ViolationType.REPLAY_ATTACK.value:
            action_taken = "Replay attack blocked, request discarded"
            if source_ip:
                self._log_suspicious_ip(source_ip)

        elif violation_type == ViolationType.INJECTION_ATTEMPT.value:
            action_taken = "Injection attempt blocked"
            if source_ip:
                self._temporary_ip_ban(source_ip, hours=self.config.injection_ban_hours)

        else:
            action_taken = f"Violation logged for review: {violation_type}"

        return action_taken

    @staticmethod
    def _invalidate_registry_sessions(user_id: int) -> list[str]:
        """Invalidate Redis sessions via UserSessionRegistry."""
        items = []
        try:
            from baldur.services.security.session_registry import (
                get_user_session_registry,
            )

            registry = get_user_session_registry()
            deleted_count = registry.invalidate_all(user_id)
            if deleted_count > 0:
                items.append(f"redis_sessions({deleted_count})")
            else:
                items.append("redis_sessions(0:no_registered_keys)")
        except ImportError:
            items.append("redis_sessions(0:registry_unavailable)")
        except Exception as e:
            logger.debug(
                "security.usersessionregistry_cleanup_failed",
                error=e,
            )
            items.append("redis_sessions(0:cleanup_failed)")
        return items

    @staticmethod
    def _invalidate_django_db_sessions(user_id: int) -> list[str]:
        """Delete Django DB sessions (only when SESSION_ENGINE is a DB backend)."""
        items = []
        try:
            # Layered read (686 D1/D5) so the security domain is read through the
            # manager-observing seam; env base when no manager is registered.
            from baldur.settings.layered_provider import get_layered_settings
            from baldur.settings.security import SecuritySettings

            sec_settings = get_layered_settings(SecuritySettings, "security")
            session_engine = sec_settings.session_engine
            if "db" in session_engine or "cached_db" in session_engine:
                from baldur.factory import ProviderRegistry

                provider = ProviderRegistry.session_invalidation.get()
                items.extend(provider.invalidate_user_sessions(user_id))
            else:
                logger.debug(
                    "security.skipping_db_session_scan",
                    session_engine=session_engine,
                )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "security.django_session_cleanup_skipped",
                error=e,
            )
        return items

    @staticmethod
    def _run_invalidation_hooks(user_id: int) -> list[str]:
        """Run registered session-invalidation callbacks (JWT blacklist, etc.)."""
        items = []
        try:
            from baldur.services.security.hooks import (
                get_session_invalidation_hooks,
            )

            for hook in get_session_invalidation_hooks():
                try:
                    result = hook(user_id)
                    if result:
                        items.append(result)
                except Exception as hook_err:
                    logger.warning(
                        "security.session_invalidation_hook_failed",
                        hook_err=hook_err,
                    )
        except ImportError:
            pass
        return items

    def _invalidate_user_sessions(self, user_id: int) -> str:
        """
        Invalidate all sessions for a user.

        1. Delete Redis sessions via a reverse lookup through UserSessionRegistry
        2. Scan the django_session table only when SESSION_ENGINE is a DB backend
        3. Run registered callbacks (JWT blacklist, etc.)
        """
        try:
            invalidated_items = []
            invalidated_items.extend(self._invalidate_registry_sessions(user_id))
            invalidated_items.extend(self._invalidate_django_db_sessions(user_id))
            invalidated_items.extend(self._run_invalidation_hooks(user_id))

            logger.info(
                "security.invalidated_sessions_user",
                user_id=user_id,
                value=", ".join(invalidated_items),
            )

            # === Audit record: session invalidation ===
            log_security_violation_audit(
                violation_type="session_invalidation",
                action="invalidate_session",
                target=f"user:{user_id}",
                result="success",
                severity="high",
                operator="system",
                user_id=user_id,
                details={"invalidated": invalidated_items},
            )

            return (
                f"User sessions cleared for user {user_id}: "
                f"{', '.join(invalidated_items)}"
            )
        except Exception as e:
            logger.exception(
                "security.invalidate_sessions_failed",
                error=e,
            )

            log_security_violation_audit(
                violation_type="session_invalidation",
                action="invalidate_session",
                target=f"user:{user_id}",
                result="failed",
                severity="high",
                operator="system",
                user_id=user_id,
                details={"error": str(e)},
            )

            return f"Session invalidation attempted but failed: {e}"

    def _log_suspicious_ip(self, ip_address: str) -> str:
        """Log an IP address as suspicious for monitoring."""
        cache_key = f"{self.config.suspicious_ip_cache_prefix}{ip_address}"

        current_count = self.cache.get(cache_key) or 0
        new_count = current_count + 1
        self.cache.set(
            cache_key,
            new_count,
            ttl=timedelta(seconds=self.config.suspicious_ip_cache_timeout),
        )

        # Security: log only the masked IP
        masked_ip = mask_ip(ip_address)
        logger.info(
            "security.suspicious_ip_logged_count",
            masked_ip=masked_ip,
            new_count=new_count,
        )

        if new_count >= self.config.permanent_ban_threshold:
            self._permanent_ip_ban(ip_address)
            return f"IP {masked_ip} marked for permanent ban (violations: {new_count})"

        return f"IP {masked_ip} logged for monitoring (violations: {new_count})"

    def _temporary_ip_ban(self, ip_address: str, hours: int = 1) -> str:
        """Temporarily ban an IP address."""
        cache_key = f"{self.config.banned_ip_cache_prefix}{ip_address}"
        self.cache.set(
            cache_key,
            {"banned": True, "type": "temporary"},
            ttl=timedelta(hours=hours),
        )
        # Security: log only the masked IP
        masked_ip = mask_ip(ip_address)
        logger.info(
            "security.ip_temporarily_banned_hours",
            masked_ip=masked_ip,
            hours=hours,
        )

        # === Audit record: temporary IP ban (85_AUDIT_INTEGRATION Phase 1) ===
        log_security_violation_audit(
            violation_type="ip_ban_temporary",
            action="block_ip",
            target=f"ip:{ip_address}",
            result="success",
            severity="high",
            operator="system",
            source_ip=ip_address,
            details={"ban_type": "temporary", "duration_hours": hours},
        )

        # Security Hardening (214_SECURITY_VULNERABILITY_FIXES): avoid exposing the plaintext IP in the return value
        return f"IP {masked_ip} temporarily banned for {hours} hour(s)"

    def _permanent_ip_ban(self, ip_address: str) -> str:
        """Permanently ban an IP address."""
        cache_key = f"{self.config.banned_ip_cache_prefix}{ip_address}"
        self.cache.set(cache_key, {"banned": True, "type": "permanent"}, ttl=None)
        # Security: log only the masked IP
        masked_ip = mask_ip(ip_address)
        logger.warning(
            "security.ip_permanently_banned",
            masked_ip=masked_ip,
        )

        # === Audit record: permanent IP ban (85_AUDIT_INTEGRATION Phase 1) ===
        log_security_violation_audit(
            violation_type="ip_ban_permanent",
            action="block_ip",
            target=f"ip:{ip_address}",
            result="success",
            severity="critical",
            operator="system",
            source_ip=ip_address,
            details={"ban_type": "permanent"},
        )

        # Security Hardening (214_SECURITY_VULNERABILITY_FIXES): avoid exposing the plaintext IP in the return value
        return f"IP {masked_ip} permanently banned"

    def _remove_ip_ban(self, ip_address: str) -> str:
        """Remove IP ban (for rollback support)."""
        cache_key = f"{self.config.banned_ip_cache_prefix}{ip_address}"
        self.cache.delete(cache_key)
        # Security Hardening (214_SECURITY_VULNERABILITY_FIXES): mask the IP in logs/return values
        masked_ip = mask_ip(ip_address)
        logger.info(
            "security.ip_ban_removed",
            masked_ip=masked_ip,
        )
        return f"IP {masked_ip} ban removed"

    def is_ip_banned(self, ip_address: str) -> bool:
        """Check if an IP address is banned."""
        cache_key = f"{self.config.banned_ip_cache_prefix}{ip_address}"
        ban_info = self.cache.get(cache_key)
        return ban_info is not None and ban_info.get("banned", False)

    # Security3: pre-compile regexes at the class level (ReDoS prevention and performance improvement)
    _SENSITIVE_FIELDS: frozenset[str] = frozenset(
        {
            "password",
            "new_password",
            "old_password",
            "token",
            "access_token",
            "refresh_token",
            "api_key",
            "secret",
            "card_number",
            "cvv",
            "cvc",
            "credit_card",
            "private_key",
            "secret_key",
            "connection_string",
            "db_password",
            "redis_password",
        }
    )

    _INTERNAL_IP_PATTERNS: tuple[re.Pattern, ...] = (
        re.compile(r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"),
        re.compile(r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"),
        re.compile(r"192\.168\.\d{1,3}\.\d{1,3}"),
    )

    _SERVER_PATH_PATTERNS: tuple[re.Pattern, ...] = (
        re.compile(r"/home/[^/\s]+"),
        re.compile(r"/var/[^/\s]+/[^/\s]+"),
        re.compile(r"/etc/[^/\s]+"),
        re.compile(r"[A-Z]:\\Users\\[^\\\s]+", re.IGNORECASE),
        re.compile(r"/app/[^/\s]+/[^/\s]+"),
    )

    # Security3: limit the maximum input-string length (ReDoS prevention)
    _MAX_SANITIZE_STRING_LENGTH: int = 10000

    def _sanitize_request_data(self, raw_data: dict[str, Any] | None) -> dict[str, Any]:  # noqa: C901
        """
        Sanitize request data by removing sensitive fields and masking IPs/paths.

        FAIL-SECURE DESIGN:
        - If masking fails for any reason, return empty dict (not raw data)
        - This prevents accidental exposure of sensitive information
        - Input string length is limited to prevent ReDoS attacks
        """
        if not raw_data:
            return {}

        try:

            def mask_string(value: str) -> str:
                # Security3: truncate long strings before processing (ReDoS prevention)
                if len(value) > self._MAX_SANITIZE_STRING_LENGTH:
                    value = value[: self._MAX_SANITIZE_STRING_LENGTH] + "[TRUNCATED]"
                result = value
                for pattern in self._INTERNAL_IP_PATTERNS:
                    result = pattern.sub("[INTERNAL_IP]", result)
                for pattern in self._SERVER_PATH_PATTERNS:
                    result = pattern.sub("[SERVER_PATH]", result)
                return result

            def sanitize(data: Any, depth: int = 0) -> Any:
                # Security3: limit recursion depth (stack-overflow prevention)
                if depth > 20:
                    return "[MAX_DEPTH_EXCEEDED]"
                if isinstance(data, dict):
                    return {
                        k: (
                            "[REDACTED]"
                            if k.lower() in self._SENSITIVE_FIELDS
                            else sanitize(v, depth + 1)
                        )
                        for k, v in data.items()
                    }
                if isinstance(data, list):
                    # Security3: limit the number of list items
                    return [sanitize(item, depth + 1) for item in data[:100]]
                if isinstance(data, str):
                    return mask_string(data)
                return data

            return sanitize(raw_data)

        except Exception as e:
            logger.exception(
                "security.masking_failed_returning_placeholder",
                error=e,
            )
            return {"error": "MASKING_ERROR: SENSITIVE_DATA_HIDDEN"}

    def _send_security_notification(
        self,
        incident_id: int,
        incident_type: str,
        severity: str,
    ) -> None:
        """Send security notification for the incident."""
        try:
            notify_incident(
                incident_id=incident_id,
                incident_type=incident_type,
                severity=severity,
            )
        except Exception as e:
            logger.exception(
                "security.send_notification_incident_failed",
                incident_id=incident_id,
                error=e,
            )

    def _emit_critical_violation_event(
        self,
        violation_type: str,
        incident_id: int,
        source_ip: str | None,
        user_id: int | None,
    ) -> None:
        """Publish an event via the EventBus on a CRITICAL security violation."""
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.emit(
                event_type=EventType.SECURITY_VIOLATION_CRITICAL,
                data={
                    "violation_type": violation_type,
                    "severity": "critical",
                    "incident_id": incident_id,
                    "source_ip": source_ip,
                    "user_id": user_id,
                    "trigger_source": "security_violation_service",
                },
                source="security_violation_service",
            )
            logger.warning(
                "security_violation_service.emitted_incident",
                incident_id=incident_id,
                violation_type=violation_type,
            )
        except Exception as e:
            logger.exception(
                "security_violation_service.emit_critical_event_failed",
                error=e,
            )
