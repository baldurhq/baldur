"""
get_cached_response() L3 CB protection tests (doc 445 G1) and L3
singleflight dedup tests (doc 594 D5).

Covers:
- Behavior: CB OPEN stale fallback, CB OPEN static fallback,
  L3 success records CB success, L3 failure records CB failure,
  no CB (cb_service=None) passthrough
- Behavior: concurrent L3 misses compute once, winner MISS / waiter DEDUP
  label matrix, per-caller fresh dicts, record_failure exactly once
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from baldur.services.precomputed_cache.multi_tier import (
    get_cached_response,
    reset_drift_stats,
)
from tests.factories.concurrency_helpers import make_observable_singleflight


class TestGetCachedResponseCBBehavior:
    """Behavior verification for L3 CB protection in get_cached_response."""

    def setup_method(self):
        reset_drift_stats()

    def test_cb_open_with_stale_returns_stale_data(self):
        """CB OPEN + stale available → returns stale with hit=STALE."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = False

        mock_worker = MagicMock()
        mock_worker.cb_service = mock_cb

        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None
            mock_l1.get_stale.return_value = '{"status": "ok", "value": 42}'

            result = get_cached_response("test_key", lambda: {"status": "fresh"})

        assert result["status"] == "ok"
        assert result["_cache"]["hit"] == "STALE"

    def test_cb_open_without_stale_returns_static_fallback(self):
        """CB OPEN + no stale → returns static fallback with hit=CB_OPEN."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = False

        mock_worker = MagicMock()
        mock_worker.cb_service = mock_cb

        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None
            mock_l1.get_stale.return_value = None

            result = get_cached_response("test_key", lambda: {"status": "fresh"})

        assert result["status"] == "unavailable"
        assert result["reason"] == "circuit_breaker_open"
        assert result["_cache"]["hit"] == "CB_OPEN"

    def test_l3_success_records_cb_success(self):
        """L3 compute success calls cb.record_success()."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = True

        mock_worker = MagicMock()
        mock_worker.cb_service = mock_cb

        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None

            result = get_cached_response("test_key", lambda: {"status": "fresh"})

        assert result["status"] == "fresh"
        assert result["_cache"]["hit"] == "MISS"
        mock_cb.record_success.assert_called_once_with("precomputed_cache_compute")

    def test_l3_failure_records_cb_failure(self):
        """L3 compute failure calls cb.record_failure()."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = True

        mock_worker = MagicMock()
        mock_worker.cb_service = mock_cb

        def failing_compute():
            raise RuntimeError("db down")

        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None

            result = get_cached_response("test_key", failing_compute)

        assert result["status"] == "error"
        assert result["_cache"]["hit"] == "ERROR"
        mock_cb.record_failure.assert_called_once_with("precomputed_cache_compute")

    def test_no_cb_service_passes_through(self):
        """When cb_service is None, L3 compute proceeds without CB."""
        mock_worker = MagicMock()
        mock_worker.cb_service = None

        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None

            result = get_cached_response("test_key", lambda: {"status": "computed"})

        assert result["status"] == "computed"
        assert result["_cache"]["hit"] == "MISS"

    def test_cb_open_static_fallback_calls_hit_rate_metrics(self):
        """CB OPEN + no stale → _update_hit_rate_metrics is called."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = False

        mock_worker = MagicMock()
        mock_worker.cb_service = mock_cb

        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
            patch(
                "baldur.services.precomputed_cache.multi_tier._update_hit_rate_metrics"
            ) as mock_metrics,
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None
            mock_l1.get_stale.return_value = None

            get_cached_response("test_key", lambda: {"status": "fresh"})

        mock_metrics.assert_called_once()

    def test_l1_hit_bypasses_cb(self):
        """L1 hit returns immediately without touching CB."""
        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache"),
        ):
            mock_l1.get.return_value = '{"status": "cached"}'

            result = get_cached_response("test_key", lambda: {"status": "fresh"})

        assert result["_cache"]["hit"] == "L1"


# =============================================================================
# L3 singleflight dedup (doc 594 D5)
# =============================================================================


