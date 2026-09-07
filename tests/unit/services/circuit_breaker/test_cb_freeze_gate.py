"""Freeze Mode, and the six automatic transition writes that consult it.

Freeze Mode is the circuit breaker's *derived* view of Emergency Level 3: it
stores nothing, so every test here drives it from a stub emergency manager
rather than from an activation call. Two properties are worth stating up
front, because they are what the suite exists to hold:

- **Only automatic decisions are gated.** The manual paths (force open, force
  close, reset) run untouched while frozen -- operator intent outranks the
  freeze -- and so does a HALF_OPEN circuit's trial-slot acquire, which is not
  a transition.
- **The gate fails open.** A gate that raises must not stop the breaker from
  protecting its caller, so a raising gate permits the write and warns once.

Verification techniques applied:
- Parametrized state transition: every emergency level, plus the non-enum and
  unreadable readings that must not be taken as evidence of a lockdown
- Negative assertion: the resolution path's cost (no packaging probe, no
  registry construction) and the void half of each gate site (the write call
  that must not happen, the event that must not be emitted, the log record
  that must not be written)
- Boundary analysis: the admission gate's OPEN-past-timeout / OPEN-inside-
  timeout / HALF_OPEN partition, and the reason label each produces
- Idempotency: the emergency manager is resolved once and reused
- Dependency interaction: exact call counts on the repository primitives
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.emergency import EmergencyManager
from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
)
from baldur.models.emergency import EmergencyLevel
from baldur.services.circuit_breaker.config import CircuitBreakerConfig, CircuitState
from baldur.services.circuit_breaker.freeze_mode import (
    FreezeModeManager,
    FreezeReason,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-gateway"
FAILURE_THRESHOLD = 5
RECOVERY_TIMEOUT = 60


# =============================================================================
# Helpers
# =============================================================================


def _emergency_stub(
    level: Any,
    *,
    activated_at: str | None = "2026-01-01T00:00:00+00:00",
) -> Mock:
    """A stub emergency manager reporting ``level``."""
    emergency = Mock(spec=EmergencyManager)
    emergency.get_current_level.return_value = level
    emergency.get_state.return_value = SimpleNamespace(activated_at=activated_at)
    return emergency


def _manager(level: Any, **kwargs: Any) -> FreezeModeManager:
    """A FreezeModeManager over a stub emergency manager reporting ``level``."""
    return FreezeModeManager(emergency_manager=_emergency_stub(level, **kwargs))


def _config(**overrides: Any) -> CircuitBreakerConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "failure_threshold": FAILURE_THRESHOLD,
        "success_threshold": 2,
        "minimum_calls": 1,
        "sliding_window_size": 100,
        "recovery_timeout": RECOVERY_TIMEOUT,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


def _state(
    state: str = CircuitBreakerStateEnum.CLOSED.value,
    **overrides: Any,
) -> CircuitBreakerStateData:
    row = CircuitBreakerStateData(service_name=SERVICE, state=state)
    return replace(row, **overrides) if overrides else row


def _service_over_mock_repo(
    state: CircuitBreakerStateData,
    **repo_overrides: Any,
) -> tuple[CircuitBreakerService, Mock]:
    """A service whose repository answers every read with ``state``.

    The transition primitives are stubbed to a *successful* write, so a gate
    that fails to hold shows up as a call that should not have happened
    rather than as an unrelated error.
    """
    repo = Mock(spec=CircuitBreakerStateRepository)
    repo.get_or_create.return_value = state
    repo.record_failure.return_value = state
    repo.update_state.return_value = True
    repo.get_all_states.return_value = [state]
    repo.trip_to_open.return_value = CircuitBreakerOpenAttempt(
        state=replace(state, state=CircuitBreakerStateEnum.OPEN.value),
        did_open=True,
    )
    repo.record_failure_with_open_check.return_value = CircuitBreakerOpenAttempt(
        state=replace(state, state=CircuitBreakerStateEnum.OPEN.value),
        did_open=True,
    )
    repo.record_success_with_close_check.return_value = CircuitBreakerCloseAttempt(
        state=replace(state, state=CircuitBreakerStateEnum.CLOSED.value),
        did_close=True,
    )
    repo.try_acquire_half_open_slot.return_value = (
        True,
        CircuitBreakerStateEnum.OPEN.value,
        CircuitBreakerStateEnum.HALF_OPEN.value,
    )
    for name, value in repo_overrides.items():
        getattr(repo, name).return_value = value

    service = CircuitBreakerService(config=_config(), repository=repo)
    service._emit_event = MagicMock(spec=service._emit_event)
    return service, repo


def _frozen(allowed: bool):
    """Patch the gate the service consults, at the name the service holds."""
    return patch(
        "baldur.services.circuit_breaker.service.should_allow_cb_state_change",
        return_value=allowed,
    )


def _emitted_types(service: CircuitBreakerService) -> list[Any]:
    return [call.args[0] for call in service._emit_event.call_args_list]


# =============================================================================
# Freeze Mode -- lockdown detection
# =============================================================================


class TestFreezeModeLockdownDetectionBehavior:
    """What counts as a lockdown, read through the enum's own ordering.

    The level is a ``(str, Enum)`` whose value is ``"level_3"``. Any numeric
    reading of it raises and, when swallowed, answers "not frozen" at every
    level -- which is the defect this class pins.
    """

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (EmergencyLevel.NORMAL, False),
            (EmergencyLevel.LEVEL_1, False),
            (EmergencyLevel.LEVEL_2, False),
            (EmergencyLevel.LEVEL_3, True),
        ],
        ids=["normal", "level_1", "level_2", "level_3"],
    )
    def test_lockdown_detection_follows_the_enum_severity_order(self, level, expected):
        """Only a level at or above LEVEL_3 freezes the breakers."""
        assert _manager(level).is_active() is expected

    def test_lockdown_detection_rejects_a_non_enum_level_as_evidence(self):
        """A raw ``"level_3"`` string is not the ordered enum, so it is not evidence."""
        assert _manager("level_3").is_active() is False

    def test_lockdown_detection_treats_a_missing_level_as_not_frozen(self):
        """``None`` from a manager that has not initialized does not freeze."""
        assert _manager(None).is_active() is False

    def test_lockdown_detection_fails_open_when_the_level_read_raises(self):
        """An unreadable level resolves to "not frozen" and says so once."""
        emergency = Mock(spec=EmergencyManager)
        emergency.get_current_level.side_effect = RuntimeError("backend down")
        manager = FreezeModeManager(emergency_manager=emergency)

        with capture_logs() as logs:
            active = manager.is_active()

        assert active is False
        assert [entry["event"] for entry in logs] == [
            "freeze_mode.lockdown_check_failed"
        ]

    def test_lockdown_detection_without_an_emergency_manager_is_not_frozen(self):
        """An install with no emergency manager can never be in lockdown."""
        manager = FreezeModeManager()

        with patch(
            "baldur.services.circuit_breaker.freeze_mode._pro_distribution_present",
            return_value=False,
        ):
            assert manager.is_active() is False


# =============================================================================
# Freeze Mode -- the cost of resolving the emergency manager
# =============================================================================


class TestFreezeModeGateCostBehavior:
    """The gate runs on the request path, so its short-circuits are contractual.

    Both of them exist to keep a per-request cost off installs that can never
    be frozen: a process without the paid distribution, and one whose
    emergency services have not registered.
    """

    @staticmethod
    def _registry_probe():
        """A registry slot recording how it was interrogated."""
        from baldur.factory.base import GenericProviderRegistry

        slot = Mock(spec=GenericProviderRegistry)
        slot.list_providers.return_value = []
        slot.safe_get.return_value = None
        return slot

    def test_gate_cost_skips_the_registry_entirely_without_the_distribution(self):
        """No paid distribution means the registry is never consulted."""
        slot = self._registry_probe()
        manager = FreezeModeManager()

        with (
            patch(
                "baldur.services.circuit_breaker.freeze_mode._pro_distribution_present",
                return_value=False,
            ),
            patch("baldur.factory.registry.ProviderRegistry.emergency_manager", slot),
        ):
            assert manager.emergency_manager is None

        slot.list_providers.assert_not_called()
        slot.safe_get.assert_not_called()

    def test_gate_cost_answers_an_empty_slot_without_constructing(self):
        """An empty slot is answered from the listing, not from a failed get.

        ``safe_get()`` constructs and catches an adapter-not-found exception
        per call; the listing answers the same question from a dict-keys copy.
        """
        slot = self._registry_probe()
        manager = FreezeModeManager()

        with (
            patch(
                "baldur.services.circuit_breaker.freeze_mode._pro_distribution_present",
                return_value=True,
            ),
            patch("baldur.factory.registry.ProviderRegistry.emergency_manager", slot),
        ):
            assert manager.emergency_manager is None

        slot.list_providers.assert_called_once()
        slot.safe_get.assert_not_called()

    def test_gate_cost_resolves_the_registry_once_and_reuses_it(self):
        """A resolved manager is cached: the second consultation is free."""
        resolved = _emergency_stub(EmergencyLevel.NORMAL)
        slot = self._registry_probe()
        slot.list_providers.return_value = ["emergency"]
        slot.safe_get.return_value = resolved
        manager = FreezeModeManager()

        with (
            patch(
                "baldur.services.circuit_breaker.freeze_mode._pro_distribution_present",
                return_value=True,
            ),
            patch("baldur.factory.registry.ProviderRegistry.emergency_manager", slot),
        ):
            first = manager.emergency_manager
            second = manager.emergency_manager

        assert first is resolved
        assert second is resolved
        slot.safe_get.assert_called_once()
        slot.list_providers.assert_called_once()

    def test_gate_cost_probes_the_packaging_question_once_per_process(self):
        """``find_spec`` is boot-only everywhere else; here it is cached."""
        from baldur.services.circuit_breaker import freeze_mode

        original = freeze_mode._pro_installed
        freeze_mode._pro_installed = None
        try:
            with patch(
                "baldur.utils.tier.is_pro_installed", return_value=False
            ) as probe:
                assert freeze_mode._pro_distribution_present() is False
                assert freeze_mode._pro_distribution_present() is False

            probe.assert_called_once()
        finally:
            freeze_mode._pro_installed = original


# =============================================================================
# Freeze Mode -- the reported state is derived, never stored
# =============================================================================


class TestFreezeModeDerivedStateBehavior:
    """``get_state()`` composes its answer from the emergency state."""

    def test_derived_state_reports_the_emergency_activation_stamp(self):
        """Active, with the lockdown's own reason and the emergency stamp."""
        state = _manager(EmergencyLevel.LEVEL_3).get_state()

        assert state.active is True
        assert state.reason == FreezeReason.LOCKDOWN_ENTRY
        assert state.activated_by == "system"
        assert state.activated_at == "2026-01-01T00:00:00+00:00"

    def test_derived_state_is_inactive_below_the_freeze_level(self):
        """Below LEVEL_3 the default (inactive) state comes back untouched."""
        state = _manager(EmergencyLevel.LEVEL_2).get_state()

        assert state.active is False
        assert state.activated_at is None
        assert state.activated_by == ""

    def test_derived_state_stays_active_when_the_stamp_is_absent(self):
        """A state object carrying no ``activated_at`` still reports the freeze.

        The stamp is a nicety; the freeze is the level. Reading the absent
        attribute must not downgrade the verdict.
        """
        emergency = Mock(spec=EmergencyManager)
        emergency.get_current_level.return_value = EmergencyLevel.LEVEL_3
        emergency.get_state.return_value = object()

        state = FreezeModeManager(emergency_manager=emergency).get_state()

        assert state.active is True
        assert state.activated_at is None

    def test_derived_state_stays_active_when_the_state_read_raises(self):
        """An unreadable emergency state costs the stamp, not the verdict."""
        emergency = Mock(spec=EmergencyManager)
        emergency.get_current_level.return_value = EmergencyLevel.LEVEL_3
        emergency.get_state.side_effect = RuntimeError("backend down")

        state = FreezeModeManager(emergency_manager=emergency).get_state()

        assert state.active is True
        assert state.activated_at is None


