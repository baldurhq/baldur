"""
X-Test-Mode Base Module

Defines shared utilities, mixins, and helper functions.

Security (two layers):
Layer 1 - Django RBAC: HasChaosTestPermission permission class
Layer 2 - XTestModeMixin: X-Test-Mode header + environment variable checks

Requirements:
- X-Test-Mode: chaos-monkey header required
- DEBUG or the CHAOS_ENABLED environment variable required
- Fully blocked in production environments

Regional Scope:
- GLOBAL scope APIs require the X-Region header
- The X-Region value must match the current cluster region
- Returns 403 Forbidden on region mismatch
"""

import os
import re
import threading
import uuid
from typing import Any

import psutil
import structlog
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.request import Request
from rest_framework.response import Response

from baldur.api.django.permissions import HasChaosTestPermission
from baldur.audit.helpers import (
    log_xtest_cleanup_audit,
    log_xtest_injection_audit,
    log_xtest_operation_audit,
)
from baldur.core.test_mode_context import TestModeContext

logger = structlog.get_logger()


# =============================================================================
# Global Scope Endpoint Patterns (region boundary must be enforced)
# =============================================================================

# GLOBAL scope APIs: endpoints that can affect other regions.
# APIs matching these patterns require the X-Region header and a match against
# the current region.
GLOBAL_SCOPE_ENDPOINT_PATTERNS: list[str] = [
    r"xtest/emergency/global/.*",  # Global Emergency state change
    r"xtest/isolation/region/.*",  # Region isolation control
    r"xtest/governance/global/.*",  # Global governance settings
]

# Compiled patterns (performance optimization)
_COMPILED_GLOBAL_PATTERNS: list[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE) for pattern in GLOBAL_SCOPE_ENDPOINT_PATTERNS
]


# =============================================================================
# X-Test-Mode Security Mixin
# =============================================================================


