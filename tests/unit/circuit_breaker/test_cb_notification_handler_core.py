"""
Circuit Breaker Notification Handler Tests.

Tests for:
1. _on_circuit_breaker_opened_notify - CB OPEN notification handler
2. EventBus handler registration
3. Celery task delegation kwargs (no trace fields, #611)
4. Exception isolation on notification failure
5. send_cb_open_notification graceful skip when baldur_pro is unavailable
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest


class TestCircuitBreakerOpenedNotifyHandler:
    """_on_circuit_breaker_opened_notify handler tests."""

    def setup_method(self):
        """Reset the event bus before each test."""
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        """Reset the event bus after each test."""
        self.bus.reset()

    def test_handler_exists(self):
        """_on_circuit_breaker_opened_notify handler exists."""
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_opened_notify,
        )

        assert callable(_on_circuit_breaker_opened_notify)

    def test_handler_registered_on_default_handlers(self):
        """register_default_handlers registers the CB OPENED handler."""
        from baldur.services.event_bus import (
            EventType,
            register_default_handlers,
        )

        register_default_handlers()

        subscriptions = self.bus.get_subscriptions(EventType.CIRCUIT_BREAKER_OPENED)
        handler_names = [s["handler_name"] for s in subscriptions]

        assert "_on_circuit_breaker_opened_notify" in handler_names

    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_notification_sent_on_cb_opened(self, mock_delay):
        """CB OPENED event delegates the notification to the Celery task."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
        )
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_opened_notify,
        )

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={
                "service_name": "payment_service",
                "timestamp": "2026-01-06T10:00:00Z",
            },
            source="circuit_breaker_service",
        )

        _on_circuit_breaker_opened_notify(event)

        mock_delay.assert_called_once_with(
            service_name="payment_service",
            timestamp="2026-01-06T10:00:00Z",
        )

    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_trace_fields_not_forwarded(self, mock_delay):
        """Stale trace fields in event data are not forwarded to the task (#611 D1)."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
        )
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_opened_notify,
        )

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={
                "service_name": "order_service",
                "trace_id": "xyz789",
                "trace_url": "https://jaeger.internal/trace/xyz789",
            },
            source="circuit_breaker_service",
        )

        _on_circuit_breaker_opened_notify(event)

        call_kwargs = mock_delay.call_args[1]
        assert "trace_id" not in call_kwargs
        assert "trace_url" not in call_kwargs

    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_service_name_passed_to_delay(self, mock_delay):
        """service_name is forwarded verbatim to the Celery task."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
        )
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_opened_notify,
        )

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={"service_name": "inventory_service"},
            source="test",
        )

        _on_circuit_breaker_opened_notify(event)

        call_kwargs = mock_delay.call_args[1]
        assert call_kwargs["service_name"] == "inventory_service"

    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_celery_enqueue_failure_does_not_raise(self, mock_delay):
        """Celery enqueue failure does not propagate (reliability guarantee)."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
        )
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_opened_notify,
        )

        mock_delay.side_effect = Exception("Broker connection failed!")

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={"service_name": "failing_service"},
            source="test",
        )

        try:
            _on_circuit_breaker_opened_notify(event)
        except Exception:
            pytest.fail("Celery enqueue failure should not raise exception")

    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_unknown_service_name_handled(self, mock_delay):
        """Missing service_name falls back to 'unknown'."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
        )
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_opened_notify,
        )

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={},  # no service_name
            source="test",
        )

        _on_circuit_breaker_opened_notify(event)

        call_kwargs = mock_delay.call_args[1]
        assert call_kwargs["service_name"] == "unknown"