# =============================================================================
# Freeze Mode -- the gate itself
# =============================================================================


class TestFreezeModeGateBehavior:
    """``should_allow_state_change()`` -- the verdict every site consults."""

    def test_gate_blocks_an_automatic_change_and_names_the_target(self):
        """The denial reason carries the service and the state it refused."""
        allowed, reason = _manager(EmergencyLevel.LEVEL_3).should_allow_state_change(
            service_id=SERVICE,
            new_state="OPEN",
        )

        assert allowed is False
        assert SERVICE in reason
        assert "OPEN" in reason

    def test_gate_allows_an_automatic_change_with_no_lockdown(self):
        """Outside a lockdown the gate is transparent: allowed, empty reason."""
        allowed, reason = _manager(EmergencyLevel.NORMAL).should_allow_state_change(
            service_id=SERVICE,
            new_state="OPEN",
        )

        assert allowed is True
        assert reason == ""

    def test_gate_writes_no_log_record_when_it_blocks(self):
        """The gate itself is silent -- the caller owns the level.

        One site re-consults on every request to a frozen OPEN circuit past
        its recovery timeout. A log line here would be one per request.
        """
        manager = _manager(EmergencyLevel.LEVEL_3)

        with capture_logs() as logs:
            for _ in range(5):
                manager.should_allow_state_change(
                    service_id=SERVICE, new_state="HALF_OPEN"
                )

        assert logs == []


