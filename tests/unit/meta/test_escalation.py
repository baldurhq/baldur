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

from baldur.interfaces.messaging_common import OFF_HOST_DELIVERY_CHANNELS
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
    """Records sent payloads and returns a configurable success flag.

    Mirrors the PRO adapters' optional extras: ``send_resolve`` stands in for
    the PagerDuty close verb the self-test fires after its trigger, recording
    its payloads separately and returning a configurable ``(ok, reason)``.
    """

    def __init__(self, channel: NotificationChannel) -> None:
        self._channel = channel
        self.ok = True
        self.calls: list = []
        self.resolve_ok = True
        self.resolve_reason: str | None = "resolve failed"
        self.resolve_calls: list = []

    def send(self, payload) -> bool:
        self.calls.append(payload)
        return self.ok

    def send_resolve(self, payload) -> tuple[bool, str | None]:
        self.resolve_calls.append(payload)
        return (True, None) if self.resolve_ok else (False, self.resolve_reason)

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


class TestDeliveredExternallyContract:
    """``delivered_externally`` answers "did this leave the host?" — ``success`` cannot.

    The notification seam substitutes the logging adapter whenever the
    configured transport cannot be resolved, and that adapter always reports
    success, so a ``success=True`` result may have reached nothing but this
    process's own log. The property reads the resolved channels against the
    off-host whitelist instead.
    """

    def test_off_host_channel_set_is_exactly_the_four_transports(self):
        # The whitelist is the spec: a channel added to MessageChannel later is
        # non-delivering until someone adds it HERE, so the counter it backs
        # understates rather than overstates.
        assert OFF_HOST_DELIVERY_CHANNELS == frozenset(
            {"slack", "teams", "pagerduty", "webhook"}
        )

    @pytest.mark.parametrize(
        ("channels_sent", "expected"),
        [
            ([], False),
            (["slack"], True),
            (["teams"], True),
            (["pagerduty"], True),
            (["webhook"], True),
            # The logging fallback's own channel — the substitution this
            # property exists to expose.
            (["log"], False),
            (["stdout"], False),
            (["file"], False),
            # dry_run_mode short-circuits before any adapter is touched.
            (["dry_run"], False),
            # Mixed: one real channel is enough to have reached a person.
            (["log", "slack"], True),
            (["stdout", "log"], False),
            # Whitelist negative: an unrecognised channel is NOT assumed to
            # deliver. A blacklist would have answered True here.
            (["carrier_pigeon"], False),
        ],
    )
    def test_delivered_externally_reflects_the_channels_that_leave_the_host(
        self, channels_sent, expected
    ):
        result = EscalationResult(
            success=True,
            channels_sent=list(channels_sent),
            channels_failed=[],
        )

        assert result.delivered_externally is expected

    def test_delivered_externally_is_false_for_a_successful_log_only_delivery(self):
        # The whole point, stated as one case: `success` and
        # `delivered_externally` disagree exactly where the seam substituted.
        result = EscalationResult(
            success=True,
            channels_sent=["log"],
            channels_failed=[],
        )

        assert result.success is True
        assert result.delivered_externally is False

    def test_delivered_externally_ignores_a_whitelisted_channel_that_failed(self):
        # A channel that was ATTEMPTED and failed reached nobody, even though
        # it is on the whitelist — the property reads channels_sent only.
        result = EscalationResult(
            success=False,
            channels_sent=[],
            channels_failed=["slack", "pagerduty"],
            error_message="all channels failed",
        )

        assert result.delivered_externally is False


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


class _ResolvelessAdapter(NotificationAdapter):
    """A PagerDuty-channel adapter with NO ``send_resolve``.

    Stands in for an adapter an operator registered themselves through the
    public seam: its trigger opens a real incident, so a missing close
    capability must be reported, never treated as a silent success. Kept
    separate from ``_FakeAdapter`` on purpose — the shared fake must keep the
    attribute for every other case.
    """

    def __init__(self, channel: NotificationChannel) -> None:
        self._channel = channel
        self.calls: list = []

    def send(self, payload) -> bool:
        self.calls.append(payload)
        return True

    def send_batch(self, payloads) -> int:
        return sum(1 for p in payloads if self.send(p))

    @property
    def channel(self) -> NotificationChannel:
        return self._channel


