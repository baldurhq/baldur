"""
Mock-based integration tests for 428 Daily Report — full collection -> persistence
-> query flow.

Composition under test:
  1. Event handler / task push  -> DailyReportCollector.add_result()
  2. Aggregation                 -> aggregate_daily_results()
  3. Snapshot + automated actions + DLQ breakdown collection
  4. Persistence                 -> per-date state backend keys
                                    "baldur:daily_reports:{YYYY-MM-DD}" (430 D1)
  5. Query                       -> DailyReportDetailView / DailyReportTrendView

Infrastructure: InMemoryCacheProvider + InMemory StateBackend.
No Docker required.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from unittest.mock import patch

import pytest

pytest.importorskip("django")

from django.test import RequestFactory


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch):
    """Bypass Baldur auth so VIEWER-gated report views return 200."""
    monkeypatch.setenv("DISABLE_BALDUR_AUTH", "true")


@pytest.fixture
def cache_provider():
    from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

    return InMemoryCacheAdapter()


@pytest.fixture
def state_backend():
    from baldur.core.state_backend import MemoryStateBackend

    return MemoryStateBackend()


@pytest.fixture
def failed_operation_repo():
    from baldur.adapters.memory import InMemoryFailedOperationRepository

    return InMemoryFailedOperationRepository()


# =============================================================================
# Full-flow integration
# =============================================================================


class TestDailyReportEndToEndFlow:
    """Event push -> aggregation -> persistence -> query round trip."""

    def test_event_push_aggregates_to_report_and_persists(
        self, cache_provider, state_backend, failed_operation_repo
    ):
        """Task result pushed via collector lands in persisted umbrella dict."""
        pytest.importorskip("baldur_pro")
        from baldur.services.daily_report import (
            DailyReportService,
            get_daily_report_collector,
        )

        # Seed a DLQ pending entry so breakdown has something to aggregate
        failed_operation_repo.create(
            domain="payment", failure_type="TIMEOUT", error_message="x"
        )

        target_date = datetime.now(UTC)

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=cache_provider,
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_failed_operation_repo",
                return_value=failed_operation_repo,
            ),
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=state_backend,
            ),
            patch(
                "baldur_pro.services.chaos.reports.get_report_generator",
                side_effect=Exception("chaos unavailable"),
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                side_effect=Exception("rc unavailable"),
            ),
            patch(
                "baldur.services.metrics.updaters.update_dlq_pending_gauges",
                return_value={"payment": 1},
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
            ),
            patch(
                "baldur_pro.services.unified_notification.notify",
            ),
        ):
            # 1. Push events into collector
            collector = get_daily_report_collector()
            collector.add_result(
                task_name="cleanup_archived_entries",
                result={"archived_count": 5},
            )
            collector.add_result(
                task_name="saga_completed",
                result={"steps_executed": 3},
            )

            # 2. Service generates + persists the report
            svc = DailyReportService()
            result = svc.generate_and_send_report(date=target_date, channels=["slack"])

        assert result.success is True

        # 3. State backend has the per-date key (430 D1: no umbrella)
        date_key = target_date.strftime("%Y-%m-%d")
        persisted = state_backend.get(f"baldur:daily_reports:{date_key}")
        assert persisted is not None
        # Event-driven counter made it through aggregation
        assert persisted["archived_count"] == 5
        # Automated actions aggregated
        assert "automated_actions_summary" in persisted
        assert persisted["automated_actions_summary"]["saga_completed"] == 1
        # DLQ breakdown populated from repo.get_statistics()
        assert "dlq_pending_breakdown" in persisted
        assert persisted["dlq_pending_breakdown"]["total"] == 1

    def test_detail_api_serves_persisted_report(self, state_backend):
        """Persisted report is retrievable via DailyReportDetailView."""
        from baldur.api.django.views.daily_report import DailyReportDetailView

        date_key = "2026-04-10"
        state_backend.set(
            f"baldur:daily_reports:{date_key}",
            {
                "date": f"{date_key}T00:00:00+00:00",
                "archived_count": 9,
                "entry_count": 1,
                "entries": [
                    {
                        "task_name": "cleanup",
                        "result": {"archived_count": 9},
                        "timestamp": "2026-04-10T01:00:00+00:00",
                        "severity": "info",
                    }
                ],
            },
        )

        request = RequestFactory().get(f"/api/reports/daily/{date_key}/")

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=state_backend,
        ):
            response = DailyReportDetailView.as_view()(request, date=date_key)

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["data"]["archived_count"] == 9
        # Default (no ?detail=1) strips entries
        assert "entries" not in body["data"]

    def test_detail_api_includes_entries_when_detail_flag_set(self, state_backend):
        """?detail=1 returns per-entry context from persisted report."""
        from baldur.api.django.views.daily_report import DailyReportDetailView

        date_key = "2026-04-10"
        state_backend.set(
            f"baldur:daily_reports:{date_key}",
            {
                "date": f"{date_key}T00:00:00+00:00",
                "entries": [
                    {
                        "task_name": "dlq_item_created",
                        "result": {
                            "domain": "payment",
                            "failure_type": "TIMEOUT",
                        },
                        "timestamp": "2026-04-10T01:00:00+00:00",
                        "severity": "warning",
                    }
                ],
            },
        )

        request = RequestFactory().get(f"/api/reports/daily/{date_key}/?detail=1")

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=state_backend,
        ):
            response = DailyReportDetailView.as_view()(request, date=date_key)

        assert response.status_code == 200
        body = json.loads(response.content)
        assert "entries" in body["data"]
        assert body["data"]["entries"][0]["result"]["domain"] == "payment"

    def test_trend_api_builds_series_from_persisted_reports(self, state_backend):
        """TrendView aggregates persisted reports across the requested window."""
        from baldur.api.django.views.daily_report import DailyReportTrendView

        # Use the UTC date — the handler builds its window from utc_now(), so a
        # local today() would drift out of the window near the midnight boundary.
        today = datetime.now(UTC).date()
        date1 = today.strftime("%Y-%m-%d")
        date2 = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        state_backend.set(
            f"baldur:daily_reports:{date1}",
            {
                "date": f"{date1}T00:00:00+00:00",
                "archived_count": 10,
                "dlq_pending_count": 3,
                "entry_count": 0,
            },
        )
        state_backend.set(
            f"baldur:daily_reports:{date2}",
            {
                "date": f"{date2}T00:00:00+00:00",
                "archived_count": 2,
                "dlq_pending_count": 1,
                "entry_count": 0,
            },
        )

        request = RequestFactory().get(
            "/api/reports/daily/trend/?days=2&metrics=archived_count,dlq_pending_count"
        )

        with patch(
            "baldur.core.state_backend.get_state_backend",
            return_value=state_backend,
        ):
            response = DailyReportTrendView.as_view()(request)

        body = json.loads(response.content)
        series = body["data"]["series"]
        archived = [p["value"] for p in series["archived_count"]]
        dlq_pending = [p["value"] for p in series["dlq_pending_count"]]
        assert sorted(archived) == [2, 10]
        assert sorted(dlq_pending) == [1, 3]

        aggregates = body["data"]["aggregates"]
        assert aggregates["archived_count"]["max"] == 10
        assert aggregates["archived_count"]["min"] == 2
        assert aggregates["dlq_pending_count"]["max"] == 3

    def test_legacy_umbrella_migrated_to_per_date_keys_on_save(
        self, cache_provider, state_backend, failed_operation_repo
    ):
        """Pre-seeded legacy umbrella is migrated to per-date keys and the
        umbrella key is deleted on the first new save (430 D4)."""
        pytest.importorskip("baldur_pro")
        from baldur.services.daily_report import (
            DailyReportService,
            get_daily_report_collector,
        )

        # Use dates relative to today so they always stay within the
        # keep_reports_days retention window — absolute dates eventually age
        # past the cutoff and are correctly skipped by the migration.
        today = date_type.today()
        legacy_date_1 = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        legacy_date_2 = (today - timedelta(days=45)).strftime("%Y-%m-%d")
        legacy_umbrella = {
            legacy_date_1: {
                "date": f"{legacy_date_1}T00:00:00+00:00",
                "archived_count": 1,
                "entry_count": 0,
            },
            legacy_date_2: {
                "date": f"{legacy_date_2}T00:00:00+00:00",
                "archived_count": 2,
                "entry_count": 0,
            },
        }
        state_backend.set("baldur:daily_reports", legacy_umbrella)

        target_date = datetime.now(UTC)

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=cache_provider,
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_failed_operation_repo",
                return_value=failed_operation_repo,
            ),
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=state_backend,
            ),
            patch(
                "baldur_pro.services.chaos.reports.get_report_generator",
                side_effect=Exception("chaos unavailable"),
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                side_effect=Exception("rc unavailable"),
            ),
            patch(
                "baldur.services.metrics.updaters.update_dlq_pending_gauges",
                return_value={},
            ),
            patch(
                "baldur.core.entitlement.get_entitlement_status",
            ),
            patch(
                "baldur_pro.services.unified_notification.notify",
            ),
        ):
            collector = get_daily_report_collector()
            collector.add_result(
                task_name="cleanup_archived_entries",
                result={"archived_count": 7},
            )

            svc = DailyReportService()
            result = svc.generate_and_send_report(date=target_date, channels=["slack"])

        assert result.success is True

        # Umbrella deleted
        assert state_backend.get("baldur:daily_reports") is None

        # Both legacy dates migrated under per-date keys with original payloads
        for legacy_date, original in legacy_umbrella.items():
            migrated = state_backend.get(f"baldur:daily_reports:{legacy_date}")
            assert migrated == original

        # Today's new report also present
        today_key = target_date.strftime("%Y-%m-%d")
        today_persisted = state_backend.get(f"baldur:daily_reports:{today_key}")
        assert today_persisted is not None
        assert today_persisted["archived_count"] == 7
