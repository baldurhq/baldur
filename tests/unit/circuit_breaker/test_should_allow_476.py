"""476 — CircuitBreakerService.should_allow rewrite contract tests.

The HALF_OPEN slot decision is now delegated to the repository's atomic
``try_acquire_half_open_slot`` (Lua / RLock single-winner). This test file
guards three properties the rewrite must hold:

- **R1**: ``CIRCUIT_BREAKER_HALF_OPENED`` is emitted **exactly once** for
  the OPEN→HALF_OPEN transition. Pre-call state inspection (the
  pre-476 pattern) had a TOCTOU race that emitted duplicate events
  under contention. Using ``previous_state == "open" AND new_state ==
  "half_open"`` from the atomic primitive's tuple is the single-winner
  signal.
- **D10**: ``blocked_total{service, reason}`` is incremented with
  ``reason="half_open_full"`` when the HALF_OPEN window rejects, and
  ``reason="open"`` when state is OPEN with recovery_timeout not yet
  elapsed.
- **R3**: When ``half_open_max_calls`` is reduced at runtime below
  the current count, the next acquire returns False with
  ``reason="half_open_full"`` and the already-acquired slots drain via
  ``record_*``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from baldur.utils.time import utc_now as tz_now


@pytest.fixture
def mock_repo():
    return Mock()


@pytest.fixture
def mock_config():
    cfg = Mock()
    cfg.enabled = True
    cfg.recovery_timeout = 60
    cfg.success_threshold = 2
    cfg.failure_threshold = 5
    cfg.half_open_max_calls = 10
    return cfg


@pytest.fixture
def service(mock_repo, mock_config):
    from baldur.services.circuit_breaker.service import CircuitBreakerService

    return CircuitBreakerService(config=mock_config, repository=mock_repo)


@pytest.fixture
def open_state_elapsed():
    """OPEN state with recovery_timeout already elapsed (eligible for HALF_OPEN)."""
    state = Mock()
    state.state = "open"
    state.manually_controlled = False
    state.opened_at = tz_now() - timedelta(seconds=120)
    return state


# =============================================================================
# R1 — single-emission CIRCUIT_BREAKER_HALF_OPENED
# =============================================================================


class TestShouldAllowSingleEmissionBehavior:
    """The atomic primitive's single-winner tuple is the only emission gate."""

    def test_emits_half_opened_exactly_once_on_winner_call(
        self, service, mock_repo, open_state_elapsed
    ):
        from baldur.services.event_bus import EventType

        mock_repo.get_or_create.return_value = open_state_elapsed
        mock_repo.try_acquire_half_open_slot.return_value = (True, "open", "half_open")

        events = []

        def capture(event_type, data, **_):
            events.append((event_type, data))

        with patch.object(service, "_emit_event", side_effect=capture):
            allowed = service.should_allow("svc")

        assert allowed is True
        half_open_events = [
            (et, d) for et, d in events if et == EventType.CIRCUIT_BREAKER_HALF_OPENED
        ]
        assert len(half_open_events) == 1
        assert half_open_events[0][1]["trigger"] == "auto"
        assert half_open_events[0][1]["previous_state"] == "open"

    def test_no_emission_when_call_only_increments_existing_window(
        self, service, mock_repo, open_state_elapsed
    ):
        """A non-winner thread joining an existing HALF_OPEN must not re-emit."""
        from baldur.services.event_bus import EventType

        # State is already HALF_OPEN — pre-existing window.
        existing = Mock()
        existing.state = "half_open"
        existing.manually_controlled = False
        existing.opened_at = open_state_elapsed.opened_at
        mock_repo.get_or_create.return_value = existing
        mock_repo.try_acquire_half_open_slot.return_value = (
            True,
            "half_open",
            "half_open",
        )

        events = []
        with patch.object(
            service, "_emit_event", side_effect=lambda et, data, **_: events.append(et)
        ):
            service.should_allow("svc")

        assert EventType.CIRCUIT_BREAKER_HALF_OPENED not in events

    def test_no_emission_when_acquire_rejects(
        self, service, mock_repo, open_state_elapsed
    ):
        """allowed=False must not emit (no transition happened)."""
        from baldur.services.event_bus import EventType

        existing = Mock()
        existing.state = "half_open"
        existing.manually_controlled = False
        existing.opened_at = open_state_elapsed.opened_at
        mock_repo.get_or_create.return_value = existing
        mock_repo.try_acquire_half_open_slot.return_value = (
            False,
            "half_open",
            "half_open",
        )

        events = []
        with patch.object(
            service, "_emit_event", side_effect=lambda et, data, **_: events.append(et)
        ):
            allowed = service.should_allow("svc")

        assert allowed is False
        assert EventType.CIRCUIT_BREAKER_HALF_OPENED not in events


# =============================================================================
# D10 — blocked_total reason label
# =============================================================================


