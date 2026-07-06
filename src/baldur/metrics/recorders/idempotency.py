"""
Idempotency metric recorder — duplicate-detection rate at domain layer.

Metrics (3):
- baldur_idempotency_check_total{result, domain}: Counter of IdempotencyService.check()
  outcomes. Distinct from the per-backend cache hit/miss recorded by
  ``MetricsAwareCacheAdapter`` — at the idempotency layer, a DB lookup hit is
  also a duplicate detection (a "hit" semantically), but the cache layer would
  see it as a miss. Two layers, two metrics, distinct meanings.
- baldur_idempotency_cache_unavailable_fallback_total{layer, reason}: Counter of
  resolver-level fallback events when no distributed cache adapter is registered
  via ``ProviderRegistry``. Distinct from ``check_total`` (which counts
  ``check()`` outcomes); a fallback is a service-instance-lifecycle event, and
  the decorator never calls ``check()`` (it uses ``IdempotencyGate``). Two
  semantically distinct events → two counters.
- baldur_idempotency_gate_decision_total{decision}: Counter of
  ``IdempotencyGate.check_and_acquire`` decisions, recorded once at the gate
  itself (a single choke point shared by every consumer). The cross-consumer
  dedup-effectiveness signal — distinct from ``check_total`` (the
  ``IdempotencyService`` layer) and from each consumer's own context signal.
  The ``cache=None`` no-op path is deliberately un-metered so "no gate" is not
  conflated with "gate said continue".
- baldur_idempotency_gate_takeover_total{reason}: Counter of successful
  failed / stale-executing takeovers at the gate (a won ``cas_takeover``). A
  takeover returns CONTINUE — indistinguishable from a fresh acquire on the
  decision counter alone — so it is metered separately. A rising rate is the
  primary ``execution_ttl``-tuning signal (undersized window, or a worker that
  died silently mid-claim). A separate counter (not a new label on the decision
  counter) keeps ``continue``'s meaning stable.

Result label values: ``cache_hit | db_hit | miss``
Domain label: closed-enum ``IdempotencyDomain`` value (~13 values today —
bounded cardinality, no resolve_domain cardinality guard required).
Layer label values (closed-enum):
``decorator | service | policy | singleton | recovery_coordinator`` (5)
Reason label values (closed-enum): ``no_cache_adapter_registered | escape_hatch_enabled`` (2)
Fallback cardinality: 5 × 2 = 10 series — well under the OSS metric budget.
Decision label values (closed-enum): ``continue | skip | abort`` (3 series, bounded).
Takeover reason label values (closed-enum): ``failed | stale`` (2 series, bounded).
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import get_or_create_counter

logger = structlog.get_logger()

__all__ = ["IdempotencyMetricRecorder"]


class IdempotencyMetricRecorder(BaseMetricRecorder):
    """Idempotency-domain duplicate-detection metric recording."""

    def __init__(self) -> None:
        self._check_total = get_or_create_counter(
            f"{self.PREFIX}_idempotency_check_total",
            "Idempotency check outcomes by result and domain",
            ["result", "domain"],
        )
        self._fallback_total = get_or_create_counter(
            f"{self.PREFIX}_idempotency_cache_unavailable_fallback_total",
            "Idempotency cache-unavailable fallback events by layer and reason",
            ["layer", "reason"],
        )
        self._gate_decision_total = get_or_create_counter(
            f"{self.PREFIX}_idempotency_gate_decision_total",
            "IdempotencyGate check-and-acquire decisions by decision type",
            ["decision"],
        )
        self._gate_takeover_total = get_or_create_counter(
            f"{self.PREFIX}_idempotency_gate_takeover_total",
            "IdempotencyGate failed/stale-executing takeovers by reason",
            ["reason"],
        )

    def record_check(self, result: str, domain: str) -> None:
        """Record an idempotency check outcome.

        Args:
            result: ``cache_hit`` | ``db_hit`` | ``miss``
            domain: ``IdempotencyDomain`` enum value
        """
        try:
            self._check_total.labels(result=result, domain=domain).inc()
        except Exception as e:
            logger.warning("metrics.record_idempotency_check_failed", error=e)

    def record_fallback(self, layer: str, reason: str) -> None:
        """Record an in-process fallback event from the cache resolver.

        Args:
            layer: ``decorator`` | ``service`` | ``policy`` | ``singleton`` |
                ``recovery_coordinator`` — which layer resolved to fallback
            reason: ``no_cache_adapter_registered`` | ``escape_hatch_enabled``
        """
        try:
            self._fallback_total.labels(layer=layer, reason=reason).inc()
        except Exception as e:
            logger.warning("metrics.record_idempotency_fallback_failed", error=e)

    def record_gate_decision(self, decision: str) -> None:
        """Record an ``IdempotencyGate.check_and_acquire`` decision.

        Recorded once at the gate (single choke point) for every consumer.
        The ``cache=None`` no-op path is not metered by the gate, so this
        only counts real-cache decisions.

        Args:
            decision: ``continue`` | ``skip`` | ``abort`` — the
                ``IdempotencyDecision`` value the gate returned.
        """
        try:
            self._gate_decision_total.labels(decision=decision).inc()
        except Exception as e:
            logger.warning("metrics.record_gate_decision_failed", error=e)

    def record_takeover(self, reason: str) -> None:
        """Record a won failed / stale-executing takeover at the gate.

        Recorded once at the gate exactly where ``cas_takeover`` /
        ``acas_takeover`` returns a win. Distinct from the decision counter (a
        takeover also bumps ``continue`` there) so ``continue``'s meaning stays
        stable.

        Args:
            reason: ``failed`` | ``stale`` — which takeover branch won.
        """
        try:
            self._gate_takeover_total.labels(reason=reason).inc()
        except Exception as e:
            logger.warning("metrics.record_takeover_failed", error=e)
