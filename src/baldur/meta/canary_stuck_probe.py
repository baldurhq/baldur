"""CanaryStuckProbe — semantic-stuck detection for canary rollouts.

The ``daemon_workers`` catch-all probe already covers canary *worker
liveness*; this probe answers the orthogonal "frozen, not just crashed"
question — is a *live* rollout wedged at a stage? It bridges
``RolloutWatchdog.detect_stalled_rollouts()`` so the meta-watchdog's "stuck
canary" definition shares a single source of truth with the Celery canary
watchdog and the two cannot drift.

Detect + escalate only — the probe returns UNHEALTHY and feeds the existing
escalation path; it takes no recovery action.
"""

from __future__ import annotations

import time

import structlog

from baldur.meta.health_probe import HealthProbe, HealthStatus, ProbeResult
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["CanaryStuckProbe"]


class CanaryStuckProbe(HealthProbe):
    """Semantic-stuck probe for canary rollout business state.

    Reports UNHEALTHY when one or more active rollouts are stalled past their
    stall threshold (CANARY beyond 2x the stage duration, PAUSED beyond the
    zombie threshold, PROMOTING beyond the transition window) — reusing the
    canary watchdog's tuned thresholds via the shared singleton — and HEALTHY
    otherwise.
    """

    @property
    def component_name(self) -> str:
        return "canary_rollout"

    def is_applicable(self) -> bool:
        """Probe only when the PRO canary service is registered.

        For this subsystem registration is enablement — the daemon worker
        the ``daemon_workers`` probe monitors runs iff the PRO service is
        registered, and no chaos-style top-level enable flag exists. OSS-only
        / unregistered → ``safe_get()`` is ``None`` → skipped, component absent
        from watchdog state (avoids a false HEALTHY/UNHEALTHY for an inactive
        subsystem). Fail-safe: any resolution error → not applicable.
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            return ProviderRegistry.canary_rollout_service.safe_get() is not None
        except Exception:
            return False

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            from baldur.tasks.canary_watchdog import get_rollout_watchdog

            # Shared singleton → same config + stall definition as the Celery
            # scan path. detect_stalled_rollouts() is read-only (no renew /
            # notify / rollback / metric side effect).
            watchdog = get_rollout_watchdog()
            stalled = watchdog.detect_stalled_rollouts()

            if not stalled:
                return ProbeResult(
                    component=self.component_name,
                    status=HealthStatus.HEALTHY,
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=utc_now(),
                )

            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNHEALTHY,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=(
                    f"{len(stalled)} canary rollout(s) stalled: "
                    + "; ".join(z.reason for z in stalled[:5])
                ),
                details={
                    "stalled_count": len(stalled),
                    "rollout_ids": [z.rollout_id for z in stalled],
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
