"""
Unit tests for rate_limit package components.

Covers:
- RateLimitEventHistory: boundary, concurrency, idempotency, stats
- ShadowAuditLogger: file write, audit service interaction, error handling
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# RateLimitEventHistory — Behavior
# =============================================================================


class TestRateLimitEventHistoryBehavior:
    """RateLimitEventHistory ring buffer behavior verification."""

    @pytest.fixture
    def history(self):
        """Create a fresh RateLimitEventHistory instance."""
        from baldur.api.django.rate_limit.event_history import (
            RateLimitEventHistory,
        )

        return RateLimitEventHistory(max_events=5)

    def test_record_adds_recorded_at_timestamp(self, history):
        """record() adds recorded_at ISO timestamp to event."""
        history.record({"client_key": "c1", "allowed": True})
        events = history.get_events()
        assert len(events) == 1
        assert "recorded_at" in events[0]

    def test_get_events_returns_reverse_chronological(self, history):
        """get_events() returns events newest-first."""
        for i in range(3):
            history.record({"client_key": f"c{i}"})

        events = history.get_events()
        assert events[0]["client_key"] == "c2"
        assert events[2]["client_key"] == "c0"

    def test_max_capacity_evicts_oldest_entries(self, history):
        """Ring buffer evicts oldest when exceeding max_events."""
        for i in range(8):
            history.record({"client_key": f"c{i}"})

        assert history.get_count() == 5
        events = history.get_events(limit=100)
        keys = [e["client_key"] for e in events]
        assert "c0" not in keys
        assert "c7" in keys

    def test_get_events_limit_capped_at_100(self, history):
        """get_events() limit is capped at 100."""
        from baldur.api.django.rate_limit.event_history import (
            RateLimitEventHistory,
        )

        big_history = RateLimitEventHistory(max_events=200)
        for i in range(150):
            big_history.record({"client_key": f"c{i}"})

        events = big_history.get_events(limit=999)
        assert len(events) == 100

    def test_get_events_by_client_filters_correctly(self, history):
        """get_events_by_client() returns only that client's events."""
        history.record({"client_key": "a", "path": "/1"})
        history.record({"client_key": "b", "path": "/2"})
        history.record({"client_key": "a", "path": "/3"})

        events = history.get_events_by_client("a")
        assert len(events) == 2
        assert all(e["client_key"] == "a" for e in events)

    def test_reset_all_returns_count_and_clears(self, history):
        """reset(None) clears all events and returns count."""
        history.record({"client_key": "c1"})
        history.record({"client_key": "c2"})

        removed = history.reset()
        assert removed == 2
        assert history.get_count() == 0

    def test_reset_specific_client_only_removes_that_client(self, history):
        """reset(client_key) removes only that client's events."""
        history.record({"client_key": "a"})
        history.record({"client_key": "b"})
        history.record({"client_key": "a"})

        removed = history.reset("a")
        assert removed == 2
        assert history.get_count() == 1

    def test_get_client_stats_aggregates_correctly(self, history):
        """get_client_stats() counts total and exceeded per client."""
        history.record({"client_key": "a", "allowed": True})
        history.record({"client_key": "a", "allowed": False})
        history.record({"client_key": "b", "allowed": True})

        stats = history.get_client_stats()
        assert stats["a"]["total"] == 2
        assert stats["a"]["exceeded"] == 1
        assert stats["b"]["total"] == 1
        assert stats["b"]["exceeded"] == 0

    def test_concurrent_record_no_data_loss(self):
        """Multi-thread recording does not lose events."""
        from baldur.api.django.rate_limit.event_history import (
            RateLimitEventHistory,
        )

        history = RateLimitEventHistory(max_events=1000)
        errors = []

        def worker(tid):
            try:
                for i in range(50):
                    history.record({"client_key": f"t{tid}", "seq": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert history.get_count() == 500

    def test_empty_history_returns_empty_list(self, history):
        """get_events() on empty history returns empty list."""
        assert history.get_events() == []

    def test_get_count_returns_zero_on_empty(self, history):
        """get_count() returns 0 on empty history."""
        assert history.get_count() == 0


# =============================================================================
# ShadowAuditLogger — Behavior
# =============================================================================


class TestShadowAuditLoggerBehavior:
    """ShadowAuditLogger file write and audit service behavior."""

    @pytest.fixture
    def mock_request(self):
        """Create a mock Django HttpRequest."""
        req = MagicMock()
        req.path = "/api/baldur/test"
        req.method = "GET"
        return req

    @pytest.fixture
    def audit_logger(self):
        """Create a ShadowAuditLogger instance."""
        from baldur.api.django.rate_limit.shadow_audit import ShadowAuditLogger

        return ShadowAuditLogger()

    def test_log_to_file_writes_valid_json(self, audit_logger, mock_request, tmp_path):
        """_log_to_file writes valid JSONL to fallback log."""
        log_file = tmp_path / "rate_limit_fallback.jsonl"

        with patch(
            "baldur.api.django.rate_limit.shadow_audit.FALLBACK_LOG_PATH",
            log_file,
        ):
            audit_logger._log_to_file(
                mock_request, True, 10, "127.0.0.1", "Redis failure"
            )

        content = log_file.read_text(encoding="utf-8").strip()
        entry = json.loads(content)
        assert entry["event"] == "rate_limit_emergency"
        assert entry["mode"] == "REDIS_FAILURE_BYPASS"
        assert entry["allowed"] is True
        assert entry["path"] == "/api/baldur/test"
        assert entry["client_ip"] == "127.0.0.1"
        assert entry["emergency_limit"] == 10

    def test_log_to_file_graceful_on_write_error(self, audit_logger, mock_request):
        """_log_to_file does not raise when file write fails."""
        with patch(
            "baldur.api.django.rate_limit.shadow_audit.FALLBACK_LOG_PATH"
        ) as mock_path:
            mock_path.parent.mkdir.side_effect = PermissionError("denied")
            # Should not raise
            audit_logger._log_to_file(
                mock_request, True, 10, "127.0.0.1", "Redis failure"
            )

    def test_log_to_audit_service_calls_log_config_change(
        self, audit_logger, mock_request
    ):
        """_log_to_audit_service calls audit.log_config_change with correct args."""
        mock_log_fn = MagicMock()
        mock_audit_module = MagicMock(log_config_change=mock_log_fn)

        with patch.dict("sys.modules", {"baldur.audit": mock_audit_module}):
            audit_logger._log_to_audit_service(
                mock_request, False, 10, "10.0.0.1", "Redis down"
            )

        mock_log_fn.assert_called_once()
        call_kwargs = mock_log_fn.call_args[1]
        assert call_kwargs["config_type"] == "rate_limit_emergency"
        assert call_kwargs["new_value"] == "REDIS_FAILURE_BYPASS"
        assert call_kwargs["metadata"]["allowed"] is False
        assert call_kwargs["metadata"]["client_ip"] == "10.0.0.1"

    def test_log_to_audit_service_graceful_on_import_error(
        self, audit_logger, mock_request
    ):
        """_log_to_audit_service does not raise when audit module unavailable."""
        with patch.dict("sys.modules", {"baldur.audit": None}):
            # Should not raise
            audit_logger._log_to_audit_service(
                mock_request, True, 10, "127.0.0.1", "Redis failure"
            )

    def test_log_rate_limit_event_calls_both_outputs(self, audit_logger, mock_request):
        """log_rate_limit_event calls both file and audit service."""
        with (
            patch.object(audit_logger, "_log_to_file") as mock_file,
            patch.object(audit_logger, "_log_to_audit_service") as mock_audit,
        ):
            audit_logger.log_rate_limit_event(mock_request, True, 10, "127.0.0.1")

            mock_file.assert_called_once()
            mock_audit.assert_called_once()
