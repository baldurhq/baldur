"""#485 D1d/G7 — DLQMetricEventHandler ``_metrics_init_failed`` sticky flag.

``DLQMetricEventHandler._get_metrics`` was the last per-call deferred
import on the 7A.2 failure path: ``from baldur.metrics.prometheus import
get_metrics`` ran on every event-handler call. The function-level
``_metrics_instance`` cache covered the success path, but the
ImportError branch was un-cached — every call re-attempted the failing
import.

D1d adds ``_metrics_init_failed`` (sticky) so once the import has failed
once, every subsequent call short-circuits to ``None`` without re-running
the failing path. ``reset_event_handler_cache()`` clears both the cache
and the sticky flag — wired into ``baldur.protect_facade.reset_protect_caches``
via the D7 reset chain.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics import event_handlers as eh_module
from baldur.metrics.event_handlers import reset_event_handler_cache


@pytest.fixture(autouse=True)
def _reset_event_handler_cache():
    reset_event_handler_cache()
    yield
    reset_event_handler_cache()


# =============================================================================
# Behavior — _get_metrics sticky lifecycle
# =============================================================================


class TestEventHandlerStickyFlagBehavior:
    """``_get_metrics`` honors the ``_metrics_init_failed`` sticky flag."""

    def test_initial_module_state_is_uninitialized(self):
        """Module starts with no cached metrics and no sticky flag."""
        assert eh_module._metrics_instance is None
        assert eh_module._metrics_init_failed is False

    def test_import_error_flips_sticky_flag_and_returns_none(self):
        """First ImportError sets the sticky flag and returns None."""
        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=ImportError("prometheus_client not installed"),
        ):
            result = eh_module._get_metrics()

        assert result is None
        assert eh_module._metrics_init_failed is True
        assert eh_module._metrics_instance is None

    def test_sticky_flag_short_circuits_subsequent_calls(self):
        """After flag set, the failing import is NOT re-invoked."""
        call_count = 0

        def counting_import_error():
            nonlocal call_count
            call_count += 1
            raise ImportError("prometheus_client missing")

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=counting_import_error,
        ):
            for _ in range(3):
                assert eh_module._get_metrics() is None

        assert call_count == 1
        assert eh_module._metrics_init_failed is True

    def test_successful_import_caches_metrics_instance(self):
        """First success caches ``_metrics_instance``; subsequent calls
        return it directly without re-importing."""
        fake_metrics = MagicMock(name="metrics_singleton")

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            return_value=fake_metrics,
        ) as mock_get:
            first = eh_module._get_metrics()
            second = eh_module._get_metrics()

        assert first is fake_metrics
        assert first is second
        # Single import attempt — second call returned cached instance.
        mock_get.assert_called_once()

    def test_reset_clears_metrics_and_sticky_flag(self):
        """``reset_event_handler_cache`` clears BOTH cache and sticky flag."""
        eh_module._metrics_instance = MagicMock()
        eh_module._metrics_init_failed = True

        reset_event_handler_cache()

        assert eh_module._metrics_instance is None
        assert eh_module._metrics_init_failed is False

    def test_reset_allows_reconstruction_after_failure(self):
        """After reset, the next call retries the failing path (recovery)."""
        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=ImportError("transient"),
        ):
            assert eh_module._get_metrics() is None
            assert eh_module._metrics_init_failed is True

        reset_event_handler_cache()

        fake_metrics = MagicMock()
        with patch(
            "baldur.metrics.prometheus.get_metrics",
            return_value=fake_metrics,
        ):
            recovered = eh_module._get_metrics()

        assert recovered is fake_metrics

    def test_dlq_event_handler_short_circuits_on_sticky_flag(self):
        """``DLQMetricEventHandler.on_item_created`` is a no-op when the
        sticky flag is set — observable via no exception and no metric calls."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        eh_module._metrics_init_failed = True

        with patch(
            "baldur.metrics.prometheus.get_metrics",
        ) as mock_get:
            DLQMetricEventHandler.on_item_created("payment", "PG_TIMEOUT")

        mock_get.assert_not_called()


# =============================================================================
# Contract — reset_event_handler_cache is exported
# =============================================================================


class TestEventHandlerCacheResetContract:
    def test_reset_event_handler_cache_in_all(self):
        """``reset_event_handler_cache`` is in the module's public API."""
        from baldur.metrics import event_handlers

        assert "reset_event_handler_cache" in event_handlers.__all__

    def test_reset_clears_safe_gauge_and_logging_caches_too(self):
        """Reset must clear all four module-level caches in one call."""
        eh_module._metrics_instance = MagicMock()
        eh_module._metrics_init_failed = True
        eh_module._safe_gauge_cache = {"dlq_pending": MagicMock()}
        eh_module._logging_config = MagicMock()

        reset_event_handler_cache()

        assert eh_module._metrics_instance is None
        assert eh_module._metrics_init_failed is False
        assert eh_module._safe_gauge_cache == {}
        assert eh_module._logging_config is None
