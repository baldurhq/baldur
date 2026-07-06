"""
DegradationBroadcaster and DegradationStatus tests (307).

Tests the new observer-pattern broadcaster and unified status query
introduced in the consolidation refactor.
"""

from unittest.mock import MagicMock, patch

from baldur.audit.resilience.degradation_protocol import (
    DegradationBroadcaster,
    DegradationObserver,
    DegradationStatus,
)

# ============================================================================
# Contract Tests
# ============================================================================


class TestDegradationStatusContract:
    """DegradationStatus.get_unified_status() response structure contract."""

    def test_unified_status_keys(self):
        """get_unified_status returns required top-level keys."""
        with (
            patch(
                "baldur.audit.resilience.degraded_mode.DegradedModeManager",
                autospec=True,
            ) as mock_dmm_cls,
            patch(
                "baldur.audit.graceful_degradation.degradation_manager.HashChainDegradationManager",
                autospec=True,
            ) as mock_hcdm_cls,
        ):
            mock_dmm_instance = MagicMock()
            mock_dmm_cls.get_instance.return_value = mock_dmm_instance
            mock_dmm_instance.get_status.return_value = {"degraded": False}

            mock_hcdm_cls._instance = None

            result = DegradationStatus.get_unified_status()

            expected_keys = {
                "external_backends",
                "redis_hashchain",
                "overall_degraded",
                "worst_level",
            }
            assert expected_keys == set(result.keys())


# ============================================================================
# Behavior Tests
# ============================================================================


class _FakeObserver:
    """Test observer implementing DegradationObserver protocol."""

    def __init__(self):
        self.calls: list[tuple] = []

    def on_degradation_changed(self, source, is_degraded, level, reason):
        self.calls.append((source, is_degraded, level, reason))


class _FailingObserver:
    """Observer that raises on notification."""

    def on_degradation_changed(self, source, is_degraded, level, reason):
        raise RuntimeError("observer failure")


class TestDegradationBroadcasterBehavior:
    """DegradationBroadcaster observer management and notification."""

    def setup_method(self):
        DegradationBroadcaster.reset()

    def teardown_method(self):
        DegradationBroadcaster.reset()

    def test_register_and_notify_delivers_to_observer(self):
        """Registered observer receives notification with correct args."""
        observer = _FakeObserver()
        DegradationBroadcaster.register(observer)

        DegradationBroadcaster.notify("source_a", True, "degraded", "test reason")

        assert len(observer.calls) == 1
        assert observer.calls[0] == ("source_a", True, "degraded", "test reason")

    def test_notify_delivers_to_multiple_observers(self):
        """Multiple registered observers all receive notification."""
        obs1 = _FakeObserver()
        obs2 = _FakeObserver()
        DegradationBroadcaster.register(obs1)
        DegradationBroadcaster.register(obs2)

        DegradationBroadcaster.notify("src", False, None, "recovered")

        assert len(obs1.calls) == 1
        assert len(obs2.calls) == 1

    def test_unregister_removes_observer(self):
        """Unregistered observer no longer receives notifications."""
        observer = _FakeObserver()
        DegradationBroadcaster.register(observer)
        DegradationBroadcaster.unregister(observer)

        DegradationBroadcaster.notify("src", True, "degraded", "reason")

        assert len(observer.calls) == 0

    def test_unregister_nonexistent_observer_is_noop(self):
        """Unregistering non-registered observer does not raise."""
        observer = _FakeObserver()
        DegradationBroadcaster.unregister(observer)

    def test_duplicate_register_is_idempotent(self):
        """Registering same observer twice delivers notification only once."""
        observer = _FakeObserver()
        DegradationBroadcaster.register(observer)
        DegradationBroadcaster.register(observer)

        DegradationBroadcaster.notify("src", True, None, "reason")

        assert len(observer.calls) == 1

    def test_reset_clears_all_observers(self):
        """reset() removes all observers."""
        observer = _FakeObserver()
        DegradationBroadcaster.register(observer)
        DegradationBroadcaster.reset()

        DegradationBroadcaster.notify("src", True, None, "reason")
        assert len(observer.calls) == 0

    def test_failing_observer_does_not_block_others(self):
        """DR-2: One observer's exception does not block other observers."""
        failing = _FailingObserver()
        healthy = _FakeObserver()
        DegradationBroadcaster.register(failing)
        DegradationBroadcaster.register(healthy)

        DegradationBroadcaster.notify("src", True, "emergency", "crisis")

        assert len(healthy.calls) == 1
        assert healthy.calls[0] == ("src", True, "emergency", "crisis")

    def test_observer_protocol_check(self):
        """DegradationObserver is a runtime_checkable Protocol."""
        observer = _FakeObserver()
        assert isinstance(observer, DegradationObserver)


