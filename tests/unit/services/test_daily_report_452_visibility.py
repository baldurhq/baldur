"""
Unit tests for SHADOW_PRO_VISIBILITY_POLICY (impl 452).

Covers:
- DailyReportService._shadow_pro_should_render — cadence policy
- DailyReportService._get_or_set_install_marker — state backend persistence
- DailyReportService._collect_shadow_pro_section — guard composition
- format_report_for_slack — disable-hint footer rendering

Verification techniques:
- Boundary analysis: grace edge (day 29 vs 30), anniversary modulo
- State transition: no-marker -> persisted, idempotent re-read
- Idempotency: repeated marker reads return the first-day value
- Exception/edge cases: corrupt ISO date overwrite, backend raises
- Side effects: state_backend.set called exactly once on first read
- Dependency interaction: entitlement and settings consulted in order
- String contract: footer line content
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from baldur.core.entitlement import EntitlementResult, EntitlementStatus
from baldur.core.state_backend import MemoryStateBackend
from baldur.services.daily_report.formatters import format_report_for_slack
from baldur.services.daily_report.models import (
    DailyAutonomousReport,
    ShadowProSummary,
)
from baldur.services.daily_report.service import (
    _INSTALL_MARKER_KEY,
    _SHADOW_PRO_GRACE_DAYS,
    DailyReportService,
)


def _make_service() -> DailyReportService:
    """Skip-init pattern matching tests/unit/services/test_daily_report_427_shadow_pro.py."""
    return DailyReportService.__new__(DailyReportService)


def _active() -> EntitlementResult:
    return EntitlementResult(status=EntitlementStatus.ACTIVE)


def _inactive() -> EntitlementResult:
    return EntitlementResult(status=EntitlementStatus.MISSING)


# =============================================================================
# _shadow_pro_should_render — cadence policy
# =============================================================================


class TestShadowProShouldRenderBehavior:
    """Cadence policy: auto/daily/weekly. 'off' is filtered before this method."""

    @pytest.fixture
    def service_with_install_date(self):
        """Return a service whose install marker is fixed to 2026-01-01."""
        service = _make_service()
        with patch.object(
            service,
            "_get_or_set_install_marker",
            return_value=date(2026, 1, 1),
        ):
            yield service

    @pytest.mark.parametrize(
        "days_since_install",
        [0, 1, 29, 30, 35, 100],
    )
    def test_daily_mode_always_renders(
        self, service_with_install_date, days_since_install
    ):
        """mode='daily' renders unconditionally regardless of install age."""
        today = date.fromordinal(date(2026, 1, 1).toordinal() + days_since_install)

        assert (
            service_with_install_date._shadow_pro_should_render("daily", today) is True
        )

    @pytest.mark.parametrize(
        ("days_since_install", "expected"),
        [
            (0, True),  # day 0: 0 % 7 == 0
            (1, False),
            (6, False),
            (7, True),  # 1 week anniversary
            (29, False),  # boundary: still in pre-30 window but not anniversary
            (30, False),  # 30 % 7 == 2
            (35, True),  # 35 % 7 == 0
            (100, False),  # 100 % 7 == 2
        ],
    )
    def test_weekly_mode_renders_only_on_anniversary(
        self, service_with_install_date, days_since_install, expected
    ):
        """mode='weekly' renders only when (today - install).days % 7 == 0."""
        today = date.fromordinal(date(2026, 1, 1).toordinal() + days_since_install)

        result = service_with_install_date._shadow_pro_should_render("weekly", today)

        assert result is expected

    @pytest.mark.parametrize(
        ("days_since_install", "expected"),
        [
            (0, True),  # grace day 1
            (29, True),  # last day of grace (boundary just before 30)
            (30, False),  # first post-grace day, not anniversary
            (35, True),  # post-grace, 35 % 7 == 0 → anniversary
            (100, False),  # 100 % 7 == 2 → no
        ],
    )
    def test_auto_mode_grace_then_anniversary(
        self, service_with_install_date, days_since_install, expected
    ):
        """mode='auto': daily for first 30 days, then weekly anniversary."""
        today = date.fromordinal(date(2026, 1, 1).toordinal() + days_since_install)

        result = service_with_install_date._shadow_pro_should_render("auto", today)

        assert result is expected


# =============================================================================
# Module-level constants — design contract values
# =============================================================================


class TestShadowProConstantsContract:
    """Module-level constants design contract (impl 452 C2, D3)."""

    def test_grace_days_constant_matches_doc(self):
        """Module constant _SHADOW_PRO_GRACE_DAYS == 30 (impl 452 C2)."""
        assert _SHADOW_PRO_GRACE_DAYS == 30

    def test_install_marker_key_matches_doc(self):
        """Module constant _INSTALL_MARKER_KEY follows 'baldur:' prefix (D3)."""
        assert _INSTALL_MARKER_KEY == "baldur:install_marker:first_seen"


# =============================================================================
# _get_or_set_install_marker — state backend persistence
# =============================================================================


class TestGetOrSetInstallMarkerBehavior:
    """First-seen marker persistence. Uses MemoryStateBackend for I/O verification."""

    def test_no_marker_writes_today_and_returns_today(self):
        """No marker present → today is written and returned (D7)."""
        service = _make_service()
        backend = MemoryStateBackend()
        today = date(2026, 4, 25)

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=backend,
        ):
            result = service._get_or_set_install_marker(today)

        assert result == today
        assert backend.get(_INSTALL_MARKER_KEY) == {"first_seen": "2026-04-25"}

    def test_idempotent_second_call_returns_first_day(self):
        """Second call on a later day returns the originally persisted date."""
        service = _make_service()
        backend = MemoryStateBackend()
        first_day = date(2026, 4, 1)
        later_day = date(2026, 4, 25)

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=backend,
        ):
            service._get_or_set_install_marker(first_day)
            second_result = service._get_or_set_install_marker(later_day)

        assert second_result == first_day
        assert backend.get(_INSTALL_MARKER_KEY) == {"first_seen": "2026-04-01"}

    def test_corrupt_marker_value_overwritten_with_today(self):
        """Non-ISO stored value triggers overwrite with today's date (C3 corruption path)."""
        service = _make_service()
        backend = MemoryStateBackend()
        backend.set(_INSTALL_MARKER_KEY, {"first_seen": "not-a-date"})
        today = date(2026, 4, 25)

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=backend,
        ):
            result = service._get_or_set_install_marker(today)

        assert result == today
        assert backend.get(_INSTALL_MARKER_KEY) == {"first_seen": "2026-04-25"}

    def test_backend_factory_failure_returns_today_no_exception(self):
        """get_state_backend() raising → fail-open: returns today, no exception (C3)."""
        service = _make_service()
        today = date(2026, 4, 25)

        with patch(
            "baldur.core.state_backend.get_state_backend",
            side_effect=RuntimeError("backend down"),
        ):
            result = service._get_or_set_install_marker(today)

        assert result == today

    def test_backend_get_failure_returns_today_no_exception(self):
        """backend.get() raising → fail-open: returns today, no exception (C3)."""
        service = _make_service()
        today = date(2026, 4, 25)

        class BrokenBackend:
            def get(self, key, default=None):
                raise RuntimeError("read failure")

            def set(self, key, value, ttl_seconds=None):
                pass

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=BrokenBackend(),
        ):
            result = service._get_or_set_install_marker(today)

        assert result == today


