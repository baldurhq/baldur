"""
Preset Pipelines — pre-defined resilience pipelines.

Convenience functions for common policy compositions.

- standard_pipeline(): Fallback + Retry + CB (industry-standard order)
- ha_pipeline(): Fallback + Retry + Bulkhead + Hedging (high availability)
- minimal_pipeline(): CB-only (lightweight)
- adaptive_pipeline(): auto-selects standard or minimal based on load

compose() argument order = outermost → innermost execution order:
    compose(A, B, C).execute(func) = A(B(C(func)))
    First argument is the outermost wrapper.

Fallback support:
    Both standard and ha presets accept fallback_chain / fallback_fn /
    fallback_default optional parameters. When any is non-None,
    FallbackPolicy is placed as the outermost policy (first in compose()).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from baldur.interfaces.resilience_policy import ResiliencePolicy
from baldur.resilience.policies.composer import PolicyComposer, compose
from baldur.resilience.policies.fallback import FallbackPolicy
from baldur.resilience.policies.guards import (
    ErrorBudgetGuard,
    KillSwitchGuard,
)
from baldur.resilience.policies.hooks import AuditHook, MetricsHook
from baldur.resilience.policies.sinks import DLQSink
from baldur.services.bulkhead.policy import bulkhead_policy

T = TypeVar("T")


_STANDARD_PIPELINE_DEFAULT_MAX_RETRIES = 3
_HA_PIPELINE_DEFAULT_MAX_RETRIES = 2
_PIPELINE_DEFAULT_DOMAIN = "default"


def _raise_if_retry_params_overridden(
    *,
    preset: str,
    max_retries: int,
    domain: str,
) -> None:
    """Reject conflicting parameters when ``retry_policy=`` is supplied.

    The caller-supplied ``retry_policy`` owns the retry loop; ``max_retries``
    and ``domain`` would be silently dropped, which contradicts the framework
    contract documented at ``protect.py`` ("Silent degradation is forbidden in
    a resilience framework"). Fail-fast forces the caller to either drop the
    advisory params or build a custom composer.
    """
    default_max = (
        _STANDARD_PIPELINE_DEFAULT_MAX_RETRIES
        if preset == "standard_pipeline"
        else _HA_PIPELINE_DEFAULT_MAX_RETRIES
    )
    conflicts: dict[str, Any] = {}
    if max_retries != default_max:
        conflicts["max_retries"] = max_retries
    if domain != _PIPELINE_DEFAULT_DOMAIN:
        conflicts["domain"] = domain
    if conflicts:
        raise ValueError(
            f"{preset}(retry_policy=...) conflicts with explicit "
            f"{sorted(conflicts.keys())}={conflicts}: the supplied retry_policy "
            "owns the retry loop, so max_retries/domain would be silently dropped. "
            "Either remove the conflicting kwargs, or compose your own pipeline "
            "with PolicyComposer/compose() if you need both."
        )


def _build_fallback_policy(
    fallback_chain: list[Callable[[], Any]] | None,
    fallback_fn: Callable[[], Any] | None,
    fallback_default: Any,
) -> FallbackPolicy | None:
    """Build a FallbackPolicy instance from fallback parameters.

    Returns a FallbackPolicy if any of the three parameters is non-None,
    otherwise returns None.
    """
    has_chain = fallback_chain is not None
    has_fn = fallback_fn is not None
    has_default = fallback_default is not None

    if not (has_chain or has_fn or has_default):
        return None

    return FallbackPolicy(
        fallback_chain=fallback_chain,
        fallback_fn=fallback_fn,
        default_value=fallback_default,
    )


def standard_pipeline(
    service_name: str,
    max_retries: int = 3,
    domain: str = "default",
    cb_enabled: bool = True,
    # --- Fallback optional params (3-tier) ---
    fallback_chain: list[Callable[[], Any]] | None = None,
    fallback_fn: Callable[[], Any] | None = None,
    fallback_default: Any = None,
    retry_policy: ResiliencePolicy[Any] | None = None,
) -> PolicyComposer:
    """
    Standard resilience pipeline — Fallback + Retry + CircuitBreaker + Guard + Audit + DLQ.

    Execution order (outermost → innermost):
      Fallback(Retry(CB(func)))
    - CB inner → short-circuits when downstream is known bad
    - Retry middle → retries transient failures; CB-open is non_retryable (fail-fast)
    - Fallback outer → provides degraded response after all retries exhausted or CB-open

    ``CircuitBreakerError`` is in Retry's non_retryable default, so CB-open
    errors stop retry immediately (1 attempt), then Fallback activates.

    Shared contract with the ``protect()`` facade: both satisfy the single
    published composition contract — retry runs first, CB-open degrades to the
    fallback, and the breaker counts failures absorbed by the fallback. The one
    observable difference is CB counting granularity: this preset places the CB
    **innermost** (inside Retry), so it records one failure per attempt; the
    facade (``protect()``) places the CB **outside** Retry, so one exhausted
    retry-sequence is a single CB failure. Choose the preset for per-attempt CB
    accounting, the facade for per-sequence.

    Args:
        service_name: Service identifier (used for metrics/logging and CB)
        max_retries: Maximum retry attempts
        domain: Domain identifier (RetryPolicyConfig.domain)
        cb_enabled: Include CircuitBreakerPolicy (default True)
        fallback_chain: Ordered fallback callable list.
        fallback_fn: Single callable fallback function.
        fallback_default: Static default value (last resort).

    Returns:
        PolicyComposer instance

    Usage::

        # Without Fallback
        pipeline = standard_pipeline("payment_api")
        result = pipeline.execute(lambda: call_payment())

        # With Fallback
        pipeline = standard_pipeline(
            "payment_api",
            fallback_default={"status": "degraded"},
        )
    """
    from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
    from baldur.services.retry_handler.models import RetryPolicyConfig
    from baldur.services.retry_handler.policy import RetryPolicy

    if retry_policy is not None:
        _raise_if_retry_params_overridden(
            preset="standard_pipeline",
            max_retries=max_retries,
            domain=domain,
        )
        retry_stage: ResiliencePolicy[Any] = retry_policy
    else:
        retry_stage = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=max_retries, domain=domain)
        )

    # Build policies list in outermost→innermost order (compose() convention)
    policies: list = []

    fallback_policy = _build_fallback_policy(
        fallback_chain,
        fallback_fn,
        fallback_default,
    )
    if fallback_policy is not None:
        policies.append(fallback_policy)  # outermost (if present)

    policies.append(retry_stage)  # middle

    if cb_enabled:
        policies.append(CircuitBreakerPolicy(service_name=service_name))  # innermost

    return (
        compose(*policies)
        .add_guard(KillSwitchGuard())
        .add_guard(ErrorBudgetGuard())
        .add_hook(AuditHook())
        .add_sink(DLQSink())
    )


def ha_pipeline(
    service_name: str,
    candidates: list[Callable[..., Any]],
    max_retries: int = 2,
    hedging_delay: float = 0.1,
    max_concurrent: int = 20,
    domain: str = "default",
    # --- Fallback optional params (3-tier) ---
    fallback_chain: list[Callable[[], Any]] | None = None,
    fallback_fn: Callable[[], Any] | None = None,
    fallback_default: Any = None,
    retry_policy: ResiliencePolicy[Any] | None = None,
) -> PolicyComposer:
    """
    High-availability pipeline — Fallback + Retry + Bulkhead + Hedging + Guard + Audit + Metrics + DLQ.

    Execution order (outermost → innermost):
      Fallback(Retry(Bulkhead(Hedging(func))))

    Args:
        service_name: Service identifier
        candidates: Hedging candidate functions
        max_retries: Maximum retry attempts
        hedging_delay: Hedging delay in seconds
        max_concurrent: Bulkhead max concurrency
        domain: Domain identifier
        fallback_chain: Ordered fallback callable list.
        fallback_fn: Single callable fallback function.
        fallback_default: Static default value (last resort).

    Returns:
        PolicyComposer instance

    Usage::

        # Without Fallback
        pipeline = ha_pipeline("product_api", [fetch_b, fetch_c])
        result = pipeline.execute(lambda: fetch_a())

        # With partition_aware_chain
        from baldur.resilience.policies.fallback import partition_aware_chain
        pipeline = ha_pipeline(
            "product_api",
            [fetch_b, fetch_c],
            fallback_chain=partition_aware_chain(
                state_provider=lambda: health_monitor.get_state(),
                cache_fn=lambda: redis.get("product:123"),
                db_fn=lambda: Product.objects.get(id=123),
            ),
            fallback_default={"status": "degraded"},
        )
    """
    from baldur.resilience.policies.hedging import HedgingPolicy
    from baldur.services.retry_handler.models import RetryPolicyConfig
    from baldur.services.retry_handler.policy import RetryPolicy

    try:
        from baldur_pro.services.hedging.config import HedgingConfig, HedgingMode
    except ImportError:
        HedgingConfig = None  # type: ignore[assignment,misc]
        HedgingMode = None  # type: ignore[assignment,misc]

    # Fail-closed: ha_pipeline composes a PRO-tier Hedging stage (the bulkhead
    # stage is core). With baldur_pro absent these names are None; raise a clear
    # error at construction time rather than letting the caller receive a
    # pipeline silently missing the hedging it asked for (claim-wiring
    # integrity: a degraded pipeline is a false guarantee, not graceful
    # degradation).
    if HedgingConfig is None or HedgingMode is None:
        raise RuntimeError("ha_pipeline requires baldur_pro (Hedging)")

    if retry_policy is not None:
        _raise_if_retry_params_overridden(
            preset="ha_pipeline",
            max_retries=max_retries,
            domain=domain,
        )
        retry_stage: ResiliencePolicy[Any] = retry_policy
    else:
        retry_stage = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=max_retries, domain=domain)
        )

    bp = bulkhead_policy(
        name=f"{service_name}_bulkhead",
        max_concurrent=max_concurrent,
    )

    hedging_config = HedgingConfig.from_settings(
        mode=HedgingMode.DELAYED,
        delay=hedging_delay,
    )

    # Build policies in outermost→innermost order (compose() convention)
    policies: list = []

    fallback_policy = _build_fallback_policy(
        fallback_chain,
        fallback_fn,
        fallback_default,
    )
    if fallback_policy is not None:
        policies.append(fallback_policy)  # outermost (if present)

    policies.append(retry_stage)  # middle
    policies.append(bp)
    policies.append(
        HedgingPolicy(
            candidates=candidates,
            config=hedging_config,
        ),
    )

    return (
        compose(*policies)
        .add_guard(KillSwitchGuard())
        .add_guard(ErrorBudgetGuard())
        .add_hook(AuditHook())
        .add_hook(MetricsHook())
        .add_sink(DLQSink())
    )


def minimal_pipeline(
    service_name: str,
    audit_sampling_rate: float = 1.0,
) -> PolicyComposer:
    """Lightweight resilience pipeline — CB check + optional audit only.

    Removes Guard (ErrorBudget Redis call) and Sink (DLQ storage) to
    minimize overhead. Suitable for read-only / non-essential requests.

    Audit logging:
    - audit_sampling_rate=1.0 (default): 100% audit (AuditHook)
    - audit_sampling_rate < 1.0: sampled audit (SampledAuditHook)
    - audit_sampling_rate=0.0: no audit

    Args:
        service_name: Service identifier protected by CB
        audit_sampling_rate: Audit sampling rate (1.0=100%)

    Returns:
        PolicyComposer instance

    Usage::

        pipeline = minimal_pipeline("product_api")
        result = pipeline.execute(lambda: get_product(id))

        pipeline = minimal_pipeline("search_api", audit_sampling_rate=0.01)
        result = pipeline.execute(lambda: search(query))
    """
    from baldur.resilience.policies.hooks.sampled_audit import (
        SampledAuditHook,
    )
    from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy

    composer = compose(CircuitBreakerPolicy(service_name=service_name))

    if audit_sampling_rate >= 1.0:
        composer.add_hook(AuditHook())
    elif audit_sampling_rate > 0.0:
        composer.add_hook(SampledAuditHook(sample_rate=audit_sampling_rate))

    return composer


def adaptive_pipeline(
    service_name: str,
    tier_id: str | None = None,
    # --- standard_pipeline params ---
    max_retries: int = 3,
    domain: str = "default",
    # --- Fallback optional params ---
    fallback_chain: list[Callable[[], Any]] | None = None,
    fallback_fn: Callable[[], Any] | None = None,
    fallback_default: Any = None,
) -> PolicyComposer:
    """Adaptive pipeline — auto-selects based on tier_id and system load.

    Operating modes:
    1. adaptive_enabled=False (default): always returns standard_pipeline
    2. adaptive_enabled=True:
       - GracefulDegradation disables full_guards → returns minimal
       - tier_id is in hot_path_tiers → returns minimal
       - Otherwise → returns standard_pipeline

    Args:
        service_name: Service identifier
        tier_id: Request tier ("critical" | "standard" | "non_essential")
        max_retries: Maximum retry attempts for standard_pipeline
        domain: Domain identifier for standard_pipeline
        fallback_chain: Fallback chain for standard_pipeline
        fallback_fn: Fallback function for standard_pipeline
        fallback_default: Fallback default value for standard_pipeline

    Returns:
        PolicyComposer instance

    Usage::

        pipeline = adaptive_pipeline("product_api", tier_id="non_essential")
        result = pipeline.execute(lambda: get_product(id))

        pipeline = adaptive_pipeline(
            "payment_api",
            tier_id="critical",
            fallback_default={"status": "degraded"},
        )
        result = pipeline.execute(lambda: process_payment(data))
    """
    from baldur.settings.pipeline import get_pipeline_settings

    settings = get_pipeline_settings()

    if not settings.adaptive_enabled:
        return standard_pipeline(
            service_name=service_name,
            max_retries=max_retries,
            domain=domain,
            fallback_chain=fallback_chain,
            fallback_fn=fallback_fn,
            fallback_default=fallback_default,
        )

    # GracefulDegradation integration: check if full_guards is disabled
    degradation_active = False
    try:
        from baldur.scaling.graceful_degradation import (
            get_graceful_degradation,
        )

        degradation = get_graceful_degradation()
        if not degradation.is_enabled("full_guards"):
            degradation_active = True
    except ImportError:
        pass

    # Return minimal pipeline when:
    # 1. GracefulDegradation has disabled full_guards
    # 2. tier_id is in hot_path_tiers
    use_minimal = degradation_active or (
        tier_id is not None and tier_id in settings.hot_path_tiers
    )

    if use_minimal:
        return minimal_pipeline(
            service_name=service_name,
            audit_sampling_rate=settings.audit_sampling_rate,
        )

    return standard_pipeline(
        service_name=service_name,
        max_retries=max_retries,
        domain=domain,
        fallback_chain=fallback_chain,
        fallback_fn=fallback_fn,
        fallback_default=fallback_default,
    )
