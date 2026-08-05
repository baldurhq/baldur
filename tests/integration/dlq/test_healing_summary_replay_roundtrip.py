"""Integration: an operator retry becomes a number on the console's trust strip.

Three components share one piece of state — the in-process metrics registry —
and no unit of the chain can prove the end-to-end claim on its own:

    the DLQ read service's replay primitive **writes** the replay families,
    ``collect_families`` **reads** them in one registry walk, and
    ``healing_summary`` **interprets** what it read into the console payload.

So this case drives the real chain: store a failed operation in the memory DLQ
backend, retry it through the OSS single-entry surface, and read
``GET /healing/summary`` back. The counter must be exactly one higher than it
was before the retry — which is the whole point of the change: before it, a
console retry produced no measurable replay at all.

Mock-based (no infra): the memory repository is injected through the read
service's constructor, and the only patched seam is the tier probe, so the
payload is asserted in its OSS shape (no ``humans_paged``) regardless of
whether the PRO distribution happens to be importable in the test environment.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.api.handlers.healing import healing_summary
from baldur.interfaces.repositories import FailedOperationData
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.models.dlq import DLQConfig
from baldur.services.dlq_read import DLQReadService
from baldur.services.replay_service import ReplayHandler, register_replay_handler
from baldur.services.replay_service.models import ReplayResult

_ATTEMPTS = "baldur_replay_attempts_total"
_OUTCOMES = "baldur_replay_outcomes_total"


class _StubReplayHandler(ReplayHandler):
    """A registered handler with a decided outcome — the customer's code."""

    def __init__(self, domain: str, *, succeed: bool = True) -> None:
        self._domain = domain
        self._succeed = succeed
        self.replay_calls: list[str] = []

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replay_calls.append(failed_op.id)
        if self._succeed:
            return ReplayResult.succeeded(failed_op.id, "replayed")
        return ReplayResult.failed(failed_op.id, "still broken")


@pytest.fixture
def register_handler():
    """Register stub replay handlers, restoring the registry afterwards."""
    from baldur.services.replay_service import handlers as _handlers

    snapshot = dict(_handlers._replay_handlers)

    def _register(domain: str, *, succeed: bool = True) -> _StubReplayHandler:
        handler = _StubReplayHandler(domain, succeed=succeed)
        register_replay_handler(handler)
        return handler

    yield _register

    _handlers._replay_handlers.clear()
    _handlers._replay_handlers.update(snapshot)


@pytest.fixture
def read_service():
    """The OSS DLQ read facade over a real in-memory repository."""
    repo = InMemoryFailedOperationRepository()
    service = DLQReadService(
        config=DLQConfig(enabled=True, max_replay_attempts=2), repository=repo
    )
    service._log_dlq_audit = lambda **kwargs: None  # type: ignore[method-assign]
    return service, repo


def _summary() -> dict:
    """Read ``GET /healing/summary`` in its OSS shape, off the live registry."""
    with patch("baldur.utils.tier.is_pro_installed", return_value=False):
        response = healing_summary(
            RequestContext(
                method=HttpMethod.GET,
                path="/healing/summary",
                query_params={},
                path_params={},
            )
        )
    assert response.status_code == 200
    return response.body


def _replayed(payload: dict) -> int:
    """The console's ``replayed`` counter, or 0 when the field is absent."""
    return payload.get("counters", {}).get("replayed", 0)


def _family_total(family: str, labels: dict[str, str] | None = None) -> float:
    """Sum a counter family's ``_total`` samples across every label set."""
    from prometheus_client import REGISTRY

    from baldur.adapters.prometheus_adapter import _family_name

    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name not in (family, _family_name(family)):
            continue
        for sample in metric.samples:
            if not sample.name.endswith("_total"):
                continue
            if labels and any(sample.labels.get(k) != v for k, v in labels.items()):
                continue
            total += sample.value
    return total


class TestHealingSummaryReplayRoundtrip:
    """A retry through the OSS surface reaches the console payload."""

    def test_successful_retry_raises_the_reported_replayed_counter_by_one(
        self, read_service, register_handler
    ):
        # Given a stored failure and a handler that can fix it
        service, repo = read_service
        domain = "roundtrip_ok"
        handler = register_handler(domain)
        entry = repo.create(domain=domain, failure_type="timeout", retry_count=0)
        before = _replayed(_summary())

        # When the operator retries it from the admin surface
        result = service.retry_entry(entry.id)

        # Then the entry really healed...
        assert result["success"] is True
        assert handler.replay_calls == [entry.id]

        # ...and the console's own data source says so, one higher than before.
        assert _replayed(_summary()) == before + 1

    def test_the_retry_moves_both_replay_families_by_one(
        self, read_service, register_handler
    ):
        # attempts and outcomes are the numerator/denominator of an operator's
        # success-rate panel. Moving one without the other pushes it past 1.
        service, repo = read_service
        domain = "roundtrip_pair"
        register_handler(domain)
        entry = repo.create(domain=domain, failure_type="timeout", retry_count=0)
        attempts_before = _family_total(_ATTEMPTS)
        outcomes_before = _family_total(_OUTCOMES, {"outcome": "success"})

        service.retry_entry(entry.id)

        assert _family_total(_ATTEMPTS) - attempts_before == 1
        assert _family_total(_OUTCOMES, {"outcome": "success"}) - outcomes_before == 1

    def test_a_failed_retry_does_not_raise_the_replayed_counter(
        self, read_service, register_handler
    ):
        # `replayed` is the healing claim, so only a successful outcome may
        # move it — a retry that failed healed nothing.
        service, repo = read_service
        domain = "roundtrip_failed"
        register_handler(domain, succeed=False)
        entry = repo.create(
            domain=domain, failure_type="timeout", retry_count=0, max_retries=2
        )
        before = _replayed(_summary())

        result = service.retry_entry(entry.id)

        assert result["success"] is False
        assert _replayed(_summary()) == before

    def test_the_oss_shape_payload_carries_no_humans_paged_field(
        self, read_service, register_handler
    ):
        # Tier parity, end to end: the OSS tree has no escalation writer, so
        # the field must not exist even on a live, populated payload.
        service, repo = read_service
        domain = "roundtrip_tier"
        register_handler(domain)
        entry = repo.create(domain=domain, failure_type="timeout", retry_count=0)

        service.retry_entry(entry.id)
        payload = _summary()

        assert "humans_paged" not in payload.get("counters", {})
        assert payload["counters"]["replayed"] >= 1
        # The window statement always ships with the numbers it scopes.
        assert "since" in payload
