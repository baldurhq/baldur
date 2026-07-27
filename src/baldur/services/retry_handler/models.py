"""
Retry Handler Models

Data classes, enums, and exceptions for retry handling.

RetryAction(Enum), MaxRetriesExceededError(RetryExhaustedError),
RetryPolicyConfig(dataclass), RetryResult(dataclass), T TypeVar.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from baldur.core.backoff import (
    BackoffStrategy,
    ConstantBackoff,
    DecorrelatedJitterBackoff,
    ExponentialBackoff,
    LinearBackoff,
)
from baldur.core.exceptions import (
    RetryExhaustedError,
    non_retryable_exceptions,
)
from baldur.settings import get_config
from baldur.settings.field_types import (
    STANDARD_BACKOFF_MULTIPLIER,
    STANDARD_BASE_DELAY,
    STANDARD_JITTER_FACTOR,
    STANDARD_LINEAR_INCREMENT,
    STANDARD_MAX_DELAY,
    STANDARD_RETRY_COUNT,
)

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyResult

logger = structlog.get_logger()

T = TypeVar("T")

#: Default jitter width as a percentage. ``BackoffSettings`` carries the same
#: quantity as a 0..1 factor, so every crossing between the two multiplies or
#: divides by 100 — the config field is a percent, the strategy field a factor.
STANDARD_JITTER_PERCENT: float = STANDARD_JITTER_FACTOR * 100

#: The strategy name used when the configured one cannot be honored.
FALLBACK_BACKOFF_STRATEGY: str = "exponential"

#: The one strategy that carries running state across attempts. Policies build a
#: fresh instance of it per execution instead of sharing one, so two concurrent
#: ladders on a cached policy cannot interleave each other's previous delay.
STATEFUL_BACKOFF_STRATEGY: str = "decorrelated_jitter"


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
class RetryPolicyConfig:
    """Configuration dedicated to the pure retry Policy. Does not include externally dependent settings."""

    max_attempts: int = STANDARD_RETRY_COUNT
    # Seconds, not an exponent base: ``backoff_base`` is the *first* retry delay
    # and ``backoff_multiplier`` grows it. Every default here is the shared
    # STANDARD_* value, so direct construction and settings resolution agree.
    backoff_base: float = STANDARD_BASE_DELAY
    backoff_max: float = STANDARD_MAX_DELAY
    jitter_percent: float = STANDARD_JITTER_PERCENT
    backoff_multiplier: float = STANDARD_BACKOFF_MULTIPLIER
    backoff_increment: float = STANDARD_LINEAR_INCREMENT
    backoff_strategy: str = FALLBACK_BACKOFF_STRATEGY
    retryable_exceptions: tuple[type[Exception], ...] = field(
        default_factory=lambda: (Exception,)
    )
    non_retryable_exceptions: tuple[type[Exception], ...] = field(
        default_factory=non_retryable_exceptions  # from core.exceptions
    )
    domain: str = "default"
    enable_dlq: bool = True

    # Result-predicate retry (constructor/decorator-only, synchronous callable)
    # and cooperative wall-clock budget (seconds, None = disabled).
    retry_on_result: Callable[[Any], bool] | None = None
    max_elapsed: float | None = None

    # --- Outbound 429 coordination (Baldur's *synchronous* retry stage only) ---
    # rate_limit_aware: opt out of the default RateLimitCoordinator resolution
    # for this policy. Default-True, but inert unless the call carries a domain
    # identity: with no rate_limit_key and the placeholder domain ("default"),
    # no coordinator is resolved — a shared placeholder key would merge
    # unrelated downstreams into a single cooldown record.
    # rate_limit_key: override the coordination key; unset falls back to domain.
    #
    # Neither field reaches the asynchronous retry stage. AsyncRetryPolicy
    # consumes this same config class and its from_policy_config mapping does
    # not carry them, so on aprotect() and the async @retry branch both are
    # silently inert. Async 429 coordination is opt-in via the tenacity bridge.
    rate_limit_aware: bool = True
    rate_limit_key: str | None = None

    # Which resolution path produced these values. Set at the exit of each
    # ``from_settings`` branch — never re-derived from registry state, because a
    # registered manager that raises mid-resolution falls through to the static
    # branch and a registry probe would then label static values "runtime_config".
    # Excluded from equality so the field cannot change any existing comparison.
    config_source: str = field(default="direct", compare=False)

    @classmethod
    def from_settings(cls, domain: str = "default") -> RetryPolicyConfig:
        """
        Load only the pure retry settings from Settings.

        Both resolution branches bottom out in the same operator-facing fields
        (``BALDUR_RETRY_*`` for the ladder, ``BALDUR_BACKOFF_*`` for its shape),
        so a domain resolves identically with and without the PRO runtime store
        when that store holds no override.

        Args:
            domain: Domain name for per-domain overrides

        Returns:
            RetryPolicyConfig instance
        """
        config = cls._from_runtime_config(domain)
        if config is None:
            config = cls._from_static_settings(domain)

        logger.debug(
            "retry.backoff_config_resolved",
            domain=domain,
            source=config.config_source,
            strategy=config.backoff_strategy,
            backoff_base=config.backoff_base,
            backoff_max=config.backoff_max,
            backoff_multiplier=config.backoff_multiplier,
            backoff_increment=config.backoff_increment,
            jitter_percent=config.jitter_percent,
            max_attempts=config.max_attempts,
        )
        return config

    @classmethod
    def _from_runtime_config(cls, domain: str) -> RetryPolicyConfig | None:
        """Resolve through the PRO runtime-config store, or ``None`` when absent.

        ``None`` covers both "no manager registered" and "the manager raised":
        the caller falls through to the static branch, which is the same silent
        fallback this method has always had.
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                return None
            retry_config = manager.get_retry_config()
            dlq_config = manager.get_dlq_config()

            # The runtime store's retry family holds only the RetrySettings
            # fields, so the backoff *shape* dials (multiplier, jitter width,
            # linear increment) fall through to BackoffSettings on this branch
            # too — that is what makes the two branches resolve alike.
            backoff_settings = get_config().core.backoff

            # ``RetrySettings`` exposes the backoff base under ``base_delay``.
            # Looking up ``backoff_base`` first preserves an explicit
            # RuntimeConfigManager override using that key, then falls through to
            # the actual field so BALDUR_RETRY_BASE_DELAY takes effect.
            return cls(
                max_attempts=retry_config.get("max_attempts", STANDARD_RETRY_COUNT),
                backoff_base=retry_config.get(
                    "backoff_base",
                    retry_config.get("base_delay", STANDARD_BASE_DELAY),
                ),
                backoff_max=retry_config.get("max_delay", STANDARD_MAX_DELAY),
                jitter_percent=retry_config.get(
                    "jitter_percent",
                    backoff_settings.exponential_jitter_factor * 100,
                ),
                backoff_multiplier=backoff_settings.exponential_multiplier,
                backoff_increment=backoff_settings.linear_increment,
                backoff_strategy=retry_config.get(
                    "backoff_strategy", FALLBACK_BACKOFF_STRATEGY
                ),
                max_elapsed=retry_config.get("max_elapsed"),
                enable_dlq=dlq_config.get("enabled", True),
                domain=domain,
                rate_limit_aware=retry_config.get("rate_limit_aware", True),
                rate_limit_key=retry_config.get("rate_limit_key"),
                config_source="runtime_config",
            )
        except Exception:
            return None

    @classmethod
    def _from_static_settings(cls, domain: str) -> RetryPolicyConfig:
        """Resolve from the settings tree plus this domain's override overlay."""
        config = get_config()
        retry_settings = config.core.retry
        backoff_settings = config.core.backoff
        dlq_settings = config.services_group.dlq
        domain_config = config.domain_configs.get(domain, {}).get("retry", {})

        return cls(
            max_attempts=domain_config.get("max_attempts", retry_settings.max_attempts),
            backoff_base=_resolve_domain_backoff_base(
                domain_config, retry_settings.base_delay, domain
            ),
            backoff_max=domain_config.get("max_delay", retry_settings.max_delay),
            jitter_percent=backoff_settings.exponential_jitter_factor * 100,
            backoff_multiplier=backoff_settings.exponential_multiplier,
            backoff_increment=backoff_settings.linear_increment,
            backoff_strategy=domain_config.get(
                "backoff_strategy", retry_settings.backoff_strategy
            ),
            max_elapsed=domain_config.get("max_elapsed", retry_settings.max_elapsed),
            enable_dlq=dlq_settings.enabled,
            domain=domain,
            rate_limit_aware=domain_config.get("rate_limit_aware", True),
            rate_limit_key=domain_config.get("rate_limit_key"),
            config_source="static",
        )

    def build_backoff(self, *, jitter: bool = True) -> BackoffStrategy:
        """Build the backoff strategy these resolved values describe.

        Reads only this dataclass's own fields — never the settings tree. Every
        strategy parameter is resolved at ``from_settings`` time so the config
        alone reproduces the ladder: that is what lets a caller reason about the
        effective backoff from a startup report, and what keeps the two
        construction sites (sync and async) from drifting apart.

        Args:
            jitter: Build the jitterless skeleton when False. Ignored by the
                decorrelated strategy, whose randomization is its definition.

        Returns:
            BackoffStrategy: the strategy named by ``backoff_strategy``, or an
            exponential one when that name cannot be honored (fail-open — a
            config-shaped side input must never fail a business call).
        """
        builder = _BACKOFF_BUILDERS.get(self.backoff_strategy)
        if builder is None:
            logger.warning(
                "retry.backoff_strategy_resolution_failed",
                domain=self.domain,
                strategy=self.backoff_strategy,
                fallback=FALLBACK_BACKOFF_STRATEGY,
            )
            builder = _BACKOFF_BUILDERS[FALLBACK_BACKOFF_STRATEGY]
        return builder(self, jitter)


