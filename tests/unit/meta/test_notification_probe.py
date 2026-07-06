"""
NotificationChannelProbe + NotificationAdapter.is_available() unit tests (409 UU-E8).

Test targets:
    - NotificationAdapter.is_available() default behavior
    - NotificationChannelProbe.component_name contract
    - NotificationChannelProbe.probe() judgment logic

Test categories:
    A. Contract: component_name, is_available default, _STALE_THRESHOLD
    B. Behavior: probe judgment (HEALTHY/DEGRADED/UNHEALTHY/UNKNOWN), stale detection
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.notification import (
    NotificationAdapter,
    NotificationChannel,
    StdoutNotificationAdapter,
)
from baldur.meta.health_probe import HealthStatus
from baldur.meta.notification_probe import (
    _STALE_THRESHOLD,
    NotificationChannelProbe,
)

# =============================================================================
# Helpers
# =============================================================================


class _AvailableAdapter(NotificationAdapter):
    """Test adapter that inherits default is_available() → True."""

    def send(self, notification):
        return True

    def send_batch(self, notifications):
        return len(notifications)

    @property
    def channel(self):
        return NotificationChannel.STDOUT


class _UnavailableAdapter(NotificationAdapter):
    """Test adapter that overrides is_available() → False."""

    def send(self, notification):
        return True

    def send_batch(self, notifications):
        return len(notifications)

    @property
    def channel(self):
        return NotificationChannel.STDOUT

    def is_available(self) -> bool:
        return False


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestNotificationAdapterIsAvailableContract:
    """409 UU-E8: NotificationAdapter.is_available() contract."""

    def test_default_is_available_returns_true(self):
        """ABC-inheriting adapter inherits is_available() → True."""
        adapter = _AvailableAdapter()
        assert adapter.is_available() is True

    def test_stdout_adapter_inherits_is_available(self):
        """StdoutNotificationAdapter inherits is_available() default."""
        adapter = StdoutNotificationAdapter()
        assert adapter.is_available() is True

    def test_override_can_return_false(self):
        """Subclass can override is_available() to return False."""
        adapter = _UnavailableAdapter()
        assert adapter.is_available() is False


class TestNotificationChannelProbeContract:
    """409 UU-E8: NotificationChannelProbe contract values."""

    def test_component_name_is_notification_channels(self):
        probe = NotificationChannelProbe()
        assert probe.component_name == "notification_channels"

    def test_stale_threshold_is_10_minutes(self):
        assert _STALE_THRESHOLD == timedelta(minutes=10)


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestNotificationChannelProbeBehavior:
    """409 UU-E8: NotificationChannelProbe.probe() judgment behavior."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_mock_manager(self, last_success=None):
        """Create a mock UNM with _channel_last_success."""
        manager = MagicMock()
        manager._channel_last_success = last_success or {}
        return manager

    def _make_probe_with_mocks(self, provider_names=None, adapters=None, manager=None):
        """Patch ProviderRegistry and UNM for probe testing."""
        if manager is None:
            manager = self._make_mock_manager()
        if provider_names is None:
            provider_names = []
        if adapters is None:
            adapters = {}

        mock_registry_cls = MagicMock()
        mock_registry_cls.notification.list_providers.return_value = provider_names
        mock_registry_cls.get_notification.side_effect = lambda n: adapters.get(n)

        patches = [
            patch(
                "baldur_pro.services.unified_notification.service.get_unified_notification_manager",
                return_value=manager,
            ),
            patch(
                "baldur.factory.ProviderRegistry",
                mock_registry_cls,
            ),
        ]
        return patches

    def test_probe_all_available_no_stale_returns_healthy(self):
        """All adapters available + no stale channels → HEALTHY."""
        # Given
        adapter = MagicMock()
        adapter.is_available.return_value = True
        patches = self._make_probe_with_mocks(
            provider_names=["slack"],
            adapters={"slack": adapter},
        )

        # When
        with patches[0], patches[1]:
            result = NotificationChannelProbe().probe()

        # Then
        assert result.status == HealthStatus.HEALTHY
        assert result.details["available_count"] == 1
        assert result.details["unavailable_count"] == 0

    def test_probe_some_unavailable_returns_degraded(self):
        """Some adapters unavailable → DEGRADED."""
        # Given
        avail = MagicMock()
        avail.is_available.return_value = True
        unavail = MagicMock()
        unavail.is_available.return_value = False

        patches = self._make_probe_with_mocks(
            provider_names=["slack", "pagerduty"],
            adapters={"slack": avail, "pagerduty": unavail},
        )

        # When
        with patches[0], patches[1]:
            result = NotificationChannelProbe().probe()

        # Then
        assert result.status == HealthStatus.DEGRADED
        assert result.details["unavailable_count"] == 1

    def test_probe_all_unavailable_returns_unhealthy(self):
        """All adapters unavailable → UNHEALTHY."""
        # Given
        unavail = MagicMock()
        unavail.is_available.return_value = False

        patches = self._make_probe_with_mocks(
            provider_names=["slack"],
            adapters={"slack": unavail},
        )

        # When
        with patches[0], patches[1]:
            result = NotificationChannelProbe().probe()

        # Then
        assert result.status == HealthStatus.UNHEALTHY

    def test_probe_stale_channel_returns_degraded(self):
        """Channel with stale last_success → DEGRADED."""
        from baldur.utils.time import utc_now

        # Given: adapter is available but last success is stale
        adapter = MagicMock()
        adapter.is_available.return_value = True
        stale_time = utc_now() - _STALE_THRESHOLD - timedelta(minutes=1)
        manager = self._make_mock_manager(last_success={"slack": stale_time})

        patches = self._make_probe_with_mocks(
            provider_names=["slack"],
            adapters={"slack": adapter},
            manager=manager,
        )

        # When
        with patches[0], patches[1]:
            result = NotificationChannelProbe().probe()

        # Then
        assert result.status == HealthStatus.DEGRADED
        assert "slack" in result.details["stale_channels"]

    def test_probe_recent_success_not_stale(self):
        """Channel with recent last_success → not stale."""
        from baldur.utils.time import utc_now

        # Given
        adapter = MagicMock()
        adapter.is_available.return_value = True
        recent_time = utc_now() - timedelta(minutes=1)
        manager = self._make_mock_manager(last_success={"slack": recent_time})

        patches = self._make_probe_with_mocks(
            provider_names=["slack"],
            adapters={"slack": adapter},
            manager=manager,
        )

        # When
        with patches[0], patches[1]:
            result = NotificationChannelProbe().probe()

        # Then
        assert result.status == HealthStatus.HEALTHY
        assert result.details["stale_channels"] == []

    def test_probe_provider_registry_error_falls_back(self):
        """ProviderRegistry attribute error → fallback to 1 available."""
        # Given
        manager = self._make_mock_manager()
        mock_registry = MagicMock()
        mock_registry.notification.list_providers.side_effect = RuntimeError("broken")

        patches = self._make_probe_with_mocks(manager=manager)
        # Override the ProviderRegistry mock to raise
        factory_patch = patch(
            "baldur.factory.ProviderRegistry",
            mock_registry,
        )
        factory_patch.start()

        with patches[0]:
            # When — inner try/except catches the error
            result = NotificationChannelProbe().probe()

        factory_patch.stop()

        # Then
        assert result.status == HealthStatus.HEALTHY
        assert result.details["available_count"] == 1

    def test_probe_duck_typed_adapter_getattr_fallback(self):
        """Adapter without is_available method → getattr fallback returns True."""
        # Given: adapter with no is_available attribute
        adapter = MagicMock(spec=[])  # empty spec → no attributes
        del adapter.is_available  # ensure getattr fallback

        patches = self._make_probe_with_mocks(
            provider_names=["custom"],
            adapters={"custom": adapter},
        )

        # When
        with patches[0], patches[1]:
            result = NotificationChannelProbe().probe()

        # Then
        assert result.status == HealthStatus.HEALTHY
        assert result.details["available_count"] == 1

    def test_probe_exception_returns_unknown(self):
        """Unhandled exception → UNKNOWN status."""
        with patch(
            "baldur_pro.services.unified_notification.service.get_unified_notification_manager",
            side_effect=RuntimeError("broken"),
        ):
            result = NotificationChannelProbe().probe()

        assert result.status == HealthStatus.UNKNOWN
        assert "broken" in result.error
