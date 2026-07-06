"""#485 D1a/G2 — CB blocked-recorder sticky-flag regression tests.

Mirrors the ``ProtectMetricRecorder`` sticky-flag pattern from
``tests/unit/metrics/recorders/test_protect_recorder.py``. Locks in the
``_cb_recorder`` + ``_cb_recorder_init_failed`` module-level state added
to ``baldur.metrics.recorders.circuit_breaker`` so the CB reject hot path
no longer pays the deferred-import cost of ``baldur.metrics.prometheus``
on every call.

Recovery contract: ``reset_blocked_recorder()`` clears both the cached
recorder ref AND the sticky failure flag — wired into
``baldur.protect_facade.reset_protect_caches`` via the D7 reset chain.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.recorders import circuit_breaker as recorder_module
from baldur.metrics.recorders.circuit_breaker import (
    record_blocked,
    reset_blocked_recorder,
)


@pytest.fixture(autouse=True)
def _reset_cb_recorder_sticky_state():
    reset_blocked_recorder()
    yield
    reset_blocked_recorder()


# =============================================================================
# Contract — record_blocked module-level shortcut
# =============================================================================


class TestRecordBlockedContract:
    """``record_blocked(service, reason)`` is the public hot-path shortcut."""

    def test_none_recorder_is_noop(self):
        """When ``_lazy_recorder`` returns None, ``record_blocked`` is a no-op
        (no exception, no metric write)."""
        recorder_module._cb_recorder = None
        recorder_module._cb_recorder_init_failed = True

        record_blocked("svc", "open")  # Must not raise

    def test_valid_recorder_delegates_with_kwargs(self):
        """Non-None recorder receives ``record_blocked(service, reason)``
        positional args verbatim."""
        fake_recorder = MagicMock()
        recorder_module._cb_recorder = fake_recorder

        record_blocked("payment_api", "half_open_full")

        fake_recorder.record_blocked.assert_called_once_with(
            "payment_api", "half_open_full"
        )

    def test_module_exports_sticky_helpers(self):
        """``reset_blocked_recorder`` is in the module's __all__."""
        from baldur.metrics.recorders import circuit_breaker

        assert "reset_blocked_recorder" in circuit_breaker.__all__
        assert "record_blocked" in circuit_breaker.__all__


# =============================================================================
# Behavior — sticky-flag lifecycle for _lazy_recorder
# =============================================================================


class TestCBRecorderBlockedStickyBehavior:
    """``_lazy_recorder`` short-circuits after the first failed lookup;
    ``reset_blocked_recorder`` is the only recovery path."""

    def test_lookup_failure_flips_sticky_flag_and_returns_none(self):
        """First failing call returns None and transitions the flag False → True."""
        assert recorder_module._cb_recorder_init_failed is False

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("prometheus_client missing"),
        ):
            result = recorder_module._lazy_recorder()

        assert result is None
        assert recorder_module._cb_recorder_init_failed is True

    def test_sticky_flag_skips_lookup_on_subsequent_calls(self):
        """After the flag is set, the failing import is NOT re-invoked."""
        call_count = 0

        def counting_boom():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("prom missing")

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=counting_boom,
        ):
            for _ in range(3):
                assert recorder_module._lazy_recorder() is None

        assert call_count == 1
        assert recorder_module._cb_recorder_init_failed is True

    def test_missing_recorder_attribute_sets_sticky_flag(self):
        """``getattr(metrics, "circuit_breaker", None) is None`` also flips
        the sticky flag — getMetrics() succeeded but the CB recorder slot is
        empty (e.g. metrics not yet initialized)."""
        bare_metrics = object()

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            return_value=bare_metrics,
        ):
            assert recorder_module._lazy_recorder() is None

        assert recorder_module._cb_recorder_init_failed is True

    def test_successful_lookup_caches_recorder_ref(self):
        """First success caches the recorder; subsequent calls return it directly."""
        fake_metrics = MagicMock()
        fake_recorder = MagicMock()
        fake_metrics.circuit_breaker = fake_recorder

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            return_value=fake_metrics,
        ) as mock_get:
            first = recorder_module._lazy_recorder()
            second = recorder_module._lazy_recorder()

        assert first is fake_recorder
        assert first is second
        mock_get.assert_called_once()

    def test_reset_clears_recorder_and_sticky_flag(self):
        """``reset_blocked_recorder`` clears BOTH cached recorder and flag."""
        recorder_module._cb_recorder = MagicMock()
        recorder_module._cb_recorder_init_failed = True

        reset_blocked_recorder()

        assert recorder_module._cb_recorder is None
        assert recorder_module._cb_recorder_init_failed is False

    def test_reset_allows_reconstruction(self):
        """After reset, the next call retries the failing path (recovery)."""
        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("transient"),
        ):
            assert recorder_module._lazy_recorder() is None
            assert recorder_module._cb_recorder_init_failed is True

        reset_blocked_recorder()

        fake_metrics = MagicMock()
        fake_recorder = MagicMock()
        fake_metrics.circuit_breaker = fake_recorder

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            return_value=fake_metrics,
        ):
            recovered = recorder_module._lazy_recorder()

        assert recovered is fake_recorder

    def test_record_blocked_uses_sticky_fast_path(self):
        """``record_blocked`` honors the sticky flag — short-circuits without
        invoking ``get_metrics`` after a prior failure."""
        recorder_module._cb_recorder_init_failed = True

        with patch(
            "baldur.metrics.prometheus.get_metrics",
        ) as mock_get:
            record_blocked("svc", "open")

        mock_get.assert_not_called()