def _resolve_domain_backoff_base(
    domain_config: Mapping[str, Any],
    fallback: float,
    domain: str,
) -> float:
    """Resolve this domain's first-retry delay, failing open to ``fallback``.

    Two spellings are honored, ``backoff_base`` ahead of ``base_delay``, because
    the settings-side merge route names the quantity ``base_delay`` while the
    resolved config names it ``backoff_base`` — an operator writing either into
    a domain overlay means the same thing.

    The overlay itself is an unvalidated mapping, so whichever key matched is
    coerced and range-checked here. Anything non-numeric or non-positive
    degrades to the settings value with a WARNING: a config typo must not
    replace a business outcome with a TypeError mid-retry, and a non-positive
    base yields a delay the retry loop skips entirely, turning the ladder into
    a hot loop against the failing upstream.
    """
    for key in ("backoff_base", "base_delay"):
        if key not in domain_config:
            continue
        raw = domain_config[key]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        if value is None or value <= 0:
            logger.warning(
                "retry.domain_override_coercion_failed",
                domain=domain,
                key=key,
                value=repr(raw),
                fallback=fallback,
            )
            return fallback
        return value
    return fallback


# Strategy name -> constructor, over the same vocabulary RetrySettings
# validates. Deliberately not routed through ``get_backoff_calculator``: that
# factory keys decorrelated jitter as "decorrelated", which no settings value
# can ever spell.
_BACKOFF_BUILDERS: dict[str, Callable[[RetryPolicyConfig, bool], BackoffStrategy]] = {
    "exponential": lambda cfg, jitter: ExponentialBackoff(
        base_delay=cfg.backoff_base,
        max_delay=cfg.backoff_max,
        multiplier=cfg.backoff_multiplier,
        jitter=jitter,
        jitter_factor=cfg.jitter_percent / 100.0,
    ),
    "linear": lambda cfg, jitter: LinearBackoff(
        base_delay=cfg.backoff_base,
        increment=cfg.backoff_increment,
        max_delay=cfg.backoff_max,
        jitter=jitter and cfg.jitter_percent > 0,
        jitter_factor=cfg.jitter_percent / 100.0,
    ),
    "constant": lambda cfg, jitter: ConstantBackoff(
        delay=cfg.backoff_base,
        jitter=jitter and cfg.jitter_percent > 0,
        jitter_factor=cfg.jitter_percent / 100.0,
        max_delay=cfg.backoff_max,
    ),
    "decorrelated_jitter": lambda cfg, jitter: DecorrelatedJitterBackoff(
        base_delay=cfg.backoff_base,
        max_delay=cfg.backoff_max,
    ),
}


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
