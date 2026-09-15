"""The process-only metrics checker starvation relief gates on.

793 D12. Relief is a per-request throttling decision, so its gate must never
dial the shared breaker store: the checker reads CPU from the background
metrics cache exactly as the live checker does — and raises the same way on a
cache that is not running or a stale sample — and the error rate from the
process-only aggregate (``fleet=False``), carrying the call count it was
measured over.

Verification techniques applied:
- Error path: cache not running / stale sample -> ``RuntimeError`` naming
  the condition, and the breaker service is never asked
- Equivalence: ``fleet=False`` is forwarded; the payload carries the evidence
  rate and its call count
"""

from __future__ import annotations

import pytest

from baldur.scaling.rate_controller import _process_local_metrics_checker
from baldur.services.circuit_breaker.config import AggregateFailureEvidence
from baldur.services.system_metrics_cache import CachedMetrics


class _FakeMetricsCache:
    def __init__(
        self, *, cpu_percent: float = 10.0, source: str = "cache", running=True
    ):
        self._metrics = CachedMetrics(cpu_percent=cpu_percent, source=source)
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def get_metrics(self) -> CachedMetrics:
        return self._metrics


class _RecordingCBService:
    """Answers the evidence read and records the ``fleet`` it was asked with."""

    def __init__(self, evidence: AggregateFailureEvidence):
        self._evidence = evidence
        self.fleet_calls: list[bool] = []

    def get_aggregate_failure_evidence(self, *, fleet: bool = True):
        self.fleet_calls.append(fleet)
        return self._evidence


def _install(monkeypatch, *, cache, cb_service) -> None:
    monkeypatch.setattr(
        "baldur.services.system_metrics_cache.get_system_metrics_cache", lambda: cache
    )
    monkeypatch.setattr(
        "baldur.services.circuit_breaker.get_circuit_breaker_service",
        lambda: cb_service,
    )


class TestProcessLocalMetricsCheckerBehavior:
    """What the checker reads, and what it refuses to."""

    @pytest.mark.parametrize(
        ("cache", "condition"),
        [
            (_FakeMetricsCache(running=False), "not running"),
            (_FakeMetricsCache(source="stale"), "stale"),
        ],
        ids=["cache_not_running", "sample_stale"],
    )
    def test_unreadable_cpu_raises_before_the_breaker_is_asked(
        self, monkeypatch, cache, condition
    ):
        service = _RecordingCBService(
            AggregateFailureEvidence(
                failures=0, total_calls=0, open_circuits=0, fleet_read=False
            )
        )
        _install(monkeypatch, cache=cache, cb_service=service)

        with pytest.raises(RuntimeError) as excinfo:
            _process_local_metrics_checker()

        assert condition in str(excinfo.value)
        assert service.fleet_calls == []

    def test_error_rate_is_the_process_only_evidence(self, monkeypatch):
        """``fleet=False`` is forwarded; the payload carries rate and call count."""
        evidence = AggregateFailureEvidence(
            failures=3, total_calls=12, open_circuits=1, fleet_read=False
        )
        service = _RecordingCBService(evidence)
        _install(
            monkeypatch, cache=_FakeMetricsCache(cpu_percent=42.0), cb_service=service
        )

        payload = _process_local_metrics_checker()

        assert service.fleet_calls == [False]
        assert payload == {
            "cpu_percent": 42.0,
            "error_rate": evidence.rate,
            "error_rate_calls": 12,
        }

    def test_zero_calls_reads_as_an_observed_zero_with_its_count(self, monkeypatch):
        evidence = AggregateFailureEvidence(
            failures=0, total_calls=0, open_circuits=0, fleet_read=False
        )
        _install(
            monkeypatch,
            cache=_FakeMetricsCache(),
            cb_service=_RecordingCBService(evidence),
        )

        payload = _process_local_metrics_checker()

        assert payload["error_rate"] == 0.0
        assert payload["error_rate_calls"] == 0
