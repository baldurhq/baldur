"""
EscalationManager tests (OSS orchestrator).

EscalationManager stays OSS as the tier-neutral orchestrator: severity-based
channel selection, per-process cooldown, cross-worker dedup, and the operator
self-test. Concrete external push is resolved through the ProviderRegistry
notification seam; the actual transports live in PRO. These tests inject a
controllable fake adapter into the seam so the orchestrator's routing /
dedup / recording is exercised independent of any transport.

The concrete push behavior (Slack/PagerDuty HTTP, Block-Kit shape, per-channel
failure reasons) and the OSS-only "logs, never pushes" assertion are PRO /
OSS-only coverage owned by their own test files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from baldur.interfaces.notification import NotificationAdapter, NotificationChannel
from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.escalation import (
    EscalationEvent,
    EscalationLevel,
    EscalationManager,
    EscalationResult,
    configure_escalation_manager,
    get_escalation_manager,
    reset_escalation_manager,
)
from baldur.meta.state_store import (
    WatchdogStateStore,
    configure_watchdog_state_store,
    reset_watchdog_state_store,
)
from tests.factories import MockRedisClient

# =============================================================================
# Fake seam — controllable slack/pagerduty adapters injected into the registry
# =============================================================================


class _FakeAdapter(NotificationAdapter):
    """Records sent payloads and returns a configurable success flag."""

    def __init__(self, channel: NotificationChannel) -> None:
        self._channel = channel
        self.ok = True
        self.calls: list = []

    def send(self, payload) -> bool:
        self.calls.append(payload)
        return self.ok

    def send_batch(self, payloads) -> int:
        return sum(1 for p in payloads if self.send(p))

    @property
    def channel(self) -> NotificationChannel:
        return self._channel


@dataclass
class _Seam:
    slack: _FakeAdapter
    pagerduty: _FakeAdapter

    def set_fail(self) -> None:
        self.slack.ok = False
        self.pagerduty.ok = False


@pytest.fixture
def seam():
    """Inject controllable fake slack/pagerduty adapters into the seam.

    Isolates the notification registry (the monorepo's PRO escalation adapters
    may otherwise be registered globally) so the orchestrator's delivery is
    deterministic and transport-independent.
    """
    from baldur.factory import ProviderRegistry

    snapshot = ProviderRegistry.notification.save_state()
    slack = _FakeAdapter(NotificationChannel.SLACK)
    pagerduty = _FakeAdapter(NotificationChannel.PAGERDUTY)
    ProviderRegistry.register_notification("slack", lambda: slack)
    ProviderRegistry.register_notification("pagerduty", lambda: pagerduty)
    ProviderRegistry.notification.set_instance("slack", slack)
    ProviderRegistry.notification.set_instance("pagerduty", pagerduty)
    yield _Seam(slack=slack, pagerduty=pagerduty)
    ProviderRegistry.notification.restore_state(snapshot)


def _info_event() -> EscalationEvent:
    """An INFO-level event matching the shape send_test() builds."""
    return EscalationEvent(
        level=EscalationLevel.INFO,
        title="self-test",
        description="test notification",
        component="escalation_self_test",
    )


def _warning_event(component: str = "redis") -> EscalationEvent:
    """A WARNING-level event (routes to Slack only, not PagerDuty)."""
    return EscalationEvent(
        level=EscalationLevel.WARNING,
        title="Incident",
        description="component unhealthy",
        component=component,
    )


class TestEscalationLevel:
    """EscalationLevel enum."""

    def test_values(self):
        """Enum string values match the design contract."""
        assert EscalationLevel.INFO.value == "info"
        assert EscalationLevel.WARNING.value == "warning"
        assert EscalationLevel.ERROR.value == "error"
        assert EscalationLevel.CRITICAL.value == "critical"


class TestEscalationEvent:
    """EscalationEvent dataclass."""

    def test_creation(self):
        """Event is created with the given fields and dataclass defaults."""
        event = EscalationEvent(
            level=EscalationLevel.CRITICAL,
            title="Test Alert",
            description="Test description",
            component="test",
        )

        assert event.level == EscalationLevel.CRITICAL
        assert event.title == "Test Alert"
        assert event.description == "Test description"
        assert event.component == "test"
        assert event.details == {}
        assert isinstance(event.timestamp, datetime)


class TestEscalationResult:
    """EscalationResult dataclass."""

    def test_success_result(self):
        """A success result carries sent channels and no error message."""
        result = EscalationResult(
            success=True,
            channels_sent=["pagerduty", "slack"],
            channels_failed=[],
        )

        assert result.success is True
        assert "pagerduty" in result.channels_sent
        assert result.error_message is None

    def test_failure_result(self):
        """A failure result carries failed channels and an error message."""
        result = EscalationResult(
            success=False,
            channels_sent=[],
            channels_failed=["pagerduty"],
            error_message="Network error",
        )

        assert result.success is False
        assert "pagerduty" in result.channels_failed


class TestEscalationManager:
    """EscalationManager incident path (escalate())."""

    def test_escalation_disabled(self):
        """escalation_enabled=False short-circuits to a disabled result."""
        settings = MetaWatchdogSettings(escalation_enabled=False)
        manager = EscalationManager(settings=settings)

        result = manager.escalate(
            EscalationEvent(
                level=EscalationLevel.CRITICAL,
                title="Test",
                description="Test",
                component="test",
            )
        )

        assert result.success is False
        assert result.error_message == "Escalation disabled"

    def test_dry_run_mode(self):
        """Dry-run mode reports a fake success on the 'dry_run' channel."""
        settings = MetaWatchdogSettings(
            escalation_enabled=True,
            dry_run_mode=True,
        )
        manager = EscalationManager(settings=settings)

        result = manager.escalate(
            EscalationEvent(
                level=EscalationLevel.CRITICAL,
                title="Test",
                description="Test",
                component="test",
            )
        )

        assert result.success is True
        assert "dry_run" in result.channels_sent

    def test_maintenance_component_suppressed(self):
        """A component under maintenance suppresses escalation."""
        settings = MetaWatchdogSettings(
            escalation_enabled=True,
            maintenance_components=["redis"],
        )
        manager = EscalationManager(settings=settings)

        result = manager.escalate(
            EscalationEvent(
                level=EscalationLevel.CRITICAL,
                title="Test",
                description="Test",
                component="redis",  # under maintenance
            )
        )

        assert result.success is False
        assert result.error_message == "Component in maintenance"

    def test_cooldown_prevents_duplicate(self, seam):
        """The per-component cooldown blocks a second escalation."""
        settings = MetaWatchdogSettings(
            escalation_enabled=True,
            escalation_cooldown_seconds=3600.0,
        )
        manager = EscalationManager(settings=settings)
        event = EscalationEvent(
            level=EscalationLevel.CRITICAL,
            title="Test",
            description="Test",
            component="test",
        )

        result1 = manager.escalate(event)
        assert result1.success is True

        # Second call: blocked by cooldown.
        result2 = manager.escalate(event)
        assert result2.success is False
        assert result2.error_message == "Cooldown active"

    def test_reset_cooldown(self, seam):
        """reset_cooldown() clears the cooldown so escalation succeeds again."""
        settings = MetaWatchdogSettings(
            escalation_enabled=True,
            escalation_cooldown_seconds=3600.0,
        )
        manager = EscalationManager(settings=settings)
        event = EscalationEvent(
            level=EscalationLevel.CRITICAL,
            title="Test",
            description="Test",
            component="test",
        )

        manager.escalate(event)
        manager.reset_cooldown("test")

        # Succeeds after cooldown reset.
        result = manager.escalate(event)
        assert result.success is True

    def test_get_last_escalation_time(self, seam):
        """The last escalation time is recorded after a successful send."""
        settings = MetaWatchdogSettings(escalation_enabled=True)
        manager = EscalationManager(settings=settings)

        # Before escalation.
        assert manager.get_last_escalation_time("test") is None

        manager.escalate(
            EscalationEvent(
                level=EscalationLevel.CRITICAL,
                title="Test",
                description="Test",
                component="test",
            )
        )

        # After escalation (recorded only when a channel succeeded).
        last_time = manager.get_last_escalation_time("test")
        assert last_time is not None
        assert last_time > 0

    def test_failed_delivery_does_not_record_success(self, seam):
        """When every channel fails, the escalation is not a success."""
        seam.set_fail()
        manager = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=True)
        )

        result = manager.escalate(_warning_event("redis"))

        assert result.success is False
        assert "slack" in result.channels_failed

    def test_warning_level_skips_pagerduty(self, seam):
        """WARNING level routes to Slack only, not PagerDuty."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=True)
        )

        result = manager.escalate(_warning_event("test"))

        assert "slack" in result.channels_sent
        assert "pagerduty" not in result.channels_sent
        assert seam.pagerduty.calls == []

    def test_critical_routes_to_both_channels(self, seam):
        """CRITICAL level routes to both PagerDuty and Slack."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=True)
        )

        result = manager.escalate(
            EscalationEvent(
                level=EscalationLevel.CRITICAL,
                title="Test",
                description="Test",
                component="test",
            )
        )

        assert result.success is True
        assert sorted(result.channels_sent) == ["pagerduty", "slack"]

    def test_recorded_channel_is_resolved_adapter_channel(self, seam):
        """channels_sent records the resolved adapter's channel (degradation-visible)."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=True)
        )

        result = manager.escalate(_warning_event("test"))

        # The fake adapter advertises channel SLACK -> recorded as "slack".
        assert result.channels_sent == ["slack"]