class TestL3SingleflightBehavior:
    """Concurrent L3 misses: one compute, MISS/DEDUP labels, fresh dicts."""

    def setup_method(self):
        reset_drift_stats()

    def test_concurrent_misses_compute_once_with_label_matrix(self):
        """N concurrent L3 misses -> 1 compute; winner labeled MISS,
        waiters labeled DEDUP; every caller gets its OWN dict object.

        Deterministic: the module singleflight is swapped for an
        observable one, so the winner's compute completes only after all
        callers have committed to the winner or waiter role.
        """
        # Given
        n_threads = 6
        sf, all_entered = make_observable_singleflight(n_threads)
        compute_calls: list[int] = []

        def gated_compute() -> dict:
            compute_calls.append(1)  # winner-only
            all_entered.wait(timeout=5.0)
            return {"status": "fresh", "value": 42}

        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = True
        mock_worker = MagicMock()
        mock_worker.cb_service = mock_cb

        results: list[dict] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()

        def worker() -> None:
            try:
                response = get_cached_response("test_key", gated_compute)
                with results_lock:
                    results.append(response)
            except Exception as e:  # pragma: no cover - failure diagnostics
                with results_lock:
                    errors.append(e)

        # When
        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
            patch("baldur.services.precomputed_cache.multi_tier._l3_singleflight", sf),
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None

            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        # Then - exactly one backend compute
        assert errors == []
        assert len(compute_calls) == 1

        # Label matrix: 1 winner MISS+computed, N-1 waiters DEDUP
        miss = [r for r in results if r["_cache"]["hit"] == "MISS"]
        dedup = [r for r in results if r["_cache"]["hit"] == "DEDUP"]
        assert len(miss) == 1
        assert miss[0]["_cache"]["computed"] is True
        assert len(dedup) == n_threads - 1
        assert all("computed" not in r["_cache"] for r in dedup)
        assert all("latency_ms" in r["_cache"] for r in results)

        # Same payload, per-caller fresh dict (no shared mutable response)
        assert all(r["status"] == "fresh" and r["value"] == 42 for r in results)
        assert len({id(r) for r in results}) == n_threads

        # Winner stored once per tier; CB success recorded once
        assert mock_l1.set.call_count == 1
        assert mock_l2.set.call_count == 1
        mock_cb.record_success.assert_called_once_with("precomputed_cache_compute")


class TestL3SingleflightFailureBehavior:
    """Winner exception shared by waiters; CB failure recorded exactly once."""

    def setup_method(self):
        reset_drift_stats()

    def test_concurrent_failure_records_cb_failure_once(self):
        """A failing compute under concurrency -> record_failure once per
        actual backend call (R3: more accurate than the previous N times),
        with each caller building its OWN error dict."""
        # Given
        n_threads = 4
        sf, all_entered = make_observable_singleflight(n_threads)
        compute_calls: list[int] = []

        def gated_failing_compute() -> dict:
            compute_calls.append(1)  # winner-only
            all_entered.wait(timeout=5.0)
            raise RuntimeError("db down")

        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = True
        mock_worker = MagicMock()
        mock_worker.cb_service = mock_cb

        results: list[dict] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()

        def worker() -> None:
            try:
                response = get_cached_response("test_key", gated_failing_compute)
                with results_lock:
                    results.append(response)
            except Exception as e:  # pragma: no cover - failure diagnostics
                with results_lock:
                    errors.append(e)

        # When
        with (
            patch("baldur.services.precomputed_cache.multi_tier._l1_cache") as mock_l1,
            patch("baldur.services.precomputed_cache.multi_tier._l2_cache") as mock_l2,
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
                return_value=mock_worker,
            ),
            patch("baldur.services.precomputed_cache.multi_tier._l3_singleflight", sf),
        ):
            mock_l1.get.return_value = None
            mock_l2.get.return_value = None

            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        # Then - one backend call, one CB failure record
        assert errors == []
        assert len(compute_calls) == 1
        mock_cb.record_failure.assert_called_once_with("precomputed_cache_compute")

        # Every caller got its own error dict built from the shared exception
        assert len(results) == n_threads
        assert all(r["status"] == "error" for r in results)
        assert all(r["_cache"]["hit"] == "ERROR" for r in results)
        assert all(r["error"] == "db down" for r in results)
        assert len({id(r) for r in results}) == n_threads

        # Nothing was cached on failure
        mock_l1.set.assert_not_called()
        mock_l2.set.assert_not_called()
