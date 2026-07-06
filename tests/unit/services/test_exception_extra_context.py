"""
Tests for exception extra_context() overrides (393 §E).

Contract verification: each exception returns the documented extra_context keys.
"""

from __future__ import annotations

import pytest

# =============================================================================
# E1. CircuitBreakerOpenError
# =============================================================================


class TestCircuitBreakerOpenErrorExtraContextContract:
    """CircuitBreakerOpenError.extra_context() returns service_name."""

    def test_extra_context_contains_service_name(self):
        """extra_context returns dict with 'service_name' key matching constructor arg."""
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        err = CircuitBreakerOpenError("payment-api")
        ctx = err.extra_context()

        assert ctx == {"service_name": "payment-api"}

    def test_inherits_from_circuit_breaker_error(self):
        """CircuitBreakerOpenError inherits from CircuitBreakerError → BaldurError."""
        from baldur.core.exceptions import BaldurError
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        err = CircuitBreakerOpenError("svc")
        assert isinstance(err, BaldurError)

    def test_default_message_includes_service_name(self):
        """Default message format includes the service name."""
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        err = CircuitBreakerOpenError("order-svc")
        assert "order-svc" in str(err)
        assert "OPEN" in str(err)


# =============================================================================
# E2. ShutdownError
# =============================================================================


class TestShutdownErrorExtraContextContract:
    """ShutdownError.extra_context() returns phase and detail."""

    def test_extra_context_contains_phase_and_detail(self):
        """extra_context returns dict with 'phase' and 'detail' keys."""
        from baldur.core.shutdown_coordinator import ShutdownError

        err = ShutdownError(
            "Cannot register",
            phase="draining",
            detail="handler_registration_after_shutdown",
        )
        ctx = err.extra_context()

        assert ctx == {
            "phase": "draining",
            "detail": "handler_registration_after_shutdown",
        }

    def test_extra_context_defaults_to_empty_strings(self):
        """extra_context returns empty strings when no kwargs provided."""
        from baldur.core.shutdown_coordinator import ShutdownError

        err = ShutdownError("test")
        ctx = err.extra_context()

        assert ctx == {"phase": "", "detail": ""}

    def test_inherits_from_baldur_error(self):
        """ShutdownError inherits from BaldurError."""
        from baldur.core.exceptions import BaldurError
        from baldur.core.shutdown_coordinator import ShutdownError

        assert issubclass(ShutdownError, BaldurError)


# =============================================================================
# E3. EmergencyStateError + RecoveryNotAllowedError
# =============================================================================


class TestEmergencyStateErrorExtraContextContract:
    """EmergencyStateError.extra_context() returns operation and detail."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_extra_context_contains_operation_and_detail(self):
        """extra_context returns dict with 'operation' and 'detail' keys."""
        from baldur_pro.services.emergency_mode.exceptions import (
            EmergencyStateError,
        )

        err = EmergencyStateError(
            "reason is required",
            operation="activate_manual",
            detail="",
        )
        ctx = err.extra_context()

        assert ctx == {"operation": "activate_manual", "detail": ""}

    def test_extra_context_with_all_fields(self):
        """extra_context reflects all constructor kwargs."""
        from baldur_pro.services.emergency_mode.exceptions import (
            EmergencyStateError,
        )

        err = EmergencyStateError(
            "Emergency mode is not active",
            operation="start_gradual_recovery",
            detail="not_active",
        )
        ctx = err.extra_context()

        assert ctx["operation"] == "start_gradual_recovery"
        assert ctx["detail"] == "not_active"

    def test_inherits_from_emergency_mode_error(self):
        """EmergencyStateError inherits from EmergencyModeError → BaldurError."""
        from baldur.core.exceptions import BaldurError
        from baldur_pro.services.emergency_mode.exceptions import (
            EmergencyModeError,
            EmergencyStateError,
        )

        assert issubclass(EmergencyStateError, EmergencyModeError)
        assert issubclass(EmergencyStateError, BaldurError)


class TestRecoveryNotAllowedErrorExtraContextContract:
    """RecoveryNotAllowedError.extra_context() returns check_reason."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_extra_context_contains_check_reason(self):
        """extra_context returns dict with 'check_reason' key."""
        from baldur_pro.services.emergency_mode.exceptions import (
            RecoveryNotAllowedError,
        )

        err = RecoveryNotAllowedError(
            "Recovery not allowed: error rate too high",
            check_reason="error rate too high",
        )
        ctx = err.extra_context()

        assert ctx == {"check_reason": "error rate too high"}

    def test_extra_context_defaults_to_empty_string(self):
        """extra_context returns empty check_reason when not provided."""
        from baldur_pro.services.emergency_mode.exceptions import (
            RecoveryNotAllowedError,
        )

        err = RecoveryNotAllowedError("test")
        assert err.extra_context() == {"check_reason": ""}

    def test_inherits_from_emergency_mode_error(self):
        """RecoveryNotAllowedError inherits from EmergencyModeError."""
        from baldur_pro.services.emergency_mode.exceptions import (
            EmergencyModeError,
            RecoveryNotAllowedError,
        )

        assert issubclass(RecoveryNotAllowedError, EmergencyModeError)
