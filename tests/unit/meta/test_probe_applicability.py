"""Probe applicability: disabled features are skipped, not reported HEALTHY.

A default-disabled feature (chaos scheduler, error budget gate) has nothing to
monitor — no chaos experiment can become a zombie while chaos is off, a disabled
gate blocks nothing — so its probe must be skipped and the component absent from
``component_statuses`` entirely. Showing it as HEALTHY surfaces a not-yet-active
feature in the operator console as if it were functioning.

Covers:
- HealthProbe.is_applicable() default contract
- ChaosSchedulerProbe / ErrorBudgetGateProbe override mirrors the enabled flag
- HealthProbeManager.probe_all() skips non-applicable probes (per-deployment)
- _probe_is_applicable() fail-safe (no method / raising → applicable)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from baldur.meta.audit_probe import AuditSystemProbe
from baldur.meta.error_budget_gate_probe import ErrorBudgetGateProbe
from baldur.meta.health_probe import (
    ChaosSchedulerProbe,
    HealthProbe,
    HealthProbeManager,
    HealthStatus,
    ProbeResult,
    _probe_is_applicable,
)


class _DummyProbe(HealthProbe):
    """Minimal probe with a controllable applicability verdict."""

    def __init__(self, name: str, applicable: bool | Exception = True):
        self._name = name
        self._applicable = applicable

    @property
    def component_name(self) -> str:
        return self._name

    def probe(self) -> ProbeResult:
        return ProbeResult(
            component=self._name,
            status=HealthStatus.HEALTHY,
            latency_ms=0.0,
            timestamp=datetime.now(UTC),
        )

    def is_applicable(self) -> bool:
        if isinstance(self._applicable, Exception):
            raise self._applicable
        return self._applicable


class _NoApplicabilityProbe(HealthProbe):
    """Structural probe that does NOT declare is_applicable (like AuditSystemProbe)."""

    @property
    def component_name(self) -> str:
        return "structural"

    def probe(self) -> ProbeResult:
        return ProbeResult(
            component="structural",
            status=HealthStatus.HEALTHY,
            latency_ms=0.0,
            timestamp=datetime.now(UTC),
        )


class TestBaseContract:
    def test_default_is_applicable_is_true(self):
        """A probe that does not override is_applicable() is always applicable."""
        assert _NoApplicabilityProbe().is_applicable() is True


class TestChaosSchedulerApplicability:
    def test_applicable_mirrors_enabled_flag_true(self):
        with patch(
            "baldur.settings.chaos.get_chaos_settings",
            return_value=MagicMock(enabled=True),
        ):
            assert ChaosSchedulerProbe().is_applicable() is True

    def test_applicable_mirrors_enabled_flag_false(self):
        with patch(
            "baldur.settings.chaos.get_chaos_settings",
            return_value=MagicMock(enabled=False),
        ):
            assert ChaosSchedulerProbe().is_applicable() is False


class TestErrorBudgetGateApplicability:
    def test_applicable_mirrors_enabled_flag_true(self):
        with patch(
            "baldur.settings.error_budget_gate.get_error_budget_gate_settings",
            return_value=MagicMock(enabled=True),
        ):
            assert ErrorBudgetGateProbe().is_applicable() is True

    def test_applicable_mirrors_enabled_flag_false(self):
        with patch(
            "baldur.settings.error_budget_gate.get_error_budget_gate_settings",
            return_value=MagicMock(enabled=False),
        ):
            assert ErrorBudgetGateProbe().is_applicable() is False


class TestAuditSystemApplicability:
    """Audit is opt-in (master switch off by default); a disabled audit
    subsystem must be skipped, not reported UNHEALTHY for the missing WAL."""

    def test_applicable_mirrors_enabled_flag_true(self):
        with patch(
            "baldur.settings.audit.get_audit_settings",
            return_value=MagicMock(enabled=True),
        ):
            assert AuditSystemProbe().is_applicable() is True

    def test_applicable_mirrors_enabled_flag_false(self):
        with patch(
            "baldur.settings.audit.get_audit_settings",
            return_value=MagicMock(enabled=False),
        ):
            assert AuditSystemProbe().is_applicable() is False

    def test_disabled_audit_skipped_by_helper_despite_structural_probe(self):
        """AuditSystemProbe does not inherit the ABC; the duck-typed helper must
        still honor its is_applicable() verdict."""
        with patch(
            "baldur.settings.audit.get_audit_settings",
            return_value=MagicMock(enabled=False),
        ):
            assert _probe_is_applicable(AuditSystemProbe()) is False


class TestProbeAllSkipsDisabled:
    def test_non_applicable_probe_absent_from_results(self):
        manager = HealthProbeManager(
            probes=[
                _DummyProbe("on", applicable=True),
                _DummyProbe("off", applicable=False),
            ]
        )

        results = manager.probe_all()

        assert "on" in results
        assert "off" not in results

    def test_enabling_a_probe_makes_it_reappear(self):
        """Per-deployment, not a static tier hide: enabled ⇒ present."""
        manager = HealthProbeManager(probes=[_DummyProbe("feature", applicable=False)])
        assert "feature" not in manager.probe_all()

        manager._probes[0]._applicable = True
        assert "feature" in manager.probe_all()


class TestApplicabilityHelperFailSafe:
    def test_probe_without_method_is_applicable(self):
        assert _probe_is_applicable(_NoApplicabilityProbe()) is True

    def test_raising_is_applicable_falls_back_to_true(self):
        """A transient settings-read failure must never silently hide a component."""
        probe = _DummyProbe("flaky", applicable=RuntimeError("settings unavailable"))
        assert _probe_is_applicable(probe) is True
