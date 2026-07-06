"""
ErrorBudgetGateProbe unit tests.

Covers:
- Contract: component_name, judgment matrix status/reason values per 411 XP7
- Behavior: state transition (10 branches), staleness detection, ImportError
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.meta.error_budget_gate_probe import ErrorBudgetGateProbe
from baldur.meta.health_probe import HealthStatus


def _make_passive_health(
    enabled: bool = True,
    effective_status: str = "open",
    current_status: str = "open",
    fault_detector_state: str = "healthy",
    fault_detector_failures: int = 0,
    fail_open_triggered: bool = False,
    last_checked_at: str | None = None,
) -> dict:
    return {
        "enabled": enabled,
        "effective_status": effective_status,
        "current_status": current_status,
        "fault_detector_state": fault_detector_state,
        "fault_detector_failures": fault_detector_failures,
        "fail_open_triggered": fail_open_triggered,
        "last_checked_at": last_checked_at,
    }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def probe():
    return ErrorBudgetGateProbe()


@pytest.fixture
def mock_meta_settings():
    s = MagicMock()
    s.probe_cache_staleness_multiplier = 2.0
    return s


@pytest.fixture
def mock_gate_settings():
    s = MagicMock()
    s.cache_ttl_seconds = 30
    return s


def _patch_probe(passive_health, mock_meta_settings, mock_gate_settings, now):
    """Context manager stack for common probe mocking."""
    return (
        patch("baldur_pro.services.error_budget_gate.gate.get_error_budget_gate"),
        patch(
            "baldur.meta.config.get_meta_watchdog_settings",
            return_value=mock_meta_settings,
        ),
        patch(
            "baldur.settings.error_budget_gate.get_error_budget_gate_settings",
            return_value=mock_gate_settings,
        ),
        patch(
            "baldur.meta.error_budget_gate_probe.utc_now",
            return_value=now,
        ),
    )


def _run_probe(probe, passive_health, mock_meta_settings, mock_gate_settings, now):
    """Execute probe with all dependencies mocked."""
    patches = _patch_probe(passive_health, mock_meta_settings, mock_gate_settings, now)
    with patches[0] as mock_gate_fn, patches[1], patches[2], patches[3]:
        mock_gate_fn.return_value.get_passive_health.return_value = passive_health
        return probe.probe()


class TestErrorBudgetGateProbeContract:
    """Contract verification for ErrorBudgetGateProbe design values."""

    def test_component_name_is_error_budget_gate(self, probe):
        """component_name must be 'error_budget_gate' per 411 XP7."""
        assert probe.component_name == "error_budget_gate"

    def test_disabled_gate_returns_healthy_with_reason(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """Disabled gate → HEALTHY with exact reason."""
        pytest.importorskip("baldur_pro")
        now = _utc_now()
        health = _make_passive_health(enabled=False)
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.HEALTHY
        assert result.reason == "Gate disabled by configuration"

    def test_fail_open_rate_limited_returns_unhealthy(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """FAIL_OPEN_RATE_LIMITED → UNHEALTHY."""
        pytest.importorskip("baldur_pro")
        now = _utc_now()
        checked = now - timedelta(seconds=5)
        health = _make_passive_health(
            effective_status="fail_open_rate_limited",
            fail_open_triggered=True,
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.UNHEALTHY
        assert "rate limit exceeded" in result.reason.lower()

    def test_fail_open_returns_degraded(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """FAIL_OPEN → DEGRADED."""
        pytest.importorskip("baldur_pro")
        now = _utc_now()
        checked = now - timedelta(seconds=5)
        health = _make_passive_health(
            effective_status="fail_open",
            fail_open_triggered=True,
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.DEGRADED
        assert "fail-open" in result.reason.lower()

    def test_blocked_healthy_fd_returns_degraded_with_budget_reason(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """BLOCKED + healthy FD → DEGRADED (low budget, not gate failure)."""
        pytest.importorskip("baldur_pro")
        now = _utc_now()
        checked = now - timedelta(seconds=5)
        health = _make_passive_health(
            effective_status="blocked",
            fault_detector_state="healthy",
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.DEGRADED
        assert "low error budget" in result.reason.lower()

    def test_import_error_returns_unknown(self, probe):
        """ImportError → UNKNOWN with error."""
        pytest.importorskip("baldur_pro")
        with patch(
            "baldur_pro.services.error_budget_gate.gate.get_error_budget_gate",
            side_effect=ImportError("no module"),
        ):
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert result.error == "error_budget_gate module not available"


class TestErrorBudgetGateProbeBehavior:
    """Behavior verification for judgment logic."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_open_healthy_fd_fresh_returns_healthy(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """OPEN + healthy FD + fresh → HEALTHY."""
        now = _utc_now()
        checked = now - timedelta(seconds=10)
        health = _make_passive_health(
            effective_status="open",
            fault_detector_state="healthy",
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.HEALTHY
        assert result.reason == ""

    def test_warning_healthy_fd_fresh_returns_healthy(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """WARNING + healthy FD + fresh → HEALTHY."""
        now = _utc_now()
        checked = now - timedelta(seconds=10)
        health = _make_passive_health(
            effective_status="warning",
            fault_detector_state="healthy",
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.HEALTHY

    def test_open_degraded_fd_returns_degraded(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """OPEN + degraded FD → DEGRADED."""
        now = _utc_now()
        checked = now - timedelta(seconds=10)
        health = _make_passive_health(
            effective_status="open",
            fault_detector_state="degraded",
            fault_detector_failures=3,
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.DEGRADED
        assert "fault detector" in result.reason.lower()
        assert "3 failures" in result.reason

    def test_blocked_degraded_fd_returns_degraded(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """BLOCKED + degraded FD → DEGRADED (both issues)."""
        now = _utc_now()
        checked = now - timedelta(seconds=5)
        health = _make_passive_health(
            effective_status="blocked",
            fault_detector_state="recovering",
            fault_detector_failures=2,
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.DEGRADED
        assert "recovering" in result.reason

    def test_stale_last_checked_returns_unknown(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """Stale last_checked_at → UNKNOWN (false positive prevention)."""
        now = _utc_now()
        # staleness_threshold = 30 * 2.0 = 60s, checked 120s ago
        checked = now - timedelta(seconds=120)
        health = _make_passive_health(
            effective_status="open",
            fault_detector_state="healthy",
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.UNKNOWN
        assert "idle" in result.reason.lower()

    def test_never_checked_returns_unknown(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """last_checked_at is None → UNKNOWN (stale)."""
        now = _utc_now()
        health = _make_passive_health(
            effective_status="open",
            fault_detector_state="healthy",
            last_checked_at=None,
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.UNKNOWN
        # Never-checked branch: a distinct human-readable reason, not the
        # elapsed-minutes one (which would render a nonsensical "unknownm").
        assert "never evaluated" in result.reason.lower()
        assert "unknownm" not in result.reason.lower()

    def test_staleness_boundary_at_exact_threshold_is_not_stale(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """Elapsed == threshold → not stale (> not >=)."""
        now = _utc_now()
        # staleness_threshold = 30 * 2.0 = 60s, checked exactly 60s ago
        checked = now - timedelta(seconds=60)
        health = _make_passive_health(
            effective_status="open",
            fault_detector_state="healthy",
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.HEALTHY

    def test_details_is_passive_health_dict(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """Probe details should be the exact get_passive_health() dict."""
        now = _utc_now()
        checked = now - timedelta(seconds=5)
        health = _make_passive_health(
            effective_status="open",
            fault_detector_state="healthy",
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.details == health

    def test_unexpected_status_returns_unknown(
        self, probe, mock_meta_settings, mock_gate_settings
    ):
        """Unknown effective_status value → UNKNOWN."""
        now = _utc_now()
        checked = now - timedelta(seconds=5)
        health = _make_passive_health(
            effective_status="some_future_status",
            last_checked_at=_iso(checked),
        )
        result = _run_probe(probe, health, mock_meta_settings, mock_gate_settings, now)

        assert result.status == HealthStatus.UNKNOWN
        assert "some_future_status" in result.reason

    def test_general_exception_returns_unknown_with_error(self, probe):
        """Unexpected exception → UNKNOWN with error message."""
        with patch(
            "baldur_pro.services.error_budget_gate.gate.get_error_budget_gate",
            side_effect=RuntimeError("boom"),
        ):
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert "boom" in result.error
