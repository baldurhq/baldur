"""Unit tests for ProtectMetricRecorder (429 Part 1, C4; 480 DEC-2 sticky failure).

Scope:
- Metric name contract: baldur_protect_attempts / _duration_seconds /
  _fallback_total.
- Label cardinality: (name, outcome) for attempts + duration; (name,) for
  fallback_total.
- record() dependency interaction: labels().observe() / inc() called.
- fallback_total only increments when fallback_used=True.
- Singleton lifecycle: get_protect_recorder() caches; reset clears.
- Sticky failure (480 DEC-2): construction failure flips _recorder_init_failed
  True; subsequent calls return None without re-running the constructor;
  reset_protect_recorder() clears the flag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.metrics.recorders.protect import (
    ProtectMetricRecorder,
    get_protect_recorder,
    reset_protect_recorder,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_protect_recorder()
    yield
    reset_protect_recorder()


# =============================================================================
# Contract — metric names + label lists per 429 Part 1 C4
# =============================================================================


class TestProtectMetricRecorderContract:
    """Hardcoded contract — metric names and label lists are product API."""

    def test_metric_names_follow_baldur_protect_convention(self):
        """Names must be baldur_protect_{attempts,duration_seconds,fallback_total}.

        prometheus_client strips the trailing ``_total`` from a Counter's
        internal ``_name`` attribute (the suffix is reappended on scrape), so
        we compare against the stripped form. The exposed wire name remains
        ``baldur_protect_fallback_total``.
        """
        recorder = ProtectMetricRecorder()

        assert recorder._attempts._name == "baldur_protect_attempts"
        assert recorder._duration_seconds._name == "baldur_protect_duration_seconds"
        assert recorder._fallback_total._name == "baldur_protect_fallback"

    def test_attempts_and_duration_labels_are_name_outcome_mode(self):
        """Labels (name, outcome, mode) pin every observation to a protect-call
        identity and the emitting facade (sync/async, D12)."""
        recorder = ProtectMetricRecorder()

        assert tuple(recorder._attempts._labelnames) == ("name", "outcome", "mode")
        assert tuple(recorder._duration_seconds._labelnames) == (
            "name",
            "outcome",
            "mode",
        )

    def test_fallback_total_labels_are_name_and_mode(self):
        """fallback_total is keyed by service name and emitting facade (mode)."""
        recorder = ProtectMetricRecorder()

        assert tuple(recorder._fallback_total._labelnames) == ("name", "mode")


# =============================================================================
# Behavior — record() dispatches observations and conditional increments
# =============================================================================


class TestProtectRecorderBehavior:
    """Dependency interaction: record() forwards to prometheus_client objects."""

    def test_record_observes_attempts_and_duration_on_every_call(self):
        """record() observes both histograms exactly once per invocation."""
        recorder = ProtectMetricRecorder()
        recorder._attempts = MagicMock()
        recorder._duration_seconds = MagicMock()
        recorder._fallback_total = MagicMock()

        recorder.record(
            name="svc.x",
            outcome="success",
            attempts=2,
            duration_seconds=0.5,
            fallback_used=False,
        )

        recorder._attempts.labels.assert_called_once_with(
            name="svc.x", outcome="success", mode="sync"
        )
        recorder._attempts.labels.return_value.observe.assert_called_once_with(2)
        recorder._duration_seconds.labels.assert_called_once_with(
            name="svc.x", outcome="success", mode="sync"
        )
        recorder._duration_seconds.labels.return_value.observe.assert_called_once_with(
            0.5
        )

    def test_record_increments_fallback_total_only_when_fallback_used(self):
        """fallback_total.labels().inc() fires iff fallback_used=True."""
        recorder = ProtectMetricRecorder()
        recorder._attempts = MagicMock()
        recorder._duration_seconds = MagicMock()
        recorder._fallback_total = MagicMock()

        # When — success branch, no fallback
        recorder.record(
            name="svc.x",
            outcome="success",
            attempts=1,
            duration_seconds=0.1,
            fallback_used=False,
        )
        assert recorder._fallback_total.labels.call_count == 0

        # When — fallback branch
        recorder.record(
            name="svc.x",
            outcome="fallback",
            attempts=1,
            duration_seconds=0.1,
            fallback_used=True,
        )
        recorder._fallback_total.labels.assert_called_once_with(
            name="svc.x", mode="sync"
        )
        recorder._fallback_total.labels.return_value.inc.assert_called_once()

    def test_record_is_fail_open_on_label_exception(self):
        """Any exception inside labels().observe() is swallowed (fail-open)."""
        recorder = ProtectMetricRecorder()
        recorder._attempts = MagicMock()
        recorder._attempts.labels.side_effect = RuntimeError("label broken")
        recorder._duration_seconds = MagicMock()
        recorder._fallback_total = MagicMock()

        # Then — no exception escapes
        recorder.record(
            name="svc.x",
            outcome="success",
            attempts=1,
            duration_seconds=0.1,
            fallback_used=False,
        )


# =============================================================================
# Mode label (D12) — per-facade sync/async separation
# =============================================================================


class TestProtectRecorderModeLabelBehavior:
    """672 D12: ``record(mode=…)`` threads the emitting facade into every
    ``.labels(...)`` — ``"sync"`` for ``protect``, ``"async"`` for ``aprotect`` —
    so a ``name`` protected via both facades splits into two series."""

    def test_default_mode_is_sync(self):
        """The default keeps the sync facade's series (backward-compatible)."""
        recorder = ProtectMetricRecorder()
        recorder._attempts = MagicMock()
        recorder._duration_seconds = MagicMock()
        recorder._fallback_total = MagicMock()

        recorder.record(
            name="svc.x",
            outcome="success",
            attempts=1,
            duration_seconds=0.1,
            fallback_used=True,
        )

        assert recorder._attempts.labels.call_args.kwargs["mode"] == "sync"
        assert recorder._duration_seconds.labels.call_args.kwargs["mode"] == "sync"
        assert recorder._fallback_total.labels.call_args.kwargs["mode"] == "sync"

    def test_async_mode_forwarded_to_all_three_metrics(self):
        """``mode="async"`` (the aprotect facade) reaches all three metrics."""
        recorder = ProtectMetricRecorder()
        recorder._attempts = MagicMock()
        recorder._duration_seconds = MagicMock()
        recorder._fallback_total = MagicMock()

        recorder.record(
            name="svc.x",
            outcome="fallback",
            attempts=2,
            duration_seconds=0.3,
            fallback_used=True,
            mode="async",
        )

        recorder._attempts.labels.assert_called_once_with(
            name="svc.x", outcome="fallback", mode="async"
        )
        recorder._duration_seconds.labels.assert_called_once_with(
            name="svc.x", outcome="fallback", mode="async"
        )
        recorder._fallback_total.labels.assert_called_once_with(
            name="svc.x", mode="async"
        )


