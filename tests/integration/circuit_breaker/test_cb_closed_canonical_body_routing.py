"""
CB CLOSED → canonical-body routing integration test (495 D7).

Cross-module integration of three pieces that the unit test cannot
exercise together:

    1. ``baldur.adapters.celery.tasks.__init__`` re-export — must
       resolve to ``baldur.celery_tasks.dlq_tasks.conditional_replay_on_circuit_close``
       (the canonical body), NOT the deleted placeholder in
       ``circuit_breaker_tasks.py``. The regression we are guarding
       against (G3 in 495) is last-import-wins on a duplicate
       ``@shared_task`` name — by definition not reproducible with a
       mock at the dispatch boundary.
    2. ``BaldurEventBus.publish`` dispatch — a real (in-memory) bus
       with ``_on_circuit_breaker_closed`` subscribed converts one
       published ``CIRCUIT_BREAKER_CLOSED`` event into one
       ``.delay()`` call.
    3. The canonical task body — when invoked, calls
       ``get_replay_service().replay_on_circuit_close(...)`` with the
       correct kwargs. The placeholder body did not.

Mock-based — no infra. The test instantiates ``BaldurEventBus``
directly to avoid singleton coupling with other test modules.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Identity: adapter re-export points to canonical body
# =============================================================================


class TestCanonicalTaskReExportIdentity:
    """495 D5: ``baldur.adapters.celery.tasks.conditional_replay_on_circuit_close``
    must be the same object as
    ``baldur.celery_tasks.dlq_tasks.conditional_replay_on_circuit_close``.

    Before 495, the adapter re-export resolved through
    ``circuit_breaker_tasks.py`` (placeholder body, no replay). After
    495, it resolves through ``dlq_tasks.py`` (canonical body, calls
    ``ReplayService.replay_on_circuit_close``).
    """

    def test_adapter_reexport_resolves_to_dlq_tasks_body(self):
        from baldur.adapters.celery.tasks import (
            conditional_replay_on_circuit_close as via_adapter,
        )
        from baldur.celery_tasks.dlq_tasks import (
            conditional_replay_on_circuit_close as canonical,
        )

        assert via_adapter is canonical

    def test_canonical_task_registered_under_celery_tasks_namespace(self):
        """The canonical task is registered under the
        ``baldur.celery_tasks.*`` namespace (not the wrong
        ``baldur.tasks.*`` prefix that the deleted dispatch paths
        used). Locks in 495 D6: no backward-compat alias was added.
        """
        from baldur.celery_tasks.dlq_tasks import conditional_replay_on_circuit_close

        assert (
            conditional_replay_on_circuit_close.name
            == "baldur.celery_tasks.conditional_replay_on_circuit_close"
        )


# =============================================================================
# End-to-end: publish → handler → canonical task body
# =============================================================================


@pytest.fixture
def fresh_event_bus():
    """An isolated ``BaldurEventBus`` instance with no default handlers.

    Bypasses the singleton so this test does not interfere with
    handlers registered by other modules during the test session.
    """
    from baldur.services.event_bus.bus.event_bus import BaldurEventBus

    bus = BaldurEventBus()
    try:
        yield bus
    finally:
        bus.reset()


@pytest.fixture
def make_cb_closed_event():
    """Factory mirroring ``CircuitBreakerService._emit_event`` payload."""

    def _make(service_name: str = "payment-api", trigger: str = "auto"):
        from baldur.services.event_bus import BaldurEvent, EventType

        return BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={
                "service_name": service_name,
                "previous_state": "half_open" if trigger == "auto" else "open",
                "trigger": trigger,
            },
            source="circuit_breaker_service",
        )

    return _make


class TestCBClosedRoutesToCanonicalBody:
    """End-to-end: a CIRCUIT_BREAKER_CLOSED publish into a real
    EventBus reaches the canonical ``dlq_tasks`` body, not the deleted
    placeholder. ``get_replay_service`` is mocked to isolate the
    test from the real DB-backed replay path.
    """

    def test_publish_invokes_canonical_body_with_expected_kwargs(
        self, fresh_event_bus, make_cb_closed_event
    ):
        pytest.importorskip("baldur_pro")
        # Given — a real bus with ONLY _on_circuit_breaker_closed
        # subscribed (no integrity_gate so the event reaches dispatch).
        from baldur.services.event_bus import EventType
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed,
        )

        fresh_event_bus.subscribe(
            EventType.CIRCUIT_BREAKER_CLOSED,
            _on_circuit_breaker_closed,
        )

        # Runtime config gate open with non-default max_items so we
        # can prove propagation through the full pipeline. The dispatch reads
        # the public get_config() accessor (never the private _get_config).
        runtime_manager = MagicMock()
        _config = {
            "on_recovery_enabled": True,
            "on_recovery_max_items": 11,
        }
        runtime_manager.get_config.return_value = _config
        runtime_manager._get_config.return_value = _config

        # Replace .delay() with a side-effect that synchronously
        # invokes the canonical task body. This forces the body to
        # execute in-process so we can verify its behavior.
        from baldur.adapters.celery.tasks import conditional_replay_on_circuit_close

        invoked_body_kwargs = {}

        def run_body_inline(**kwargs):
            invoked_body_kwargs.update(kwargs)
            return conditional_replay_on_circuit_close.run(**kwargs)

        # Mock the replay service so the canonical body's
        # `service.replay_on_circuit_close(...)` call is observable.
        mock_replay_service = MagicMock()
        mock_replay_service.replay_on_circuit_close.return_value = MagicMock(
            governance_blocked=False,
            total=0,
            success_count=0,
            failed_count=0,
        )

        event = make_cb_closed_event(service_name="payment-api", trigger="auto")

        # When
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=runtime_manager,
        ):
            with patch.object(
                conditional_replay_on_circuit_close,
                "delay",
                side_effect=run_body_inline,
            ):
                with patch(
                    "baldur.services.get_replay_service",
                    return_value=mock_replay_service,
                ):
                    fresh_event_bus.publish(event)

        # Then — the canonical body was reached (placeholder did not
        # call get_replay_service) with the expected kwargs.
        assert invoked_body_kwargs == {
            "service_name": "payment-api",
            "max_items": 11,
        }
        mock_replay_service.replay_on_circuit_close.assert_called_once_with(
            service_name="payment-api",
            max_items=11,
        )

    def test_publish_with_no_subscribers_is_noop(
        self, fresh_event_bus, make_cb_closed_event
    ):
        """Sanity: an isolated bus with no handlers does not raise
        or dispatch. Establishes the test isolation baseline.
        """
        event = make_cb_closed_event()
        handlers_called = fresh_event_bus.publish(event)
        assert handlers_called == 0
