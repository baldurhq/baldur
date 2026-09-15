"""A hinted success appends read-free only while nothing happened to the name.

793 D1/D2. The admission-time hint unlocks ``record_success``'s read-free fast
path. The window's epoch and its in-flight marker keep that path honest: a
failure write in flight, a failure that landed, a transition this process
observed or an operator's extension all move the name past the hint, and the
success then takes the slow path's fresh read — so it is never appended to a
name that is no longer CLOSED, and the consecutive-count reset it owes is never
skipped. A trip keeps the evidence that produced it; only an observed re-entry
into CLOSED, or a pin that swallowed the trip, clears the window.

Verification techniques applied:
- Deterministic interleaves through the repository's own ``record_failure``
  seam (a barrier cannot place an event inside a method; wrapping the write
  can): the success records while the failure write is in flight, and the
  admission lands between the marker and the write
- Negative assertions: a stored count of ``1`` after the reset-owing success
  never occurs; no success is appended to a non-CLOSED name, cascade trip
  included; a post-trip window is never ``(0, 0)``
- Error path: the in-flight marker is released when the write raises
- State transition: the HALF_OPEN -> OPEN attempt is observed; a layered
  ``did_close=False`` writeback clears exactly once; the first post-close
  failure does not re-trip
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
    pinned_trip_attempt,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.outcome_window import evaluate_trip
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now
from tests.factories import InMemoryRateLimitTracker

SERVICE = "payment-api"
FAILURE_THRESHOLD = 5
WINDOW_SIZE = 100


def _config(**overrides) -> CircuitBreakerConfig:
    base = {
        "enabled": True,
        "failure_threshold": FAILURE_THRESHOLD,
        "success_threshold": 1,
        "sliding_window_size": WINDOW_SIZE,
        "minimum_calls": 10,
        "recovery_timeout": 60,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def service(repo) -> CircuitBreakerService:
    svc = CircuitBreakerService(config=_config(), repository=repo)
    # The trip's audit write and burn-rate multiplier reach outside the
    # breaker; the window and the stored row are what these tests assert on.
    with (
        patch.object(svc, "_log_circuit_open_audit"),
        patch.object(svc, "_apply_burn_rate_multiplier"),
    ):
        yield svc


def _admit(service: CircuitBreakerService):
    """Take a CLOSED admission and return the hint pair it carries."""
    decision = service.should_allow_with_state(SERVICE)
    assert decision.allowed is True
    return decision.state, decision.window_epoch


def _record_hinted_success(service: CircuitBreakerService, hint) -> None:
    hint_state, hint_epoch = hint
    service.record_success(SERVICE, hint_state=hint_state, hint_epoch=hint_epoch)


# =============================================================================
# Behavior — the two interleaves the epoch and the marker exist for
# =============================================================================


class TestRecordSuccessEpochRaceBehavior:
    """The hint falls to the slow path whenever the name moved past it."""

    def test_success_recorded_while_the_failure_write_is_in_flight_resets_the_count(
        self, service, repo
    ):
        """Interleave 1: the marker is held, so the success takes the slow path.

        The hint was taken on a clean CLOSED row; the failure's repository
        write is in progress when the success records. Without the marker the
        fast path would append read-free and skip the reset the failure now
        owes; with it the slow path reads the landed failure and resets it.
        """
        # Given: a clean admission, and a failure whose write is about to run.
        repo.get_or_create(SERVICE)
        hint = _admit(service)
        original_write = repo.record_failure
        fired: list[bool] = []

        def _write_then_record_success(service_name: str):
            row = original_write(service_name)
            if not fired:
                fired.append(True)
                # The write has landed, the marker is still held.
                assert service._outcome_window._writes_in_flight.get(SERVICE) == 1
                _record_hinted_success(service, hint)
            return row

        # When
        with patch.object(
            repo, "record_failure", side_effect=_write_then_record_success
        ):
            service.record_failure(SERVICE)

        # Then: the interleave happened, the reset landed, the count is 0.
        assert fired == [True]
        assert repo.get_by_service_name(SERVICE).failure_count == 0
        assert service._outcome_window._writes_in_flight.get(SERVICE, 0) == 0

    def test_admission_between_the_marker_and_the_write_then_success_after_it_resets(
        self, service, repo
    ):
        """Interleave 2: the hint predates the write; the success comes after it.

        The admission lands after ``begin_write`` and before the repository
        write, so its epoch is the pre-write one. ``end_write`` moves the
        epoch past the write, the later success sees a stale hint, and the
        stored count is reset to 0 — never left at 1.
        """
        # Given: the admission is taken inside the failure's bracket.
        repo.get_or_create(SERVICE)
        original_write = repo.record_failure
        hints: list = []

        def _admit_then_write(service_name: str):
            if not hints:
                hints.append(_admit(service))
            return original_write(service_name)

        with patch.object(repo, "record_failure", side_effect=_admit_then_write):
            service.record_failure(SERVICE)
        assert repo.get_by_service_name(SERVICE).failure_count == 1

        # When: the success hinted inside the bracket records after the write.
        _record_hinted_success(service, hints[0])

        # Then
        assert repo.get_by_service_name(SERVICE).failure_count == 0

    def test_fresh_hint_after_a_landed_failure_still_takes_the_slow_path(
        self, service, repo
    ):
        """A hint on a row carrying failures is never a fast-path hint."""
        repo.get_or_create(SERVICE)
        service.record_failure(SERVICE)
        hint = _admit(service)

        _record_hinted_success(service, hint)

        assert repo.get_by_service_name(SERVICE).failure_count == 0

    def test_success_hinted_before_a_trip_is_not_appended_to_the_open_name(
        self, service, repo
    ):
        """A stale hint never lands a success on a name that is no longer CLOSED."""
        # Given: a clean admission, then the burst that trips the name.
        repo.get_or_create(SERVICE)
        hint = _admit(service)
        for _ in range(FAILURE_THRESHOLD):
            service.record_failure(SERVICE)
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )
        evidence_before = service.get_window_evidence(SERVICE)

        # When
        _record_hinted_success(service, hint)

        # Then: no success entered the window; the trip's evidence stands.
        assert service.get_window_evidence(SERVICE) == evidence_before
        assert evidence_before == (FAILURE_THRESHOLD, FAILURE_THRESHOLD)

    def test_success_hinted_before_a_cascade_trip_is_not_appended(self, service, repo):
        """The cascade trip is observed like any other: the hint falls stale."""
        repo.get_or_create(SERVICE)
        hint = _admit(service)
        tracker = InMemoryRateLimitTracker()
        tracker._rate_limits[SERVICE] = 50
        tracker._requests[SERVICE] = 100
        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=tracker,
        ):
            tripped = service.evaluate_rate_limit_cascade(SERVICE)
        assert tripped is not None
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )

        _record_hinted_success(service, hint)

        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_hint_without_an_epoch_takes_the_slow_path(self, service, repo):
        """``hint_epoch=None`` reads: a caller that carries no epoch gets no fast path."""
        repo.get_or_create(SERVICE)
        hint_state, _ = _admit(service)

        with patch.object(repo, "get_or_create", wraps=repo.get_or_create) as read:
            service.record_success(SERVICE, hint_state=hint_state, hint_epoch=None)

        read.assert_called_once_with(SERVICE)

    def test_matching_hint_takes_the_fast_path_and_appends(self, service, repo):
        """Control: with nothing in between, the hint is honoured read-free."""
        repo.get_or_create(SERVICE)
        hint = _admit(service)

        with patch.object(repo, "get_or_create", wraps=repo.get_or_create) as read:
            _record_hinted_success(service, hint)

        read.assert_not_called()
        assert service.get_window_evidence(SERVICE) == (0, 1)


# =============================================================================
# Behavior — the failure write's bracket
# =============================================================================


class TestRecordFailureInFlightMarkerBehavior:
    """The marker is held across the write and released whatever happens."""

    def test_marker_is_released_when_the_repository_write_raises(self, service, repo):
        """Error path: a raising write leaves no marker behind."""
        repo.get_or_create(SERVICE)

        with (
            patch.object(
                repo, "record_failure", side_effect=RuntimeError("store down")
            ),
            pytest.raises(RuntimeError),
        ):
            service.record_failure(SERVICE)

        assert service._outcome_window._writes_in_flight.get(SERVICE, 0) == 0

    def test_marker_is_held_exactly_across_the_write(self, service, repo):
        """The write sees the marker; the caller never does."""
        repo.get_or_create(SERVICE)
        seen: list[int] = []
        original_write = repo.record_failure

        def _observe(service_name: str):
            seen.append(service._outcome_window._writes_in_flight.get(SERVICE, 0))
            return original_write(service_name)

        with patch.object(repo, "record_failure", side_effect=_observe):
            service.record_failure(SERVICE)

        assert seen == [1]
        assert service._outcome_window._writes_in_flight.get(SERVICE, 0) == 0

    def test_half_open_to_open_attempt_is_observed(self, service, repo):
        """State transition: the revert moves the epoch and keeps the evidence."""
        # Given: a HALF_OPEN row this process has observed as such.
        repo.hydrate_snapshot(
            CircuitBreakerStateData(
                service_name=SERVICE,
                state=CircuitBreakerStateEnum.HALF_OPEN.value,
                opened_at=utc_now() - timedelta(seconds=120),
            )
        )
        service._outcome_window.record_rejection(SERVICE, WINDOW_SIZE)
        service._outcome_window.observe_state(
            SERVICE, CircuitBreakerStateEnum.HALF_OPEN.value
        )
        epoch_before = service._outcome_window.epoch_of(SERVICE)

        # When: the trial fails.
        service.record_failure(SERVICE)

        # Then: the row is OPEN, the window saw it, nothing was cleared.
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )
        assert service._outcome_window.epoch_of(SERVICE) == epoch_before + 1
        assert service.get_window_evidence(SERVICE) == (1, 1)
        # The next observation of OPEN is the steady state, not a transition.
        assert (
            service._outcome_window.observe_state(
                SERVICE, CircuitBreakerStateEnum.OPEN.value
            )
            is False
        )


# =============================================================================
# Behavior — a trip keeps its evidence; a close clears it once
# =============================================================================


class TestTripKeepsEvidenceBehavior:
    """What clears the window, what does not, and how many times."""

    def test_trip_keeps_the_failures_and_calls_that_produced_it(self, service, repo):
        """The post-trip read is ``(f, c)`` — never the pre-fix ``(0, 0)``."""
        repo.get_or_create(SERVICE)
        for _ in range(3):
            service.record_success(SERVICE)
        for _ in range(FAILURE_THRESHOLD):
            service.record_failure(SERVICE)

        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )
        assert service.get_window_evidence(SERVICE) == (FAILURE_THRESHOLD, 8)

    def test_pinned_trip_clears_the_evidence(self):
        """A peer's pin swallowed the burst: the operator's decision is not evidence."""
        repo = Mock(spec=CircuitBreakerStateRepository)
        repo.trip_to_open.return_value = pinned_trip_attempt(
            SERVICE, utc_now() + timedelta(minutes=10)
        )
        service = CircuitBreakerService(config=_config(), repository=repo)
        for _ in range(FAILURE_THRESHOLD):
            service._outcome_window.record_failure(SERVICE, WINDOW_SIZE)
        decided_on = CircuitBreakerStateData(
            service_name=SERVICE,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=FAILURE_THRESHOLD,
        )

        with patch.object(service, "_collect_failure_snapshot", return_value={}):
            service._trip_circuit_open(
                SERVICE,
                decided_on,
                None,
                effective_config=_config(),
                window_failures=FAILURE_THRESHOLD,
                window_total=FAILURE_THRESHOLD,
            )

        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_layered_writeback_close_clears_exactly_once_and_a_later_success_survives(
        self,
    ):
        """A close the store decided (``did_close=False`` here) is observed once.

        The L2 double answers the trial success with the store's CLOSED row and
        ``did_close=False`` (another worker performed the close). Observing the
        row back in CLOSED clears the window; a success admitted after that is
        not wiped by the next observation.
        """
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        l2 = Mock(spec=InMemoryCircuitBreakerStateRepository)
        l2.get_all_states.return_value = []
        l2.record_success_with_close_check.return_value = CircuitBreakerCloseAttempt(
            state=CircuitBreakerStateData(
                service_name=SERVICE, state=CircuitBreakerStateEnum.CLOSED.value
            ),
            did_close=False,
        )
        with (
            patch(
                "baldur.adapters.memory.layered_repository.drift_operations."
                "DriftOperationsMixin._schedule_drift_reconciliation",
                return_value=None,
            ),
            patch(
                "baldur.adapters.memory.layered_repository.base."
                "LayeredRepositoryBase._ensure_l2_warmup_once",
                return_value=None,
            ),
        ):
            layered = LayeredCircuitBreakerStateRepository(
                l2_repo=l2, adapter_type="redis"
            )
            layered._get_timeout_seconds = lambda: 5.0
            service = CircuitBreakerService(config=_config(), repository=layered)

            # Given: this process saw the name HALF_OPEN with refusals recorded.
            layered._l1.hydrate_snapshot(
                CircuitBreakerStateData(
                    service_name=SERVICE,
                    state=CircuitBreakerStateEnum.HALF_OPEN.value,
                    opened_at=utc_now() - timedelta(seconds=120),
                )
            )
            service._outcome_window.record_rejection(SERVICE, WINDOW_SIZE)
            service._outcome_window.observe_state(
                SERVICE, CircuitBreakerStateEnum.HALF_OPEN.value
            )

            # When: the trial success comes back CLOSED from the store.
            service.record_success(SERVICE)

            # Then: cleared once — and a success admitted afterwards survives.
            assert service.get_window_evidence(SERVICE) == (0, 0)
            assert layered._l1.get_by_service_name(SERVICE).state == (
                CircuitBreakerStateEnum.CLOSED.value
            )
            service.record_success(SERVICE)
            service.should_allow(SERVICE)
            assert service.get_window_evidence(SERVICE) == (0, 1)

    def test_first_failure_after_a_close_does_not_re_trip(self, service, repo):
        """The new CLOSED period starts without the last one's evidence."""
        # Given: tripped, then recovered through a trial success.
        repo.get_or_create(SERVICE)
        for _ in range(FAILURE_THRESHOLD):
            service.record_failure(SERVICE)
        repo.hydrate_snapshot(
            CircuitBreakerStateData(
                service_name=SERVICE,
                state=CircuitBreakerStateEnum.HALF_OPEN.value,
                failure_count=FAILURE_THRESHOLD,
                opened_at=utc_now() - timedelta(seconds=120),
            )
        )
        service.record_success(SERVICE)
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.CLOSED.value
        )
        assert service.get_window_evidence(SERVICE) == (0, 0)

        # When: one failure in the new period.
        service.record_failure(SERVICE)

        # Then: no trip — by the stored row and by the predicate itself.
        stored = repo.get_by_service_name(SERVICE)
        assert stored.state == CircuitBreakerStateEnum.CLOSED.value
        assert (
            evaluate_trip(
                stored.failure_count, *service.get_window_evidence(SERVICE), _config()
            )
            is None
        )