class TestEscalationSendTest:
    """EscalationManager.send_test() — operator self-test.

    send_test routes by configuration (not severity level), bypasses every
    escalate() gate, skips (never "fails") an unconfigured channel, and
    aggregates per-channel failure causes into error_message.
    """

    @pytest.mark.parametrize(
        ("slack_url", "pd_key", "expected_sent"),
        [
            ("https://hooks.slack.com/test", None, ["slack"]),
            (None, "pd-routing-key", ["pagerduty"]),
            ("https://hooks.slack.com/test", "pd-routing-key", ["slack", "pagerduty"]),
        ],
        ids=["slack_only", "pagerduty_only", "both"],
    )
    def test_send_test_configured_channels_deliver_returns_success(
        self, seam, slack_url, pd_key, expected_sent
    ):
        """Every configured channel delivering -> success=True, all sent."""
        settings = MetaWatchdogSettings(
            slack_webhook_url=slack_url, pagerduty_routing_key=pd_key
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        assert result.success is True
        assert sorted(result.channels_sent) == sorted(expected_sent)
        assert result.channels_failed == []
        assert result.error_message is None

    @pytest.mark.parametrize(
        ("slack_url", "pd_key", "expected_failed"),
        [
            ("https://hooks.slack.com/test", None, ["slack"]),
            (None, "pd-routing-key", ["pagerduty"]),
            ("https://hooks.slack.com/test", "pd-routing-key", ["slack", "pagerduty"]),
        ],
        ids=["slack_only", "pagerduty_only", "both"],
    )
    def test_send_test_configured_channels_fail_reports_failure(
        self, seam, slack_url, pd_key, expected_failed
    ):
        """Every configured channel failing -> success=False, all failed."""
        seam.set_fail()
        settings = MetaWatchdogSettings(
            slack_webhook_url=slack_url, pagerduty_routing_key=pd_key
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        assert result.success is False
        assert sorted(result.channels_failed) == sorted(expected_failed)
        assert result.channels_sent == []
        assert result.error_message is not None
        for channel in expected_failed:
            assert f"{channel}:" in result.error_message

    def test_send_test_no_channel_configured_returns_explicit_failure(self, seam):
        """No channel configured -> success=False, empty lists, explicit message."""
        settings = MetaWatchdogSettings(
            slack_webhook_url=None, pagerduty_routing_key=None
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        assert result.success is False
        assert result.channels_sent == []
        assert result.channels_failed == []
        assert result.error_message == "No escalation channel configured"

    def test_send_test_unconfigured_channel_is_skipped_not_failed(self, seam):
        """An unconfigured channel never appears in sent or failed lists."""
        settings = MetaWatchdogSettings(
            slack_webhook_url="https://hooks.slack.com/test",
            pagerduty_routing_key=None,
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        assert result.channels_sent == ["slack"]
        assert "pagerduty" not in result.channels_sent
        assert "pagerduty" not in result.channels_failed

    def test_send_test_bypasses_escalation_disabled_gate(self, seam):
        """send_test ignores escalation_enabled=False (it is the config check)."""
        settings = MetaWatchdogSettings(
            escalation_enabled=False,
            slack_webhook_url="https://hooks.slack.com/test",
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        assert result.success is True
        assert result.channels_sent == ["slack"]

    def test_send_test_bypasses_dry_run_mode_sends_real_notification(self, seam):
        """dry_run_mode does not turn send_test into a fake success."""
        settings = MetaWatchdogSettings(
            dry_run_mode=True,
            slack_webhook_url="https://hooks.slack.com/test",
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        # A real seam delivery, not escalate()'s "dry_run" channel.
        assert result.success is True
        assert result.channels_sent == ["slack"]
        assert "dry_run" not in result.channels_sent

    def test_send_test_is_repeatable_without_cooldown(self, seam):
        """send_test bypasses the cooldown — repeated calls all deliver."""
        settings = MetaWatchdogSettings(
            escalation_cooldown_seconds=3600.0,
            slack_webhook_url="https://hooks.slack.com/test",
        )
        manager = EscalationManager(settings=settings)

        first = manager.send_test()
        second = manager.send_test()

        assert first.success is True
        assert second.success is True
        assert second.channels_sent == ["slack"]

    def test_send_test_failure_reason_is_prefixed_with_channel(self, seam):
        """A failed channel's reason is prefixed with the channel name."""
        seam.set_fail()
        settings = MetaWatchdogSettings(
            slack_webhook_url="https://hooks.slack.com/test"
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        assert result.error_message is not None
        assert result.error_message.startswith("slack:")

    def test_send_test_partial_failure_aggregates_only_failed_channel(self, seam):
        """With one channel failing, only that channel's reason is aggregated."""
        # Slack fails, PagerDuty delivers.
        seam.slack.ok = False
        settings = MetaWatchdogSettings(
            slack_webhook_url="https://hooks.slack.com/test",
            pagerduty_routing_key="pd-routing-key",
        )
        manager = EscalationManager(settings=settings)

        result = manager.send_test()

        assert result.success is False
        assert result.channels_sent == ["pagerduty"]
        assert result.channels_failed == ["slack"]
        assert "slack:" in result.error_message
        assert "pagerduty:" not in result.error_message


class TestEscalationProPresentPushRegression:
    """640 regression guard: a registered Slack push adapter still pushes.

    Doc 640 reverts OSS escalation to log-only by removing the OSS
    auto-registration of a push adapter into the SLACK seam — so on an OSS-only
    install escalation resolves the logging fallback (``channels_sent == ["log"]``).
    This guard pins the *other* side: the orchestrator's delivery logic is
    unchanged, so when a push-capable Slack adapter IS registered in the seam
    (exactly what ``baldur_pro`` registers at init via ``register_escalation_adapters``),
    ``send_test`` / ``escalate`` resolve it and record ``slack``. Uses the
    tier-neutral ``seam`` fixture (explicit registration), so it holds with or
    without ``baldur_pro`` installed.
    """

    def test_send_test_still_pushes_slack_when_pro_adapter_registered(self, seam):
        """PRO Slack transport present -> self-test pushes, records ``slack``."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(
                slack_webhook_url="https://hooks.slack.com/test",
            )
        )

        result = manager.send_test()

        assert result.success is True
        assert result.channels_sent == ["slack"]
        assert len(seam.slack.calls) == 1

    def test_escalate_warning_still_pushes_slack_when_pro_adapter_registered(
        self, seam, shared_state_store
    ):
        """PRO Slack transport present -> WARNING escalation pushes, records ``slack``."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=True)
        )

        result = manager.escalate(_warning_event("redis"))

        assert result.success is True
        assert result.channels_sent == ["slack"]
        assert len(seam.slack.calls) == 1


# =============================================================================
# Cross-worker dedup / cooldown robustness
# =============================================================================


@pytest.fixture
def shared_state_store():
    """Install a MockRedis-backed WatchdogStateStore as the cross-worker store.

    Two EscalationManager instances over this single store genuinely contend
    for one ``SET NX EX`` escalation slot (MockRedisClient.set(nx=True) returns
    False on an existing key, mirroring Redis). The singleton is reset on
    teardown so the rest of the suite keeps its no-Redis fail-open behaviour.
    """
    store = WatchdogStateStore(redis_client=MockRedisClient())
    configure_watchdog_state_store(store)
    yield store
    reset_watchdog_state_store()


@pytest.fixture
def reset_escalation_manager_singleton():
    """Isolate the module-level EscalationManager singleton."""
    reset_escalation_manager()
    yield
    reset_escalation_manager()


class TestEscalateRejectionInvariant:
    """Every policy-rejection return carries an empty channels_failed.

    The watchdog's _escalate discriminates a genuine delivery failure from a
    non-failure rejection purely on ``channels_failed`` being non-empty. This
    locks the invariant. Contract: the rejection error_message strings are the
    design-doc literals.
    """

    def test_disabled_rejection_has_empty_channels_failed(self):
        """escalation_enabled=False rejects with no attempted channel."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=False)
        )

        result = manager.escalate(_warning_event())

        assert result.success is False
        assert result.channels_sent == []
        assert result.channels_failed == []
        assert result.error_message == "Escalation disabled"

    def test_maintenance_rejection_has_empty_channels_failed(self):
        """A component under maintenance rejects with no attempted channel."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(
                escalation_enabled=True,
                maintenance_components=["redis"],
            )
        )

        result = manager.escalate(_warning_event("redis"))

        assert result.success is False
        assert result.channels_sent == []
        assert result.channels_failed == []
        assert result.error_message == "Component in maintenance"

    def test_cooldown_rejection_has_empty_channels_failed(self, seam):
        """The local cooldown rejects the 2nd escalation with empty lists."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(
                escalation_enabled=True,
                escalation_cooldown_seconds=3600.0,
            )
        )
        first = manager.escalate(_warning_event("redis"))
        assert first.success is True

        result = manager.escalate(_warning_event("redis"))

        assert result.success is False
        assert result.channels_sent == []
        assert result.channels_failed == []
        assert result.error_message == "Cooldown active"

    def test_cross_worker_rejection_has_empty_channels_failed_and_distinct_message(
        self, seam, shared_state_store
    ):
        """A lost cross-worker slot rejects with empty lists, distinct message."""
        shared_state_store.acquire_escalation_lock("redis", lock_ttl_seconds=3600)

        manager = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=True)
        )

        result = manager.escalate(_warning_event("redis"))

        assert result.success is False
        assert result.channels_sent == []
        assert result.channels_failed == []
        assert result.error_message == "Cross-worker cooldown active"
        assert seam.slack.calls == []  # never reached the send loop


class TestEscalateCrossWorkerDedup:
    """One incident pages at most once per cooldown window cluster-wide.

    Two EscalationManager instances (the gunicorn-worker model) share one
    WatchdogStateStore. The cross-worker SET NX EX claim makes the second
    worker's escalation a no-op even though its per-process cooldown is fresh.
    """

    def test_second_worker_within_window_is_deduped(self, seam, shared_state_store):
        """The 2nd worker is skipped; delivery happens exactly once cluster-wide."""
        settings = MetaWatchdogSettings(
            escalation_enabled=True,
            escalation_cooldown_seconds=3600.0,
        )
        worker_a = EscalationManager(settings=settings)
        worker_b = EscalationManager(settings=settings)
        event = _warning_event("redis")

        result_a = worker_a.escalate(event)
        result_b = worker_b.escalate(event)

        assert result_a.success is True
        assert result_a.channels_sent == ["slack"]
        assert result_b.success is False
        assert result_b.channels_sent == []
        assert result_b.error_message == "Cross-worker cooldown active"
        assert len(seam.slack.calls) == 1  # one delivery cluster-wide

    def test_all_channel_failure_releases_slot_so_retry_is_not_blocked(
        self, seam, shared_state_store
    ):
        """On all-channel failure the cross-worker slot is released for retry."""
        seam.set_fail()
        settings = MetaWatchdogSettings(escalation_enabled=True)
        worker = EscalationManager(settings=settings)

        from unittest import mock

        with mock.patch.object(
            shared_state_store,
            "release_escalation_lock",
            wraps=shared_state_store.release_escalation_lock,
        ) as release:
            result = worker.escalate(_warning_event("redis"))

        assert result.success is False
        assert "slack" in result.channels_failed
        release.assert_called_once_with("redis")
        assert (
            shared_state_store.acquire_escalation_lock("redis", lock_ttl_seconds=3600)
            is True
        )

    def test_redis_down_fails_open_to_per_process_cooldown(self, seam):
        """A down dedup store fails open — both workers page (degraded N×M)."""
        store = WatchdogStateStore(redis_client=MockRedisClient(should_fail=True))
        configure_watchdog_state_store(store)
        try:
            settings = MetaWatchdogSettings(escalation_enabled=True)
            worker_a = EscalationManager(settings=settings)
            worker_b = EscalationManager(settings=settings)
            event = _warning_event("redis")

            result_a = worker_a.escalate(event)
            result_b = worker_b.escalate(event)

            assert result_a.success is True
            assert result_b.success is True
            assert len(seam.slack.calls) == 2
        finally:
            reset_watchdog_state_store()


class TestEscalationManagerSingleton:
    """Module-level get/configure/reset singleton pair."""

    def test_get_returns_same_instance(self, reset_escalation_manager_singleton):
        """get_escalation_manager caches a single instance."""
        first = get_escalation_manager()
        second = get_escalation_manager()

        assert first is second
        assert isinstance(first, EscalationManager)

    def test_reset_returns_fresh_instance(self, reset_escalation_manager_singleton):
        """reset_escalation_manager forces a fresh instance on next get."""
        first = get_escalation_manager()
        reset_escalation_manager()
        second = get_escalation_manager()

        assert first is not second

    def test_configure_injects_instance(self, reset_escalation_manager_singleton):
        """configure_escalation_manager installs the given instance."""
        custom = EscalationManager(
            settings=MetaWatchdogSettings(escalation_enabled=False)
        )
        configure_escalation_manager(custom)

        assert get_escalation_manager() is custom
