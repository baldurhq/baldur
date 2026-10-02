"""676 — On-recovery dispatch visibility.

Target: ``baldur.services.event_bus.bus._cb_handlers._on_circuit_breaker_closed``
through the one recovery dispatch path
(``baldur.services.replay_service.recovery.dispatch_recovery_sweep``)

    - armed-aware skip semantics (D3): a CB auto-CLOSE either dispatches, skips
      (disabled), or WARNs (armed-but-undeliverable / error) — never goes
      silently inert. Each outcome records a dispatch counter; an attempt also
      lands in the arming ledger the operator surfaces read as
      ``last_dispatch``. The dispatch path is slot-blind (710): it never
      consults the PRO ``dlq_service`` slot — auto-replay on CB recovery is OSS.
    - D2/D5 config precedence: on the RuntimeConfig-absent path the dispatch
      resolves ``on_recovery_max_items`` from ``ReplayAutomationSettings`` (env-
      honoring), not a hardcoded literal.

Provider slots are stubbed in-test by patching ``safe_get`` on the shared
``ProviderRegistry`` slot instances — no ``baldur_pro`` import (G19/G20/G21
safe, PRO-absent safe).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.factory.registry import ProviderRegistry
from baldur.metrics.recorders.dlq import DLQMetricRecorder
from baldur.services.event_bus.bus._cb_handlers import _on_circuit_breaker_closed
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.arming import (
    get_dispatch_ledger,
    reset_dispatch_ledger,
)
from baldur.services.replay_service.recovery import _record_dispatch_outcome
from baldur.settings.replay_automation import (
    ReplayAutomationSettings,
    get_replay_automation_settings,
)

_TASK_PATH = "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close"
_CELERY_TASKS_MODULE = "baldur.adapters.celery.tasks"
_RECORD_PATH = "baldur.services.replay_service.recovery._record_dispatch_outcome"

# What a CLOSED event's dispatch adds to the chain's first pass: the provenance
# and the escalation of the CLOSED-transition lane, not operator-requested.
_CLOSED_EVENT_CHAIN = {
    "trigger": "auto_replay_circuit_close",
    "escalate_failures": True,
    "operator_requested": False,
}


# =============================================================================
# Fixtures / helpers
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_dispatch_markers():
    """Reset the process-global arming ledger the dispatch path writes into
    around every test (xdist-safe isolation)."""
    reset_dispatch_ledger()
    yield
    reset_dispatch_ledger()


def _make_event(service_name: str = "payment-api", trigger: str = "auto"):
    from baldur.services.event_bus import BaldurEvent, EventType

    return BaldurEvent(
        event_type=EventType.CIRCUIT_BREAKER_CLOSED,
        data={
            "service_name": service_name,
            "previous_state": "half_open",
            "trigger": trigger,
        },
        source="circuit_breaker_service",
    )


def _events(cap_logs: list[dict], name: str) -> list[dict]:
    return [e for e in cap_logs if e.get("event") == name]


def _patch_config(config):
    """Patch the resolved RuntimeConfig block the dispatch reads."""
    return patch(
        "baldur.services.replay_service.recovery._replay_automation_config",
        return_value=config or {},
    )


def _make_task_mock():
    task_mock = MagicMock()
    task_mock.delay = MagicMock()
    return task_mock


def _patch_parked(count):
    """Patch what the replay service counts as parked under the closing name."""
    service = MagicMock(spec=ReplayService)
    service.parked_count_for_recovery.return_value = count
    return patch(
        "baldur.services.replay_service.get_replay_service", return_value=service
    )


# =============================================================================
# D3 — armed-aware dispatch outcomes
# =============================================================================


class TestOnRecoveryDispatchVisibilityBehavior:
    """Each CB-close dispatch evaluation resolves to exactly one visible
    outcome (dispatch / skip / WARNING) plus its dispatch-counter label.
    """

    def test_dispatch_is_slot_blind_to_the_pro_dlq_service(self):
        # 710: auto-replay on CB recovery is OSS — the dispatch path must
        # never consult the PRO ``dlq_service`` slot. Poison the slot so a
        # reintroduced consultation fails loud (as the propagated
        # AssertionError or as outcome "error"), never as a silent skip.
        event = _make_event()
        task_mock = _make_task_mock()

        with (
            patch.object(
                ProviderRegistry.dlq_service,
                "safe_get",
                side_effect=AssertionError(
                    "dispatch path must not consult the PRO dlq_service slot"
                ),
            ),
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(_RECORD_PATH) as record,
            patch(_TASK_PATH, new=task_mock),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: the slot was never read and the dispatch proceeded on OSS.
        task_mock.delay.assert_called_once()
        record.assert_called_once_with("dispatched", service_name="payment-api")
        # Negatives: the old pro_absent categorization never occurs.
        assert _events(cap, "event_handler.replay_dispatch_skipped") == []
        assert all(
            call.args[0] != "skipped_pro_absent" for call in record.call_args_list
        )

    def test_disabled_on_recovery_logs_info_and_does_not_dispatch(self):
        # Given: on-recovery replay disabled in config.
        event = _make_event()

        with (
            _patch_config({"on_recovery_enabled": False}),
            patch(_RECORD_PATH) as record,
            patch(_TASK_PATH, new=_make_task_mock()) as task_mock,
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: INFO (existing behavior), no dispatch, counter=skipped_disabled.
        infos = _events(cap, "event_handler.circuit_breaker_closed_track")
        assert len(infos) == 1
        assert infos[0]["log_level"] == "info"
        assert task_mock.delay.call_count == 0
        record.assert_called_once_with("skipped_disabled", service_name="payment-api")

    def test_armed_dispatches_task_with_service_and_max_items(self):
        # Given: enabled + a configured max_items.
        event = _make_event(service_name="orders-api")
        task_mock = _make_task_mock()

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 42}),
            patch(_RECORD_PATH) as record,
            patch(_TASK_PATH, new=task_mock),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: exactly-once dispatch with the resolved kwargs, counter=dispatched.
        task_mock.delay.assert_called_once_with(
            service_name="orders-api",
            max_items=42,
            max_continuations=100,
            **_CLOSED_EVENT_CHAIN,
        )
        assert len(_events(cap, "event_handler.circuit_breaker_closed_triggered")) == 1
        record.assert_called_once_with("dispatched", service_name="orders-api")

    def test_armed_but_celery_missing_warns_with_remediation(self):
        # Given: armed (enabled), one entry parked under the name, but the
        # Celery task import fails.
        # A None entry in sys.modules makes ``import <module>`` raise ImportError.
        event = _make_event()

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(_RECORD_PATH) as record,
            _patch_parked(1),
            patch.dict("sys.modules", {_CELERY_TASKS_MODULE: None}),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: WARNING (not a silent DEBUG skip) naming the remediation, and
        # the counter records celery_missing.
        blocked = _events(cap, "event_handler.replay_dispatch_blocked")
        assert len(blocked) == 1
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["reason"] == "celery_missing"
        assert blocked[0]["queue"] == "dlq_processing"
        assert "remediation" in blocked[0]
        record.assert_called_once_with("celery_missing", service_name="payment-api")

    def test_dispatch_broker_error_takes_error_path(self):
        # Given: the task imports fine but ``.delay`` raises a non-ImportError.
        event = _make_event()
        task_mock = _make_task_mock()
        task_mock.delay.side_effect = RuntimeError("broker down")

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(_RECORD_PATH) as record,
            patch(_TASK_PATH, new=task_mock),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: the existing ERROR path is kept (handler does not crash), and
        # the counter records error.
        assert len(_events(cap, "event_handler.trigger_track_replay_failed")) == 1
        record.assert_called_once_with(
            "error", service_name="payment-api", error="broker down"
        )

    def test_armed_skip_semantics_are_never_a_silent_debug_when_undeliverable(self):
        # Regression for the claim-wiring class this doc fixes: an armed-but-
        # undeliverable dispatch must NOT emit the old misleading
        # "celery_tasks_available_skipping" DEBUG on this path.
        event = _make_event()

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(_RECORD_PATH),
            _patch_parked(1),
            patch.dict("sys.modules", {_CELERY_TASKS_MODULE: None}),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        assert _events(cap, "event_handler.celery_tasks_available_skipping") == []

    def test_dispatch_outcome_is_handed_to_the_arming_ledger_verbatim(self):
        # Every other test on this class patches this seam, so without one that
        # exercises it a signature drift between the two sides would pass here
        # and fail only in production.
        with patch(
            "baldur.services.replay_service.arming.record_dispatch_outcome"
        ) as ledger:
            _record_dispatch_outcome("error", service_name="orders-api", error="boom")

        ledger.assert_called_once_with("error", service_name="orders-api", error="boom")

    def test_dispatch_counts_the_outcome_but_never_writes_the_armed_gauge(self):
        # One gauge writer, and it is the arming probe. A dispatch observes a
        # single moment and would publish a claim the next poll contradicts —
        # which is how a broker-down deployment used to read as armed.
        recorder = MagicMock(spec=DLQMetricRecorder)

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            return_value=SimpleNamespace(dlq=recorder),
        ):
            _record_dispatch_outcome("dispatched", service_name="orders-api")

        recorder.record_replay_dispatch.assert_called_once_with("dispatched")
        recorder.set_auto_replay_armed.assert_not_called()
        # What the path observed reaches the operator as last_dispatch instead.
        assert get_dispatch_ledger().service_name == "orders-api"


# =============================================================================
# 809 D5 — the celery-missing WARNING only when something waits for a worker
# =============================================================================


class TestCeleryMissingParkedGateBehavior:
    """Without Celery, a recovery that left nothing parked is not told to run
    a worker; anything else keeps the WARNING. The counter records
    ``celery_missing`` either way, so the arming surface is unchanged.
    """

    @pytest.mark.parametrize(
        ("parked", "warns"),
        [
            (0, False),
            (1, True),
            (None, True),
            (RuntimeError("count unavailable"), True),
        ],
        ids=["nothing_parked", "one_parked", "count_unknown", "count_raises"],
    )
    def test_celery_missing_log_level_follows_what_is_parked(self, parked, warns):
        # Given: armed, Celery not importable, and the closing name's count.
        event = _make_event()
        service = MagicMock(spec=ReplayService)
        if isinstance(parked, Exception):
            service.parked_count_for_recovery.side_effect = parked
        else:
            service.parked_count_for_recovery.return_value = parked

        # When
        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(_RECORD_PATH) as record,
            patch(
                "baldur.services.replay_service.get_replay_service",
                return_value=service,
            ),
            patch.dict("sys.modules", {_CELERY_TASKS_MODULE: None}),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then
        blocked = _events(cap, "event_handler.replay_dispatch_blocked")
        skipped = _events(cap, "event_handler.replay_dispatch_skipped")
        if warns:
            assert len(blocked) == 1
            assert blocked[0]["log_level"] == "warning"
            assert skipped == []
        else:
            assert blocked == []
            assert len(skipped) == 1
            assert skipped[0]["log_level"] == "debug"
            assert skipped[0]["reason"] == "celery_missing"
            assert skipped[0]["nothing_parked"] is True
        service.parked_count_for_recovery.assert_called_once_with("payment-api")
        record.assert_called_once_with("celery_missing", service_name="payment-api")

    def test_celery_missing_with_no_replay_service_keeps_the_warning(self):
        """A failure to reach the replay service at all reads as "parked"."""
        event = _make_event()

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(_RECORD_PATH) as record,
            patch(
                "baldur.services.replay_service.get_replay_service",
                side_effect=RuntimeError("registry unavailable"),
            ),
            patch.dict("sys.modules", {_CELERY_TASKS_MODULE: None}),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        assert len(_events(cap, "event_handler.replay_dispatch_blocked")) == 1
        record.assert_called_once_with("celery_missing", service_name="payment-api")

    def test_celery_importable_dispatches_even_with_nothing_parked(self):
        """A count of 0 at CLOSED time cannot prove the worker will find
        nothing — a capture still in its outbox lands after it — so the
        dispatch never consults the count."""
        event = _make_event()
        task_mock = _make_task_mock()
        service = MagicMock(spec=ReplayService)
        service.parked_count_for_recovery.return_value = 0

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(_RECORD_PATH) as record,
            patch(
                "baldur.services.replay_service.get_replay_service",
                return_value=service,
            ),
            patch(_TASK_PATH, new=task_mock),
        ):
            _on_circuit_breaker_closed(event)

        task_mock.delay.assert_called_once_with(
            service_name="payment-api",
            max_items=50,
            max_continuations=(
                get_replay_automation_settings().on_recovery_max_continuations
            ),
            **_CLOSED_EVENT_CHAIN,
        )
        service.parked_count_for_recovery.assert_not_called()
        record.assert_called_once_with("dispatched", service_name="payment-api")


# =============================================================================
# D2/D5 — config precedence (settings fallback, no hardcoded literals)
# =============================================================================


class TestOnRecoveryDispatchSettingsFallbackBehavior:
    """On the RuntimeConfig-absent path the dispatch resolves max_items from
    ``ReplayAutomationSettings`` (env-honoring) — not a hardcoded literal.
    """

    def test_max_items_honors_env_when_runtime_config_absent(self, monkeypatch):
        # Given: RuntimeConfig absent AND an env-var override on the settings.
        monkeypatch.setenv("BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_MAX_ITEMS", "77")
        fresh = ReplayAutomationSettings()
        # Sanity: the env var is actually parsed by the settings model.
        assert fresh.on_recovery_max_items == 77

        event = _make_event()
        task_mock = _make_task_mock()

        with (
            patch.object(
                ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
            ),
            patch(
                "baldur.settings.replay_automation.get_replay_automation_settings",
                return_value=fresh,
            ),
            patch(_RECORD_PATH),
            patch(_TASK_PATH, new=task_mock),
        ):
            _on_circuit_breaker_closed(event)

        # Then: the dispatch used the env-derived settings value, proving the
        # fallback reads settings rather than the old hardcoded 50/100.
        task_mock.delay.assert_called_once_with(
            service_name="payment-api",
            max_items=77,
            max_continuations=100,
            **_CLOSED_EVENT_CHAIN,
        )