# =============================================================================
# The service-side gate wrapper
# =============================================================================


class TestAutoTransitionGateBehavior:
    """``_auto_transition_allowed()`` -- fail-open, and audible when it does."""

    def test_auto_transition_gate_forwards_the_freeze_verdict(self):
        """A blocking gate reaches the caller as False."""
        service, _ = _service_over_mock_repo(_state())

        with _frozen(False):
            assert service._auto_transition_allowed(SERVICE, "open") is False

    def test_auto_transition_gate_forwards_the_service_and_target_state(self):
        """The call is forwarded, not re-derived: both arguments are asserted."""
        service, _ = _service_over_mock_repo(_state())

        with patch(
            "baldur.services.circuit_breaker.service.should_allow_cb_state_change",
            return_value=True,
        ) as gate:
            service._auto_transition_allowed(SERVICE, "half_open")

        gate.assert_called_once_with(service_id=SERVICE, new_state="half_open")

    def test_auto_transition_gate_fails_open_and_warns_once_when_it_raises(self):
        """A gate that cannot answer must not stop the breaker protecting."""
        service, _ = _service_over_mock_repo(_state())

        with patch(
            "baldur.services.circuit_breaker.service.should_allow_cb_state_change",
            side_effect=RuntimeError("registry exploded"),
        ):
            with capture_logs() as logs:
                allowed = service._auto_transition_allowed(SERVICE, "open")

        assert allowed is True
        assert [entry["event"] for entry in logs] == [
            "circuit_breaker.freeze_check_failed"
        ]


