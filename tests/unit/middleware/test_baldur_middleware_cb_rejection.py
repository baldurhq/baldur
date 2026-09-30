"""The middleware's own refusal is recorded against the breaker that made it.

793 D1. The preemptive 503 exit never reaches the breaker's admission path,
so the middleware records the refusal itself: a request turned away because a
dependency is cut off is a call that did not succeed, and the system-wide rate
must see the inbound share of them too. The refusing row is re-resolved on
the refusal path only (a cold path, one L1 lookup) so the boolean check stays
the single seam; a row that closed in between records nothing. Recording is
evidence, never a gate — a raising service is logged at DEBUG and the 503
still goes out.

Verification techniques applied:
- State transition: the 503 exit records exactly once, against the refusing
  row; a DLQ-ineligible request, an observe-only request and a pin-active
  Block record nothing (the last through the service's own pin check)
- Error path: a raising service leaves the response unchanged
- Dependency interaction: the recorded call carries the row's name and the row
- Branch outcome (802 D3): under observe-only an operator's Block on the
  request domain's row is refused without a DLQ entry, while an automatic
  OPEN falls through; the withheld store is on dry-run's timeline only
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock, Mock

import pytest
from structlog.testing import capture_logs

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.api.django.middleware.baldur import BaldurMiddleware
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now
from tests.factories import dry_run_active, kill_switch_active

DB_DOMAIN = BaldurMiddleware.CB_DATABASE_DOMAIN


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeRequest:
    def __init__(self, path: str = "/api/orders/", method: str = "POST"):
        self.path = path
        self.method = method
        self.body = b""
        self.META: dict = {}


@pytest.fixture(autouse=True)
def _reset_class_state():
    BaldurMiddleware._paths_loaded = False
    yield
    BaldurMiddleware._paths_loaded = False


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def cb_service(repo) -> CircuitBreakerService:
    """A real breaker service: the refusal's effect is its window delta."""
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True, failure_threshold=5, minimum_calls=10
        ),
        repository=repo,
    )


def _open_row(repo, name: str, **overrides) -> None:
    repo.hydrate_snapshot(
        CircuitBreakerStateData(
            service_name=name,
            state=CircuitBreakerStateEnum.OPEN.value,
            failure_count=5,
            opened_at=utc_now(),
            **overrides,
        )
    )


def _preemptive_middleware(
    cb_service, *, dlq_eligible: bool = True
) -> BaldurMiddleware:
    """Middleware whose next request would take the preemptive branch."""
    mw = BaldurMiddleware(get_response=lambda r: FakeResponse(200))
    mw._initialized = True
    mw._audit_logger = None
    mw._cb_service = cb_service
    mw._retry_after_max = 300
    mw._is_dlq_eligible = Mock(
        spec=BaldurMiddleware._is_dlq_eligible, return_value=dlq_eligible
    )
    mw._store_to_dlq = Mock(spec=BaldurMiddleware._store_to_dlq, return_value="dlq-1")
    return mw


