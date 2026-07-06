"""WorkloadProfile presets — workload characteristic-based settings presets.

Complementary to ScaleProfile (infra capacity): WorkloadProfile addresses
workload-specific tuning (latency tolerance, retry aggressiveness, etc.).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = [
    "WORKLOAD_PRESETS",
    "WorkloadProfile",
    "apply_workload_profile",
]


class WorkloadProfile(str, Enum):
    """Workload characteristic-based preset."""

    API_GATEWAY = "api_gateway"  # High RPS, low latency
    BATCH_PROCESSOR = "batch_processor"  # High throughput, latency tolerant
    EVENT_DRIVEN = "event_driven"  # Kafka/async, burst traffic
    MICROSERVICE = "microservice"  # Standard service-to-service
    DATA_PIPELINE = "data_pipeline"  # ETL, long-running operations
    REAL_TIME = "real_time"  # WebSocket/streaming, ultra-low latency


WORKLOAD_PRESETS: dict[WorkloadProfile, dict[str, Any]] = {
    WorkloadProfile.API_GATEWAY: {
        # Circuit Breaker: aggressive — protect downstream
        "circuit_breaker_threshold": 0.3,
        "circuit_breaker_recovery_timeout": 30,
        # Retry: minimal — fast failure preferred
        "retry_count": 2,
        "backoff_base_ms": 50,
        "backoff_max_ms": 500,
        # Timeout: tight
        "timeout_ms": 3000,
        # Rate limit: high
        "rate_limit_rps": 5000,
        # Jitter: moderate
        "jitter_range": 0.3,
        # Throttle SLA: tight
        "throttle_sla_warning_ms": 200,
        "throttle_sla_critical_ms": 500,
        # Connection pool: large
        "connection_pool_size": 50,
    },
    WorkloadProfile.BATCH_PROCESSOR: {
        # Circuit Breaker: lenient — retries expected
        "circuit_breaker_threshold": 0.7,
        "circuit_breaker_recovery_timeout": 120,
        # Retry: aggressive — don't lose work
        "retry_count": 5,
        "backoff_base_ms": 500,
        "backoff_max_ms": 30000,
        # Timeout: generous
        "timeout_ms": 30000,
        # Rate limit: moderate
        "rate_limit_rps": 500,
        # Jitter: high — spread retries
        "jitter_range": 0.8,
        # Throttle SLA: lenient
        "throttle_sla_warning_ms": 1000,
        "throttle_sla_critical_ms": 3000,
        # Connection pool: small
        "connection_pool_size": 10,
    },
    WorkloadProfile.EVENT_DRIVEN: {
        "circuit_breaker_threshold": 0.5,
        "circuit_breaker_recovery_timeout": 60,
        "retry_count": 3,
        "backoff_base_ms": 200,
        "backoff_max_ms": 10000,
        "timeout_ms": 15000,
        "rate_limit_rps": 2000,
        "jitter_range": 0.5,
        "throttle_sla_warning_ms": 500,
        "throttle_sla_critical_ms": 2000,
        "connection_pool_size": 20,
    },
    WorkloadProfile.MICROSERVICE: {
        # Balanced defaults
        "circuit_breaker_threshold": 0.5,
        "circuit_breaker_recovery_timeout": 60,
        "retry_count": 3,
        "backoff_base_ms": 100,
        "backoff_max_ms": 5000,
        "timeout_ms": 10000,
        "rate_limit_rps": 1000,
        "jitter_range": 0.3,
        "throttle_sla_warning_ms": 300,
        "throttle_sla_critical_ms": 1000,
        "connection_pool_size": 20,
    },
    WorkloadProfile.DATA_PIPELINE: {
        "circuit_breaker_threshold": 0.8,
        "circuit_breaker_recovery_timeout": 180,
        "retry_count": 7,
        "backoff_base_ms": 1000,
        "backoff_max_ms": 60000,
        "timeout_ms": 30000,
        "rate_limit_rps": 200,
        "jitter_range": 0.5,
        "throttle_sla_warning_ms": 2000,
        "throttle_sla_critical_ms": 5000,
        "connection_pool_size": 5,
    },
    WorkloadProfile.REAL_TIME: {
        "circuit_breaker_threshold": 0.2,
        "circuit_breaker_recovery_timeout": 15,
        "retry_count": 1,
        "backoff_base_ms": 10,
        "backoff_max_ms": 100,
        "timeout_ms": 1000,
        "rate_limit_rps": 10000,
        "jitter_range": 0.1,
        "throttle_sla_warning_ms": 50,
        "throttle_sla_critical_ms": 200,
        "connection_pool_size": 80,
    },
}


def apply_workload_profile(
    profile: WorkloadProfile,
    scale_profile: Any | None = None,
    custom_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate effective settings from profile combination.

    Merge order (later wins):
        1. Framework defaults (existing)
        2. ScaleProfile presets (infra capacity)
        3. WorkloadProfile presets (workload characteristics)
        4. Custom overrides (user-specified)

    Returns:
        Effective settings dict to apply.
    """
    effective: dict[str, Any] = {}

    # Layer 2: ScaleProfile presets
    if scale_profile is not None:
        try:
            from baldur.settings.scale import PROFILE_DEFAULTS

            scale_defaults = PROFILE_DEFAULTS.get(scale_profile, {})
            effective.update(scale_defaults)
        except ImportError:
            pass

    # Layer 3: WorkloadProfile presets
    workload_defaults = WORKLOAD_PRESETS.get(profile, {})
    effective.update(workload_defaults)

    # Layer 4: Custom overrides
    if custom_overrides:
        effective.update(custom_overrides)

    return effective