# =============================================================================
# Sites A / B / D / F -- the request-path writes
# =============================================================================


class TestCBFreezeGateRequestPathBehavior:
    """Four automatic writes on the request path, each with its void half.

    Every test asserts the *write that did not happen*: the gate's whole
    contract is that the decision is withheld, and a return-value assertion
    cannot see that.
    """

    def test_closed_trip_is_withheld_while_frozen(self):
        """Site A: the failure is recorded, the trip is not taken."""
        service, repo = _service_over_mock_repo(_state(failure_count=FAILURE_THRESHOLD))

        with _frozen(False):
            service.record_failure(SERVICE)

        repo.record_failure.assert_called_once_with(SERVICE)
        repo.trip_to_open.assert_not_called()
        assert _emitted_types(service) == []

    def test_closed_trip_is_taken_when_not_frozen(self):
        """Site A, the other side: without a freeze the trip still happens."""
        service, repo = _service_over_mock_repo(_state(failure_count=FAILURE_THRESHOLD))

        with _frozen(True):
            service.record_failure(SERVICE)

        repo.trip_to_open.assert_called_once()

    def test_half_open_revert_is_withheld_while_frozen(self):
        """Site B: a HALF_OPEN failure does not revert the circuit to OPEN."""
        service, repo = _service_over_mock_repo(
            _state(CircuitBreakerStateEnum.HALF_OPEN.value)
        )

        with _frozen(False):
            service.record_failure(SERVICE)

        repo.record_failure_with_open_check.assert_not_called()
        assert _emitted_types(service) == []

    def test_half_open_revert_is_taken_when_not_frozen(self):
        """Site B, the other side."""
        service, repo = _service_over_mock_repo(
            _state(CircuitBreakerStateEnum.HALF_OPEN.value)
        )

        with _frozen(True):
            service.record_failure(SERVICE)

        repo.record_failure_with_open_check.assert_called_once_with(SERVICE)

    def test_half_open_close_is_withheld_while_frozen(self):
        """Site D: the success is not counted toward ``success_threshold``."""
        service, repo = _service_over_mock_repo(
            _state(CircuitBreakerStateEnum.HALF_OPEN.value)
        )

        with _frozen(False):
            service.record_success(SERVICE)

        repo.record_success_with_close_check.assert_not_called()
        assert _emitted_types(service) == []

    def test_half_open_close_is_taken_when_not_frozen(self):
        """Site D, the other side."""
        service, repo = _service_over_mock_repo(
            _state(CircuitBreakerStateEnum.HALF_OPEN.value)
        )

        with _frozen(True):
            service.record_success(SERVICE)

        repo.record_success_with_close_check.assert_called_once_with(
            SERVICE, service.config.success_threshold
        )

    def test_rate_limit_cascade_auto_open_is_withheld_while_frozen(self):
        """Site F: the 429 verdict borrows ``force_open``, so it is gated here."""
        service, _ = _service_over_mock_repo(_state())
        service.force_open = MagicMock(spec=service.force_open)

        with _frozen(False):
            result = self._drive_cascade(service)

        assert result is None
        service.force_open.assert_not_called()

    def test_rate_limit_cascade_auto_open_is_taken_when_not_frozen(self):
        """Site F, the other side: the cascade still auto-opens."""
        service, _ = _service_over_mock_repo(_state())
        service.force_open = MagicMock(spec=service.force_open)
        service.force_open.return_value = SimpleNamespace(success=False)

        with _frozen(True):
            self._drive_cascade(service)

        service.force_open.assert_called_once()

    def test_frozen_request_path_writes_no_warning_record(self):
        """The request-path sites log at DEBUG: a freeze is not per-call news.

        The sweep is the one site that warns, because it fires once a minute
        at most. Here N frozen calls must stay quiet.
        """
        service, _ = _service_over_mock_repo(_state(failure_count=FAILURE_THRESHOLD))

        with _frozen(False):
            with capture_logs() as logs:
                for _ in range(4):
                    service.record_failure(SERVICE)

        skipped = [
            entry
            for entry in logs
            if entry["event"] == "circuit_breaker.auto_transition_skipped"
        ]
        assert len(skipped) == 4
        assert {entry["log_level"] for entry in skipped} == {"debug"}
        assert [
            entry
            for entry in logs
            if entry["log_level"] in ("warning", "error", "critical")
        ] == []

    @staticmethod
    def _drive_cascade(service: CircuitBreakerService):
        """Feed the tracker enough 429s to satisfy the cascade condition."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            reset_rate_limit_tracker,
        )

        cfg = service.config
        # Each call records both a 429 and the request it answered, so one
        # loop satisfies the absolute floor and the minimum-sample term.
        calls = max(
            cfg.rate_limit_cascade_threshold, cfg.rate_limit_cascade_minimum_calls
        )

        reset_rate_limit_tracker()
        try:
            result = None
            for _ in range(calls):
                result = service.record_rate_limit_response(SERVICE)
            return result
        finally:
            reset_rate_limit_tracker()


# =============================================================================
# Site C -- the admission decision
# =============================================================================


class TestCBFreezeGateAdmissionBehavior:
    """The OPEN->HALF_OPEN combo is a transition, so admission rejects instead.

    The reason label is the discriminator an operator reads: with a shared
    ``open`` label a breaker the freeze is holding is indistinguishable from
    one still inside its recovery timeout.
    """

    @staticmethod
    def _open_row(*, age_seconds: float, **overrides: Any) -> CircuitBreakerStateData:
        return _state(
            CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now() - timedelta(seconds=age_seconds),
            **overrides,
        )

    @staticmethod
    def _decide(service: CircuitBreakerService):
        with patch("baldur.services.circuit_breaker.service.record_blocked") as blocked:
            decision = service.should_allow_with_state(SERVICE)
        reasons = [call.args[1] for call in blocked.call_args_list]
        return decision, reasons

    def test_admission_rejects_a_due_open_circuit_under_its_own_reason(self):
        """Past the recovery timeout and frozen: rejected, labelled ``frozen``."""
        service, repo = _service_over_mock_repo(
            self._open_row(age_seconds=RECOVERY_TIMEOUT + 10)
        )

        with _frozen(False):
            decision, reasons = self._decide(service)

        assert decision.allowed is False
        assert reasons == ["frozen"]
        repo.try_acquire_half_open_slot.assert_not_called()

    def test_admission_admits_a_due_open_circuit_when_not_frozen(self):
        """The same row without a freeze reaches the atomic trial acquire."""
        service, repo = _service_over_mock_repo(
            self._open_row(age_seconds=RECOVERY_TIMEOUT + 10)
        )

        with _frozen(True):
            decision, reasons = self._decide(service)

        assert decision.allowed is True
        assert reasons == []
        repo.try_acquire_half_open_slot.assert_called_once()

    def test_admission_labels_an_undue_open_circuit_open_not_frozen(self):
        """Inside the recovery timeout the freeze never gets a say.

        The recovery gate rejects first, so the label must stay ``open``:
        reporting ``frozen`` here would name the freeze for breakers it is
        not holding.
        """
        service, _ = _service_over_mock_repo(
            self._open_row(age_seconds=RECOVERY_TIMEOUT - 10)
        )

        with _frozen(False):
            decision, reasons = self._decide(service)

        assert decision.allowed is False
        assert reasons == ["open"]

    def test_admission_rejects_a_half_open_row_while_frozen(self):
        """A local HALF_OPEN row is not evidence that the shared row is.

        The acquire is L2-authoritative and the layered L1 is never refreshed
        for rows another worker changed, so from a stale HALF_OPEN row the
        primitive performs the very OPEN->HALF_OPEN write the freeze exists to
        withhold. The gate therefore covers every non-CLOSED entry to it.
        """
        service, repo = _service_over_mock_repo(
            _state(CircuitBreakerStateEnum.HALF_OPEN.value),
            try_acquire_half_open_slot=(
                True,
                CircuitBreakerStateEnum.HALF_OPEN.value,
                CircuitBreakerStateEnum.HALF_OPEN.value,
            ),
        )

        with _frozen(False):
            decision, reasons = self._decide(service)

        assert decision.allowed is False
        assert reasons == ["frozen"]
        repo.try_acquire_half_open_slot.assert_not_called()

    def test_admission_withholds_the_transition_a_stale_half_open_row_hides(self):
        """The regression: L1 says HALF_OPEN, the shared store still says OPEN.

        The primitive would report the OPEN->HALF_OPEN combo it just wrote --
        an automatic transition performed during LOCKDOWN, with its audit
        record, its ``CIRCUIT_BREAKER_HALF_OPENED`` event and an admitted
        trial call. Nothing about the local row distinguishes this from a
        circuit that really is HALF_OPEN, which is why the gate cannot key on
        the local state.
        """
        service, repo = _service_over_mock_repo(
            _state(CircuitBreakerStateEnum.HALF_OPEN.value),
            try_acquire_half_open_slot=(
                True,
                CircuitBreakerStateEnum.OPEN.value,
                CircuitBreakerStateEnum.HALF_OPEN.value,
            ),
        )

        with _frozen(False):
            decision, reasons = self._decide(service)

        assert decision.allowed is False
        assert reasons == ["frozen"]
        repo.try_acquire_half_open_slot.assert_not_called()

    def test_admission_lets_a_half_open_circuit_acquire_a_slot_when_not_frozen(self):
        """Without a freeze the trial acquire is reached exactly as before."""
        service, repo = _service_over_mock_repo(
            _state(CircuitBreakerStateEnum.HALF_OPEN.value),
            try_acquire_half_open_slot=(
                True,
                CircuitBreakerStateEnum.HALF_OPEN.value,
                CircuitBreakerStateEnum.HALF_OPEN.value,
            ),
        )

        with _frozen(True):
            decision, reasons = self._decide(service)

        assert decision.allowed is True
        assert reasons == []
        repo.try_acquire_half_open_slot.assert_called_once()

    def test_admission_keeps_manual_pin_precedence_over_the_freeze_label(self):
        """An operator's block is reported as its own rejection, not the freeze."""
        service, _ = _service_over_mock_repo(
            self._open_row(
                age_seconds=RECOVERY_TIMEOUT + 10,
                manually_controlled=True,
                manual_override_expires_at=utc_now() + timedelta(minutes=5),
            )
        )

        with _frozen(False):
            decision, reasons = self._decide(service)

        assert decision.allowed is False
        assert reasons == ["open"]