class TestMiddlewareRefusalEvidenceBehavior:
    """When the preemptive exit records, and against which row."""

    def test_503_exit_records_one_rejection_against_the_refusing_row(
        self, cb_service, repo
    ):
        # Given: the database breaker is open; the request is DLQ-eligible.
        _open_row(repo, DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)

        # When
        response = mw(FakeRequest())

        # Then: one refusal in the database breaker's window, nowhere else.
        assert response.status_code == 503
        assert cb_service.get_window_evidence(DB_DOMAIN) == (1, 1)
        mw._store_to_dlq.assert_called_once()

    def test_domain_breaker_refusal_is_recorded_against_the_domain(
        self, cb_service, repo
    ):
        """The domain row is the refusing one when the database breaker is closed."""
        repo.get_or_create(DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)
        domain = mw._infer_domain("/api/orders/")
        _open_row(repo, domain)

        response = mw(FakeRequest(path="/api/orders/"))

        assert response.status_code == 503
        assert cb_service.get_window_evidence(domain) == (1, 1)
        assert cb_service.get_window_evidence(DB_DOMAIN) == (0, 0)

    def test_dlq_ineligible_request_takes_no_preemptive_exit_and_records_nothing(
        self, cb_service, repo
    ):
        _open_row(repo, DB_DOMAIN)
        mw = _preemptive_middleware(cb_service, dlq_eligible=False)

        response = mw(FakeRequest(method="GET"))

        assert response.status_code == 200
        assert cb_service.get_window_evidence(DB_DOMAIN) == (0, 0)

    def test_observe_only_request_records_nothing(self, cb_service, repo):
        """Shadow mode decides without acting: neither the 503 nor the evidence."""
        _open_row(repo, DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)

        with dry_run_active():
            response = mw(FakeRequest())

        assert response.status_code == 200
        assert cb_service.get_window_evidence(DB_DOMAIN) == (0, 0)

    def test_pin_active_block_refuses_but_records_nothing(self, cb_service, repo):
        """An operator's Block turns the request away without being evidence."""
        _open_row(
            repo,
            DB_DOMAIN,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=10),
        )
        mw = _preemptive_middleware(cb_service)

        response = mw(FakeRequest())

        assert response.status_code == 503
        assert cb_service.get_window_evidence(DB_DOMAIN) == (0, 0)

    def test_row_that_closed_between_the_check_and_the_record_records_nothing(
        self, cb_service, repo
    ):
        """The conservative direction: the refusal path re-resolves the row."""
        _open_row(repo, DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)
        original = mw._refusing_cb_row
        reads: list[int] = []

        def _close_after_first_read(request=None):
            row = original(request)
            reads.append(1)
            if len(reads) == 1:
                repo.hydrate_snapshot(
                    CircuitBreakerStateData(
                        service_name=DB_DOMAIN,
                        state=CircuitBreakerStateEnum.CLOSED.value,
                    )
                )
            return row

        mw._refusing_cb_row = _close_after_first_read

        response = mw(FakeRequest())

        assert response.status_code == 503
        assert len(reads) == 2
        assert cb_service.get_window_evidence(DB_DOMAIN) == (0, 0)

    def test_raising_service_logs_at_debug_and_leaves_the_response_unchanged(
        self, repo
    ):
        """Error path: recording is evidence, never a gate."""
        service = MagicMock(spec=CircuitBreakerService)
        service.is_enabled = True
        service.get_or_create_state.side_effect = lambda name: CircuitBreakerStateData(
            service_name=name,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )
        service.record_rejection.side_effect = RuntimeError("window lock broken")
        mw = _preemptive_middleware(service)

        with capture_logs() as logs:
            response = mw(FakeRequest())

        assert response.status_code == 503
        failed = [
            entry
            for entry in logs
            if entry.get("event") == "baldur_middleware.cb_rejection_record_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "debug"

    def test_record_forwards_the_row_name_and_the_row(self):
        """Dependency interaction: the service receives the refusing row itself."""
        service = MagicMock(spec=CircuitBreakerService)
        service.is_enabled = True
        row = CircuitBreakerStateData(
            service_name=DB_DOMAIN,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )
        service.get_or_create_state.return_value = row
        mw = _preemptive_middleware(service)

        mw._record_cb_rejection(FakeRequest())

        service.record_rejection.assert_called_once_with(DB_DOMAIN, row)

    def test_disabled_service_records_nothing(self):
        service = MagicMock(spec=CircuitBreakerService)
        service.is_enabled = False
        mw = _preemptive_middleware(service)

        mw._record_cb_rejection(FakeRequest())

        service.record_rejection.assert_not_called()


_OBSERVE_ONLY = pytest.mark.parametrize(
    "switch", [kill_switch_active, dry_run_active], ids=["kill_switch", "dry_run"]
)


def _pin(repo, name: str, *, expires_in_minutes: int = 10) -> None:
    _open_row(
        repo,
        name,
        manually_controlled=True,
        manual_override_expires_at=utc_now() + timedelta(minutes=expires_in_minutes),
    )


class TestOperatorBlockPreemptiveUnderObserveOnlyBehavior:
    """The preemptive branch refuses an operator's Block under observe-only (802 D3).

    Asked of the request domain's own row — the database row, checked first by
    the refusal, carries no domain pin — and refused without parking the
    request: the DLQ capture is an automatic intervention.
    """

    @_OBSERVE_ONLY
    def test_domain_block_is_refused_without_a_dlq_entry(
        self, switch, cb_service, repo
    ):
        """503 naming the operator's Block; nothing stored, nothing claimed stored."""
        # Given
        repo.get_or_create(DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)
        _pin(repo, mw._infer_domain("/api/orders/"))

        # When
        with switch():
            response = mw(FakeRequest(path="/api/orders/"))

        # Then
        body = json.loads(response.content)
        assert response.status_code == 503
        assert body["code"] == "CIRCUIT_BREAKER_OPEN"
        assert (body["dlq_stored"], body["dlq_id"]) == (False, None)
        mw._store_to_dlq.assert_not_called()

    @_OBSERVE_ONLY
    def test_domain_block_is_found_behind_an_open_database_row(
        self, switch, cb_service, repo
    ):
        """The database row refuses first; the pin is read from the domain's row."""
        _open_row(repo, DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)
        _pin(repo, mw._infer_domain("/api/orders/"))

        with switch():
            response = mw(FakeRequest(path="/api/orders/"))

        assert response.status_code == 503
        mw._store_to_dlq.assert_not_called()

    @_OBSERVE_ONLY
    def test_automatically_opened_domain_row_falls_through(
        self, switch, cb_service, repo
    ):
        """Negative twin: an automatic OPEN steps aside and the request is served."""
        repo.get_or_create(DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)
        _open_row(repo, mw._infer_domain("/api/orders/"))

        with switch():
            response = mw(FakeRequest(path="/api/orders/"))

        assert response.status_code == 200
        mw._store_to_dlq.assert_not_called()

    def test_withheld_store_is_recorded_under_dry_run(self, cb_service, repo):
        """Dry-run's would-have timeline names the DLQ store the Block did not take."""
        repo.get_or_create(DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)
        _pin(repo, mw._infer_domain("/api/orders/"))

        with dry_run_active(), capture_logs() as logs:
            mw(FakeRequest(path="/api/orders/"))

        withheld = [
            entry
            for entry in logs
            if entry.get("event") == "execution_mode.intervention_suppressed"
        ]
        assert [entry["action"] for entry in withheld] == ["dlq_store"]

    def test_withheld_store_is_not_logged_per_request_under_kill_switch(
        self, cb_service, repo
    ):
        """Under the brake the suppression is silent (no per-call record)."""
        repo.get_or_create(DB_DOMAIN)
        mw = _preemptive_middleware(cb_service)
        _pin(repo, mw._infer_domain("/api/orders/"))

        with kill_switch_active(), capture_logs() as logs:
            response = mw(FakeRequest(path="/api/orders/"))

        assert response.status_code == 503
        assert "execution_mode.intervention_suppressed" not in [
            entry.get("event") for entry in logs
        ]
