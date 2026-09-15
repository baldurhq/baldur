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
"""

from __future__ import annotations

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
from tests.factories import dry_run_active

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
