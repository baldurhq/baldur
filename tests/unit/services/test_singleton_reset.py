"""
Tests for singleton reset functions (393 §B).

Verifies get_*() / reset_*() lifecycle for 7 services:
- Circuit Breaker, DLQ, Replay, Idempotency, Health Check (simple pattern)
- System Control (cleanup pattern: dual singleton + state reset)
- Emergency Mode (full cleanup: dual singleton + thread join)
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Simple Reset Pattern (5 services)
# =============================================================================


class TestCircuitBreakerServiceResetBehavior:
    """Circuit breaker singleton get/reset lifecycle."""

    def test_get_returns_same_instance(self):
        """get_circuit_breaker_service returns same instance on consecutive calls."""
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
            reset_circuit_breaker_service,
        )

        try:
            svc1 = get_circuit_breaker_service()
            svc2 = get_circuit_breaker_service()
            assert svc1 is svc2
        finally:
            reset_circuit_breaker_service()

    def test_reset_clears_singleton(self):
        """reset creates a new instance on next get call."""
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
            reset_circuit_breaker_service,
        )

        try:
            svc_before = get_circuit_breaker_service()
            reset_circuit_breaker_service()
            svc_after = get_circuit_breaker_service()
            assert svc_before is not svc_after
        finally:
            reset_circuit_breaker_service()

    def test_reset_idempotent_when_no_instance(self):
        """reset does not raise when called without prior get."""
        from baldur.services.circuit_breaker.convenience import (
            reset_circuit_breaker_service,
        )

        reset_circuit_breaker_service()
        reset_circuit_breaker_service()  # should not raise


class TestDLQServiceResetBehavior:
    """DLQ singleton get/reset lifecycle."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_get_returns_same_instance(self):
        """get_dlq_service returns same instance on consecutive calls."""
        from baldur_pro.services.dlq import get_dlq_service, reset_dlq_service

        try:
            svc1 = get_dlq_service()
            svc2 = get_dlq_service()
            assert svc1 is svc2
        finally:
            reset_dlq_service()

    def test_reset_clears_singleton(self):
        """reset creates a new instance on next get call."""
        from baldur_pro.services.dlq import get_dlq_service, reset_dlq_service

        try:
            svc_before = get_dlq_service()
            reset_dlq_service()
            svc_after = get_dlq_service()
            assert svc_before is not svc_after
        finally:
            reset_dlq_service()


class TestReplayServiceResetBehavior:
    """Replay singleton get/reset lifecycle."""

    def test_get_returns_same_instance(self):
        """get_replay_service returns same instance on consecutive calls."""
        from baldur.services.replay_service import (
            get_replay_service,
            reset_replay_service,
        )

        try:
            svc1 = get_replay_service()
            svc2 = get_replay_service()
            assert svc1 is svc2
        finally:
            reset_replay_service()

    def test_reset_clears_singleton(self):
        """reset creates a new instance on next get call."""
        from baldur.services.replay_service import (
            get_replay_service,
            reset_replay_service,
        )

        try:
            svc_before = get_replay_service()
            reset_replay_service()
            svc_after = get_replay_service()
            assert svc_before is not svc_after
        finally:
            reset_replay_service()


class TestIdempotencyServiceResetBehavior:
    """Idempotency singleton get/reset lifecycle."""

    def test_get_returns_same_instance(self):
        """get_idempotency_service returns same instance on consecutive calls."""
        from baldur.services.idempotency import (
            get_idempotency_service,
            reset_idempotency_service,
        )

        try:
            svc1 = get_idempotency_service()
            svc2 = get_idempotency_service()
            assert svc1 is svc2
        finally:
            reset_idempotency_service()

    def test_reset_clears_singleton(self):
        """reset creates a new instance on next get call."""
        from baldur.services.idempotency import (
            get_idempotency_service,
            reset_idempotency_service,
        )

        try:
            svc_before = get_idempotency_service()
            reset_idempotency_service()
            svc_after = get_idempotency_service()
            assert svc_before is not svc_after
        finally:
            reset_idempotency_service()


class TestHealthCheckServiceResetBehavior:
    """Health check singleton get/reset lifecycle."""

    def test_get_returns_same_instance(self):
        """get_health_check_service returns same instance on consecutive calls."""
        from baldur.services.health_check import (
            get_health_check_service,
            reset_health_check_service,
        )

        try:
            svc1 = get_health_check_service()
            svc2 = get_health_check_service()
            assert svc1 is svc2
        finally:
            reset_health_check_service()

    def test_reset_clears_singleton(self):
        """reset creates a new instance on next get call."""
        from baldur.services.health_check import (
            get_health_check_service,
            reset_health_check_service,
        )

        try:
            svc_before = get_health_check_service()
            reset_health_check_service()
            svc_after = get_health_check_service()
            assert svc_before is not svc_after
        finally:
            reset_health_check_service()