class TestSendCbOpenNotificationProUnavailable:
    """send_cb_open_notification OSS-seam fallback when baldur_pro is absent."""

    def test_falls_back_to_oss_seam_without_raising(self):
        """ImportError on baldur_pro routes through the OSS direct construction.

        Post-640 the fallback constructs its own Slack transport from
        MetaWatchdogSettings.slack_webhook_url (outside the registry seam) and
        POSTs — no raise, no autoretry, no legacy ``baldur_pro_unavailable``
        skip. Patches the settings URL home + safe_urlopen so the result is
        independent of registry state.
        """

        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_open_notification,
        )

        with (
            patch.dict(
                "sys.modules",
                {"baldur_pro.services.unified_notification": None},
            ),
            patch(
                "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                return_value=SimpleNamespace(
                    slack_webhook_url="https://hooks.slack.com/services/T/B/X"
                ),
            ),
            patch(
                "baldur.adapters.notification.webhook_adapter.safe_urlopen",
                autospec=True,
            ) as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__.return_value.status = 200
            result = send_cb_open_notification(
                service_name="payment_service",
                timestamp="2026-01-06T10:00:00Z",
            )

        mock_urlopen.assert_called_once()
        assert result["success"] is True
        assert result["notification_sent"] is True
        assert result["channel"] == "slack"
        assert "reason" not in result

    def test_metadata_carries_no_trace_keys(self):
        """The notification payload metadata no longer carries trace fields (#611 D1)."""
        captured: dict = {}

        mock_pro = MagicMock()
        mock_pro.NotificationPayload.side_effect = lambda **kwargs: (
            captured.update(kwargs) or MagicMock()
        )

        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_open_notification,
        )

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.unified_notification": mock_pro},
        ):
            result = send_cb_open_notification(
                service_name="payment_service",
                timestamp="2026-01-06T10:00:00Z",
            )

        assert result["success"] is True
        metadata = captured["metadata"]
        assert "trace_id" not in metadata
        assert "trace_url" not in metadata
        assert metadata["service_name"] == "payment_service"
        assert set(metadata) >= {"dashboard_url", "admin_url", "runbook_url"}


class TestEventBusIntegration:
    """EventBus + CB notification handler integration tests."""

    def setup_method(self):
        """Reset the event bus before each test."""
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        """Reset the event bus after each test."""
        self.bus.reset()

    @patch("baldur.adapters.celery.tasks.collect_cb_open_snapshot.delay")
    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_full_integration_via_emit(self, mock_notify_delay, mock_snapshot_delay):
        """Full flow through emit() — Celery task delegation.

        The CB OPEN notify/snapshot handlers are dispatched fire-and-forget
        (636 D2), so emit() returns before the handler bodies run under the
        default async_pool dispatch. Drain the shared dispatch executor before
        asserting the Celery delegation fired.
        """
        from baldur.services.event_bus import (
            EventType,
            register_default_handlers,
        )
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        register_default_handlers()

        handlers_called = self.bus.emit(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={"service_name": "user_service"},
            source="circuit_breaker_service",
        )

        assert handlers_called >= 1

        # Drain in-flight fire-and-forget handlers before asserting delegation.
        BaldurEventBus.shutdown_dispatch_executor()

        mock_notify_delay.assert_called_once()

    def test_handler_priority_is_high(self):
        """CB OPENED handler is registered with HIGH priority."""
        from baldur.services.event_bus import (
            EventPriority,
            EventType,
            register_default_handlers,
        )

        register_default_handlers()

        subscriptions = self.bus.get_subscriptions(EventType.CIRCUIT_BREAKER_OPENED)
        notify_handler = next(
            (
                s
                for s in subscriptions
                if s["handler_name"] == "_on_circuit_breaker_opened_notify"
            ),
            None,
        )

        assert notify_handler is not None
        assert notify_handler["priority"] == EventPriority.HIGH.name