# =============================================================================
# Site E -- the periodic recovery sweep
# =============================================================================


class TestCBFreezeGateSweepBehavior:
    """One gate check per sweep, and one operator-facing line when it holds."""

    @staticmethod
    def _open_rows(*ages: float) -> list[CircuitBreakerStateData]:
        return [
            CircuitBreakerStateData(
                service_name=f"svc-{index}",
                state=CircuitBreakerStateEnum.OPEN.value,
                opened_at=utc_now() - timedelta(seconds=age),
            )
            for index, age in enumerate(ages)
        ]

    def test_sweep_writes_nothing_while_frozen(self):
        """No row is transitioned, and the result says why."""
        service, repo = _service_over_mock_repo(_state())
        repo.get_all_states.return_value = self._open_rows(RECOVERY_TIMEOUT + 10)

        with _frozen(False):
            result = service.check_recovery_transitions()

        repo.update_state.assert_not_called()
        assert result == {"success": True, "message": "frozen", "count": 0}

    def test_sweep_transitions_due_rows_when_not_frozen(self):
        """The other side: the sweep still moves due rows to HALF_OPEN."""
        service, repo = _service_over_mock_repo(_state())
        repo.get_all_states.return_value = self._open_rows(RECOVERY_TIMEOUT + 10)

        with _frozen(True):
            result = service.check_recovery_transitions()

        repo.update_state.assert_called_once_with(
            service_name="svc-0",
            state=CircuitState.HALF_OPEN,
            success_count=0,
        )
        assert result["count"] == 1

    def test_sweep_reports_the_held_count_once_per_sweep(self):
        """Exactly one WARNING, carrying how many breakers the freeze holds."""
        service, repo = _service_over_mock_repo(_state())
        repo.get_all_states.return_value = self._open_rows(
            RECOVERY_TIMEOUT + 10, RECOVERY_TIMEOUT + 30
        )

        with _frozen(False):
            with capture_logs() as logs:
                service.check_recovery_transitions()

        blocked = [
            entry
            for entry in logs
            if entry["event"] == "circuit_breaker.recovery_sweep_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["held_count"] == 2
        assert blocked[0]["log_level"] == "warning"

    def test_sweep_held_count_counts_only_rows_past_their_timeout(self):
        """A row still inside its recovery window is not one the freeze holds."""
        service, repo = _service_over_mock_repo(_state())
        repo.get_all_states.return_value = self._open_rows(
            RECOVERY_TIMEOUT + 10, RECOVERY_TIMEOUT - 10
        )

        with _frozen(False):
            with capture_logs() as logs:
                service.check_recovery_transitions()

        (blocked,) = [
            entry
            for entry in logs
            if entry["event"] == "circuit_breaker.recovery_sweep_blocked"
        ]
        assert blocked["held_count"] == 1


# =============================================================================
# Manual paths -- never gated
# =============================================================================


class TestCBFreezeGateManualPathsBehavior:
    """Operator intent outranks the freeze, by design.

    Each of these would be a support incident if it were gated: the freeze
    exists precisely so an operator can intervene while automation is held.
    """

    @staticmethod
    def _manual_service() -> tuple[CircuitBreakerService, Mock]:
        service, repo = _service_over_mock_repo(_state())
        repo.atomic_force_open.return_value = (
            True,
            CircuitBreakerStateEnum.CLOSED.value,
            CircuitBreakerStateEnum.OPEN.value,
        )
        repo.atomic_force_close.return_value = (
            True,
            CircuitBreakerStateEnum.OPEN.value,
            CircuitBreakerStateEnum.CLOSED.value,
        )
        repo.atomic_reset.return_value = (
            True,
            CircuitBreakerStateEnum.OPEN.value,
            CircuitBreakerStateEnum.CLOSED.value,
        )
        return service, repo

    def test_force_open_succeeds_while_frozen(self):
        """The operator's own force is the intervention the freeze invites."""
        service, repo = self._manual_service()

        with _frozen(False):
            result = service.force_open(service_name=SERVICE, reason="operator")

        assert result.success is True
        repo.atomic_force_open.assert_called_once()

    def test_force_close_succeeds_while_frozen(self):
        """Closing a breaker by hand is likewise ungated."""
        service, repo = self._manual_service()

        with _frozen(False):
            result = service.force_close(service_name=SERVICE, reason="operator")

        assert result.success is True
        repo.atomic_force_close.assert_called_once()

    def test_reset_succeeds_while_frozen(self):
        """The reset path clears an override; it is not an automatic decision."""
        service, _ = self._manual_service()

        with _frozen(False):
            result = service.reset(service_name=SERVICE)

        assert result.success is True