class TestDegradationStatusBehavior:
    """DegradationStatus unified query behavior."""

    def test_overall_degraded_false_when_both_normal(self):
        """overall_degraded is False when neither manager is degraded."""
        with (
            patch(
                "baldur.audit.resilience.degraded_mode.DegradedModeManager",
                autospec=True,
            ) as mock_dmm,
            patch(
                "baldur.audit.graceful_degradation.degradation_manager.HashChainDegradationManager",
                autospec=True,
            ) as mock_hcdm,
        ):
            mock_dmm.get_instance.return_value.get_status.return_value = {
                "degraded": False
            }
            mock_hcdm._instance = MagicMock()
            mock_hcdm._instance.get_status.return_value = {
                "level": "normal",
                "is_degraded": False,
            }

            result = DegradationStatus.get_unified_status()
            assert result["overall_degraded"] is False
            assert result["worst_level"] == "normal"

    def test_overall_degraded_true_when_external_degraded(self):
        """overall_degraded is True when external backends are degraded."""
        with (
            patch(
                "baldur.audit.resilience.degraded_mode.DegradedModeManager",
                autospec=True,
            ) as mock_dmm,
            patch(
                "baldur.audit.graceful_degradation.degradation_manager.HashChainDegradationManager",
                autospec=True,
            ) as mock_hcdm,
        ):
            mock_dmm.get_instance.return_value.get_status.return_value = {
                "degraded": True
            }
            mock_hcdm._instance = None

            result = DegradationStatus.get_unified_status()
            assert result["overall_degraded"] is True

    def test_worst_level_picks_highest_severity(self):
        """worst_level selects the highest severity from both managers."""
        with (
            patch(
                "baldur.audit.resilience.degraded_mode.DegradedModeManager",
                autospec=True,
            ) as mock_dmm,
            patch(
                "baldur.audit.graceful_degradation.degradation_manager.HashChainDegradationManager",
                autospec=True,
            ) as mock_hcdm,
        ):
            mock_dmm.get_instance.return_value.get_status.return_value = {
                "degraded": True
            }
            mock_hcdm._instance = MagicMock()
            mock_hcdm._instance.get_status.return_value = {
                "level": "emergency",
                "is_degraded": True,
            }

            result = DegradationStatus.get_unified_status()
            assert result["worst_level"] == "emergency"

    def test_redis_hashchain_fallback_when_no_instance(self):
        """When HashChainDegradationManager has no instance, defaults are used."""
        with (
            patch(
                "baldur.audit.resilience.degraded_mode.DegradedModeManager",
                autospec=True,
            ) as mock_dmm,
            patch(
                "baldur.audit.graceful_degradation.degradation_manager.HashChainDegradationManager",
                autospec=True,
            ) as mock_hcdm,
        ):
            mock_dmm.get_instance.return_value.get_status.return_value = {
                "degraded": False
            }
            mock_hcdm._instance = None

            result = DegradationStatus.get_unified_status()
            assert result["redis_hashchain"]["level"] == "normal"
            assert result["redis_hashchain"]["is_degraded"] is False
