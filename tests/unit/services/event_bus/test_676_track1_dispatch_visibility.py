"""676 — On-recovery dispatch visibility + RuntimeConfig reader pattern.

Target: ``baldur.services.event_bus.bus._cb_handlers``

    - ``_on_circuit_breaker_closed`` — armed-aware skip semantics (D3): a
      CB auto-CLOSE either dispatches, skips (disabled), or WARNs
      (armed-but-undeliverable / error) — never goes silently inert. Each
      outcome records a dispatch counter + armed gauge. The dispatch path is
      slot-blind (710): it never consults the PRO ``dlq_service`` slot —
      auto-replay on CB recovery is OSS.
    - ``_get_replay_automation_config`` — the 617 reader pattern (D1):
      RuntimeConfig absent = DEBUG-once (OSS-normal), read-failure = WARNING
      every time, and the public ``get_config`` accessor is used (never the
      private ``_get_config``).
    - ``get_cb_replay_dispatch_state`` / ``reset_cb_replay_dispatch_state``
      — the DEBUG-once marker and its reset hook (test isolation).
    - D2/D5 config precedence: on the RuntimeConfig-absent path the dispatch
      resolves ``on_recovery_max_items`` from ``ReplayAutomationSettings`` (env-
      honoring), not a hardcoded literal.

Provider slots are stubbed in-test by patching ``safe_get`` on the shared
``ProviderRegistry`` slot instances — no ``baldur_pro`` import (G19/G20/G21
safe, PRO-absent safe).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.factory.registry import ProviderRegistry
from baldur.services.event_bus.bus._cb_handlers import (
    _get_replay_automation_config,
    _on_circuit_breaker_closed,
    get_cb_replay_dispatch_state,
    reset_cb_replay_dispatch_state,
)
from baldur.settings.replay_automation import ReplayAutomationSettings

_TASK_PATH = "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close"
_CELERY_TASKS_MODULE = "baldur.adapters.celery.tasks"


# =============================================================================
# Fixtures / helpers
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_dispatch_markers():
    """Reset the module-global DEBUG-once marker around every test.

    The marker is process-global (``get_*/reset_*`` convention), so without
    this the first test's ``runtime_config_absent`` DEBUG would suppress a
    later test's expectation (xdist-safe isolation).
    """
    reset_cb_replay_dispatch_state()
    yield
    reset_cb_replay_dispatch_state()


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
        "baldur.services.event_bus.bus._cb_handlers._get_replay_automation_config",
        return_value=config,
    )


def _make_task_mock():
    task_mock = MagicMock()
    task_mock.delay = MagicMock()
    return task_mock


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
            patch(
                "baldur.services.event_bus.bus._cb_handlers._record_dispatch_outcome"
            ) as record,
            patch(_TASK_PATH, new=task_mock),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: the slot was never read and the dispatch proceeded on OSS.
        task_mock.delay.assert_called_once()
        record.assert_called_once_with("dispatched", armed=True)
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
            patch(
                "baldur.services.event_bus.bus._cb_handlers._record_dispatch_outcome"
            ) as record,
            patch(_TASK_PATH, new=_make_task_mock()) as task_mock,
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: INFO (existing behavior), no dispatch, counter=skipped_disabled.
        infos = _events(cap, "event_handler.circuit_breaker_closed_track")
        assert len(infos) == 1
        assert infos[0]["log_level"] == "info"
        assert task_mock.delay.call_count == 0
        record.assert_called_once_with("skipped_disabled", armed=False)

    def test_armed_dispatches_task_with_service_and_max_items(self):
        # Given: enabled + a configured max_items.
        event = _make_event(service_name="orders-api")
        task_mock = _make_task_mock()

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 42}),
            patch(
                "baldur.services.event_bus.bus._cb_handlers._record_dispatch_outcome"
            ) as record,
            patch(_TASK_PATH, new=task_mock),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: exactly-once dispatch with the resolved kwargs, counter=dispatched.
        task_mock.delay.assert_called_once_with(service_name="orders-api", max_items=42)
        assert len(_events(cap, "event_handler.circuit_breaker_closed_triggered")) == 1
        record.assert_called_once_with("dispatched", armed=True)

    def test_armed_but_celery_missing_warns_with_remediation(self):
        # Given: armed (enabled) but the Celery task import fails.
        # A None entry in sys.modules makes ``import <module>`` raise ImportError.
        event = _make_event()

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(
                "baldur.services.event_bus.bus._cb_handlers._record_dispatch_outcome"
            ) as record,
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
        record.assert_called_once_with("celery_missing", armed=False)

    def test_dispatch_broker_error_takes_error_path(self):
        # Given: the task imports fine but ``.delay`` raises a non-ImportError.
        event = _make_event()
        task_mock = _make_task_mock()
        task_mock.delay.side_effect = RuntimeError("broker down")

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(
                "baldur.services.event_bus.bus._cb_handlers._record_dispatch_outcome"
            ) as record,
            patch(_TASK_PATH, new=task_mock),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        # Then: the existing ERROR path is kept (handler does not crash), and
        # the counter records error.
        assert len(_events(cap, "event_handler.trigger_track_replay_failed")) == 1
        record.assert_called_once_with("error", armed=False)

    def test_armed_skip_semantics_are_never_a_silent_debug_when_undeliverable(self):
        # Regression for the claim-wiring class this doc fixes: an armed-but-
        # undeliverable dispatch must NOT emit the old misleading
        # "celery_tasks_available_skipping" DEBUG on this path.
        event = _make_event()

        with (
            _patch_config({"on_recovery_enabled": True, "on_recovery_max_items": 50}),
            patch(
                "baldur.services.event_bus.bus._cb_handlers._record_dispatch_outcome"
            ),
            patch.dict("sys.modules", {_CELERY_TASKS_MODULE: None}),
            capture_logs() as cap,
        ):
            _on_circuit_breaker_closed(event)

        assert _events(cap, "event_handler.celery_tasks_available_skipping") == []


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
            patch(
                "baldur.services.event_bus.bus._cb_handlers._record_dispatch_outcome"
            ),
            patch(_TASK_PATH, new=task_mock),
        ):
            _on_circuit_breaker_closed(event)

        # Then: the dispatch used the env-derived settings value, proving the
        # fallback reads settings rather than the old hardcoded 50/100.
        task_mock.delay.assert_called_once_with(
            service_name="payment-api", max_items=77
        )


# =============================================================================
# D1 — RuntimeConfig reader pattern (617 sister site)
# =============================================================================


class TestCBReaderBehavior:
    """``_get_replay_automation_config`` classifies absent (DEBUG-once) vs
    read-failure (WARNING each) and reads via the public ``get_config``.
    """

    def test_absent_manager_returns_none_and_debugs_once(self):
        with (
            patch.object(
                ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
            ),
            capture_logs() as cap,
        ):
            first = _get_replay_automation_config()
            second = _get_replay_automation_config()

        assert first is None
        assert second is None
        # OSS-normal absence surfaces as DEBUG, at most once per process.
        absent = _events(cap, "event_handler.runtime_config_absent")
        assert len(absent) == 1
        assert absent[0]["log_level"] == "debug"

    def test_read_failure_warns_every_occurrence(self):
        manager = MagicMock()
        manager.get_config.side_effect = RuntimeError("config store down")

        with (
            patch.object(
                ProviderRegistry.runtime_config_manager,
                "safe_get",
                return_value=manager,
            ),
            capture_logs() as cap,
        ):
            first = _get_replay_automation_config()
            second = _get_replay_automation_config()

        assert first is None
        assert second is None
        # A genuine read failure is abnormal — WARNING on every occurrence.
        failures = _events(cap, "event_handler.runtime_config_read_failed")
        assert len(failures) == 2
        assert all(e["log_level"] == "warning" for e in failures)

    def test_uses_public_get_config_accessor_not_private(self):
        manager = MagicMock()
        manager.get_config.return_value = {"on_recovery_enabled": True}

        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=manager
        ):
            result = _get_replay_automation_config()

        assert result == {"on_recovery_enabled": True}
        manager.get_config.assert_called_once_with("replay_automation")
        manager._get_config.assert_not_called()


# =============================================================================
# DEBUG-once marker state accessor / reset hook
# =============================================================================


class TestCBDispatchStateContract:
    """``get_cb_replay_dispatch_state`` / ``reset_cb_replay_dispatch_state``
    expose and clear the module-global DEBUG-once marker.
    """

    def test_reset_clears_the_runtime_config_marker(self):
        # Given: an absent-manager read flips the DEBUG-once marker.
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
        ):
            _get_replay_automation_config()
        assert get_cb_replay_dispatch_state()["runtime_config_absent_logged"] is True

        # When
        reset_cb_replay_dispatch_state()

        # Then: the marker is back to the pristine False state.
        assert get_cb_replay_dispatch_state() == {
            "runtime_config_absent_logged": False,
        }

    def test_absent_reader_flips_runtime_config_marker(self):
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
        ):
            _get_replay_automation_config()

        assert get_cb_replay_dispatch_state()["runtime_config_absent_logged"] is True
