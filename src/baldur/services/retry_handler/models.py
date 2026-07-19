"""
Retry Handler Models

Data classes, enums, and exceptions for retry handling.

RetryAction(Enum), MaxRetriesExceededError(RetryExhaustedError),
RetryConfig(dataclass), RetryPolicyConfig(dataclass),
RetryResult(dataclass), T TypeVar.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

from baldur.core.exceptions import (
    RetryExhaustedError,
    non_retryable_exceptions,
)
from baldur.settings import get_config

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyResult

T = TypeVar("T")


class RetryAction(str, Enum):
    """Actions that can be taken after a failure."""

    RETRY = "retry"
    DLQ = "dlq"
    ABORT = "abort"
    SUCCESS = "success"


class MaxRetriesExceededError(RetryExhaustedError):
    """Raised when maximum retry attempts have been exhausted.

    Carries the terminal cause via two mutually-exclusive slots:
    ``last_error`` (the final exception, for exception-driven exhaustion) or
    ``last_result`` + ``result_rejected`` (the final rejected value, for
    result-predicate exhaustion). ``is_result_exhaustion`` is the first-class
    discriminator — do not infer it from ``last_result is not None`` (the
    predicate may legitimately reject ``None``) or ``last_error is None``.
    """

    def __init__(
        self,
        message: str,
        retry_count: int,
        max_retries: int,
        last_error: Exception | None = None,
        last_result: Any = None,
        result_rejected: bool = False,
    ):
        super().__init__(message)
        self.retry_count = retry_count
        self.max_retries = max_retries
        self.last_error = last_error
        self.last_result = last_result
        self.result_rejected = result_rejected

    @property
    def is_result_exhaustion(self) -> bool:
        """True when exhaustion was caused by a rejected result, not an exception."""
        return self.result_rejected

    def extra_context(self) -> dict:
        ctx = super().extra_context()
        ctx["retry_count"] = self.retry_count
        ctx["max_retries"] = self.max_retries
        if self.last_error:
            ctx["last_error"] = str(self.last_error)
        # Marker only — never the rejected value itself (audit/DLQ payload safety).
        if self.result_rejected:
            ctx["result_rejected"] = True
        return ctx


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    backoff_base: int = 4
    backoff_max: int = 180
    jitter_percent: int = 25
    retryable_exceptions: tuple[type[Exception], ...] = field(
        default_factory=lambda: (Exception,)
    )
    non_retryable_exceptions: tuple[type[Exception], ...] = field(
        default_factory=non_retryable_exceptions  # from core.exceptions
    )
    enable_dlq: bool = True
    domain: str = "default"

    # Rate limit awareness settings.
    # NOTE: no built-in wiring consumes either field. Coordinator integration is
    # always explicit — RetryPolicy(rate_limit_coordinator=...), the tenacity
    # bridge's rate_limit_key, or @coordinator.rate_limit_aware. Setting these
    # two alone enables nothing.
    rate_limit_aware: bool = True  # Enable Self-DDoS prevention
    rate_limit_key: str | None = None  # Custom key, defaults to domain

    # Throttle awareness settings (v2.0)
    throttle_aware: bool = True  # Enable Throttle-aware backoff
    throttle_backoff_multiplier_cap: float = 4.0  # Maximum multiplier cap

    # Critical tier settings (v2.0)
    critical_tier_full_stop_grace_retries: int = 1
    """Number of extra retries allowed for CRITICAL-tier requests even in FULL_STOP"""

    critical_tier_full_stop_max_delay: int = 720
    """Maximum wait time for CRITICAL-tier requests in FULL_STOP (12 minutes)"""

    # Result-predicate retry: retry a call that *returned* a soft-error value
    # (200 + error payload, None, partial response). Constructor/decorator-only —
    # not env-expressible (a callable cannot round-trip through settings),
    # matching the retryable_exceptions precedent. Must be a synchronous callable.
    retry_on_result: Callable[[Any], bool] | None = None

    # Cooperative wall-clock retry budget (seconds). None disables it. Combined
    # min-of-two with the request-scoped deadline in the policy loop. Distinct
    # from backoff_max (per-sleep cap): max_elapsed bounds the whole ladder.
    max_elapsed: float | None = None

    @classmethod
    def from_settings(cls, domain: str = "default") -> RetryConfig:
        """
        Load configuration from RuntimeConfigManager (preferred) or core config.

        Args:
            domain: Domain name for per-domain overrides

        Returns:
            RetryConfig instance
        """
        # Try RuntimeConfigManager first (runtime-configurable)
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
            retry_config = manager.get_retry_config()
            dlq_config = manager.get_dlq_config()

            # ``RetryConfig`` (the constants alias for RetrySettings) exposes the
            # backoff base under the ``base_delay`` key — not ``backoff_base``.
            # Looking up ``backoff_base`` first preserves any explicit
            # RuntimeConfigManager override that uses the legacy key, but falls
            # through to the actual RetrySettings field so env vars like
            # BALDUR_RETRY_BASE_DELAY take effect.
            return cls(
                max_attempts=retry_config.get("max_attempts", 3),
                backoff_base=retry_config.get(
                    "backoff_base", retry_config.get("base_delay", 4)
                ),
                backoff_max=int(retry_config.get("max_delay", 180)),
                jitter_percent=retry_config.get("jitter_percent", 25),
                max_elapsed=retry_config.get("max_elapsed"),
                enable_dlq=dlq_config.get("enabled", True),
                domain=domain,
            )
        except Exception:
            pass  # Fall through to static config

        # Fallback to static core config
        config = get_config()
        retry_settings = config.core.retry
        backoff_settings = config.core.backoff
        dlq_settings = config.services_group.dlq

        # Per-domain overrides from domain_configs
        domain_config = config.domain_configs.get(domain, {}).get("retry", {})

        # Legacy backoff fields now in BackoffSettings (doc 359 Option B)
        return cls(
            max_attempts=domain_config.get("max_attempts", retry_settings.max_attempts),
            backoff_base=domain_config.get(
                "backoff_base", backoff_settings.legacy_base
            ),
            backoff_max=domain_config.get("max_delay", retry_settings.max_delay),
            jitter_percent=backoff_settings.legacy_jitter_percent,
            max_elapsed=domain_config.get("max_elapsed", retry_settings.max_elapsed),
            enable_dlq=dlq_settings.enabled,
            domain=domain,
        )


@dataclass
class RetryPolicyConfig:
    """Configuration dedicated to the pure retry Policy. Does not include externally dependent settings."""

    max_attempts: int = 3
    backoff_base: int = 4
    backoff_max: int = 180
    jitter_percent: int = 25
    retryable_exceptions: tuple[type[Exception], ...] = field(
        default_factory=lambda: (Exception,)
    )
    non_retryable_exceptions: tuple[type[Exception], ...] = field(
        default_factory=non_retryable_exceptions  # from core.exceptions
    )
    domain: str = "default"
    enable_dlq: bool = True

    # Result-predicate retry (constructor/decorator-only, synchronous callable)
    # and cooperative wall-clock budget (seconds, None = disabled). See the
    # matching fields on RetryConfig for the full contract.
    retry_on_result: Callable[[Any], bool] | None = None
    max_elapsed: float | None = None

    @classmethod
    def from_settings(cls, domain: str = "default") -> RetryPolicyConfig:
        """
        Load only the pure retry settings from Settings.

        Args:
            domain: Domain name for per-domain overrides

        Returns:
            RetryPolicyConfig instance
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
            retry_config = manager.get_retry_config()
            dlq_config = manager.get_dlq_config()

            # ``RetryConfig`` (the constants alias for RetrySettings) exposes the
            # backoff base under the ``base_delay`` key — not ``backoff_base``.
            # Looking up ``backoff_base`` first preserves any explicit
            # RuntimeConfigManager override that uses the legacy key, but falls
            # through to the actual RetrySettings field so env vars like
            # BALDUR_RETRY_BASE_DELAY take effect.
            return cls(
                max_attempts=retry_config.get("max_attempts", 3),
                backoff_base=retry_config.get(
                    "backoff_base", retry_config.get("base_delay", 4)
                ),
                backoff_max=int(retry_config.get("max_delay", 180)),
                jitter_percent=retry_config.get("jitter_percent", 25),
                max_elapsed=retry_config.get("max_elapsed"),
                enable_dlq=dlq_config.get("enabled", True),
                domain=domain,
            )
        except Exception:
            pass

        config = get_config()
        retry_settings = config.core.retry
        backoff_settings = config.core.backoff
        dlq_settings = config.services_group.dlq
        domain_config = config.domain_configs.get(domain, {}).get("retry", {})

        # Legacy backoff fields now in BackoffSettings (doc 359 Option B)
        return cls(
            max_attempts=domain_config.get("max_attempts", retry_settings.max_attempts),
            backoff_base=domain_config.get(
                "backoff_base", backoff_settings.legacy_base
            ),
            backoff_max=domain_config.get("max_delay", retry_settings.max_delay),
            jitter_percent=backoff_settings.legacy_jitter_percent,
            max_elapsed=domain_config.get("max_elapsed", retry_settings.max_elapsed),
            enable_dlq=dlq_settings.enabled,
            domain=domain,
        )

    @classmethod
    def from_retry_config(cls, config: RetryConfig) -> RetryPolicyConfig:
        """Extract only the pure retry settings from an existing RetryConfig."""
        return cls(
            max_attempts=config.max_attempts,
            backoff_base=config.backoff_base,
            backoff_max=config.backoff_max,
            jitter_percent=config.jitter_percent,
            retryable_exceptions=config.retryable_exceptions,
            non_retryable_exceptions=config.non_retryable_exceptions,
            domain=config.domain,
            enable_dlq=config.enable_dlq,
            retry_on_result=config.retry_on_result,
            max_elapsed=config.max_elapsed,
        )


@dataclass
class RetryResult:
    """Result of a retry operation."""

    success: bool
    action: RetryAction
    attempt: int
    value: Any = None
    error: Exception | None = None
    dlq_id: int | None = None
    next_delay: int | None = None

    @property
    def should_retry(self) -> bool:
        """Whether another retry should be attempted."""
        return self.action == RetryAction.RETRY

    @property
    def was_retried(self) -> bool:
        """Whether this result came from a retry (not first attempt)."""
        return self.attempt > 1

    def to_policy_result(self) -> PolicyResult:
        """Convert to the unified PolicyResult result type."""
        from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult

        outcome = PolicyOutcome.SUCCESS if self.success else PolicyOutcome.FAILURE

        return PolicyResult(
            value=self.value,
            outcome=outcome,
            error=self.error,
            total_attempts=self.attempt,
            executed_policies=["retry"],
            metadata={"dlq_id": self.dlq_id, "action": self.action.value},
        )