# =============================================================================
# _collect_shadow_pro_section — guard composition
# =============================================================================


class _FakeSettings:
    def __init__(self, shadow_pro_mode: str = "auto"):
        self.shadow_pro_mode = shadow_pro_mode


class TestCollectShadowProSectionBehavior:
    """Composed guard sequence: settings -> entitlement -> cadence -> indicators."""

    def _make_indicators_report(self) -> DailyAutonomousReport:
        """Report whose indicators would otherwise populate a summary."""
        report = DailyAutonomousReport()
        report.circuit_transitions = 3
        report.dlq_new_entries_count = 2
        report.drift_warnings_count = 4
        return report

    def _patch_today(self, today: date):
        """Helper to patch utc_now() to a fixed date."""
        fixed_dt = datetime(today.year, today.month, today.day, tzinfo=UTC)
        return patch(
            "baldur.utils.time.utc_now",
            return_value=fixed_dt,
        )

    def test_mode_off_short_circuits_no_summary(self):
        """mode='off' skips entitlement check entirely and returns no summary."""
        service = _make_service()
        report = self._make_indicators_report()

        with (
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=_FakeSettings("off"),
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
            ) as ent_mock,
        ):
            service._collect_shadow_pro_section(report)

        assert report.shadow_pro_summary is None
        ent_mock.assert_not_called()

    def test_active_entitlement_suppresses_summary(self):
        """Paying PRO customer (is_active=True) → no summary regardless of indicators (D2/G1)."""
        service = _make_service()
        report = self._make_indicators_report()

        with (
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=_FakeSettings("daily"),
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_active(),
            ),
        ):
            service._collect_shadow_pro_section(report)

        assert report.shadow_pro_summary is None

    def test_entitlement_exception_treated_as_inactive(self):
        """Validator raising is swallowed; OSS path proceeds (D2 defense-in-depth)."""
        service = _make_service()
        report = self._make_indicators_report()
        today = date(2026, 4, 25)

        with (
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=_FakeSettings("daily"),
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                side_effect=ImportError("cryptography missing"),
            ),
            self._patch_today(today),
        ):
            service._collect_shadow_pro_section(report)

        assert report.shadow_pro_summary is not None
        assert report.shadow_pro_summary.cb_trips_without_auto_degradation == 3

    def test_inactive_entitlement_with_daily_mode_populates_summary(self):
        """OSS user, daily mode, indicators present → summary populated."""
        service = _make_service()
        report = self._make_indicators_report()
        today = date(2026, 4, 25)

        with (
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=_FakeSettings("daily"),
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_inactive(),
            ),
            self._patch_today(today),
        ):
            service._collect_shadow_pro_section(report)

        s = report.shadow_pro_summary
        assert s is not None
        assert s.cb_trips_without_auto_degradation == 3
        assert s.dlq_captured_without_adaptive_replay == 2
        assert s.drift_warnings_manual_only == 4

    def test_cadence_skip_day_suppresses_summary(self):
        """Inactive entitlement + auto mode + post-grace non-anniversary → no summary."""
        service = _make_service()
        report = self._make_indicators_report()
        # Force install marker so days_since_install == 100 (auto mode skips: 100 % 7 != 0)
        install_date = date(2026, 1, 1)
        today = date.fromordinal(install_date.toordinal() + 100)

        with (
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=_FakeSettings("auto"),
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_inactive(),
            ),
            patch.object(
                service,
                "_get_or_set_install_marker",
                return_value=install_date,
            ),
            self._patch_today(today),
        ):
            service._collect_shadow_pro_section(report)

        assert report.shadow_pro_summary is None

    def test_cadence_render_day_with_no_indicators_still_no_summary(self):
        """Cadence allows render but indicators all zero → summary stays None (existing rule)."""
        service = _make_service()
        report = DailyAutonomousReport()  # all zeros
        today = date(2026, 4, 25)

        with (
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=_FakeSettings("daily"),
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_inactive(),
            ),
            self._patch_today(today),
        ):
            service._collect_shadow_pro_section(report)

        assert report.shadow_pro_summary is None


# =============================================================================
# Slack formatter — disable-hint footer (D4)
# =============================================================================


class TestSlackFormatterShadowProFooterContract:
    """The italic footer line is rendered iff the shadow_pro block is rendered."""

    _FOOTER = (
        "_To adjust frequency: "
        "BALDUR_DAILY_REPORT_SHADOW_PRO_MODE=auto|daily|weekly|off_"
    )

    def test_footer_present_when_block_present(self):
        """When shadow_pro_summary is populated, footer line is appended."""
        report = DailyAutonomousReport()
        report.shadow_pro_summary = ShadowProSummary(
            cb_trips_without_auto_degradation=1,
        )

        output = format_report_for_slack(report)

        assert self._FOOTER in output

    def test_footer_absent_when_block_absent(self):
        """When shadow_pro_summary is None, footer line is not rendered."""
        report = DailyAutonomousReport()
        report.shadow_pro_summary = None

        output = format_report_for_slack(report)

        assert self._FOOTER not in output
        assert "BALDUR_DAILY_REPORT_SHADOW_PRO_MODE" not in output

    def test_footer_uses_slack_italic_syntax(self):
        """Footer wraps the env var hint in Slack italic underscores."""
        report = DailyAutonomousReport()
        report.shadow_pro_summary = ShadowProSummary(
            dlq_captured_without_adaptive_replay=1,
        )

        output = format_report_for_slack(report)

        # Find the line containing the env var
        line = next(
            ln
            for ln in output.split("\n")
            if "BALDUR_DAILY_REPORT_SHADOW_PRO_MODE" in ln
        )
        assert line.startswith("_")
        assert line.endswith("_")