class TestShouldAllowBlockedReasonBehavior:
    """blocked_total must be tagged with the precise rejection reason."""

    def test_open_pre_recovery_tags_reason_open(self, service, mock_repo):
        """OPEN with recovery_timeout not elapsed → reason='open' (no Lua call)."""
        state = Mock()
        state.state = "open"
        state.manually_controlled = False
        state.opened_at = tz_now() - timedelta(seconds=5)  # < recovery_timeout=60
        mock_repo.get_or_create.return_value = state

        with patch(
            "baldur.services.circuit_breaker.service.record_blocked"
        ) as mock_record:
            allowed = service.should_allow("svc")

        assert allowed is False
        mock_repo.try_acquire_half_open_slot.assert_not_called()
        mock_record.assert_called_once_with("svc", "open")

    def test_half_open_full_tags_reason_half_open_full(
        self, service, mock_repo, open_state_elapsed
    ):
        """HALF_OPEN limit reached → reason='half_open_full'."""
        existing = Mock()
        existing.state = "half_open"
        existing.manually_controlled = False
        existing.opened_at = open_state_elapsed.opened_at
        mock_repo.get_or_create.return_value = existing
        mock_repo.try_acquire_half_open_slot.return_value = (
            False,
            "half_open",
            "half_open",
        )

        with patch(
            "baldur.services.circuit_breaker.service.record_blocked"
        ) as mock_record:
            allowed = service.should_allow("svc")

        assert allowed is False
        mock_record.assert_called_once_with("svc", "half_open_full")

    def test_allowed_path_does_not_record_blocked(
        self, service, mock_repo, open_state_elapsed
    ):
        mock_repo.get_or_create.return_value = open_state_elapsed
        mock_repo.try_acquire_half_open_slot.return_value = (True, "open", "half_open")

        with patch(
            "baldur.services.circuit_breaker.service.record_blocked"
        ) as mock_record:
            allowed = service.should_allow("svc")

        assert allowed is True
        mock_record.assert_not_called()


# =============================================================================
# R3 — dynamic limit reduction
# =============================================================================


class TestShouldAllowDynamicLimitBehavior:
    """Lowering ``half_open_max_calls`` mid-window must reject excess acquires."""

    def test_reduced_limit_below_current_count_returns_false(
        self, service, mock_repo, mock_config, open_state_elapsed
    ):
        """Effective config drives the limit on every call (no stale cache).

        Lua receives ``limit`` as ARGV every call (R3 — see doc). When the
        operator lowers ``half_open_max_calls`` from 10 → 3 while
        ``count == 5``, the very next acquire must reject because
        count >= new_limit.
        """
        existing = Mock()
        existing.state = "half_open"
        existing.manually_controlled = False
        existing.opened_at = open_state_elapsed.opened_at
        mock_repo.get_or_create.return_value = existing

        # Operator reduces the limit at runtime.
        mock_config.half_open_max_calls = 3

        # Repository (correctly) returns rejected because count(5) >= limit(3).
        mock_repo.try_acquire_half_open_slot.return_value = (
            False,
            "half_open",
            "half_open",
        )

        with patch(
            "baldur.services.circuit_breaker.service.record_blocked"
        ) as mock_record:
            allowed = service.should_allow("svc")

        # Service must have forwarded the *reduced* limit verbatim.
        mock_repo.try_acquire_half_open_slot.assert_called_once()
        call_kwargs = mock_repo.try_acquire_half_open_slot.call_args.kwargs
        assert call_kwargs.get("limit") == 3 or (
            mock_repo.try_acquire_half_open_slot.call_args.args
            and mock_repo.try_acquire_half_open_slot.call_args.args[1] == 3
        )

        assert allowed is False
        mock_record.assert_called_once_with("svc", "half_open_full")

    def test_limit_passed_per_call_not_cached(
        self, service, mock_repo, mock_config, open_state_elapsed
    ):
        """Two consecutive calls with different limits must each receive their own."""
        existing = Mock()
        existing.state = "half_open"
        existing.manually_controlled = False
        existing.opened_at = open_state_elapsed.opened_at
        mock_repo.get_or_create.return_value = existing
        mock_repo.try_acquire_half_open_slot.return_value = (
            True,
            "half_open",
            "half_open",
        )

        mock_config.half_open_max_calls = 10
        service.should_allow("svc")
        mock_config.half_open_max_calls = 2
        service.should_allow("svc")

        calls = mock_repo.try_acquire_half_open_slot.call_args_list
        assert len(calls) == 2
        # Limit is the second positional arg or a `limit` kwarg.
        first_limit = (
            calls[0].kwargs.get("limit") if calls[0].kwargs else calls[0].args[1]
        )
        second_limit = (
            calls[1].kwargs.get("limit") if calls[1].kwargs else calls[1].args[1]
        )
        assert first_limit == 10
        assert second_limit == 2
