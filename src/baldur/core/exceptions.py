"""Baldur library exception hierarchy.

All library exceptions inherit from BaldurError.
Callers can use ``except BaldurError`` to catch any library error.

Top-level re-export selection rule:
    A name in ``core/exceptions.__all__`` is re-exported by ``baldur/__init__.py``
    iff it is either (a) a domain base class, or (b) a leaf class raised by code
    reachable from a top-level public surface (``protect``, decorators, ...).

Re-exported at ``baldur`` top-level (12 names):
    Bases — ``BaldurError``, ``AdapterError``, ``CircuitBreakerError``,
            ``DLQError``, ``ResilienceError``, ``ConfigurationError``
    Leaves — ``AdapterNotFoundError``, ``RetryExhaustedError``,
             ``TimeoutPolicyError``, ``RateLimitExceeded``,
             ``IdempotencyDuplicateError``, ``DLQReplayError``

Internal / nested-only (``baldur.core.exceptions``):
    ``AdapterInitializationError``, ``AdapterConnectionError``,
    ``RecoveryAdapterError``, ``StoreError``,
    ``CircuitBreakerTransitionError``, ``InvalidStateTransitionError``,
    ``DLQEntryNotFoundError``, ``AuditError``, ``RunbookError``,
    ``SettingsValidationError``, ``StepExecutionError``, ``StepTimeoutError``,
    ``CompensationError``, ``ConcurrencyConflictError``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BaldurError",
    # Adapter
    "AdapterError",
    "AdapterNotFoundError",
    "AdapterInitializationError",
    "AdapterConnectionError",
    "RecoveryAdapterError",
    # Store (domain state management)
    "StoreError",
    # Circuit Breaker
    "CircuitBreakerError",
    "CircuitBreakerTransitionError",
    "non_retryable_exceptions",
    # State Transition
    "InvalidStateTransitionError",
    # DLQ
    "DLQError",
    "DLQEntryNotFoundError",
    "DLQStateConflictError",
    "DLQReplayError",
    # Resilience
    "ResilienceError",
    "RetryExhaustedError",
    "TimeoutPolicyError",
    "RateLimitExceeded",
    # Idempotency
    "IdempotencyDuplicateError",
    "IdempotencyUnavailableError",
    # Domain input validation
    "DomainValidationError",
    # Audit
    "AuditError",
    # Runbook
    "RunbookError",
    # Configuration
    "ConfigurationError",
    "SettingsValidationError",
    # Step Execution Engine
    "StepExecutionError",
    "StepTimeoutError",
    "CompensationError",
    "ConcurrencyConflictError",
    "ConfigVersionConflictError",
]


class BaldurError(Exception):
    """Base exception for all baldur library errors."""

    def __init__(self, message: str = "", *, code: str = ""):
        super().__init__(message)
        self.code = code

    def extra_context(self) -> dict[str, Any]:
        """Return structlog-bindable context. Override in subclasses."""
        return {"error_code": self.code} if self.code else {}


# ── Adapter errors ───────────────────────────────────────────


class AdapterError(BaldurError):
    """Base exception for adapter-related errors."""

    pass


class AdapterNotFoundError(AdapterError):
    """Raised when a requested adapter is not registered in ProviderRegistry."""

    def __init__(
        self,
        message: str = "",
        *,
        adapter_type: str = "",
        adapter_name: str = "",
        code: str = "",
    ):
        if not message and adapter_type:
            message = f"Adapter not found: type={adapter_type!r}, name={adapter_name!r}"
        super().__init__(message, code=code)
        self.adapter_type = adapter_type
        self.adapter_name = adapter_name

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self.adapter_type:
            ctx["adapter_type"] = self.adapter_type
            ctx["adapter_name"] = self.adapter_name
        return ctx


class AdapterInitializationError(AdapterError):
    """Raised when an adapter fails to initialize."""

    pass


class AdapterConnectionError(AdapterError):
    """Raised when an external system connection fails."""

    pass


# Raised by meta/recovery_adapter.py.
class RecoveryAdapterError(AdapterError):
    """Raised when the recovery adapter encounters an error.

    Used for input validation and workload detection failures.
    """

    def __init__(
        self,
        message: str = "",
        *,
        service_name: str = "",
        replicas: int | None = None,
        namespace: str = "",
        code: str = "",
    ):
        super().__init__(message, code=code)
        self.service_name = service_name
        self.replicas = replicas
        self.namespace = namespace

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self.service_name:
            ctx["service_name"] = self.service_name
        if self.replicas is not None:
            ctx["replicas"] = self.replicas
        if self.namespace:
            ctx["namespace"] = self.namespace
        return ctx


# ── Store errors ─────────────────────────────────────────────


class StoreError(AdapterError):
    """Base exception for domain state store errors.

    Used by domain-specific stores (ConfigHistoryStore, CanaryRolloutStore,
    ChaosExperimentStore, CrossClusterStore) for data corruption or
    store-level logic errors.

    Infrastructure failures (Redis down) are handled transparently by
    ResilientStorageBackend's silent fallback — this exception covers
    cases where the store itself encounters an unrecoverable problem
    (e.g. unparseable data, schema mismatch).
    """

    pass


# ── Circuit Breaker errors ───────────────────────────────────


class CircuitBreakerError(BaldurError):
    """Base exception for circuit breaker errors."""

    pass


class CircuitBreakerTransitionError(CircuitBreakerError):
    """Raised when a circuit breaker state transition fails."""

    pass


def non_retryable_exceptions() -> tuple[type[Exception], ...]:
    """Exceptions that must never be retried.

    CircuitBreakerError: CB OPEN means 'stop sending traffic'.
    Retrying defeats circuit breaker semantics.
    Industry standard (Hystrix, Resilience4j, Polly).
    """
    return (CircuitBreakerError,)


# ── State Transition errors ─────────────────────────────────


class InvalidStateTransitionError(BaldurError):
    """Raised when an invalid state transition is attempted.

    Follows the same pattern as CircuitBreakerTransitionError
    but is domain-agnostic (Recovery Session, etc.).
    """

    def __init__(
        self,
        message: str = "",
        *,
        current: str = "",
        target: str = "",
        entity_id: str = "",
        code: str = "",
    ):
        if not message and current:
            message = (
                f"Invalid state transition: {current!r} → {target!r}"
                f" (entity={entity_id!r})"
            )
        super().__init__(message, code=code)
        self.current = current
        self.target = target
        self.entity_id = entity_id

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self.current:
            ctx["current_state"] = self.current
            ctx["target_state"] = self.target
            ctx["entity_id"] = self.entity_id
        return ctx


# ── DLQ errors ───────────────────────────────────────────────


class DLQError(BaldurError):
    """Base exception for DLQ (Dead Letter Queue) errors."""

    pass


class DLQEntryNotFoundError(DLQError):
    """Raised when a DLQ entry is not found."""

    pass


class DLQStateConflictError(DLQError):
    """Raised when a DLQ operation violates an entry's state precondition.

    Covers resolved/archived/at-cap/not-in-replayable-state conflicts (e.g. a
    double-click force-redrive, or a retry of an already-resolved entry). Maps
    to HTTP 409 Conflict at the handler layer, distinct from a not-found (404)
    or an unexpected replay-execution failure (500).
    """

    pass


class DLQReplayError(DLQError):
    """Raised when a DLQ replay operation fails."""

    pass


# ── Resilience errors ────────────────────────────────────────


class ResilienceError(BaldurError):
    """Base exception for resilience pattern errors (bulkhead, retry, and related)."""

    pass


class RetryExhaustedError(ResilienceError):
    """Raised when all retry attempts are exhausted."""

    pass


class TimeoutPolicyError(ResilienceError):
    """Raised when a protected call exceeds its timeout budget."""

    def __init__(self, timeout_seconds: float, message: str = ""):
        self.timeout_seconds = timeout_seconds
        if not message:
            message = f"Call timed out after {timeout_seconds}s"
        super().__init__(message)

    def extra_context(self) -> dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds}


class RateLimitExceeded(ResilienceError):
    """Raised by ``@rate_limit`` when a call is rejected by the limiter.

    Function-level rejection signal — distinct from
    ``RateLimitStorageError`` (storage-backend failure).
    """

    def __init__(
        self,
        message: str = "",
        *,
        key: str = "",
        limit: int = 0,
        window_seconds: int = 0,
        reset_at: int = 0,
    ):
        if not message:
            message = (
                f"Rate limit exceeded: key={key!r}, limit={limit}/{window_seconds}s"
            )
        super().__init__(message)
        self.key = key
        self.limit = limit
        self.window_seconds = window_seconds
        self.reset_at = reset_at

    def extra_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {}
        if self.key:
            ctx["key"] = self.key
            ctx["limit"] = self.limit
            ctx["window_seconds"] = self.window_seconds
            ctx["reset_at"] = self.reset_at
        return ctx


# ── Idempotency errors ───────────────────────────────────────


class IdempotencyDuplicateError(BaldurError):
    """Raised by ``@idempotent`` on a detected duplicate or in-flight collision.

    Inherits ``BaldurError`` directly (correctness contract, not a
    resilience stage). Non-retryable by default — outer ``@dlq_protect``
    layers should treat this as a terminal signal.
    """

    def __init__(
        self,
        message: str = "",
        *,
        key: str = "",
        domain: str = "",
        decision: str = "",
    ):
        if not message:
            message = f"Idempotency duplicate: key={key!r}, decision={decision!r}"
        super().__init__(message)
        self.key = key
        self.domain = domain
        self.decision = decision

    def extra_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {}
        if self.key:
            ctx["key"] = self.key
            ctx["domain"] = self.domain
            ctx["decision"] = self.decision
        return ctx


class IdempotencyUnavailableError(BaldurError):
    """Raised when an idempotency check cannot complete due to a cache I/O
    failure (e.g. Redis unreachable) on an enabled, explicitly-requested gate.

    Distinct from ``IdempotencyDuplicateError`` (a *successful* dedup verdict):
    this signals the verdict is *unknown*, so the caller can assume neither
    "safe to skip" nor "safe to run". Fail-closed by default — opt into
    fail-open via ``BALDUR_IDEMPOTENCY_FAIL_OPEN_ON_CACHE_ERROR`` or the per-call
    ``idempotency_fail_open=True``. Wraps the original cache exception (raised
    ``from`` it) so a backend-specific error never leaks across the boundary.
    """

    def __init__(
        self,
        message: str = "",
        *,
        key: str = "",
        error: str = "",
    ):
        if not message:
            message = f"Idempotency check unavailable: key={key!r} ({error})"
        super().__init__(message)
        self.key = key
        self.error = error

    def extra_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {}
        if self.key:
            ctx["key"] = self.key
        if self.error:
            ctx["error"] = self.error
        return ctx


# ── Domain input validation errors ───────────────────────────


class DomainValidationError(BaldurError):
    """Raised when a domain input string fails validation.

    Carries the original (pre-normalization) input and a typed reject reason
    for downstream logging / metric labelling.

    Modeled on ``RecoveryAdapterError``: raised at validation sites that have
    a loud failure mode (decoration-time, where a CI/dev surface can recover
    via test or rename). Runtime APIs catch this and fall back to
    ``FALLBACK_DOMAIN`` instead of propagating.
    """

    def __init__(
        self,
        message: str = "",
        *,
        original_domain: str = "",
        reason: Any = None,
    ):
        if not message:
            reason_value = getattr(reason, "value", reason)
            message = (
                f"Invalid domain: {original_domain!r} ({reason_value})"
                if original_domain
                else f"Invalid domain ({reason_value})"
            )
        super().__init__(message)
        self.original_domain = original_domain
        self.reason = reason

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        ctx["original_domain"] = self.original_domain
        # reason is a (str, Enum) so .value yields the JSON-safe string
        ctx["reason"] = getattr(self.reason, "value", self.reason)
        return ctx


# ── Audit errors ─────────────────────────────────────────────


class AuditError(BaldurError):
    """Base exception for audit-related errors (cascade, WAL, mmap buffer)."""

    pass


# ── Runbook errors ───────────────────────────────────────────


class RunbookError(BaldurError):
    """Base exception for runbook errors."""

    pass


# ── Configuration errors ─────────────────────────────────────


class ConfigurationError(BaldurError):
    """Base exception for configuration and settings errors."""

    pass


class SettingsValidationError(ConfigurationError):
    """Raised when settings validation fails."""

    pass


# ── Step Execution Engine errors ────────────────────────────


class StepExecutionError(BaldurError):
    """Base exception for step execution engine errors.

    Shared by the step-based execution engines. Domain-specific subclasses
    live in each service module.
    """

    pass


class StepTimeoutError(StepExecutionError):
    """Step execution timed out.

    Raised by TimeoutExecutor when a handler does not complete
    within timeout_seconds.

    Supports two call conventions:
        StepTimeoutError(step_type="X", timeout_seconds=30)  # keyword
        StepTimeoutError("X", 30)  # positional (legacy compat)
        StepTimeoutError(timeout_seconds=30)  # timeout only (TimeoutExecutor)
    """

    def __init__(
        self,
        step_type: str | float = "",
        timeout_seconds: float | int = 0,
        message: str = "",
    ):
        # Handle positional: StepTimeoutError("STEP", 300)
        # AND keyword-only: StepTimeoutError(timeout_seconds=300)
        if isinstance(step_type, (int, float)):
            # Called as StepTimeoutError(timeout_seconds=N) with positional
            timeout_seconds = step_type
            step_type = ""
        self.step_type = str(step_type)
        self.timeout_seconds = timeout_seconds
        if not message:
            if self.step_type:
                message = f"Step '{self.step_type}' timed out after {timeout_seconds}s"
            else:
                message = f"Step timed out after {timeout_seconds}s"
        super().__init__(message)

    def extra_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {"timeout_seconds": self.timeout_seconds}
        if self.step_type:
            ctx["step_type"] = self.step_type
        return ctx


class CompensationError(StepExecutionError):
    """Raised when step compensation fails.

    Domain-specific subclasses add service-specific extra_context.
    """

    pass


class ConcurrencyConflictError(BaldurError):
    """Raised on optimistic concurrency control (OCC) conflicts.

    Covers version CAS failures across the step-execution engines.
    """

    def __init__(
        self,
        message: str = "",
        *,
        entity_id: str = "",
        expected_version: int = 0,
        actual_version: int = 0,
    ):
        if not message and entity_id:
            message = (
                f"Concurrency conflict: entity={entity_id}, "
                f"expected v{expected_version}, actual v{actual_version}"
            )
        super().__init__(message)
        self.entity_id = entity_id
        self.expected_version = expected_version
        self.actual_version = actual_version

    def extra_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {}
        if self.entity_id:
            ctx["entity_id"] = self.entity_id
            ctx["expected_version"] = self.expected_version
            ctx["actual_version"] = self.actual_version
        return ctx


class ConfigVersionConflictError(ConcurrencyConflictError):
    """Raised when a runtime-config write conflicts with a concurrent version.

    An optimistic-lock consumer for runtime-config writes, it carries the
    config section (``config_type``) plus the inherited
    ``expected_version`` / ``actual_version`` so the REST layer can map it to
    HTTP 409 and hand the client a usable retry token. Lives in OSS core so the
    OSS config REST handler can ``except`` it across the PRO-manager →
    OSS-handler boundary.
    """

    def __init__(
        self,
        config_type: str = "",
        *,
        expected_version: int = 0,
        actual_version: int = 0,
        message: str = "",
    ):
        if not message:
            message = (
                f"Config version conflict: section={config_type}, "
                f"expected v{expected_version}, actual v{actual_version}"
            )
        super().__init__(
            message,
            entity_id=config_type,
            expected_version=expected_version,
            actual_version=actual_version,
        )
        self.config_type = config_type

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self.config_type:
            ctx["config_type"] = self.config_type
        return ctx
