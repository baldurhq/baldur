"""Failure-rate producer integration tests (746).

The per-service failure rate is one logical claim spanning three collaborators
that share process state, and none of them expresses it alone:

    1. ``protect()`` builds a ``CircuitBreakerPolicy``, which resolves its
       window key once at construction and feeds the module-level outcome window
       from ``_on_success`` / ``_on_failure``.
    2. The same policy builds its breaker against the ``"layered"``
       circuit-breaker repository obtained from ``ProviderRegistry``, and writes
       breaker state into it.
    3. ``ControlAPIService.get_metrics()`` reads *that same repository object*
       through a separate call path, and *that same window* through another —
       then joins both against a canonical row key.

The read/write-instance agreement is the part no unit test can prove: the two
sides ask the registry for the repository independently, and nothing inside
either module can show that the two requests resolve to one instance. Reading
the registry's module-load default instead resolves nothing on any deployment
whose Redis L2 is absent, so the honest ``null`` this change introduces would
have been the answer for every service on exactly those deployments.

Mock-based — no infra. The layered repository's ``get_all_states()`` reads its
in-process L1 tier, and that L2-absent condition is precisely what these tests
assert against. The registry seam is patched only in the degradation case, where
the point is what the payload renders when the named provider is missing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.factory import ProviderRegistry
from baldur.factory.base import GenericProviderRegistry
from baldur.protect_facade import protect, reset_protect_caches
from baldur.services.circuit_breaker.time_outcome_window import (
    get_call_outcome_window,
)
from baldur.services.control_api_service import ControlAPIService

SERVICE = "checkout_api"
DOTTED_SERVICE = "orders.charge"


class _Boom(Exception):
    """The failure the protected call raises."""


def _raise_boom():
    raise _Boom("downstream down")


@pytest.fixture(autouse=True)
def _clean_protect_state():
    """Start and end with no policy cache and no recorded outcomes.

    The window is a process singleton fed by every protected call, so without
    this a neighbouring test's traffic lands in this payload.
    """
    reset_protect_caches()
    yield
    reset_protect_caches()


@pytest.fixture(autouse=True)
def _no_dlq_repository():
    """Leave the DLQ statistics repository unresolved.

    The failed-operation store is an independent failure domain: the rate
    fields are computed without it, and the payload already models its absence
    (the DLQ columns render as an empty breakdown). Resolving the real one here
    reaches for a Redis connection that no part of this claim needs, and costs
    a connect timeout per case on any host without one.
    """
    unresolved = MagicMock(spec=GenericProviderRegistry)
    unresolved.safe_get.return_value = None
    with patch.object(ProviderRegistry, "failed_op_repo", unresolved):
        yield


def _drive_failures(name: str, count: int) -> None:
    """Run ``count`` failing calls through the default protect() pipeline.

    Swallows what propagates: with no fallback the pipeline re-raises the
    business error, and once the breaker opens it raises its own rejection
    instead. Which of the two arrives is not what these tests are about.
    """
    for _ in range(count):
        try:
            protect(name, _raise_boom)
        except Exception:
            pass


def _drive_successes(name: str, count: int) -> None:
    """Run ``count`` succeeding calls through the default protect() pipeline."""
    for _ in range(count):
        assert protect(name, lambda: "ok") == "ok"


def _trip_the_breaker(name: str) -> None:
    """Fail exactly as many protected calls as the configured trip point.

    Read from settings rather than written as a literal, so the test tracks the
    shipped threshold. Every one of these calls is admitted and counted, which
    is what leaves the measured rate at exactly 1.0.
    """
    from baldur.settings.circuit_breaker import get_circuit_breaker_settings

    _drive_failures(name, get_circuit_breaker_settings().failure_threshold)


def _row(payload: dict, service_name: str) -> dict | None:
    """The one per-service row naming this service, or None."""
    for row in payload["services"]:
        if row["service_name"] == service_name:
            return row
    return None


# =============================================================================
# A. End-to-end producer — protected traffic reaches the payload
# =============================================================================


class TestProtectedTrafficReachesTheMetricsPayload:
    """A service failing every call must not read healthy on the metrics API."""

    def test_failing_protected_calls_produce_a_row_with_a_real_failure_rate(self):
        """The whole claim, end to end.

        Given/When/Then: the service is deliberately never registered as a
        metric domain — the default circuit-breaker-only ``protect()`` registers
        none — so the row can only exist because the window contributed it.
        """
        # Given: no evidence at all
        assert get_call_outcome_window().snapshot() == {}

        # When: three protected calls fail and one succeeds
        _drive_failures(SERVICE, 3)
        _drive_successes(SERVICE, 1)
        payload = ControlAPIService().get_metrics()

        # Then: the row exists and carries the measured rate
        row = _row(payload, SERVICE)
        assert row is not None
        assert row["failure_rate_5m"] == 0.75

    def test_aggregate_matches_the_traffic_the_worker_actually_served(self):
        """The aggregate rides the same snapshot as the rows."""
        _drive_failures(SERVICE, 2)
        _drive_successes(SERVICE, 2)

        payload = ControlAPIService().get_metrics()

        assert payload["last_5m_request_count"] == 4
        assert payload["last_5m_failure_rate"] == 0.5

    def test_row_count_never_exceeds_the_reported_service_total(self):
        """The window's keys are members of the total's union, by construction."""
        _drive_failures(SERVICE, 1)
        _drive_successes("payment_api", 1)

        payload = ControlAPIService().get_metrics()

        assert len(payload["services"]) <= payload["total_services"]

    def test_untouched_service_row_reports_an_unmeasured_rate(self):
        """A registered domain this worker never served renders null, not 0.0."""
        _drive_failures(SERVICE, 1)

        payload = ControlAPIService().get_metrics()

        other_rows = [
            row for row in payload["services"] if row["service_name"] != SERVICE
        ]
        assert other_rows, "the registry's seed domains should still produce rows"
        assert all(row["failure_rate_5m"] is None for row in other_rows)

    def test_dotted_service_name_lands_on_its_canonical_row(self):
        """One join vocabulary, proven through the real projection.

        A name a metric label cannot carry verbatim must still be measurable:
        the window key and the row key are the same canonical form, so the rate
        is not lost between the policy and the payload.
        """
        _drive_failures(DOTTED_SERVICE, 2)

        payload = ControlAPIService().get_metrics()

        row = _row(payload, "orders_charge")
        assert row is not None
        assert row["failure_rate_5m"] == 1.0
        assert _row(payload, DOTTED_SERVICE) is None


# =============================================================================
# B. Read/write instance agreement (D15)
# =============================================================================


class TestMetricsReadsTheBreakerStateThePolicyWrote:
    """The payload's breaker state and its rate describe one process.

    With no Redis L2 reachable, the layered repository still holds real state in
    its in-process L1 tier — so a breaker opened by protected traffic has to be
    visible to the endpoint. If the read resolved a different repository
    instance, the state column would be empty here while the breaker is open.
    """

    def test_breaker_opened_by_protected_traffic_renders_open_on_its_row(self):
        """The state the policy wrote is the state the payload reads.

        Given/When/Then: the breaker is driven open through the public
        ``protect()`` surface with nothing stubbed between the policy and the
        repository, and the row is asserted to name the open state beside its
        measured rate — three fields describing one process.
        """
        # Given/When: enough protected failures to trip the breaker
        _trip_the_breaker(SERVICE)
        payload = ControlAPIService().get_metrics()

        # Then: the row reports the open breaker and a fully-failing rate
        row = _row(payload, SERVICE)
        assert row["circuit_state"] == "open"
        assert row["failure_rate_5m"] == 1.0

    def test_read_side_sees_the_state_the_write_side_stored(self):
        """The named lookup is a per-name singleton, so both sides share it.

        Asserted against the repository the endpoint resolves, not against the
        rendered string: this is the instance-identity claim itself.
        """
        _trip_the_breaker(SERVICE)

        repo = ControlAPIService._resolve_circuit_breaker_repo()

        assert repo is not None
        stored = {state.service_name: state.state for state in repo.get_all_states()}
        assert stored.get(SERVICE) == "open"

    def test_healthy_protected_traffic_renders_a_closed_breaker(self):
        """The state column is real in both directions, not only when open."""
        _drive_successes(SERVICE, 1)

        payload = ControlAPIService().get_metrics()

        assert _row(payload, SERVICE)["circuit_state"] == "closed"

    def test_missing_layered_provider_degrades_to_an_unknown_state(self):
        """On a redis-client-absent install the name is unregistered.

        The read falls back the same way the policy falls back for its own
        lookup, and when that also yields nothing the state is unknown — never a
        raise, and never a fabricated "closed". The rate still comes through,
        because the two fields have independent sources.
        """
        _drive_failures(SERVICE, 1)

        absent_default = MagicMock(spec=GenericProviderRegistry)
        absent_default.safe_get.return_value = None
        with (
            patch.object(
                ProviderRegistry,
                "get_circuit_breaker_repo",
                side_effect=ValueError("provider 'layered' is not registered"),
            ),
            patch.object(ProviderRegistry, "circuit_breaker_repo", absent_default),
        ):
            payload = ControlAPIService().get_metrics()

        row = _row(payload, SERVICE)
        assert row["circuit_state"] is None
        assert row["failure_rate_5m"] == 1.0


# =============================================================================
# C. Producer isolation — a reset drops the evidence
# =============================================================================


class TestProtectResetDropsTheMeasuredEvidence:
    """The producer is process-local state on the protect-cache reset chain.

    Without the explicit drop, one settings-reset boundary's traffic becomes the
    next one's rows — which is a test-isolation problem in this suite and a
    stale-evidence problem for anything that reloads configuration.
    """

    def test_reset_protect_caches_returns_the_payload_to_honest_absence(self):
        """Given/When/Then: measured traffic, a reset, then an unmeasured payload."""
        # Given: a measured failure rate
        _drive_failures(SERVICE, 2)
        measured = _row(ControlAPIService().get_metrics(), SERVICE)
        assert measured["failure_rate_5m"] == 1.0

        # When: the protect caches are reset
        reset_protect_caches()

        # Then: the payload reports absence rather than a stale rate
        payload = ControlAPIService().get_metrics()
        assert payload["last_5m_failure_rate"] is None
        assert payload["last_5m_request_count"] == 0