class TestCircuitBreakerClosedNotifyHandler:
    """_on_circuit_breaker_closed_notify handler tests (612 D4).

    Mirror of the OPEN notify handler: CB CLOSED delegates the recovery
    (stand-down) notification to the send_cb_close_notification Celery task so
    the Slack HTTP call stays off the app request thread.
    """

    def setup_method(self):
        """Reset the event bus before each test."""
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        """Reset the event bus after each test."""
        self.bus.reset()

    def test_handler_exists(self):
        """_on_circuit_breaker_closed_notify handler exists."""
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed_notify,
        )

        assert callable(_on_circuit_breaker_closed_notify)

    def test_handler_registered_at_high_priority(self):
        """register_default_handlers registers the CLOSED notify handler at HIGH."""
        from baldur.services.event_bus import (
            EventPriority,
            EventType,
            register_default_handlers,
        )

        register_default_handlers()

        subscriptions = self.bus.get_subscriptions(EventType.CIRCUIT_BREAKER_CLOSED)
        notify_handler = next(
            (
                s
                for s in subscriptions
                if s["handler_name"] == "_on_circuit_breaker_closed_notify"
            ),
            None,
        )

        assert notify_handler is not None
        assert notify_handler["priority"] == EventPriority.HIGH.name

    @patch("baldur.adapters.celery.tasks.send_cb_close_notification.delay")
    def test_notification_delegates_forwarding_all_recovery_fields(self, mock_delay):
        """CB CLOSED forwards service_name/timestamp/previous_state/trigger to the task."""
        from baldur.services.event_bus import BaldurEvent, EventType
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed_notify,
        )

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={
                "service_name": "payment_service",
                "timestamp": "2026-01-06T14:00:00Z",
                "previous_state": "open",
                "trigger": "auto",
            },
            source="circuit_breaker_service",
        )

        _on_circuit_breaker_closed_notify(event)

        mock_delay.assert_called_once_with(
            service_name="payment_service",
            timestamp="2026-01-06T14:00:00Z",
            previous_state="open",
            trigger="auto",
        )

    @patch("baldur.adapters.celery.tasks.send_cb_close_notification.delay")
    def test_missing_fields_fall_back_to_defaults(self, mock_delay):
        """Missing service_name falls back to 'unknown'; absent fields to empty strings."""
        from baldur.services.event_bus import BaldurEvent, EventType
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed_notify,
        )

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={},  # no fields
            source="test",
        )

        _on_circuit_breaker_closed_notify(event)

        mock_delay.assert_called_once_with(
            service_name="unknown",
            timestamp="",
            previous_state="",
            trigger="",
        )

    @patch("baldur.adapters.celery.tasks.send_cb_close_notification.delay")
    def test_celery_enqueue_failure_does_not_raise(self, mock_delay):
        """Celery enqueue failure does not propagate (reliability guarantee)."""
        from baldur.services.event_bus import BaldurEvent, EventType
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed_notify,
        )

        mock_delay.side_effect = Exception("Broker connection failed!")

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "failing_service"},
            source="test",
        )

        try:
            _on_circuit_breaker_closed_notify(event)
        except Exception:
            pytest.fail("Celery enqueue failure should not raise exception")

    def test_no_celery_delivers_via_oss_fallback(self):
        """No celery extra -> the CLOSED handler delivers via the OSS fallback.

        Post-641 the ImportError path no longer skips silently — it delivers
        through the relocated OSS wrapper. Pinned to a no-URL settings namespace
        so the deterministic LoggingNotificationAdapter is used (no network);
        ``safe_urlopen`` is patched as a guard so a host with
        BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL set cannot make a real POST. The
        handler must not raise and must not POST.
        """
        from baldur.services.event_bus import BaldurEvent, EventType
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed_notify,
        )

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "payment_service"},
            source="test",
        )

        with (
            patch.dict("sys.modules", {"baldur.adapters.celery.tasks": None}),
            patch(
                "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                return_value=SimpleNamespace(slack_webhook_url=None),
            ),
            patch(
                "baldur.adapters.notification.webhook_adapter.safe_urlopen",
                autospec=True,
            ) as mock_urlopen,
        ):
            try:
                _on_circuit_breaker_closed_notify(event)
            except Exception:
                pytest.fail("no-celery OSS fallback must not raise")

        # No URL -> LoggingNotificationAdapter -> zero POSTs.
        mock_urlopen.assert_not_called()