# =============================================================================
# Cleanup Reset Pattern — System Control (dual singleton)
# =============================================================================


class TestSystemControlResetBehavior:
    """System control cleanup reset: clears both module-level global and class _instance."""

    def test_reset_clears_module_global_and_class_instance(self):
        """reset nullifies singleton and SystemControlManager._instance."""
        from baldur.services.system_control import (
            SystemControlManager,
            get_system_control,
            reset_system_control,
        )

        try:
            svc1 = get_system_control()
            assert svc1 is not None
            assert SystemControlManager._instance is not None

            reset_system_control()

            assert SystemControlManager._instance is None

            svc2 = get_system_control()
            assert svc2 is not svc1
        finally:
            reset_system_control()
            SystemControlManager._instance = None

    def test_reset_calls_instance_reset_method(self):
        """reset invokes the instance's reset() to clear internal state."""
        from baldur.services.system_control import (
            SystemControlManager,
            get_system_control,
            reset_system_control,
        )

        try:
            ctrl = get_system_control()
            with patch.object(ctrl, "reset", autospec=True) as mock_reset:
                reset_system_control()
                mock_reset.assert_called_once()
        finally:
            reset_system_control()
            SystemControlManager._instance = None

    def test_reset_skips_reset_call_when_no_instance(self):
        """reset does not error when called with no active instance."""
        from baldur.services.system_control import reset_system_control

        # Should not raise
        reset_system_control()
        reset_system_control()


# =============================================================================
# Full Cleanup Reset Pattern — Emergency Mode (dual singleton + thread join)
# =============================================================================


class TestEmergencyManagerResetBehavior:
    """Emergency manager full cleanup reset: dual singleton + recovery thread join."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_reset_clears_module_global_and_class_instance(self):
        """reset nullifies both _emergency_manager and GracefulDegradationManager._instance."""
        import baldur_pro.services.emergency_mode as mod
        from baldur_pro.services.emergency_mode import (
            get_emergency_manager,
            reset_emergency_manager,
        )
        from baldur_pro.services.emergency_mode.manager import (
            GracefulDegradationManager,
        )

        try:
            # Ensure clean state: clear both singletons so get_emergency_manager()
            # creates a fresh instance (previous tests may have cleared only one).
            mod._emergency_manager = None
            GracefulDegradationManager._instance = None

            get_emergency_manager()
            assert mod._emergency_manager is not None
            assert GracefulDegradationManager._instance is not None

            reset_emergency_manager()

            assert mod._emergency_manager is None
            assert GracefulDegradationManager._instance is None
        finally:
            mod._emergency_manager = None
            GracefulDegradationManager._instance = None

    def test_reset_calls_instance_reset_and_joins_thread(self):
        """reset invokes mgr.reset() and joins recovery thread if alive."""
        import baldur_pro.services.emergency_mode as mod
        from baldur_pro.services.emergency_mode import (
            get_emergency_manager,
            reset_emergency_manager,
        )
        from baldur_pro.services.emergency_mode.manager import (
            GracefulDegradationManager,
        )

        try:
            mgr = get_emergency_manager()

            # Simulate a running recovery thread
            mock_thread = MagicMock(spec=threading.Thread)
            mock_thread.is_alive.return_value = True
            mgr._recovery_thread = mock_thread

            with patch.object(mgr, "reset", autospec=True) as mock_reset:
                reset_emergency_manager()
                mock_reset.assert_called_once()

            mock_thread.join.assert_called_once_with(timeout=0.5)
        finally:
            mod._emergency_manager = None
            GracefulDegradationManager._instance = None

    def test_reset_skips_thread_join_when_no_thread(self):
        """reset does not error when no recovery thread exists."""
        import baldur_pro.services.emergency_mode as mod
        from baldur_pro.services.emergency_mode import (
            get_emergency_manager,
            reset_emergency_manager,
        )
        from baldur_pro.services.emergency_mode.manager import (
            GracefulDegradationManager,
        )

        try:
            get_emergency_manager()
            reset_emergency_manager()  # Should not raise
        finally:
            mod._emergency_manager = None
            GracefulDegradationManager._instance = None
