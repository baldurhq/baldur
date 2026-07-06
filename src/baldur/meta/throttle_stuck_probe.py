"""ThrottleStuckProbe — semantic-stuck detection for the adaptive throttle.

The ``daemon_workers`` catch-all probe covers the throttle audit worker's
*liveness*; that says nothing about whether the throttle *limit* is
recovering. This probe answers the orthogonal "frozen, not just crashed"
question against the throttle state owner (``ProviderRegistry.adaptive_throttle``,
NOT ``ThrottleAuditWorker``): it feeds ``current_limit`` into the shared
``StuckDetector`` and reports UNHEALTHY when the limit is frozen (near-zero
variance) while constrained.

The zero-variance signal is the frozen-vs-adapting discriminator: a throttle
actively managing load moves ``current_limit`` (variance > 0) and is not
flagged; only a *frozen* constrained limit trips. ``constrained`` is
demand-gated so a throttle resting at its floor with no traffic (a legitimate
idle state) is not a false positive. Detect + escalate only.
"""

from __future__ import annotations

import math
import time

import structlog

from baldur.meta.health_probe import HealthProbe, HealthStatus, ProbeResult
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["ThrottleStuckProbe"]


class ThrottleStuckProbe(HealthProbe):
    """Semantic-stuck probe for adaptive throttle limit/recovery state."""

    def __init__(self) -> None:
        # Previous tick's rejected_requests, for the demand-gate delta. The
        # probe instance persists across ticks (one per HealthProbeManager), so
        # this tracks rejection activity between samples. ``None`` until the
        # first tick (no delta available → at-floor term cannot fire yet).
        self._prev_rejected_requests: int | None = None

    @property
    def component_name(self) -> str:
        return "adaptive_throttle"

    def is_applicable(self) -> bool:
        """Probe only when the PRO adaptive throttle is registered.

        Registration is enablement (no chaos-style top-level enable flag);
        OSS-only / unregistered → ``safe_get()`` is ``None`` → skipped.
        Fail-safe: resolution error → not applicable.
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            return ProviderRegistry.adaptive_throttle.safe_get() is not None
        except Exception:
            return False

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            from baldur.factory.registry import ProviderRegistry

            throttle = ProviderRegistry.adaptive_throttle.safe_get()
            if throttle is None:
                raise RuntimeError("baldur_pro AdaptiveThrottle not registered")

            stats = throttle.get_stats()
            current_limit = stats.get("current_limit", 0)
            min_limit = stats.get("min_limit", 0)
            rejected = stats.get("rejected_requests", 0)
            emergency = stats.get("emergency") or {}
            recovery = stats.get("recovery") or {}
            full_stop = bool(emergency.get("full_stop_active", False))
            emergency_active = bool(emergency.get("active", False))

            # Demand-gate (D7): current_limit <= min_limit ALONE is a legitimate
            # resting value (a service with min_limit == initial_limit, or any
            # idle / low-traffic throttle, sits at the floor with zero variance).
            # Only flag the floor when real demand is being denied — rejections
            # rising since the previous tick. full_stop / emergency are explicit
            # distress states and stay ungated regardless of traffic.
            rejections_rising = (
                self._prev_rejected_requests is not None
                and rejected > self._prev_rejected_requests
            )
            self._prev_rejected_requests = rejected
            at_floor_under_demand = current_limit <= min_limit and rejections_rising
            constrained = full_stop or emergency_active or at_floor_under_demand

            # Feed the shared StuckDetector singleton (DLQ-probe precedent). The
            # zero-variance + error-rate gate fires only when a constrained
            # limit is pinned across the sample window.
            from baldur.meta.stuck_detector import get_stuck_detector

            detector = get_stuck_detector()
            detector.record(
                component=self.component_name,
                value=float(current_limit),
                error=constrained,
            )
            check = detector.check(self.component_name)

            # Gate UNHEALTHY on the CURRENT tick still being constrained, not on
            # detector history alone. The shared StuckDetector's sliding window
            # retains a past incident's error samples, and a frozen limit keeps
            # variance ~0, so is_stuck can stay True for the tail of ticks after
            # a genuine full_stop / emergency clears and the throttle goes idle
            # at the floor — the exact low-traffic false positive the demand-gate
            # exists to prevent. Requiring constrained now means "only a frozen
            # limit that is STILL constrained trips" (D7), so a recovered /
            # idle-at-floor throttle returns HEALTHY immediately instead of
            # flapping through a multi-tick window-flush tail.
            status = HealthStatus.HEALTHY
            reason = ""
            if check.is_stuck and constrained:
                status = HealthStatus.UNHEALTHY
                reason = (
                    f"Throttle limit frozen at {current_limit} while constrained "
                    f"(variance {check.variance:.4f}, "
                    f"error_rate {check.error_rate:.0%})"
                )

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details={
                    "current_limit": current_limit,
                    "min_limit": min_limit,
                    # The detector's under-2-samples sentinel is float("inf"),
                    # which is not JSON-representable — the admin response writer
                    # rejects non-finite floats, so the first post-boot status
                    # payload would 500. Report "insufficient samples" as null.
                    "variance": (
                        check.variance if math.isfinite(check.variance) else None
                    ),
                    "error_rate": check.error_rate,
                    "sample_count": check.sample_count,
                    "constrained": constrained,
                    "full_stop_active": full_stop,
                    "emergency_active": emergency_active,
                    "dampening_active": bool(recovery.get("dampening_active", False)),
                },
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )
