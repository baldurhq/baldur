"""
Unit tests for 428 Daily Report handlers (Phase 4 / D8).

Post-429 Phase 2a: the Django views are thin HandlerAPIView wrappers;
the real business logic lives in ``baldur.api.handlers.daily_report``.
Tests exercise the handler functions directly via RequestContext.

430: the umbrella-load loader is replaced by three tailored loaders —
``_load_recent_reports`` (list), ``_load_report_for_date`` (detail),
``_load_reports_in_window`` (trend). Availability is checked via
``_get_state_backend_or_none`` (returns None -> 503).

Test targets:
  - daily_report_list — pagination, days clamping, entries stripping.
  - daily_report_detail — date validation (400/404), detail flag.
  - daily_report_trend — missing-date omission, metric filtering,
    non-numeric skip, default metric set.
  - _aggregate — min/max/avg/p95 edge cases.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.daily_report import (
    _DEFAULT_TREND_METRICS,
    _aggregate,
    _BackendUnavailable,
    _load_recent_reports,
    _load_report_for_date,
    _load_reports_in_window,
    daily_report_detail,
    daily_report_list,
    daily_report_trend,
)
from baldur.core.state_backend import MemoryStateBackend
from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
)
from baldur.services.daily_report.service import _STATE_BACKEND_KEY_PREFIX
from baldur.utils.time import utc_now

# =============================================================================
# Shared helpers
# =============================================================================


def _make_ctx(method="GET", path="/test/", query=None, path_params=None):
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params=path_params or {},
    )


def _sample_report(
    date_str: str, archived_count: int = 0, dlq_pending_count: int = 0
) -> dict:
    """Shape matches DailyAutonomousReport.to_dict(include_entries=True)."""
    return {
        "date": f"{date_str}T00:00:00+00:00",
        "archived_count": archived_count,
        "expired_count": 0,
        "purged_count": 0,
        "recovered_count": 0,
        "circuit_transitions": 0,
        "dlq_pending_count": dlq_pending_count,
        "dlq_new_entries_count": 0,
        "dlq_resolved_count": 0,
        "task_failures": 0,
        "entry_count": 1,
        "entries": [
            {"task_name": "x", "result": {}, "timestamp": "t", "severity": "info"}
        ],
    }


def _patch_backend_ok():
    """Patch backend availability check to return a dummy backend (non-None)."""
    return patch(
        "baldur.api.handlers.daily_report._get_state_backend_or_none",
        return_value=MagicMock(),
    )


# =============================================================================
# daily_report_list — Behavior tests
# =============================================================================


class TestDailyReportListBehavior:
    """Pagination, clamping, entries stripping."""

    def test_empty_state_returns_empty_data(self):
        """No persisted reports -> data=[], count=0."""
        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_recent_reports",
                return_value={},
            ),
        ):
            response = daily_report_list(_make_ctx())

        assert response.body == {"status": "success", "count": 0, "data": []}

    def test_returns_most_recent_days(self):
        """sort desc, take top N."""
        reports = {
            "2026-04-01": _sample_report("2026-04-01", archived_count=1),
            "2026-04-05": _sample_report("2026-04-05", archived_count=5),
            "2026-04-10": _sample_report("2026-04-10", archived_count=10),
        }

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_recent_reports",
                return_value={
                    "2026-04-10": reports["2026-04-10"],
                    "2026-04-05": reports["2026-04-05"],
                },
            ),
        ):
            response = daily_report_list(_make_ctx(query={"days": "2"}))

        assert response.body["count"] == 2
        returned_dates = {item["date"][:10] for item in response.body["data"]}
        assert returned_dates == {"2026-04-10", "2026-04-05"}

    def test_response_strips_entries(self):
        """Summary view must not include per-entry detail."""
        reports = {"2026-04-10": _sample_report("2026-04-10")}

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_recent_reports",
                return_value=reports,
            ),
        ):
            response = daily_report_list(_make_ctx())

        for item in response.body["data"]:
            assert "entries" not in item

    def test_days_param_clamped_to_keep_reports_days(self):
        """days query value capped by configured keep_reports_days."""
        # Loader receives clamped days — simulate it returning up to 3 reports.
        reports = {
            f"2026-04-{i:02d}": _sample_report(f"2026-04-{i:02d}") for i in range(1, 4)
        }

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_recent_reports",
                return_value=reports,
            ),
            patch(
                "baldur.api.handlers.daily_report._get_keep_days",
                return_value=3,
            ),
        ):
            response = daily_report_list(_make_ctx(query={"days": "9999"}))

        # keep_days=3 caps the result to 3 dates regardless of requested days
        assert response.body["count"] == 3

    def test_days_param_negative_clamped_to_one(self):
        """days<1 -> clamped to 1 (boundary)."""
        reports = {"2026-04-01": _sample_report("2026-04-01")}

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_recent_reports",
                return_value=reports,
            ),
        ):
            response = daily_report_list(_make_ctx(query={"days": "-5"}))

        assert response.body["count"] == 1


# =============================================================================
# daily_report_detail — Behavior tests
# =============================================================================


class TestDailyReportDetailBehavior:
    """Date validation and detail flag."""

    def test_invalid_date_returns_400(self):
        """Non-YYYY-MM-DD date -> 400 Bad Request."""
        response = daily_report_detail(_make_ctx(path_params={"date": "not-a-date"}))

        assert response.status_code == 400
        assert response.body["status"] == "error"

    def test_missing_report_returns_404(self):
        """Valid date but no persisted report -> 404."""
        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_report_for_date",
                return_value=None,
            ),
        ):
            response = daily_report_detail(
                _make_ctx(path_params={"date": "2026-04-10"})
            )

        assert response.status_code == 404

    def test_default_detail_flag_strips_entries(self):
        """No ?detail= -> entries omitted (summary)."""
        report_dict = _sample_report("2026-04-10")

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_report_for_date",
                return_value=report_dict,
            ),
        ):
            response = daily_report_detail(
                _make_ctx(path_params={"date": "2026-04-10"})
            )

        assert "entries" not in response.body["data"]

    @pytest.mark.parametrize("detail_value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_detail_flag_truthy_values_include_entries(self, detail_value):
        """detail=1 / true / yes (case-insensitive) -> entries included."""
        report_dict = _sample_report("2026-04-10")

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_report_for_date",
                return_value=report_dict,
            ),
        ):
            response = daily_report_detail(
                _make_ctx(
                    query={"detail": detail_value},
                    path_params={"date": "2026-04-10"},
                )
            )

        assert "entries" in response.body["data"]

    @pytest.mark.parametrize("detail_value", ["0", "false", "no", "", "foo"])
    def test_detail_flag_non_truthy_strips_entries(self, detail_value):
        """detail= value not in truthy set -> entries stripped."""
        report_dict = _sample_report("2026-04-10")

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_report_for_date",
                return_value=report_dict,
            ),
        ):
            response = daily_report_detail(
                _make_ctx(
                    query={"detail": detail_value},
                    path_params={"date": "2026-04-10"},
                )
            )

        assert "entries" not in response.body["data"]


# =============================================================================
# daily_report_trend — Behavior tests
# =============================================================================


class TestDailyReportTrendBehavior:
    """Series construction, missing-date handling, metric filtering."""

    def test_missing_dates_are_omitted_from_series(self):
        """Dates without a report don't appear in series."""
        today = utc_now().date()
        present = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        reports = {present: _sample_report(present, archived_count=7)}

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_reports_in_window",
                return_value=reports,
            ),
        ):
            response = daily_report_trend(
                _make_ctx(query={"days": "3", "metrics": "archived_count"})
            )

        series = response.body["data"]["series"]
        points = series["archived_count"]
        assert len(points) == 1
        assert points[0]["date"] == present
        assert points[0]["value"] == 7

    def test_default_metrics_used_when_not_specified(self):
        """No metrics query param -> defaults applied."""
        today = utc_now().date().strftime("%Y-%m-%d")
        reports = {today: _sample_report(today)}

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_reports_in_window",
                return_value=reports,
            ),
        ):
            response = daily_report_trend(_make_ctx(query={"days": "1"}))

        series_keys = set(response.body["data"]["series"].keys())
        assert series_keys == set(_DEFAULT_TREND_METRICS)

    def test_non_numeric_metric_values_are_skipped(self):
        """Metric values that are not int/float never make it to the series."""
        today = utc_now().date().strftime("%Y-%m-%d")
        report_with_string = _sample_report(today)
        report_with_string["archived_count"] = "not-a-number"
        reports = {today: report_with_string}

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_reports_in_window",
                return_value=reports,
            ),
        ):
            response = daily_report_trend(
                _make_ctx(query={"days": "1", "metrics": "archived_count"})
            )

        assert response.body["data"]["series"]["archived_count"] == []

    def test_period_reflects_requested_window(self):
        """period.days matches requested, from/to cover the window."""
        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_reports_in_window",
                return_value={},
            ),
        ):
            response = daily_report_trend(_make_ctx(query={"days": "5"}))

        period = response.body["data"]["period"]
        assert period["days"] == 5
        expected_to = utc_now().date().strftime("%Y-%m-%d")
        expected_from = (utc_now().date() - timedelta(days=4)).strftime("%Y-%m-%d")
        assert period["to"] == expected_to
        assert period["from"] == expected_from

    def test_aggregates_computed_for_each_metric(self):
        """Each metric in the series gets a matching aggregates entry."""
        today = utc_now().date()
        date_1 = today.strftime("%Y-%m-%d")
        date_2 = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        reports = {
            date_1: _sample_report(date_1, archived_count=10),
            date_2: _sample_report(date_2, archived_count=4),
        }

        with (
            _patch_backend_ok(),
            patch(
                "baldur.api.handlers.daily_report._load_reports_in_window",
                return_value=reports,
            ),
        ):
            response = daily_report_trend(
                _make_ctx(query={"days": "2", "metrics": "archived_count"})
            )

        agg = response.body["data"]["aggregates"]["archived_count"]
        assert agg["min"] == 4
        assert agg["max"] == 10
        assert agg["avg"] == 7.0


# =============================================================================
# _aggregate — Boundary tests
# =============================================================================


class TestAggregateBehavior:
    """Percentile math and boundary values."""

    def test_empty_list_returns_zero_defaults(self):
        """Empty input -> all zeros (no division)."""
        agg = _aggregate([])
        assert agg == {"min": 0, "max": 0, "avg": 0.0, "p95": 0}

    def test_single_value_returns_same_for_all(self):
        """Single value -> min==max==p95==value."""
        agg = _aggregate([42])
        assert agg["min"] == 42
        assert agg["max"] == 42
        assert agg["p95"] == 42
        assert agg["avg"] == 42.0

    def test_avg_rounded_to_four_decimals(self):
        """avg rounded to 4 decimals."""
        agg = _aggregate([1, 2, 3])
        assert agg["avg"] == 2.0

        agg2 = _aggregate([1, 2])
        assert agg2["avg"] == 1.5

    def test_p95_sorts_input_before_indexing(self):
        """Input order doesn't matter — sorted internally."""
        unsorted = [5, 1, 9, 2, 7, 3, 8, 4, 6, 10]
        sorted_vals = sorted(unsorted)
        agg = _aggregate(unsorted)
        # p95 index for N=10: round(0.95*9) = 9 -> sorted[9] = 10
        assert agg["p95"] == sorted_vals[9]

    def test_p95_clamped_to_valid_range(self):
        """p95_idx is clamped to [0, n-1]."""
        # N=2: p95_idx = round(0.95*1) = 1 -> sorted[1] = larger value
        agg = _aggregate([3, 7])
        assert agg["p95"] == 7
        assert agg["min"] == 3
        assert agg["max"] == 7


# =============================================================================
# Loader integration with real MemoryStateBackend (430 D2)
# =============================================================================


def _seed(backend: MemoryStateBackend, date_str: str, archived_count: int) -> None:
    backend.set(
        f"{_STATE_BACKEND_KEY_PREFIX}{date_str}",
        _sample_report(date_str, archived_count=archived_count),
    )


class TestLoadReportForDateBehavior:
    """_load_report_for_date — single per-date GET."""

    def test_returns_report_when_key_exists(self):
        """Pre-seeded per-date key is returned as-is."""
        backend = MemoryStateBackend()
        _seed(backend, "2026-04-10", archived_count=13)

        result = _load_report_for_date(backend, "2026-04-10")

        assert result is not None
        assert result["archived_count"] == 13

    def test_returns_none_when_key_absent(self):
        """Missing per-date key -> None (no default injection)."""
        backend = MemoryStateBackend()

        result = _load_report_for_date(backend, "2026-04-10")

        assert result is None

    def test_raises_backend_unavailable_on_exception(self):
        """backend.get() raising -> _BackendUnavailable (so handler 503s)."""
        mock_backend = MagicMock()
        mock_backend.get.side_effect = RuntimeError("redis down")

        with pytest.raises(_BackendUnavailable):
            _load_report_for_date(mock_backend, "2026-04-10")


class TestLoadReportsInWindowBehavior:
    """_load_reports_in_window — iterate exact date window."""

    def test_missing_dates_are_skipped(self):
        """Gaps within the window are omitted from the result dict."""
        backend = MemoryStateBackend()
        today = date_type(2026, 4, 10)
        _seed(backend, "2026-04-09", archived_count=1)
        # 2026-04-10 intentionally missing
        _seed(backend, "2026-04-08", archived_count=3)

        result = _load_reports_in_window(backend, today, days=3)

        assert set(result.keys()) == {"2026-04-08", "2026-04-09"}

    def test_loads_exactly_requested_window(self):
        """Window covers `today - (days-1) .. today` inclusive."""
        backend = MemoryStateBackend()
        today = date_type(2026, 4, 10)
        # Seed 5 days but request only 3 — expect the 3 most recent.
        for d in ("2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09", "2026-04-10"):
            _seed(backend, d, archived_count=int(d[-2:]))

        result = _load_reports_in_window(backend, today, days=3)

        assert set(result.keys()) == {"2026-04-08", "2026-04-09", "2026-04-10"}

    def test_raises_backend_unavailable_on_exception(self):
        """backend.get() raising -> _BackendUnavailable."""
        mock_backend = MagicMock()
        mock_backend.get.side_effect = RuntimeError("redis down")

        with pytest.raises(_BackendUnavailable):
            _load_reports_in_window(mock_backend, date_type(2026, 4, 10), days=3)


class TestLoadRecentReportsBehavior:
    """_load_recent_reports — backward scan with early-stop on N collected."""

    def test_early_stops_when_days_collected(self):
        """Stops scanning once `days` non-empty reports are accumulated."""
        backend = MemoryStateBackend()
        today = date_type(2026, 4, 10)
        # Dense recent window — 5 contiguous days.
        for i in range(5):
            date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            _seed(backend, date_str, archived_count=i)

        result = _load_recent_reports(backend, today, days=3, keep_days=90)

        # Collected exactly `days` most-recent dates.
        assert len(result) == 3
        expected = {(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)}
        assert set(result.keys()) == expected

    def test_skips_gaps_within_keep_days_window(self):
        """Non-contiguous history: gaps are skipped, scan continues until N
        collected or keep_days exhausted."""
        backend = MemoryStateBackend()
        today = date_type(2026, 4, 10)
        # Seed only days 0, 2, 4 back (gaps at 1, 3).
        for i in (0, 2, 4):
            date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            _seed(backend, date_str, archived_count=i)

        result = _load_recent_reports(backend, today, days=3, keep_days=90)

        expected_dates = {
            (today - timedelta(days=i)).strftime("%Y-%m-%d") for i in (0, 2, 4)
        }
        assert set(result.keys()) == expected_dates

    def test_returns_partial_when_keep_days_exhausted(self):
        """If `keep_days` is reached before collecting `days` reports,
        returns what was found without going further back."""
        backend = MemoryStateBackend()
        today = date_type(2026, 4, 10)
        # Only one report inside the keep_days window.
        _seed(backend, "2026-04-10", archived_count=0)

        result = _load_recent_reports(backend, today, days=5, keep_days=3)

        assert set(result.keys()) == {"2026-04-10"}

    def test_raises_backend_unavailable_on_exception(self):
        """backend.get() raising -> _BackendUnavailable."""
        mock_backend = MagicMock()
        mock_backend.get.side_effect = RuntimeError("redis down")

        with pytest.raises(_BackendUnavailable):
            _load_recent_reports(
                mock_backend, date_type(2026, 4, 10), days=3, keep_days=90
            )


class TestHandlerBackendUnavailableBehavior:
    """503 is returned whenever the state backend is unreachable — both at
    singleton init (``get_state_backend`` raising) and mid-request
    (``backend.get`` raising). The latter was silently masked as 404/empty
    before 430 and is now surfaced as 503 (see ``_BackendUnavailable``)."""

    def test_list_returns_503_when_backend_factory_raises(self):
        """daily_report_list surfaces backend init failure as 503."""
        with patch(
            "baldur.core.state_backend.get_state_backend",
            side_effect=RuntimeError("backend init failed"),
        ):
            response = daily_report_list(_make_ctx())

        assert response.status_code == 503

    def test_detail_returns_503_when_backend_factory_raises(self):
        """daily_report_detail surfaces backend init failure as 503 (after
        date validation passes)."""
        with patch(
            "baldur.core.state_backend.get_state_backend",
            side_effect=RuntimeError("backend init failed"),
        ):
            response = daily_report_detail(
                _make_ctx(path_params={"date": "2026-04-10"})
            )

        assert response.status_code == 503

    def test_trend_returns_503_when_backend_factory_raises(self):
        """daily_report_trend surfaces backend init failure as 503."""
        with patch(
            "baldur.core.state_backend.get_state_backend",
            side_effect=RuntimeError("backend init failed"),
        ):
            response = daily_report_trend(_make_ctx(query={"days": "7"}))

        assert response.status_code == 503

    def test_list_returns_503_when_backend_get_raises_mid_request(self):
        """daily_report_list surfaces mid-request backend failure as 503
        (not as count=0). get_state_backend() succeeds; the subsequent
        backend.get() raises."""
        mock_backend = MagicMock()
        mock_backend.get.side_effect = RuntimeError("redis connection lost")

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=mock_backend,
        ):
            response = daily_report_list(_make_ctx())

        assert response.status_code == 503

    def test_detail_returns_503_when_backend_get_raises_mid_request(self):
        """daily_report_detail surfaces mid-request backend failure as 503
        (not as 404 Report-not-found)."""
        mock_backend = MagicMock()
        mock_backend.get.side_effect = RuntimeError("redis connection lost")

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=mock_backend,
        ):
            response = daily_report_detail(
                _make_ctx(path_params={"date": "2026-04-10"})
            )

        assert response.status_code == 503

    def test_trend_returns_503_when_backend_get_raises_mid_request(self):
        """daily_report_trend surfaces mid-request backend failure as 503
        (not as an empty trend)."""
        mock_backend = MagicMock()
        mock_backend.get.side_effect = RuntimeError("redis connection lost")

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=mock_backend,
        ):
            response = daily_report_trend(_make_ctx(query={"days": "7"}))

        assert response.status_code == 503


class TestLoaderExceptionChainingBehavior:
    """Loaders preserve the original backend error via exception chaining
    (`raise _BackendUnavailable from e`) so operators can diagnose the root
    cause from the traceback — not just see a generic 'backend unavailable'."""

    @pytest.mark.parametrize(
        ("loader", "args"),
        [
            (_load_report_for_date, ("2026-04-10",)),
            (_load_reports_in_window, (date_type(2026, 4, 10), 3)),
            (_load_recent_reports, (date_type(2026, 4, 10), 3, 90)),
        ],
        ids=["load_report_for_date", "load_reports_in_window", "load_recent_reports"],
    )
    def test_loader_backend_unavailable_chains_root_cause(self, loader, args):
        """__cause__ on the raised _BackendUnavailable is the original exception."""
        mock_backend = MagicMock()
        original = RuntimeError("redis connection refused")
        mock_backend.get.side_effect = original

        with pytest.raises(_BackendUnavailable) as exc_info:
            loader(mock_backend, *args)

        assert exc_info.value.__cause__ is original
