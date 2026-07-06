"""
CB CLOSED → conditional_replay_on_circuit_close single-fire dispatch tests
(495 D7).

Verifies the contract established by ``495``:

    The EventBus handler ``_on_circuit_breaker_closed`` is the single
    dispatch path from a CB CLOSED transition to
    ``conditional_replay_on_circuit_close.delay()``. The handler is
    gated by two enforcement points — ``INTEGRITY_FAILED_KEY`` on the
    event data and ``on_recovery_enabled`` from runtime config — and the
    redundant call sites that previously caused dual-fire have been
    removed (D2/D3/D4).

Test classes:
    - TestCBClosedDispatchSingleFireBehavior: exact-args, single-fire
      dispatch per CB CLOSED event (parametrized over trigger source).
    - TestCBClosedDispatchGateBehavior: gate enforcement (on-recovery
      disabled, integrity gate failed). Closed gates → zero dispatches.
    - TestCBClosedTriggerReplayGateBehavior: 507 D8 — handler honours
      the ``trigger_replay`` operator-intent flag on event data and
      falls back to default-True when the key is absent.
    - TestCBClosedDispatchSingleFireContract: D2/D3/D4 deletions —
      the dead delegation surface no longer exists on the framework
      classes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Sentinel for "trigger_replay key not present on event.data" — distinct
# from any plausible value an operator could pass (None, True, False).
_TRIGGER_REPLAY_MISSING = object()

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def make_event():
    """Factory for a ``BaldurEvent`` representing a CB CLOSED transition.

    Mirrors the payload shape emitted by
    ``CircuitBreakerService.record_success`` (auto) and
    ``ManualControlMixin._on_force_close_success`` (manual).
    """

    def _make(
        service_name: str = "payment-api",
        trigger: str = "auto",
        integrity_failed: bool = False,
        trigger_replay=_TRIGGER_REPLAY_MISSING,
    ):
        from baldur.services.event_bus import BaldurEvent, EventType
        from baldur.services.event_bus.integrity_gate import INTEGRITY_FAILED_KEY

        previous_state = "half_open" if trigger == "auto" else "open"
        data: dict = {
            "service_name": service_name,
            "previous_state": previous_state,
            "trigger": trigger,
        }
        if integrity_failed:
            data[INTEGRITY_FAILED_KEY] = True
        if trigger_replay is not _TRIGGER_REPLAY_MISSING:
            data["trigger_replay"] = trigger_replay
        return BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data=data,
            source="circuit_breaker_service",
        )

    return _make


@pytest.fixture
def patch_runtime_config():
    """Context manager factory that patches RuntimeConfig's replay_automation config.

    Returns a patcher; callers use it as ``with patch_runtime_config(...):``
    to make ``manager._get_config("replay_automation")`` return the
    supplied gate state for the duration of the test body.
    """

    def _patch(on_recovery_enabled: bool = True, on_recovery_max_items: int = 50):
        manager = MagicMock()
        _config = {
            "on_recovery_enabled": on_recovery_enabled,
            "on_recovery_max_items": on_recovery_max_items,
        }
        # Dispatch reads the public get_config() accessor (never _get_config).
        manager.get_config.return_value = _config
        manager._get_config.return_value = _config
        return patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        )

    return _patch


@pytest.fixture
def patch_task_delay():
    """Context manager factory that replaces the celery task with a Mock.

    Patches the symbol the handler imports
    (``baldur.adapters.celery.tasks.conditional_replay_on_circuit_close``)
    so calls to ``.delay(...)`` go to a ``MagicMock`` whose call args
    can be inspected.
    """

    def _patch():
        delay_mock = MagicMock()
        task_mock = MagicMock()
        task_mock.delay = delay_mock
        return (
            delay_mock,
            patch(
                "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close",
                new=task_mock,
            ),
        )

    return _patch


# =============================================================================
# Single-fire dispatch
# =============================================================================


class TestCBClosedDispatchSingleFireBehavior:
    """D7: every CB CLOSED transition results in exactly one
    ``conditional_replay_on_circuit_close.delay()`` invocation with
    the expected ``service_name``/``max_items`` kwargs.
    """

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @pytest.mark.parametrize("trigger", ["auto", "manual"])
    def test_handler_dispatches_exactly_once_with_expected_kwargs(
        self, make_event, patch_runtime_config, patch_task_delay, trigger
    ):
        # Given
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        event = make_event(service_name="payment-api", trigger=trigger)
        delay_mock, task_patcher = patch_task_delay()

        # When
        with patch_runtime_config(on_recovery_enabled=True, on_recovery_max_items=50):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        # Then
        delay_mock.assert_called_once_with(
            service_name="payment-api",
            max_items=50,
        )

    @pytest.mark.parametrize("trigger", ["auto", "manual"])
    def test_handler_propagates_runtime_config_max_items(
        self, make_event, patch_runtime_config, patch_task_delay, trigger
    ):
        """The ``max_items`` kwarg matches ``on_recovery_max_items`` from
        runtime config — not the handler's hard-coded default.
        """
        # Given
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        event = make_event(service_name="orders-api", trigger=trigger)
        delay_mock, task_patcher = patch_task_delay()

        # When
        with patch_runtime_config(on_recovery_enabled=True, on_recovery_max_items=17):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        # Then
        delay_mock.assert_called_once_with(
            service_name="orders-api",
            max_items=17,
        )


# =============================================================================
# Gate enforcement
# =============================================================================


class TestCBClosedDispatchGateBehavior:
    """D7: parametrized gate enforcement — the handler suppresses
    dispatch when either gate is closed and dispatches exactly once
    when both gates are open.

    Gates (in handler order):
        1. ``event.data[INTEGRITY_FAILED_KEY]`` — set by
           ``on_circuit_breaker_closed_integrity_gate`` (CRITICAL
           priority, runs first).
        2. ``runtime_config.replay_automation.on_recovery_enabled`` —
           operator-controlled feature flag.

    Before D3, ``_trigger_conditional_replay()`` bypassed both gates;
    after D3 they are genuinely enforced.
    """

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @pytest.mark.parametrize("trigger", ["auto", "manual"])
    def test_dispatch_suppressed_when_on_recovery_disabled(
        self, make_event, patch_runtime_config, patch_task_delay, trigger
    ):
        # Given
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        event = make_event(trigger=trigger)
        delay_mock, task_patcher = patch_task_delay()

        # When
        with patch_runtime_config(on_recovery_enabled=False):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        # Then
        assert delay_mock.call_count == 0

    @pytest.mark.parametrize("trigger", ["auto", "manual"])
    def test_dispatch_suppressed_when_integrity_gate_failed(
        self, make_event, patch_runtime_config, patch_task_delay, trigger
    ):
        # Given
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        event = make_event(trigger=trigger, integrity_failed=True)
        delay_mock, task_patcher = patch_task_delay()

        # When — runtime config on-recovery is permissive; the integrity
        # gate should short-circuit before reaching the dispatch.
        with patch_runtime_config(on_recovery_enabled=True):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        # Then
        assert delay_mock.call_count == 0

    @pytest.mark.parametrize(
        ("on_recovery_enabled", "integrity_failed", "expected_calls"),
        [
            (True, False, 1),
            (False, False, 0),
            (True, True, 0),
            (False, True, 0),
        ],
    )
    def test_dispatch_only_when_both_gates_open(
        self,
        make_event,
        patch_runtime_config,
        patch_task_delay,
        on_recovery_enabled,
        integrity_failed,
        expected_calls,
    ):
        # Given
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        event = make_event(trigger="auto", integrity_failed=integrity_failed)
        delay_mock, task_patcher = patch_task_delay()

        # When
        with patch_runtime_config(on_recovery_enabled=on_recovery_enabled):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        # Then
        assert delay_mock.call_count == expected_calls


# =============================================================================
# trigger_replay operator-intent gate (507 D8)
# =============================================================================


class TestCBClosedTriggerReplayGateBehavior:
    """507 D8: ``_on_circuit_breaker_closed`` honours the
    ``trigger_replay`` flag on event data — operator-requested
    suppression (e.g., ``force_close(trigger_replay=False)``, Chaos
    rollback, Django admin "without replay" button) short-circuits
    dispatch before the ``on_recovery_enabled`` check.

    Assertions are grouped into two intent buckets:

    - **Normal-spec** (``trigger_replay`` explicitly True or False):
      verifies the operator-intent contract restored by 507. After
      D6/D7 land, every CB CLOSED emit site in production populates
      this key, so this is the only path exercised by real emits.
    - **Defensive-fallback** (``trigger_replay`` key absent): regression
      tests the handler's default-True path described in D1 — the
      safety net that protects future emit sites which forget the key.
    """

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    # ----- Normal-spec: explicit True / False from operator emit ----------

    @pytest.mark.parametrize("trigger", ["auto", "manual", "manual_reset"])
    def test_explicit_true_dispatches_once(
        self, make_event, patch_runtime_config, patch_task_delay, trigger
    ):
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        event = make_event(
            service_name="payment-api",
            trigger=trigger,
            trigger_replay=True,
        )
        delay_mock, task_patcher = patch_task_delay()

        with patch_runtime_config(on_recovery_enabled=True, on_recovery_max_items=50):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        delay_mock.assert_called_once_with(
            service_name="payment-api",
            max_items=50,
        )

    @pytest.mark.parametrize("trigger", ["auto", "manual", "manual_reset"])
    def test_explicit_false_suppresses_dispatch(
        self, make_event, patch_runtime_config, patch_task_delay, trigger
    ):
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        event = make_event(
            service_name="payment-api",
            trigger=trigger,
            trigger_replay=False,
        )
        delay_mock, task_patcher = patch_task_delay()

        with patch_runtime_config(on_recovery_enabled=True, on_recovery_max_items=50):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        assert delay_mock.call_count == 0

    # ----- Defensive-fallback: missing key defaults to dispatch ----------

    @pytest.mark.parametrize("trigger", ["auto", "manual", "manual_reset"])
    def test_missing_key_defaults_to_dispatch(
        self, make_event, patch_runtime_config, patch_task_delay, trigger
    ):
        """Regression: a future emit site that forgets to set
        ``trigger_replay`` must NOT silently suppress replay. The
        handler defaults to True so the always-replay contract for
        auto-recovery survives unchanged.
        """
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        # No trigger_replay kwarg → key absent on event.data
        event = make_event(service_name="payment-api", trigger=trigger)
        delay_mock, task_patcher = patch_task_delay()

        with patch_runtime_config(on_recovery_enabled=True, on_recovery_max_items=50):
            with task_patcher:
                _on_circuit_breaker_closed(event)

        delay_mock.assert_called_once_with(
            service_name="payment-api",
            max_items=50,
        )


# =============================================================================
# Structural contract: deleted dispatch surface
# =============================================================================


class TestCBClosedDispatchSingleFireContract:
    """495 D2/D3/D4: the redundant dispatch surface that caused dual-fire
    has been removed. Verified structurally so the deletion cannot be
    silently reintroduced.
    """

    def test_manual_control_no_longer_exposes_trigger_conditional_replay(self):
        """D3: ``ManualControlMixin._trigger_conditional_replay`` deleted.

        Both call sites in ``service.py`` (auto-recovery) and
        ``manual_control.py`` (manual force_close) are gone with it;
        the EventBus emit alone now drives replay.
        """
        from baldur.services.circuit_breaker import manual_control

        assert not hasattr(
            manual_control.ManualControlMixin, "_trigger_conditional_replay"
        )

    def test_cb_recorder_no_longer_exposes_trigger_conditional_replay(self):
        """D4: ``CircuitBreakerRecorder.trigger_conditional_replay`` deleted.

        The method had zero callers across ``src/`` and ``tests/``.
        ``record_failure``/``record_success`` are still in use and
        remain on the class.
        """
        from baldur.adapters.celery.integrations.cb_recorder import (
            CircuitBreakerRecorder,
        )

        assert not hasattr(CircuitBreakerRecorder, "trigger_conditional_replay")
        # Defensive: the two methods that ARE in use must remain.
        assert hasattr(CircuitBreakerRecorder, "record_failure")
        assert hasattr(CircuitBreakerRecorder, "record_success")

    def test_recovery_coordinator_no_longer_exposes_request_recovery_surface(
        self,
    ):
        """507 D3+D5: the whole ``request_recovery`` orchestration entry
        point on ``RecoveryCoordinator`` was orphaned after doc 495 D3
        and is deleted in this revision. The asserted surface here is
        the public method only — its private strategy helpers
        (``_request_force_close``, ``_request_full_saga``) and the
        ``RecoveryRequestResult`` dataclass are removed together but
        are not part of the structural contract that must survive in
        this test.
        """
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.coordination.recovery_coordinator.coordinator import (
            RecoveryCoordinator,
        )

        assert not hasattr(RecoveryCoordinator, "request_recovery")
