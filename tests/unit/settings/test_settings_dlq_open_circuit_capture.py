"""``DLQSettings.open_circuit_capture_enabled`` — the open-circuit capture switch.

Governs only the policy chain built by ``protect(dlq=True)`` / ``@dlq_protect``.
The request-boundary preemptive store and the Celery terminal capture carry
their own switches, so turning this off does not silence them.

Manifest agreement (row present, default matching, env var derived from the
class prefix) is covered exhaustively by the launch-manifest fitness function
over every enable-shape field, so it is deliberately not restated per field
here.
"""

from __future__ import annotations

from baldur.settings.dlq import DLQSettings

_ENV_VAR = "BALDUR_DLQ_OPEN_CIRCUIT_CAPTURE_ENABLED"

# =============================================================================
# Contract — design default
# =============================================================================


class TestDLQOpenCircuitCaptureSettingsContract:
    """Design-contract value: capture is on wherever DLQ was asked for."""

    def test_open_circuit_capture_enabled_default_is_true(self):
        """Default True — `dlq=True` alone parks open-circuit rejections."""
        assert DLQSettings().open_circuit_capture_enabled is True


# =============================================================================
# Behavior — env override
# =============================================================================


class TestDLQOpenCircuitCaptureSettingsBehavior:
    """The env var is the operator's way back to the pre-capture behavior."""

    def test_env_false_disables_capture(self, monkeypatch):
        monkeypatch.setenv(_ENV_VAR, "false")

        assert DLQSettings().open_circuit_capture_enabled is False

    def test_env_true_enables_capture(self, monkeypatch):
        monkeypatch.setenv(_ENV_VAR, "true")

        assert DLQSettings().open_circuit_capture_enabled is True

    def test_capture_switch_does_not_disturb_the_dlq_master_switch(self, monkeypatch):
        """Scope: this flag governs one capture trigger, not the DLQ itself."""
        monkeypatch.setenv(_ENV_VAR, "false")

        settings = DLQSettings()

        assert settings.open_circuit_capture_enabled is False
        assert settings.enabled is True