# =============================================================================
# Behavior — the one operator write that moves the epoch
# =============================================================================


class TestExtendOverrideBumpsEpochBehavior:
    """An extension re-arms a lapsed pin; a pre-extension hint must not bypass it."""

    def _lapsed_allow_pin(self, repo) -> None:
        repo.get_or_create(SERVICE)
        repo.set_manual_control(
            SERVICE,
            CircuitBreakerStateEnum.CLOSED.value,
            reason="lapsed allow",
            expires_at=utc_now() - timedelta(seconds=1),
        )

    def test_extension_moves_the_epoch(self, service, repo):
        self._lapsed_allow_pin(repo)
        epoch_before = service._outcome_window.epoch_of(SERVICE)

        result = service.extend_manual_override(SERVICE, additional_minutes=30)

        assert result.success is True
        assert service._outcome_window.epoch_of(SERVICE) == epoch_before + 1

    def test_pre_extension_hint_falls_to_the_slow_path_and_its_pin_check(
        self, service, repo
    ):
        """The success admitted against the lapsed row meets the re-armed pin."""
        # Given: a hint taken while the pin was lapsed.
        self._lapsed_allow_pin(repo)
        hint = _admit(service)
        service.extend_manual_override(SERVICE, additional_minutes=30)

        # When
        with patch.object(repo, "get_or_create", wraps=repo.get_or_create) as read:
            _record_hinted_success(service, hint)

        # Then: the slow path ran, and the pin kept the success out of the window.
        read.assert_called_once_with(SERVICE)
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_without_the_extension_the_same_hint_appends(self, service, repo):
        """Control for the test above: the lapsed pin alone does not stop the append."""
        self._lapsed_allow_pin(repo)
        hint = _admit(service)

        _record_hinted_success(service, hint)

        assert service.get_window_evidence(SERVICE) == (0, 1)

    def test_a_failed_extension_moves_nothing(self, service, repo):
        """A row not under manual control is refused before the bump."""
        repo.get_or_create(SERVICE)
        epoch_before = service._outcome_window.epoch_of(SERVICE)

        result = service.extend_manual_override(SERVICE, additional_minutes=30)

        assert result.success is False
        assert service._outcome_window.epoch_of(SERVICE) == epoch_before