class _ContractBreakingAdapter(_ResolvelessAdapter):
    """A PagerDuty-channel adapter whose ``send_resolve`` breaks its contract.

    The other half of the third-party-adapter case: the method exists, so the
    capability probe admits it, but it does not honor ``(ok, reason)``. Both
    breakages funnel through the same unpack site — ``raise`` propagates, and
    a bare bool fails to unpack — so one adapter covers them by mode.
    """

    def __init__(self, channel: NotificationChannel, mode: str) -> None:
        super().__init__(channel)
        self._mode = mode

    def send_resolve(self, payload):
        if self._mode == "raises":
            raise ConnectionError("pd unreachable")
        return True  # bare bool, not the (ok, reason) tuple


def _install_adapter(channel_name: str, adapter) -> None:
    """Swap one seam slot; the ``seam`` fixture restores it on teardown."""
    from baldur.factory import ProviderRegistry

    ProviderRegistry.register_notification(channel_name, lambda: adapter)
    ProviderRegistry.notification.set_instance(channel_name, adapter)


class TestEscalationSelfTestResolveBehavior:
    """send_test()'s PagerDuty close leg.

    The self-test opens a real PagerDuty incident, so it closes it in the same
    call. The leg fires on *attempted* rather than delivered — a client-visible
    send failure does not mean PagerDuty rejected the event, and resolving a
    dedup key with no open incident is an accepted no-op — and its outcome is
    reported separately from delivery, so ``success`` stays delivery-based.
    """

    @staticmethod
    def _pagerduty_only() -> MetaWatchdogSettings:
        """Settings with PagerDuty as the single configured channel."""
        return MetaWatchdogSettings(
            slack_webhook_url=None,
            pagerduty_routing_key="pd-routing-key",
        )

    def test_delivered_trigger_closes_the_incident_with_the_trigger_payload(self, seam):
        """A delivered self-test closes itself, on the payload it opened with."""
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        # The same payload object reaches both verbs — it carries the dedup
        # key's inputs, so trigger and close address one incident.
        assert result.success is True
        assert len(seam.pagerduty.resolve_calls) == 1
        assert seam.pagerduty.resolve_calls[0] is seam.pagerduty.calls[0]
        assert result.error_message is None

    def test_failed_trigger_still_closes_the_incident(self, seam):
        """The close fires on *attempted* — a failed send may still have landed."""
        # Given PagerDuty reports the trigger as failed (the enqueue may
        # nevertheless have succeeded and only the read timed out)
        seam.pagerduty.ok = False
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        # Then the channel is recorded failed AND the close still went out —
        # a delivered-keyed leg would leak exactly this incident
        assert result.channels_failed == ["pagerduty"]
        assert len(seam.pagerduty.resolve_calls) == 1

    def test_failed_close_after_failed_trigger_adds_no_manual_close_note(self, seam):
        """No incident is known to exist, so "close manually" would be false guidance."""
        seam.pagerduty.ok = False
        seam.pagerduty.resolve_ok = False
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        assert result.success is False
        assert result.error_message.startswith("pagerduty:")
        assert "close manually" not in result.error_message

    def test_failed_close_after_delivered_trigger_reports_note_and_keeps_success(
        self, seam
    ):
        """The close failure is reported without turning a delivered test red.

        Also pins the all-succeeded path: ``error_message`` is None there, so
        composing the note (rather than appending to it) is what keeps this
        from raising.
        """
        seam.pagerduty.resolve_ok = False
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        assert result.success is True
        assert result.channels_sent == ["pagerduty"]
        assert result.channels_failed == []
        assert "close manually" in result.error_message

    @pytest.mark.parametrize(
        "reason",
        [
            "unexpected status 429",
            "HTTPSConnectionPool(host='events.pagerduty.com'): Read timed out.",
        ],
        ids=["rejected_status", "transport_exception"],
    )
    def test_failed_close_note_carries_the_adapter_reason_verbatim(self, seam, reason):
        """The note names the cause so the operator can pick retry vs PD console."""
        seam.pagerduty.resolve_ok = False
        seam.pagerduty.resolve_reason = reason
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        assert reason in result.error_message

    def test_close_reporting_not_configured_is_a_silent_noop(self, seam):
        """A routing key cleared mid-call leaves no configuration to report on."""
        seam.pagerduty.resolve_ok = False
        seam.pagerduty.resolve_reason = "not configured"
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        assert result.success is True
        assert result.error_message is None

    def test_adapter_without_close_capability_is_reported_as_a_close_failure(
        self, seam
    ):
        """A missing capability is a failure, never a silently claimed close."""
        # Given an operator-registered PagerDuty adapter with no send_resolve
        _install_adapter(
            "pagerduty", _ResolvelessAdapter(NotificationChannel.PAGERDUTY)
        )
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        assert result.success is True
        assert "adapter has no resolve capability" in result.error_message
        assert "close manually" in result.error_message

    @pytest.mark.parametrize(
        ("mode", "expected_cause"),
        [
            ("raises", "pd unreachable"),
            ("bare_bool", "cannot unpack"),
        ],
        ids=["raises", "returns_bare_bool"],
    )
    def test_close_capability_that_breaks_its_contract_is_a_close_failure(
        self, seam, mode, expected_cause
    ):
        """A third-party adapter's broken close must not break the self-test.

        The probe admits any callable, but an adapter registered through the
        public seam is not bound by the ``(ok, reason)`` contract. Cleanup is
        best-effort, so a raise (or a bare bool that fails to unpack) is
        reported like any other close failure — it must not propagate out of a
        self-test whose delivery succeeded, which would turn a green channel
        check into a 500.
        """
        # Given an operator-registered PagerDuty adapter with a broken close
        _install_adapter(
            "pagerduty", _ContractBreakingAdapter(NotificationChannel.PAGERDUTY, mode)
        )
        manager = EscalationManager(settings=self._pagerduty_only())

        result = manager.send_test()

        # Then delivery still reads green and the cause reaches the operator
        assert result.success is True
        assert result.channels_sent == ["pagerduty"]
        assert expected_cause in result.error_message
        assert "close manually" in result.error_message

    def test_unattempted_pagerduty_channel_is_never_closed(self, seam):
        """An unconfigured PagerDuty opened nothing, so nothing is closed."""
        manager = EscalationManager(
            settings=MetaWatchdogSettings(
                slack_webhook_url="https://hooks.slack.com/test",
                pagerduty_routing_key=None,
            )
        )

        result = manager.send_test()

        assert result.channels_sent == ["slack"]
        assert seam.pagerduty.resolve_calls == []

    def test_logging_fallback_install_never_closes_an_incident(self, seam):
        """OSS-only: both channels resolve to the log adapter, so the leg is dead.

        The fallback fake *does* expose ``send_resolve``, so an empty call list
        pins the attempted-channel guard rather than a missing attribute.
        """
        # Given an OSS-only seam: every configured channel resolves to "log"
        fallback = _FakeAdapter(NotificationChannel.LOG)
        _install_adapter("slack", fallback)
        _install_adapter("pagerduty", fallback)
        manager = EscalationManager(
            settings=MetaWatchdogSettings(
                slack_webhook_url="https://hooks.slack.com/test",
                pagerduty_routing_key="pd-routing-key",
            )
        )

        result = manager.send_test()

        assert result.channels_sent == ["log"]
        assert fallback.resolve_calls == []


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
