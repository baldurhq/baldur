"""
Baldur Middleware

Middleware for automatic failure detection and DLQ storage.

Features:
1. DB error detection: OperationalError, InterfaceError, etc.
2. HTTP 5xx error detection: 500, 502, 503, 504
3. CircuitBreaker auto-recording: record_failure() on failure, record_success() on recovery
4. DLQ auto-storage: automatically enqueue recoverable requests into DLQ
5. Self-Audit integration: write events to hash-chain audit log

Domain separation (v6.2.0):
- 5xx responses record CB failure against the inferred domain (not always "database")
- DB exceptions always record against the "database" domain
- pool_circuit_breaker records failure only for database-domain failures
- _is_cb_open() checks both the database CB (shared) and the per-domain CB

CRITICAL: This middleware must be placed after HealthBridgeMiddleware!

Usage in settings.py:
    MIDDLEWARE = [
        "baldur.api.django.middleware.HealthBridgeMiddleware",  # must be first
        "baldur.api.django.middleware.BaldurMiddleware",   # must be second
        "django.middleware.security.SecurityMiddleware",
        ...
    ]
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from baldur.dlq.helpers import store_to_dlq
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


class BaldurMiddleware:
    """
    Baldur Middleware for automatic failure detection and DLQ storage.
    """

    # Monitored DB exception class names (stored as strings for lazy import)
    MONITORED_DB_ERRORS = (
        "OperationalError",  # DB connection errors, query failures
        "InterfaceError",  # DB interface errors
        "DatabaseError",  # generic DB errors
        "ConnectionDoesNotExist",  # Django connection does not exist
    )

    # DLQ-eligible path patterns — loaded from settings (domain-free)
    DLQ_ELIGIBLE_PATHS: list = []  # initialized by _load_path_patterns()

    # Paths treated as infrastructure failures — loaded from settings (domain-free)
    INFRASTRUCTURE_FAILURE_PATHS: list = []  # initialized by _load_path_patterns()

    # Domain inference mapping — loaded from settings (domain-free)
    DOMAIN_MAPPING: dict[str, str] = {}  # initialized by _load_path_patterns()

    _paths_loaded: bool = False

    # CB domain name for actual database errors and pool circuit breaker
    CB_DATABASE_DOMAIN = "database"

    def __init__(self, get_response: Callable):
        """Initialize middleware."""
        from baldur.audit import AuditLogger
        from baldur.services.circuit_breaker.convenience import (
            CircuitBreakerService,
        )
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        self.get_response = get_response
        self._audit_logger: AuditLogger | None = None
        self._cb_service: CircuitBreakerService | None = None
        self._initialized = False
        # Safe defaults overridden by _lazy_init() from BaldurMiddlewareSettings
        self._cb_status_codes: frozenset[int] = frozenset({500, 502, 503, 504})
        self._rate_limit_codes: frozenset[int] = frozenset({429})
        self._retry_after_max: int = 300
        self._rate_limit_tracker: RateLimitTracker | None = None

    def _lazy_init(self) -> None:
        """Lazy initialization to avoid circular imports."""
        if self._initialized:
            return

        # Load path patterns once per class
        self._load_path_patterns()

        try:
            from baldur.services.circuit_breaker.convenience import (
                get_circuit_breaker_service,
            )

            self._cb_service = get_circuit_breaker_service()
        except Exception as e:
            logger.warning(
                "baldur_middleware.cb_service_init_failed",
                error=e,
            )

        try:
            from baldur.audit import get_audit_logger

            self._audit_logger = get_audit_logger()
        except Exception as e:
            logger.warning(
                "baldur_middleware.audit_logger_init_failed",
                error=e,
            )

        try:
            from baldur.settings.middleware import get_middleware_settings

            mw = get_middleware_settings()
            self._cb_status_codes = frozenset(mw.cb_status_codes)
            self._rate_limit_codes = frozenset(mw.rate_limit_codes)
            self._retry_after_max = mw.retry_after_max
        except Exception as e:
            logger.warning(
                "baldur_middleware.settings_load_failed",
                error=e,
            )

        try:
            from baldur.services.circuit_breaker.rate_limit_tracker import (
                get_rate_limit_tracker,
            )

            self._rate_limit_tracker = get_rate_limit_tracker()
        except Exception as e:
            logger.warning(
                "baldur_middleware.rate_limit_tracker_init_failed",
                error=e,
            )

        self._initialized = True

    @classmethod
    def _load_path_patterns(cls) -> None:
        """
        Load path patterns from Django settings (domain-free).

        Define the following in settings.py:

        BALDUR_DLQ_ELIGIBLE_PATHS = [
            r"^/api/orders/",
            r"^/api/payments/",
            r"^/api/cart/",
            ...
        ]

        BALDUR_INFRA_FAILURE_PATHS = [
            r"^/api/orders/",
            r"^/api/payments/",
            ...
        ]

        BALDUR_DOMAIN_MAPPING = {
            "/payments/": "payment",
            "/checkout/": "payment",
            "/orders/": "order",
            "/points/": "point",
            "/cart/": "cart",
            "/webhooks/": "webhook",
        }
        """
        if cls._paths_loaded:
            return

        from django.conf import settings

        # DLQ-eligible paths
        dlq_patterns = getattr(settings, "BALDUR_DLQ_ELIGIBLE_PATHS", [])
        cls.DLQ_ELIGIBLE_PATHS = [re.compile(p) for p in dlq_patterns]

        # Infrastructure failure paths
        infra_patterns = getattr(settings, "BALDUR_INFRA_FAILURE_PATHS", [])
        cls.INFRASTRUCTURE_FAILURE_PATHS = [re.compile(p) for p in infra_patterns]

        # Domain mapping
        cls.DOMAIN_MAPPING = getattr(settings, "BALDUR_DOMAIN_MAPPING", {})

        cls._paths_loaded = True

        logger.info(
            "baldur_middleware.loaded_patterns",
            dlq_eligible_paths_count=len(cls.DLQ_ELIGIBLE_PATHS),
            infra_paths_count=len(cls.INFRASTRUCTURE_FAILURE_PATHS),
            domain_mapping_count=len(cls.DOMAIN_MAPPING),
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:  # noqa: C901, PLR0912
        """Process request/response with baldur logic."""
        from django.http import JsonResponse

        self._lazy_init()

        request_data = self._capture_request_data(request)

        # =====================================================================
        # Preemptive DLQ storage when CB is OPEN (automatic routing)
        # =====================================================================
        if self._is_cb_open(request) and self._is_dlq_eligible(request):
            error_context = {
                "error_type": "CIRCUIT_BREAKER_OPEN",
                "error_message": "Circuit breaker is OPEN - request queued for later retry",
                "path": request.path,
                "method": request.method,
                "preemptive": True,
            }

            dlq_id = self._store_to_dlq(request_data, error_context, request=request)

            logger.info(
                "baldur_middleware.preemptive_dlq_cb_open",
                dlq_id=dlq_id,
                request_path=request.path,
            )

            self._log_audit_event(
                "preemptive_dlq_stored",
                {
                    "dlq_id": dlq_id,
                    "reason": "circuit_breaker_open",
                    "path": request.path,
                },
                request=request,
            )

            return JsonResponse(
                {
                    "error": "Service temporarily unavailable",
                    "code": "CIRCUIT_BREAKER_OPEN",
                    "retry_after": 30,
                    "dlq_stored": True,
                    "dlq_id": dlq_id,
                    "message": "Request has been queued for automatic retry when service recovers",
                },
                status=503,
            )

        # =====================================================================
        # Main path: process request and monitor exceptions / responses
        # =====================================================================
        try:
            response: HttpResponse = self.get_response(request)

            # Record all requests that reached upstream for rate calculation (D1)
            if self._rate_limit_tracker:
                try:
                    domain = self._infer_domain(request.path)
                    self._rate_limit_tracker.record_request(domain)
                except Exception as e:
                    logger.debug(
                        "baldur_middleware.record_request_failed", error=str(e)
                    )

        except Exception as e:
            error_type = type(e).__name__

            if error_type in self.MONITORED_DB_ERRORS or self._is_db_connection_error(
                e
            ):
                db_error_context = {
                    "error_type": error_type,
                    "error_message": str(e),
                    "path": request.path,
                    "method": request.method,
                }

                # DB exceptions always record against the database domain (D2)
                self._record_cb_failure(
                    self.CB_DATABASE_DOMAIN, db_error_context, request=request
                )

                if self._is_dlq_eligible(request):
                    self._store_to_dlq(request_data, db_error_context, request=request)

                return JsonResponse(
                    {
                        "error": "Service temporarily unavailable",
                        "code": "DB_CONNECTION_ERROR",
                        "retry_after": 30,
                        "dlq_stored": self._is_dlq_eligible(request),
                    },
                    status=503,
                )

            raise

        # HTTP 5xx CB failure recording
        if response.status_code in self._cb_status_codes:
            is_infra_failure_path = self._is_infrastructure_failure_path(request)
            # Record against the inferred domain, not the hardcoded "database" (D1)
            domain = self._infer_domain(request.path)

            error_context = {
                "error_type": f"HTTP_{response.status_code}",
                "error_message": f"Server returned {response.status_code}",
                "path": request.path,
                "method": request.method,
                "infrastructure_failure": is_infra_failure_path,
            }

            self._record_cb_failure(domain, error_context, request=request)

            if is_infra_failure_path:
                logger.warning(
                    "baldur_middleware.infra_failure_detected",
                    request_path=request.path,
                    response=response.status_code,
                )

            if self._is_dlq_eligible(request):
                self._store_to_dlq(request_data, error_context, request=request)

        # HTTP 429 rate limit cascade detection
        elif response.status_code in self._rate_limit_codes:
            if not self._is_internal_429(response):
                self._handle_external_429(request, response)

        else:
            if response.status_code < 400:
                self._record_cb_success(self._infer_domain(request.path))

        return response

    def _is_internal_429(self, response: Any) -> bool:
        """Check if 429 was generated internally (not from an upstream service).

        Internal 429s from HybridRateLimitMiddleware or DRF AdaptiveDRFThrottle
        must not trigger CB cascade detection — they are self-imposed limits.
        """
        # HybridRateLimitMiddleware sets X-RateLimit-Mode on its 429 responses
        if response.get("X-RateLimit-Mode"):
            return True
        # X-RateLimit-Limit header indicates any local rate limiter
        return bool(response.get("X-RateLimit-Limit"))

    def _parse_retry_after(self, response: Any) -> float | None:
        """Parse Retry-After header, supporting both seconds and HTTP-date formats.

        Returns the wait time in seconds clamped to _retry_after_max, or None on
        parse failure (fail-open — Retry-After simply not forwarded).
        """
        raw = response.get("Retry-After")
        if not raw:
            return None
        try:
            seconds = float(raw)
        except (ValueError, TypeError):
            # HTTP-date format: "Fri, 31 Dec 2025 23:59:59 GMT"
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(raw)
                seconds = (dt - utc_now()).total_seconds()
                if seconds < 0:
                    return None
            except Exception:
                return None
        # Guard against negative values and NaN (float("nan") >= 0 is False)
        if not (seconds >= 0):
            return None
        return min(seconds, self._retry_after_max)

    def _handle_external_429(self, request: Any, response: Any) -> None:
        """Handle an external 429 response: cascade detection + EventBus + audit."""
        retry_after = self._parse_retry_after(response)
        domain = self._infer_domain(request.path)

        cb_result = None
        if self._cb_service:
            try:
                cb_result = self._cb_service.record_rate_limit_response(domain)
            except Exception as e:
                logger.warning(
                    "baldur_middleware.rate_limit_cb_record_failed",
                    error=e,
                )

        try:
            from baldur.services.rate_limit_coordinator.helpers import (
                _emit_rate_limit_event,
            )

            _emit_rate_limit_event(
                "RATE_LIMIT_429",
                {
                    "service_name": domain,
                    "path": request.path,
                    "retry_after": retry_after,
                },
            )
        except ImportError:
            pass

        if retry_after is not None:
            response["Retry-After"] = str(int(retry_after))

        self._log_audit_event(
            "rate_limit_detected",
            {
                "domain": domain,
                "path": request.path,
                "retry_after": retry_after,
                "cb_opened": cb_result.success if cb_result else False,
            },
            request=request,
        )

    def _is_db_connection_error(self, error: Exception) -> bool:
        """Check if the error is a DB connection related error."""
        error_str = str(error).lower()
        db_error_keywords = [
            "connection refused",
            "too many clients",
            "connection timed out",
            "could not connect",
            "server closed the connection",
            "connection reset",
            "pool exhausted",
            "no connection available",
        ]
        return any(keyword in error_str for keyword in db_error_keywords)

    def _capture_request_data(self, request: HttpRequest) -> dict[str, Any]:
        """Capture request data for DLQ storage."""
        try:
            body = {}
            if request.body:
                try:
                    body = json.loads(request.body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = {"raw": request.body.decode("utf-8", errors="replace")}

            return {
                "method": request.method,
                "path": request.path,
                "query_string": request.META.get("QUERY_STRING", ""),
                "body": body,
                "headers": {
                    "content_type": request.META.get("CONTENT_TYPE", ""),
                    "user_agent": request.META.get("HTTP_USER_AGENT", ""),
                    "x_request_id": request.META.get("HTTP_X_REQUEST_ID", ""),
                    "x_idempotency_key": request.META.get("HTTP_X_IDEMPOTENCY_KEY", ""),
                },
                "user_id": (
                    getattr(request.user, "id", None)
                    if hasattr(request, "user")
                    else None
                ),
                "timestamp": utc_now().isoformat(),
            }
        except Exception as e:
            logger.warning(
                "baldur_middleware.request_capture_failed",
                error=e,
            )
            return {"path": getattr(request, "path", "unknown"), "error": str(e)}

    def _is_dlq_eligible(self, request: HttpRequest) -> bool:
        """Check if request is eligible for DLQ storage."""
        if request.method not in ("POST", "PUT", "PATCH"):
            return False

        return any(pattern.match(request.path) for pattern in self.DLQ_ELIGIBLE_PATHS)

    def _is_infrastructure_failure_path(self, request: HttpRequest) -> bool:
        """Check if request path is an infrastructure failure path."""
        for pattern in self.INFRASTRUCTURE_FAILURE_PATHS:
            if pattern.match(request.path):
                return True
        return False

    def _is_cb_open(self, request: HttpRequest | None = None) -> bool:
        """Check if any relevant CircuitBreaker is in open or half_open state.

        Checks three independent sources (D5):
        1. Database CB — shared resource; affects all requests when open.
        2. Domain-specific CB — affects only requests for that domain.
        3. pool_circuit_breaker — pool exhaustion; affects all requests.
        """
        try:
            if self._cb_service and self._cb_service.is_enabled:
                # Database CB is a shared resource: open state affects all requests
                db_state = self._cb_service.get_state(self.CB_DATABASE_DOMAIN)
                if db_state and db_state.lower() in ("open", "half_open"):
                    logger.debug(
                        "baldur_middleware.cb_service",
                        state=db_state,
                        cb_service_name=self.CB_DATABASE_DOMAIN,
                    )
                    return True

                # Domain-specific CB: open state affects only that domain's requests
                if request:
                    domain = self._infer_domain(request.path)
                    if domain != self.CB_DATABASE_DOMAIN:
                        domain_state = self._cb_service.get_state(domain)
                        if domain_state and domain_state.lower() in (
                            "open",
                            "half_open",
                        ):
                            logger.debug(
                                "baldur_middleware.cb_service",
                                state=domain_state,
                                cb_service_name=domain,
                            )
                            return True

            try:
                from baldur.api.django.pool_circuit_breaker import (
                    pool_circuit_breaker,
                )

                pool_state = pool_circuit_breaker.state
                if pool_state in ("open", "half_open"):
                    logger.debug(
                        "baldur_middleware.poolcb",
                        pool_state=pool_state,
                    )
                    return True
            except Exception:
                pass

            return False

        except Exception as e:
            logger.warning(
                "baldur_middleware.cb_state_check_failed",
                error=e,
            )
            return False

    def _record_cb_failure(
        self,
        service_name: str,
        error_context: dict[str, Any],
        request: HttpRequest | None = None,
    ) -> None:
        """Record failure to CircuitBreaker for the given service domain (D3).

        Callers are responsible for supplying the correct domain:
        - DB exception handler passes CB_DATABASE_DOMAIN (D2)
        - 5xx handler passes _infer_domain(request.path) (D1)
        """
        try:
            if self._cb_service and self._cb_service.is_enabled:
                self._cb_service.record_failure(
                    service_name,
                    error_context=error_context,
                )
                logger.info(
                    "baldur_middleware.cb_failure_recorded",
                    cb_service_name=service_name,
                    error_context=error_context.get("error_type"),
                )

                self._log_audit_event(
                    "cb_failure_recorded",
                    {"service": service_name, "error_context": error_context},
                    request=request,
                )
        except Exception as e:
            logger.exception(
                "baldur_middleware.cb_failure_recording_failed",
                error=e,
            )

        # Pool CB records only for actual DB failures — not for non-DB 5xx (D6)
        if service_name == self.CB_DATABASE_DOMAIN:
            try:
                from baldur.api.django.pool_circuit_breaker import (
                    pool_circuit_breaker,
                )

                pool_circuit_breaker.record_failure()
                logger.info(
                    "baldur_middleware.poolcb_failure_recorded",
                    pool_circuit_breaker=pool_circuit_breaker.state,
                    failure_count=pool_circuit_breaker._failure_count,
                )
            except Exception as e:
                logger.warning(
                    "baldur_middleware.poolcb_failed",
                    error=e,
                )

    def _record_cb_success(self, service_name: str) -> None:
        """Record success to CircuitBreaker for the given domain (D4).

        When service_name is not the database domain, also records success for
        the database domain so the database CB can recover through the middleware
        success path (no DOMAIN_MAPPING pattern maps to "database" directly).
        """
        try:
            if self._cb_service and self._cb_service.is_enabled:
                self._cb_service.record_success(service_name)
                if service_name != self.CB_DATABASE_DOMAIN:
                    self._cb_service.record_success(self.CB_DATABASE_DOMAIN)
        except Exception:
            pass

    def _store_to_dlq(
        self,
        request_data: dict[str, Any],
        error_context: dict[str, Any],
        request: HttpRequest | None = None,
    ) -> int | None:
        """Store failed request to DLQ."""
        try:
            domain = self._infer_domain(request_data.get("path", ""))

            # impl doc 486 D2 G4 — middleware needs the real ``dlq_id`` for the
            # HTTP response + audit, so opt into sync dispatch explicitly.
            result = store_to_dlq(
                domain=domain,
                failure_type=error_context.get("error_type", "UNKNOWN"),
                entity_type="http_request",
                entity_id=request_data.get("headers", {}).get("x_idempotency_key", ""),
                user_id=request_data.get("user_id"),
                error_code=error_context.get("error_type", ""),
                error_message=error_context.get("error_message", ""),
                request_data=request_data,
                response_data={"status_code": 503},
                metadata={
                    "source": "BaldurMiddleware",
                    "auto_stored": True,
                    "path": request_data.get("path"),
                    "method": request_data.get("method"),
                },
                recommended_action="replay",
                next_action_hint="Eligible for automatic replay after system recovery",
                mode="sync",
            )

            if result is None:
                logger.warning(
                    "baldur_middleware.dlq_storage_unavailable",
                    healing_domain=domain,
                )
                return None

            if result.success:
                dlq_id = result.dlq_id
                logger.info(
                    "baldur_middleware.dlq_stored",
                    dlq_id=dlq_id,
                    healing_domain=domain,
                    request_data=request_data.get("path"),
                )

                self._log_audit_event(
                    "dlq_auto_stored",
                    {
                        "dlq_id": dlq_id,
                        "domain": domain,
                        "path": request_data.get("path"),
                        "error_type": error_context.get("error_type"),
                    },
                    request=request,
                )

                return dlq_id
            logger.warning(
                "baldur_middleware.dlq_storage_failed",
                result_error=result.error,
            )
            return None

        except Exception as e:
            logger.exception(
                "baldur_middleware.dlq_storage_error",
                error=e,
            )
            return None

    def _infer_domain(self, path: str) -> str:
        """Infer domain from request path using configurable mapping."""
        for pattern, domain in self.DOMAIN_MAPPING.items():
            if pattern in path:
                return domain
        return "http"

    def _log_audit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        request: HttpRequest | None = None,
    ) -> None:
        """Log event to audit system."""
        # === Buffer pattern preferred ===
        if request is not None:
            try:
                from baldur.audit.event_buffer import (
                    AuditEventType,
                    RequestAuditBuffer,
                )

                event_type_map = {
                    "preemptive_dlq_stored": AuditEventType.DLQ_STORE,
                    "dlq_auto_stored": AuditEventType.DLQ_STORE,
                    "cb_failure_recorded": AuditEventType.CB_STATE_CHANGE,
                }
                audit_event_type = event_type_map.get(
                    event_type, AuditEventType.ERROR_DETECTED
                )

                buffer = RequestAuditBuffer.get_or_create(request)
                buffer.add(
                    event_type=audit_event_type,
                    source="BaldurMiddleware",
                    details={"event_type": event_type, **data},
                    success=True,
                )
                return
            except ImportError:
                pass

        # === Fallback: legacy path ===
        try:
            if self._audit_logger:
                self._audit_logger.log_change(
                    {
                        "event_type": event_type,
                        "source": "BaldurMiddleware",
                        "timestamp": utc_now().isoformat(),
                        **data,
                    }
                )
        except Exception as e:
            logger.warning(
                "baldur_middleware.audit_log_failed",
                error=e,
            )