class XTestModeMixin:
    """
    Two-layer X-Test-Mode security mixin.

    Security (two layers):
    Layer 1 - Django RBAC: HasChaosTestPermission (auth/group based)
    Layer 2 - XTestModeMixin: header + environment variable checks

    Requirements:
    1. Django authentication + HasChaosTestPermission
    2. X-Test-Mode: chaos-monkey header
    3. DEBUG=True or CHAOS_ENABLED=true
    4. ENVIRONMENT != production
    """

    # Layer 1 security: Django RBAC based authentication/permission
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [HasChaosTestPermission]

    # Layer 2 security: header validation constants
    CHAOS_HEADER = "X-Test-Mode"
    CHAOS_VALUE = "chaos-monkey"

    def is_chaos_allowed(self, request: Request) -> tuple[bool, str]:
        """
        Check whether chaos mode is allowed.

        Returns:
            (allowed: bool, reason: str)
        """
        # 1. Check the header
        header_value = request.headers.get(self.CHAOS_HEADER, "")
        if header_value != self.CHAOS_VALUE:
            return False, f"Missing or invalid {self.CHAOS_HEADER} header"

        # 2. Block production
        environment = os.getenv("ENVIRONMENT", "development").lower()
        if environment == "production":
            return False, "X-Test-Mode is disabled in production"

        # 3. Check DEBUG or CHAOS_ENABLED
        debug_mode = getattr(settings, "DEBUG", False)
        chaos_enabled = os.getenv("CHAOS_ENABLED", "false").lower() == "true"

        if not debug_mode and not chaos_enabled:
            return False, "Chaos mode requires DEBUG=True or CHAOS_ENABLED=true"

        return True, "Chaos mode allowed"

    def get_current_region(self) -> str | None:
        """
        Look up the current cluster's region.

        Reads the region from the BALDUR_NAMESPACE_REGION environment variable
        or from ClusterIdentity.

        Returns:
            Region identifier (e.g. 'seoul', 'tokyo') or None
        """
        # 1. Read directly from the environment variable (fastest)
        region = os.getenv("BALDUR_NAMESPACE_REGION")
        if region:
            return region

        # 2. Read from ClusterIdentity
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            return identity.region
        except Exception as e:
            logger.warning(
                "test.mode_failed_get",
                error=e,
            )
            return None

    def is_global_scope_endpoint(self, request: Request) -> bool:
        """
        Determine whether the current request targets a GLOBAL scope API.

        GLOBAL scope APIs are endpoints that can affect other regions:
        - xtest/emergency/global/* : global Emergency state change
        - xtest/isolation/region/* : region isolation control
        - xtest/governance/global/* : global governance settings

        Args:
            request: HTTP request object

        Returns:
            True for GLOBAL scope, False for LOCAL scope
        """
        path = request.path.lstrip("/")

        return any(pattern.search(path) for pattern in _COMPILED_GLOBAL_PATTERNS)

    def _get_endpoint_pattern_name(self, request: Request) -> str:
        """
        Extract the GLOBAL scope endpoint pattern name.

        Args:
            request: HTTP request object

        Returns:
            Pattern name (e.g., 'emergency', 'isolation', 'governance')
        """
        path = request.path.lower()
        if "emergency" in path:
            return "emergency"
        if "isolation" in path:
            return "isolation"
        if "governance" in path:
            return "governance"
        return "unknown"

    def _record_regional_scope_metrics(
        self,
        request: Request,
        current_region: str | None,
        target_region: str | None,
        result: str,
    ) -> None:
        """
        Record region scope related metrics.

        Args:
            request: HTTP request object
            current_region: current cluster region
            target_region: requested target region
            result: outcome ('allowed', 'denied_no_header', 'denied_mismatch',
                'denied_no_region')
        """
        try:
            from baldur.services.metrics.recorders import (
                record_xtest_cross_region_denied,
                record_xtest_global_scope_request,
            )

            pattern_name = self._get_endpoint_pattern_name(request)
            region = current_region or "unknown"

            # Record the GLOBAL scope request metric
            record_xtest_global_scope_request(
                endpoint_pattern=pattern_name,
                region=region,
                result=result,
            )

            # Extra metric when a cross-region request is denied
            if result == "denied_mismatch" and current_region and target_region:
                record_xtest_cross_region_denied(
                    current_region=current_region,
                    target_region=target_region,
                )

        except Exception as e:
            logger.warning(
                "test.mode_failed_record",
                error=e,
            )

    def check_regional_scope(self, request: Request) -> tuple[bool, Response | None]:
        """
        Validate the region boundary for GLOBAL scope APIs.

        On a GLOBAL scope API call:
        1. Verify the X-Region header is present
        2. Verify the header value matches the current cluster region
        3. Return 403 Forbidden on mismatch

        Args:
            request: HTTP request object

        Returns:
            (is_allowed, response): whether allowed, plus the Response on denial
        """
        # LOCAL scope APIs do not need a region check
        if not self.is_global_scope_endpoint(request):
            return True, None

        # Look up the current cluster region
        current_region = self.get_current_region()

        # Block GLOBAL scope when no region is configured
        if not current_region:
            environment = os.getenv("ENVIRONMENT", "development").lower()
            if environment == "development":
                # Only warn in development environments
                logger.warning("testmode.development_flag_set")
                return True, None

            logger.warning("test_mode.global_flag_warning")
            return False, Response(
                {
                    "status": "error",
                    "error": "region_not_configured",
                    "message": "BALDUR_NAMESPACE_REGION not configured. GLOBAL scope API denied.",
                    "hint": "Set BALDUR_NAMESPACE_REGION environment variable",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check the X-Region header
        target_region = request.headers.get("X-Region")

        if not target_region:
            logger.warning(
                "testmode.region_header_missing",
                current_region=current_region,
                request_path=request.path,
            )
            self._record_regional_scope_metrics(
                request, current_region, None, "denied_no_header"
            )
            return False, Response(
                {
                    "status": "error",
                    "error": "missing_region_header",
                    "message": "X-Region header required for GLOBAL scope API",
                    "current_region": current_region,
                    "hint": f"Add header 'X-Region: {current_region}'",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check that the regions match
        if target_region.lower() != current_region.lower():
            logger.warning(
                "testmode.cross_region_denied",
                current_region=current_region,
                target_region=target_region,
                request_path=request.path,
            )
            self._record_regional_scope_metrics(
                request, current_region, target_region, "denied_mismatch"
            )
            return False, Response(
                {
                    "status": "error",
                    "error": "cross_region_xtest_denied",
                    "message": (
                        f"Cross-region X-Test operation denied. "
                        f"Target region '{target_region}' does not match "
                        f"current cluster region '{current_region}'."
                    ),
                    "current_region": current_region,
                    "target_region": target_region,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        self._record_regional_scope_metrics(
            request, current_region, target_region, "allowed"
        )
        logger.debug(
            "testmode.regional_scope_validated",
            current_region=current_region,
            request_path=request.path,
        )
        return True, None

    def check_resource_constraints(self, request: Request) -> Response | None:
        """
        Check system resource constraints.

        Returns a 429 response when CPU exceeds 80% or memory exceeds 85%.
        Prevents X-Test from adding load while the system is already saturated.

        Returns:
            None if allowed, 429 Response if resource overloaded
        """
        try:
            from baldur_pro.services.chaos.safety_guard import (
                get_resource_guard,
            )

            guard = get_resource_guard()
            result = guard.is_safe_for_chaos()

            if not result.is_safe:
                logger.warning(
                    "test.mode_resource_constraint",
                    block_reason=result.block_reason,
                    request_user=request.user,
                )

                response = Response(
                    {
                        "status": "error",
                        **result.to_response_dict(),
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
                response["Retry-After"] = str(guard.get_recommended_wait())
                return response

            logger.debug(
                "test.mode_resource_check",
                cpu_percent=result.cpu_percent,
                memory_percent=result.memory_percent,
            )
            return None

        except ImportError:
            logger.debug("test_mode.resource_guard_unavailable")
            return None
        except Exception as e:
            logger.warning(
                "test.mode_resource_check",
                error=e,
            )
            # Allow conservatively when the check fails (availability first)
            return None

    def check_chaos_permission(self, request: Request) -> Response | None:
        """
        Check chaos permission. Returns a Response on failure.

        Validation order:
        1. Resource constraint check (CPU/memory saturation)
        2. Chaos mode allowance (header, environment variables)
        3. Region boundary check for GLOBAL scope APIs

        Returns:
            None if allowed, Response if denied
        """
        # 1. Resource constraint check (CPU/memory)
        resource_response = self.check_resource_constraints(request)
        if resource_response is not None:
            return resource_response

        # 2. Baseline chaos mode validation
        allowed, reason = self.is_chaos_allowed(request)
        if not allowed:
            logger.warning(
                "test.mode_denied_user",
                reason=reason,
                request_user=request.user,
            )
            return Response(
                {
                    "status": "error",
                    "error": "chaos_mode_disabled",
                    "message": reason,
                    "hint": f"Add header '{self.CHAOS_HEADER}: {self.CHAOS_VALUE}' and ensure CHAOS_ENABLED=true",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # 3. Region boundary check for GLOBAL scope APIs
        region_allowed, region_response = self.check_regional_scope(request)
        if not region_allowed:
            return region_response

        return None

    def get_xtest_session_id(self, request: Request) -> str:
        """Extract the X-Test session ID. Auto-generated when the header is absent."""
        header = request.headers.get("X-Test-Session")
        return str(header) if header else str(uuid.uuid4())[:8]

    def ensure_xtest_session(self, request: Request) -> str:
        """
        Create or refresh the X-Test session.

        Creates a new session when none exists, otherwise returns the existing
        one. Session metadata is stored in Redis and used during auto-cleanup.

        Args:
            request: HTTP request object

        Returns:
            Session ID
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)

        try:
            from baldur.services.xtest_session_manager import (
                get_xtest_session_manager,
            )

            session_manager = get_xtest_session_manager()

            # Check for an existing session
            existing = session_manager.get_session(session_id)
            if not existing:
                # Create a new session
                session_manager.create_session(session_id=session_id, user=user)
                logger.debug(
                    "test.mode_created_new",
                    session_id=session_id,
                )

        except ImportError:
            logger.debug("test_mode.session_manager_unavailable")
        except Exception as e:
            logger.warning(
                "test.mode_failed_ensure",
                error=e,
            )

        return session_id

    def register_xtest_artifact(
        self,
        request: Request,
        artifact_id: str,
        component: str,
    ) -> bool:
        """
        Register an X-Test artifact with the session.

        Registers DLQ entries, CB state changes, and similar objects created
        during a test so they can be cleaned up automatically when the session
        expires.

        Args:
            request: HTTP request object
            artifact_id: artifact ID (DLQ entry ID, CB service name, etc.)
            component: component name (dlq, cb, idempotency, etc.)

        Returns:
            Whether registration succeeded
        """
        session_id = self.get_xtest_session_id(request)

        try:
            from baldur.services.xtest_session_manager import (
                get_xtest_session_manager,
            )

            session_manager = get_xtest_session_manager()

            success = session_manager.register_artifact(
                session_id=session_id,
                artifact_id=artifact_id,
                component=component,
            )

            if success:
                logger.debug(
                    "cell_registry.bulkheads_registered",
                    session_id=session_id,
                    component=component,
                    artifact_id=artifact_id,
                )
            return success

        except ImportError:
            logger.debug("test_mode.session_manager_unavailable")
            return False
        except Exception as e:
            logger.warning(
                "test.mode_failed_register",
                error=e,
            )
            return False

    def enter_synthetic_context(self, request: Request) -> None:
        """
        Enter the synthetic request context.

        Called when X-Test request handling begins, to activate
        TestModeContext. From then on all metrics and Redis keys are tagged as
        synthetic requests. A session is created automatically if none exists.

        Args:
            request: HTTP request object
        """
        session_id = self.ensure_xtest_session(request)
        TestModeContext.enter_synthetic_mode(session_id=session_id)
        logger.debug(
            "test_mode.synthetic_context_unavailable",
            session_id=session_id,
        )

    def exit_synthetic_context(self) -> None:
        """
        Exit the synthetic request context.

        Called when X-Test request handling completes, to deactivate
        TestModeContext.
        """
        TestModeContext.exit_synthetic_mode()
        logger.debug("test_mode.synthetic_context_unavailable")

    def get_xtest_user(self, request: Request) -> str:
        """Extract the X-Test user."""
        if hasattr(request, "user") and request.user.is_authenticated:
            return str(request.user)
        return "anonymous"

    def log_xtest_audit(
        self,
        request: Request,
        action: str,
        component: str,
        details: dict[str, Any],
        result: str = "success",
        error_message: str | None = None,
    ) -> int | None:
        """
        Record an X-Test operation in the WAL audit log.

        Args:
            request: HTTP request object
            action: operation performed (inject, force_status, reset, query, etc.)
            component: target component (dlq, cb, idempotency, etc.)
            details: response data or operation details
            result: result status (success, failed, error)
            error_message: error message on failure

        Returns:
            WAL sequence number
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)
        trace_id = request.headers.get("X-Trace-ID")

        return log_xtest_operation_audit(
            session_id=session_id,
            action=action,
            component=component,
            details=details,
            result=result,
            user=user,
            trace_id=trace_id,
            error_message=error_message,
        )

    def log_xtest_injection(
        self,
        request: Request,
        component: str,
        injection_type: str,
        count: int,
        target_ids: list,
    ) -> int | None:
        """
        Record an X-Test data injection in the WAL audit log.

        Args:
            request: HTTP request object
            component: target component
            injection_type: injection type (create, override, etc.)
            count: number of injected items
            target_ids: list of created IDs
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)

        return log_xtest_injection_audit(
            session_id=session_id,
            component=component,
            injection_type=injection_type,
            count=count,
            target_ids=target_ids,
            user=user,
        )

    def log_xtest_cleanup(
        self,
        request: Request,
        component: str,
        cleaned_count: int,
        cleaned_ids: list,
    ) -> int | None:
        """
        Record an X-Test cleanup (reset) in the WAL audit log.

        Args:
            request: HTTP request object
            component: target component
            cleaned_count: number of cleaned items
            cleaned_ids: list of cleaned IDs
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)

        return log_xtest_cleanup_audit(
            session_id=session_id,
            component=component,
            cleaned_count=cleaned_count,
            cleaned_ids=cleaned_ids,
            user=user,
        )


# =============================================================================
# System Snapshot Utility
# =============================================================================


def collect_system_snapshot() -> dict[str, Any]:  # noqa: C901, PLR0912
    """Collect a system snapshot (CPU, memory, connections, error/request rate).

    Collects the system state to embed in a postmortem timeline snapshot.

    Returns:
        System snapshot dictionary:
        - timestamp: capture time
        - cpu_percent: CPU utilization
        - memory_percent: memory utilization
        - memory_used_mb: used memory (MB)
        - memory_available_mb: available memory (MB)
        - db_active_connections: number of active DB connections
        - error_rate: error rate (when available)
        - request_rate: request rate (when available)
    """
    try:
        # Read CPU/memory from the cache (~0ms); fall back to direct measurement
        # (100ms) when the cache is not running.
        try:
            from baldur.services.system_metrics_cache import (
                get_system_metrics_cache,
            )

            cache = get_system_metrics_cache()
            if cache.is_running():
                metrics = cache.get_metrics()
                snapshot = {
                    "timestamp": timezone.now().isoformat(),
                    "cpu_percent": metrics.cpu_percent,
                    "memory_percent": metrics.memory_percent,
                    "memory_used_mb": metrics.memory_used_mb,
                    "memory_available_mb": metrics.memory_available_mb,
                    "metrics_source": metrics.source,
                }
            else:
                raise RuntimeError("Cache not running")
        except Exception:
            # Fallback: direct measurement (preserves the previous behavior)
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            snapshot = {
                "timestamp": timezone.now().isoformat(),
                "cpu_percent": round(cpu_percent, 1),
                "memory_percent": round(memory.percent, 1),
                "memory_used_mb": round(memory.used / (1024 * 1024), 1),
                "memory_available_mb": round(memory.available / (1024 * 1024), 1),
                "metrics_source": "direct",
            }

        # DB active connection count via the pg_admin registry surface.
        try:
            from baldur.factory import ProviderRegistry

            pg_admin = ProviderRegistry.pg_admin.get()
            if pg_admin.is_available():
                snapshot["db_active_connections"] = (
                    pg_admin.get_active_connection_count()
                )
            else:
                snapshot["db_active_connections"] = None
        except Exception:
            snapshot["db_active_connections"] = None

        # Read the error rate from the error budget
        try:
            from baldur_pro.services.error_budget import (
                get_error_budget_service,
            )

            error_budget_service = get_error_budget_service()
            budget_status = error_budget_service.get_budget_status()
            if budget_status is not None:
                snapshot["error_rate"] = float(budget_status.burn_rate_1h)
                snapshot["remaining_budget_percent"] = float(
                    budget_status.budget_remaining_percent
                )
        except Exception:
            snapshot["error_rate"] = None

        # Read the request rate from the metric adapter
        try:
            from baldur.adapters.metrics import get_metric_adapter

            adapter = get_metric_adapter()
            # Try to read the request counter from the MetricSourceAdapter
            if hasattr(adapter, "get_counter_value"):
                request_counter = adapter.get_counter_value(
                    "baldur_http_requests_total"
                )
                if request_counter is not None:
                    snapshot["request_rate"] = request_counter
            else:
                snapshot["request_rate"] = None
        except Exception:
            snapshot["request_rate"] = None

        return snapshot
    except Exception as e:
        logger.warning(
            "test.mode_snapshot_collection",
            error=e,
        )
        # The snapshot is returned in an HTTP response body, so the exception
        # text stays server-side: it can carry adapter internals and connection
        # strings, and the caller only needs to know the snapshot is missing.
        # The log line above keeps the detail for whoever is debugging.
        return {
            "timestamp": timezone.now().isoformat(),
            "error": "snapshot_collection_failed",
        }


# =============================================================================
# In-Memory Event Storage + Redis Persistence
# =============================================================================

_healing_events_lock = threading.Lock()
_healing_events: list[dict[str, Any]] = []
_max_events = 500


def add_healing_event(event: dict[str, Any]) -> None:
    """
    Record a healing event.

    Stores the event in Redis to keep multiple workers in sync.
    Falls back to in-memory-only storage when Redis fails.
    """
    global _healing_events

    # Try to store in Redis
    try:
        from baldur.services.healing_events_store import add_healing_event_redis

        add_healing_event_redis(event)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "test.mode_redis_event",
            error=e,
        )

    # Also store in memory (fast-lookup cache)
    with _healing_events_lock:
        if "recorded_at" not in event:
            event["recorded_at"] = timezone.now().isoformat()
        _healing_events.append(event)
        if len(_healing_events) > _max_events:
            _healing_events = _healing_events[-_max_events:]


def get_healing_events(limit: int = 50, use_redis: bool = True) -> list[dict[str, Any]]:
    """
    Query healing events.

    Tries Redis first and falls back to the in-memory store on failure.

    Args:
        limit: maximum number of events to return
        use_redis: whether to query Redis

    Returns:
        List of event dictionaries (newest first)
    """
    if use_redis:
        try:
            from baldur.services.healing_events_store import (
                get_healing_events_redis,
            )

            return get_healing_events_redis(limit=limit, days_back=1)
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "test.mode_redis_event",
                error=e,
            )

    # In-Memory fallback
    with _healing_events_lock:
        return list(_healing_events[-limit:])


def get_healing_events_count(use_redis: bool = True) -> int:
    """
    Total number of healing events.

    Args:
        use_redis: whether to query Redis

    Returns:
        Total event count
    """
    if use_redis:
        try:
            from baldur.services.healing_events_store import (
                get_healing_events_count_redis,
            )

            return get_healing_events_count_redis(days_back=1)
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "test.mode_redis_event",
                error=e,
            )

    # In-Memory fallback
    with _healing_events_lock:
        return len(_healing_events)


def clear_healing_events() -> int:
    """
    Clear healing events (for testing).

    Returns:
        Number of events cleared
    """
    global _healing_events

    # Try to clear Redis
    try:
        from baldur.services.healing_events_store import clear_healing_events_redis

        clear_healing_events_redis()
    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "test.mode_redis_event",
            error=e,
        )

    # Clear in-memory storage
    with _healing_events_lock:
        count = len(_healing_events)
        _healing_events = []
        return count