# =============================================================================
# Singleton lifecycle
# =============================================================================


class TestGetProtectRecorderLifecycle:
    """get_protect_recorder caches a single instance; reset clears."""

    def test_get_returns_same_instance_on_repeat_call(self):
        first = get_protect_recorder()
        second = get_protect_recorder()

        assert first is second

    def test_reset_forces_fresh_instance_next_call(self):
        first = get_protect_recorder()
        reset_protect_recorder()
        second = get_protect_recorder()

        assert first is not second


# =============================================================================
# Sticky failure (480 DEC-2)
#
# When ProtectMetricRecorder() raises (e.g. prometheus_client missing or
# registry collision), get_protect_recorder() must:
#   1. Set _recorder_init_failed = True (sticky)
#   2. Return None
#   3. Skip the failing constructor on subsequent calls (no retry, no log spam)
# reset_protect_recorder() must clear BOTH _recorder and _recorder_init_failed
# so a recovery path remains available via the explicit reset hook.
# =============================================================================


class TestProtectRecorderStickyFailure:
    """480 DEC-2 — sticky failure flag prevents per-call constructor retries."""

    def test_construction_failure_returns_none_and_flips_sticky_flag(self, monkeypatch):
        """First failing call returns None; module flag transitions False → True."""
        from baldur.metrics.recorders import protect as recorder_module

        # Given — flag starts cleared (autouse fixture) and constructor will raise
        assert recorder_module._recorder_init_failed is False

        def boom(self):
            raise RuntimeError("prometheus_client missing")

        monkeypatch.setattr(ProtectMetricRecorder, "__init__", boom)

        # When
        result = get_protect_recorder()

        # Then
        assert result is None
        assert recorder_module._recorder_init_failed is True

    def test_sticky_flag_skips_constructor_on_subsequent_calls(self, monkeypatch):
        """After the flag is set, the failing __init__ is NOT re-invoked."""
        from baldur.metrics.recorders import protect as recorder_module

        call_count = 0

        def counting_boom(self):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("prom missing")

        monkeypatch.setattr(ProtectMetricRecorder, "__init__", counting_boom)

        # When — three calls
        for _ in range(3):
            assert get_protect_recorder() is None

        # Then — constructor invoked exactly once across all calls
        assert call_count == 1
        assert recorder_module._recorder_init_failed is True

    def test_reset_clears_sticky_flag_and_allows_reconstruction(self, monkeypatch):
        """reset_protect_recorder() clears the sticky flag — next call retries."""
        from baldur.metrics.recorders import protect as recorder_module

        call_count = 0
        original_init = ProtectMetricRecorder.__init__

        def first_call_raises(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")
            original_init(self)

        monkeypatch.setattr(ProtectMetricRecorder, "__init__", first_call_raises)

        # Given — first call fails and sets sticky flag
        assert get_protect_recorder() is None
        assert recorder_module._recorder_init_failed is True

        # When — explicit reset
        reset_protect_recorder()

        # Then — flag cleared and recorder cleared
        assert recorder_module._recorder_init_failed is False
        assert recorder_module._recorder is None

        # Subsequent call retries construction (call_count goes from 1 → 2)
        recorder = get_protect_recorder()
        assert recorder is not None
        assert call_count == 2

    def test_sticky_flag_emits_warning_log_once_on_first_failure(self, monkeypatch):
        """DEC-2 logging contract: first transition False → True emits a single
        WARNING with event ``metrics.protect_recorder_unavailable_sticky``.
        Subsequent silenced calls do NOT re-emit (sticky flag short-circuits
        before reaching the except branch)."""
        from baldur.metrics.recorders import protect as recorder_module

        def boom(self):
            raise RuntimeError("prom missing")

        monkeypatch.setattr(ProtectMetricRecorder, "__init__", boom)

        warning_calls: list[tuple[str, dict]] = []

        def capture_warning(event, **kwargs):
            warning_calls.append((event, kwargs))

        monkeypatch.setattr(recorder_module.logger, "warning", capture_warning)

        # When — three calls (only the first hits the except branch)
        for _ in range(3):
            get_protect_recorder()

        # Then — exactly one warning, with the documented sticky event name
        assert len(warning_calls) == 1
        event, kwargs = warning_calls[0]
        assert event == "metrics.protect_recorder_unavailable_sticky"
        assert "error" in kwargs

    def test_sticky_flag_returns_none_without_recreating_existing_instance(
        self, monkeypatch
    ):
        """When _recorder is already set, sticky-flag path is irrelevant —
        cached instance is returned. Sets the sticky flag manually to confirm
        the ``_recorder is not None`` short-circuit wins over the flag check
        (matches the source ordering in get_protect_recorder)."""
        from baldur.metrics.recorders import protect as recorder_module

        # Given — both _recorder set AND sticky flag set
        cached = ProtectMetricRecorder()
        recorder_module._recorder = cached
        recorder_module._recorder_init_failed = True

        # When
        result = get_protect_recorder()

        # Then — cached instance wins; flag is ignored when _recorder present
        assert result is cached
