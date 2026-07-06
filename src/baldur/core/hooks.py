"""
Hook Registry System for Baldur Infrastructure.

Enterprise-grade hook system for runtime behavior modification.
Supports priority-based execution and audit logging integration.

Key Features:
- Priority-based hook execution (higher priority runs first)
- Audit trail for all bypass decisions
- Environment-conditional hook registration
- Thread-safe operation

Usage:
    # Register a bypass hook (in testing/resilience module)
    from baldur.core.hooks import BypassRegistry

    def platinum_bypass_hook(request) -> bool:
        return request.headers.get("X-Test-Mode") == "platinum"

    BypassRegistry.register(
        platinum_bypass_hook,
        priority=1000,
        name="platinum_mode",
        description="Bypass for PLATINUM stress testing"
    )

    # Check bypass in middleware
    should_bypass, reason = BypassRegistry.should_bypass(request)
    if should_bypass:
        audit_logger.log_bypass(reason, request)

기업 감사 준수: 모든 바이패스 결정이 로그로 기록됩니다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = structlog.get_logger()


# =============================================================================
# Hook Data Structures
# =============================================================================


@dataclass
class HookInfo:
    """Metadata for a registered hook."""

    func: Callable[[HttpRequest], bool]
    priority: int
    name: str
    description: str
    registered_at: str = field(default_factory=lambda: utc_now().isoformat())
    invocation_count: int = 0
    bypass_count: int = 0

    def __call__(self, request: HttpRequest) -> bool:
        """Execute the hook and track statistics."""
        self.invocation_count += 1
        result = self.func(request)
        if result:
            self.bypass_count += 1
        return result


@dataclass
class BypassResult:
    """Result of a bypass check with audit information."""

    bypassed: bool
    reason: str
    hook_name: str
    priority: int
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())
    request_path: str = ""
    request_method: str = ""

    def to_audit_dict(self) -> dict[str, Any]:
        """Convert to audit log format."""
        return {
            "event_type": "bypass_decision",
            "bypassed": self.bypassed,
            "reason": self.reason,
            "hook_name": self.hook_name,
            "priority": self.priority,
            "timestamp": self.timestamp,
            "request_path": self.request_path,
            "request_method": self.request_method,
        }


# =============================================================================
# Bypass Registry (Singleton)
# =============================================================================


class BypassRegistry:
    """
    Enterprise-grade Hook Registry for bypass decisions.

    All bypass decisions are:
    1. Priority-ordered (higher priority runs first)
    2. Audit-logged (for compliance)
    3. Thread-safe

    Architecture:
    - Production code calls should_bypass() only
    - Test/Resilience modules register hooks conditionally
    - All decisions are traceable

    Example:
        # In rate_limit.py (production code)
        result = BypassRegistry.should_bypass(request)
        if result.bypassed:
            self._log_bypass_audit(result)
            return True
    """

    _hooks: list[HookInfo] = []
    _lock = threading.Lock()
    _audit_logger: Any | None = None
    _initialized: bool = False

    @classmethod
    def register(
        cls,
        hook_func: Callable[[HttpRequest], bool],
        priority: int = 100,
        name: str | None = None,
        description: str = "",
    ) -> None:
        """
        Register a bypass hook.

        Args:
            hook_func: Function that returns True if bypass should occur
            priority: Higher priority hooks run first (default: 100)
            name: Hook identifier for audit logs (default: function name)
            description: Human-readable description

        Example:
            def my_hook(request):
                return request.headers.get("X-Test") == "true"

            BypassRegistry.register(my_hook, priority=500, name="test_mode")
        """
        hook_name = name or hook_func.__name__

        with cls._lock:
            # Check for duplicate registration
            existing = [h for h in cls._hooks if h.name == hook_name]
            if existing:
                logger.warning(
                    "bypass_registry.hook_already_registered_skipping",
                    hook_name=hook_name,
                )
                return

            hook_info = HookInfo(
                func=hook_func,
                priority=priority,
                name=hook_name,
                description=description,
            )

            cls._hooks.append(hook_info)
            # Sort by priority descending (higher priority first)
            cls._hooks.sort(key=lambda h: -h.priority)

            logger.info(
                "cell_registry.bulkheads_registered",
                hook_name=hook_name,
                priority=priority,
                hooks_count=len(cls._hooks),
            )

    @classmethod
    def unregister(cls, name: str) -> bool:
        """
        Unregister a hook by name.

        Returns:
            True if hook was found and removed
        """
        with cls._lock:
            original_count = len(cls._hooks)
            cls._hooks = [h for h in cls._hooks if h.name != name]
            removed = len(cls._hooks) < original_count

            if removed:
                logger.info(
                    "bypass_registry.unregistered_hook",
                    hook_name=name,
                )

            return removed

    @classmethod
    def should_bypass(cls, request: HttpRequest) -> BypassResult:
        """
        Check if request should bypass (e.g., rate limiting).

        Executes hooks in priority order until one returns True.
        All decisions are returned with audit information.

        Args:
            request: Django HttpRequest object

        Returns:
            BypassResult with bypass decision and audit info
        """
        request_path = getattr(request, "path", "unknown")
        request_method = getattr(request, "method", "unknown")

        with cls._lock:
            hooks_snapshot = list(cls._hooks)

        for hook in hooks_snapshot:
            try:
                if hook(request):
                    result = BypassResult(
                        bypassed=True,
                        reason=hook.description or f"Hook '{hook.name}' triggered",
                        hook_name=hook.name,
                        priority=hook.priority,
                        request_path=request_path,
                        request_method=request_method,
                    )

                    # Audit log
                    cls._log_bypass(result)

                    return result

            except Exception as e:
                logger.exception(
                    "bypass_registry.hook_raised_exception",
                    hook=hook.name,
                    error=e,
                )
                # Continue to next hook on error (fail-open for hooks)

        # No bypass
        return BypassResult(
            bypassed=False,
            reason="No bypass hook triggered",
            hook_name="",
            priority=0,
            request_path=request_path,
            request_method=request_method,
        )

    @classmethod
    def _log_bypass(cls, result: BypassResult) -> None:
        """Log bypass decision to audit system."""
        # Standard logging
        logger.info(
            "bypass_registry.bypass_granted",
            hook_name=result.hook_name,
            reason=result.reason,
            request_path=result.request_path,
        )

        # Audit system integration (lazy init)
        try:
            if cls._audit_logger is None:
                from baldur.audit import get_audit_logger

                cls._audit_logger = get_audit_logger()

            if cls._audit_logger:
                # `AuditLogger` exposes type-specific methods (`log_change`,
                # `log_config_update`, ...); duck-type the generic `.log()`
                # entry so PRO or test stubs that still expose it work, and
                # OSS-only deployments fall open without an AttributeError.
                log_fn = getattr(cls._audit_logger, "log", None)
                if callable(log_fn):
                    log_fn(result.to_audit_dict())
        except Exception as e:
            # Audit failure should not block bypass
            logger.warning(
                "bypass_registry.audit_log_failed",
                error=e,
            )

    @classmethod
    def get_registered_hooks(cls) -> list[dict[str, Any]]:
        """Get list of all registered hooks (for debugging/monitoring)."""
        with cls._lock:
            return [
                {
                    "name": h.name,
                    "priority": h.priority,
                    "description": h.description,
                    "registered_at": h.registered_at,
                    "invocation_count": h.invocation_count,
                    "bypass_count": h.bypass_count,
                }
                for h in cls._hooks
            ]

    @classmethod
    def clear_all(cls) -> int:
        """
        Clear all registered hooks. For testing only.

        Returns:
            Number of hooks cleared
        """
        with cls._lock:
            count = len(cls._hooks)
            cls._hooks = []
            logger.warning(
                "bypass_registry.cleared_all_hooks",
                hooks_count=count,
            )
            return count

    @classmethod
    def get_statistics(cls) -> dict[str, Any]:
        """Get registry statistics for monitoring."""
        with cls._lock:
            total_invocations = sum(h.invocation_count for h in cls._hooks)
            total_bypasses = sum(h.bypass_count for h in cls._hooks)

            # Build hooks list inside the lock to avoid re-acquiring
            hooks_list = [
                {
                    "name": h.name,
                    "priority": h.priority,
                    "description": h.description,
                    "registered_at": h.registered_at,
                    "invocation_count": h.invocation_count,
                    "bypass_count": h.bypass_count,
                }
                for h in cls._hooks
            ]

            return {
                "total_hooks": len(cls._hooks),
                "total_invocations": total_invocations,
                "total_bypasses": total_bypasses,
                "bypass_rate": (
                    total_bypasses / total_invocations if total_invocations > 0 else 0
                ),
                "hooks": hooks_list,
            }


# =============================================================================
# Convenience Functions
# =============================================================================


def register_bypass_hook(
    priority: int = 100,
    name: str | None = None,
    description: str = "",
) -> Callable:
    """
    Decorator for registering bypass hooks.

    Example:
        @register_bypass_hook(priority=1000, name="platinum")
        def platinum_hook(request):
            return request.headers.get("X-Test-Mode") == "platinum"
    """

    def decorator(func: Callable[[HttpRequest], bool]) -> Callable:
        BypassRegistry.register(
            func, priority=priority, name=name, description=description
        )
        return func

    return decorator


# =============================================================================
# Error Budget Bypass Hooks
# =============================================================================


def _error_budget_admin_bypass(request: HttpRequest) -> bool:
    """
    Bypass Error Budget Gate restrictions for admin API requests.

    Admin endpoints (/admin/, /api/admin/) always need to be accessible
    even when error budget is exhausted to allow operations teams to:
    - Monitor system status
    - Apply manual overrides
    - Execute recovery procedures

    Returns:
        True if request should bypass Error Budget restrictions
    """
    path = request.path.lower()
    admin_prefixes = ("/admin/", "/api/admin/", "/_admin/")
    return any(path.startswith(prefix) for prefix in admin_prefixes)


def _error_budget_critical_path_bypass(request: HttpRequest) -> bool:
    """
    Bypass Error Budget Gate restrictions for critical path requests.

    Critical paths are essential operations that must succeed even during
    error budget exhaustion to maintain system integrity:
    - Health checks (for load balancer)
    - Liveness/readiness probes (for Kubernetes)
    - Internal service-to-service auth

    Returns:
        True if request should bypass Error Budget restrictions
    """
    path = request.path.lower()
    critical_paths = (
        "/health",
        "/healthz",
        "/ready",
        "/readyz",
        "/live",
        "/livez",
        "/_internal/",
        "/api/v1/ping",
    )

    # Check path-based critical routes
    if any(
        path.startswith(critical) or path == critical for critical in critical_paths
    ):
        return True

    # Check header-based critical path marker
    critical_header = request.headers.get("X-Critical-Path", "").lower()
    return critical_header == "true"


def register_error_budget_bypass_hooks() -> None:
    """
    Register Error Budget bypass hooks with the BypassRegistry.

    Should be called during application initialization to enable
    critical path and admin bypasses for Error Budget Gate.

    Priorities:
    - Admin bypass: 950 (high priority, just below platinum)
    - Critical path bypass: 900 (high priority for health checks)
    """
    BypassRegistry.register(
        _error_budget_admin_bypass,
        priority=950,
        name="error_budget_admin",
        description="Bypass Error Budget restrictions for admin API requests",
    )

    BypassRegistry.register(
        _error_budget_critical_path_bypass,
        priority=900,
        name="error_budget_critical_path",
        description="Bypass Error Budget restrictions for critical path requests (health, probes)",
    )

    logger.info("cell_registry.bulkheads_registered")


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "BypassRegistry",
    "BypassResult",
    "HookInfo",
    "register_bypass_hook",
    "register_error_budget_bypass_hooks",
]