class TestCbNotificationHandlerNoCeleryFallback:
    """641: core-only (no ``celery`` extra) — the notify handlers deliver the
    OSS Slack push synchronously through the relocated wrapper instead of
    skipping.

    ``celery`` absence is simulated by pinning ``baldur.adapters.celery.tasks``
    to ``None`` in ``sys.modules`` (forces the handler's ImportError branch).
    The POST is captured at the same seam the relocated helper builds:
    ``safe_urlopen`` + ``get_meta_watchdog_settings``. Parametrized OPEN/CLOSED
    — same fallback, different payload wrapper.
    """

    _URL = "https://hooks.slack.com/services/T000/B000/XXX"
    _SAFE_URLOPEN = "baldur.adapters.notification.webhook_adapter.safe_urlopen"
    _CB_SETTINGS = "baldur.settings.meta_watchdog.get_meta_watchdog_settings"
    _CELERY_TASKS = "baldur.adapters.celery.tasks"

    def setup_method(self):
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        self.bus.reset()

    def _event(self, kind):
        from baldur.services.event_bus import BaldurEvent, EventType

        if kind == "open":
            return BaldurEvent(
                event_type=EventType.CIRCUIT_BREAKER_OPENED,
                data={
                    "service_name": "payment_service",
                    "timestamp": "2026-01-06T10:00:00Z",
                },
                source="circuit_breaker_service",
            )
        return BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={
                "service_name": "payment_service",
                "timestamp": "2026-01-06T10:05:00Z",
                "previous_state": "open",
                "trigger": "auto",
            },
            source="circuit_breaker_service",
        )

    def _handler(self, kind):
        from baldur.services.event_bus.bus._cb_handlers import (
            _on_circuit_breaker_closed_notify,
            _on_circuit_breaker_opened_notify,
        )

        return (
            _on_circuit_breaker_opened_notify
            if kind == "open"
            else _on_circuit_breaker_closed_notify
        )

    def _patch_settings(self, url):
        return patch(
            self._CB_SETTINGS,
            return_value=SimpleNamespace(slack_webhook_url=url),
        )

    @pytest.mark.parametrize("kind", ["open", "closed"])
    def test_no_celery_post_once(self, kind):
        """No celery extra -> the handler delivers exactly one POST to the URL."""
        handler = self._handler(kind)
        event = self._event(kind)

        with (
            patch.dict("sys.modules", {self._CELERY_TASKS: None}),
            self._patch_settings(self._URL),
            patch(self._SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__.return_value.status = 200
            handler(event)

        mock_urlopen.assert_called_once()
        assert mock_urlopen.call_args.args[0].full_url == self._URL

    @pytest.mark.parametrize("kind", ["open", "closed"])
    def test_no_celery_fail_open_unreachable_webhook(self, kind):
        """No celery extra + unreachable webhook -> URLError does not propagate."""
        handler = self._handler(kind)
        event = self._event(kind)

        with (
            patch.dict("sys.modules", {self._CELERY_TASKS: None}),
            self._patch_settings(self._URL),
            patch(self._SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            mock_urlopen.side_effect = URLError("unreachable")
            try:
                handler(event)
            except Exception:
                pytest.fail("OSS fallback must be fail-open (no propagation)")

    @pytest.mark.parametrize(
        ("kind", "delay_target"),
        [
            (
                "open",
                "baldur.adapters.celery.tasks.send_cb_open_notification.delay",
            ),
            (
                "closed",
                "baldur.adapters.celery.tasks.send_cb_close_notification.delay",
            ),
        ],
    )
    def test_no_double_send_when_celery_present(self, kind, delay_target):
        """Celery importable -> the handler .delay()s; the OSS fallback never POSTs."""
        handler = self._handler(kind)
        event = self._event(kind)

        with (
            patch(delay_target) as mock_delay,
            patch(self._SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            handler(event)

        mock_delay.assert_called_once()
        mock_urlopen.assert_not_called()

    def test_helper_importable_without_celery(self):
        """The OSS CB-delivery seam imports without celery; the task module does not.

        (1) The helper + two wrappers import from the celery-free adapter
        package with no simulation — the relocation moved them off the
        celery-import path. (2) The task module top-imports ``from celery import
        shared_task``, so a fresh import with ``celery`` pinned absent raises
        ImportError, proving the helper no longer shares that path.
        """
        import importlib

        from baldur.adapters.notification import (
            _send_cb_close_notification_oss,
            _send_cb_notification_oss,
            _send_cb_open_notification_oss,
        )

        assert callable(_send_cb_notification_oss)
        assert callable(_send_cb_open_notification_oss)
        assert callable(_send_cb_close_notification_oss)

        task_mod = "baldur.celery_tasks.circuit_breaker_tasks"
        with patch.dict("sys.modules", {"celery": None}):
            sys.modules.pop(task_mod, None)
            with pytest.raises(ImportError):
                importlib.import_module(task_mod)
