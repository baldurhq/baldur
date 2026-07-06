"""
Tests for Metric Decorators.
"""

from unittest.mock import MagicMock, create_autospec, patch

import pytest
from structlog.testing import capture_logs

from baldur.metrics.decorators import (
    _create_metric_safe,
    _is_valid_label_key,
    _is_valid_metric_name,
    _record_counter,
    _record_histogram,
    _record_safe,
    track_counter,
    track_dlq_creation,
    track_dlq_resolution,
    track_execution_time,
    track_replay,
)
from baldur.metrics.registry import get_or_create_counter


class TestTrackDLQCreation:
    """Tests for track_dlq_creation decorator."""

    @patch("baldur.metrics.decorators.DLQMetricEventHandler")
    def test_calls_event_handler_on_success(self, mock_handler):
        """Decorator should call on_item_created after function succeeds."""

        @track_dlq_creation(domain="payment")
        def create_dlq(failure_type: str, payload: dict):
            return {"id": 1, "failure_type": failure_type}

        result = create_dlq(failure_type="PG_TIMEOUT", payload={"order_id": "123"})

        assert result["id"] == 1
        mock_handler.on_item_created.assert_called_once_with("payment", "PG_TIMEOUT")

    @patch("baldur.metrics.decorators.DLQMetricEventHandler")
    def test_uses_unknown_for_missing_failure_type(self, mock_handler):
        """Should use 'unknown' when failure_type is not provided."""

        @track_dlq_creation(domain="payment")
        def create_dlq():
            return {"id": 1}

        create_dlq()

        mock_handler.on_item_created.assert_called_once_with("payment", "unknown")


class TestTrackDLQResolution:
    """Tests for track_dlq_resolution decorator."""

    @patch("baldur.metrics.decorators.DLQMetricEventHandler")
    def test_calls_event_handler_with_duration(self, mock_handler):
        """Decorator should measure duration and call on_item_resolved."""

        @track_dlq_resolution(domain="payment")
        def resolve_dlq(dlq_item, resolution_type: str = "auto_replay"):
            return dlq_item

        resolve_dlq({"id": 1}, resolution_type="manual")

        mock_handler.on_item_resolved.assert_called_once()
        call_args = mock_handler.on_item_resolved.call_args
        assert call_args.kwargs["domain"] == "payment"
        assert call_args.kwargs["resolution_type"] == "manual"
        assert call_args.kwargs["duration_seconds"] >= 0


class TestTrackReplay:
    """Tests for track_replay decorator."""

    @patch("baldur.metrics.decorators.ReplayEventHandler")
    def test_tracks_successful_replay(self, mock_handler):
        """Should track successful replay completion."""

        @track_replay(domain="payment")
        def replay_item(item):
            return True

        result = replay_item({"id": 1})

        assert result is True
        mock_handler.on_replay_started.assert_called_once()
        mock_handler.on_replay_completed.assert_called_once()

    @patch("baldur.metrics.decorators.ReplayEventHandler")
    def test_tracks_failed_replay(self, mock_handler):
        """Should track failed replay when exception is raised."""

        @track_replay(domain="payment")
        def replay_item(item):
            raise ValueError("Replay failed")

        with pytest.raises(ValueError):
            replay_item({"id": 1})

        mock_handler.on_replay_started.assert_called_once()
        # Completion should still be called with success=False
        mock_handler.on_replay_completed.assert_called_once()
        call_args = mock_handler.on_replay_completed.call_args
        assert call_args[0][1] is False  # success argument


# =============================================================================
# 602 — Metric Recording Wiring (track_counter / track_execution_time)
#
# get_or_create_* dedups by name across the in-process registry shared by the
# whole xdist worker, so metric names carry a globally-unique prefix and the
# effect is asserted via a before/after sample delta (a missing series == 0.0).
# =============================================================================

_PREFIX = "baldur_test602_"


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Current Prometheus sample value, treating a missing series as 0.0."""
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


class TestTrackCounterRecording:
    """track_counter wires a real counter .inc() (602 D1/D2)."""

    def test_track_counter_records_increment_on_success(self):
        """Sync success path increments the counter by exactly 1."""

        # Given
        @track_counter(_PREFIX + "counter_records_total")
        def api_call():
            return {"status": "ok"}

        before = _sample(_PREFIX + "counter_records_total")

        # When
        result = api_call()

        # Then
        assert result["status"] == "ok"
        assert _sample(_PREFIX + "counter_records_total") - before == 1.0

    @pytest.mark.parametrize(
        ("name", "on_success", "on_failure", "fn_raises", "expected_delta"),
        [
            (_PREFIX + "cnt_so_succ_total", True, False, False, 1.0),
            (_PREFIX + "cnt_so_fail_total", True, False, True, 0.0),
            (_PREFIX + "cnt_fo_succ_total", False, True, False, 0.0),
            (_PREFIX + "cnt_fo_fail_total", False, True, True, 1.0),
        ],
        ids=[
            "success_flag_on_success_call",
            "success_flag_on_failure_call",
            "failure_flag_on_success_call",
            "failure_flag_on_failure_call",
        ],
    )
    def test_track_counter_flag_matrix_records_per_outcome(
        self, name, on_success, on_failure, fn_raises, expected_delta
    ):
        """on_success/on_failure flags gate which call outcome is counted."""

        # Given
        @track_counter(name, on_success=on_success, on_failure=on_failure)
        def fn():
            if fn_raises:
                raise RuntimeError("boom")
            return "ok"

        before = _sample(name)

        # When
        if fn_raises:
            with pytest.raises(RuntimeError):
                fn()
        else:
            fn()

        # Then
        assert _sample(name) - before == expected_delta

    @pytest.mark.parametrize(
        ("labels", "sample_labels"),
        [
            (None, None),
            ({"endpoint": "/payment"}, {"endpoint": "/payment"}),
        ],
        ids=["labels_absent", "labels_present"],
    )
    def test_track_counter_records_with_and_without_labels(self, labels, sample_labels):
        """Both the .inc() and .labels().inc() branches record the increment."""
        # Given
        name = f"{_PREFIX}cnt_{'labeled' if labels else 'unlabeled'}_total"

        @track_counter(name, labels=labels)
        def fn():
            return "ok"

        before = _sample(name, sample_labels)

        # When
        fn()

        # Then
        assert _sample(name, sample_labels) - before == 1.0

    def test_track_counter_importerror_fail_open_function_returns(self):
        """ImportError at decoration-time creation → silent no-op; fn still runs."""
        with patch(
            "baldur.metrics.decorators.get_or_create_counter",
            side_effect=ImportError("prometheus_client absent"),
            autospec=True,
        ):

            @track_counter(_PREFIX + "failopen_total")
            def fn():
                return "ok"

            # No exception propagates; the wrapped function returns normally.
            assert fn() == "ok"


class TestTrackCounterAsync:
    """track_counter records on the async path after await (602 D3)."""

    @pytest.mark.asyncio
    async def test_track_counter_async_records_after_await(self):
        """Async success path increments the counter after the await resolves."""

        # Given
        @track_counter(_PREFIX + "counter_async_total")
        async def fn():
            return "ok"

        before = _sample(_PREFIX + "counter_async_total")

        # When
        result = await fn()

        # Then
        assert result == "ok"
        assert _sample(_PREFIX + "counter_async_total") - before == 1.0


class TestTrackExecutionTimeRecording:
    """track_execution_time wires a real histogram .observe() (602 D1/D2)."""

    def test_track_execution_time_records_histogram_count(self):
        """Sync call observes one sample (the _count series increments by 1)."""

        # Given
        @track_execution_time(_PREFIX + "exec_seconds")
        def fn():
            return "done"

        before = _sample(_PREFIX + "exec_seconds_count")

        # When
        result = fn()

        # Then
        assert result == "done"
        assert _sample(_PREFIX + "exec_seconds_count") - before == 1.0

    def test_track_execution_time_observes_in_finally_when_fn_raises(self):
        """Duration is observed in the finally block even when the fn raises."""

        # Given
        @track_execution_time(_PREFIX + "exec_raises_seconds")
        def fn():
            raise ValueError("boom")

        before = _sample(_PREFIX + "exec_raises_seconds_count")

        # When
        with pytest.raises(ValueError):
            fn()

        # Then — observed despite the exception
        assert _sample(_PREFIX + "exec_raises_seconds_count") - before == 1.0


class TestTrackExecutionTimeAsync:
    """track_execution_time records on the async path after await (602 D3)."""

    @pytest.mark.asyncio
    async def test_track_execution_time_async_records_after_await(self):
        """Async call observes one histogram sample after the await resolves."""

        # Given
        @track_execution_time(_PREFIX + "exec_async_seconds")
        async def fn():
            return "done"

        before = _sample(_PREFIX + "exec_async_seconds_count")

        # When
        result = await fn()

        # Then
        assert result == "done"
        assert _sample(_PREFIX + "exec_async_seconds_count") - before == 1.0


class TestCreateMetricSafe:
    """_create_metric_safe pre-check, two-tier fail-open, label-mismatch (602 D2)."""

    def test_create_metric_safe_invalid_name_returns_none_warns(self):
        """Invalid metric name → None, factory NOT called, warning emitted."""
        # Given
        factory = create_autospec(get_or_create_counter)

        # When — the validity pre-check short-circuits before registration
        with capture_logs() as logs:
            result = _create_metric_safe(factory, "bad-name!", "desc", [])

        # Then
        assert result is None
        factory.assert_not_called()
        warnings = [e for e in logs if e["event"] == "metrics.decorator_invalid_name"]
        assert len(warnings) == 1

    def test_create_metric_safe_invalid_label_key_returns_none_warns(self):
        """Invalid label key → None, factory NOT called, warning names the key."""
        # Given
        factory = create_autospec(get_or_create_counter)

        # When
        with capture_logs() as logs:
            result = _create_metric_safe(
                factory, _PREFIX + "valid_total", "desc", ["bad-key"]
            )

        # Then
        assert result is None
        factory.assert_not_called()
        warnings = [e for e in logs if e["event"] == "metrics.decorator_invalid_name"]
        assert len(warnings) == 1
        assert warnings[0]["invalid_label_keys"] == ["bad-key"]

    def test_create_metric_safe_importerror_returns_none_silently(self):
        """ImportError → None with no warning (honors the quiet no-op contract)."""
        # Given
        factory = create_autospec(get_or_create_counter)
        factory.side_effect = ImportError("prometheus_client absent")

        # When
        with capture_logs() as logs:
            result = _create_metric_safe(factory, _PREFIX + "imp_total", "desc", [])

        # Then
        assert result is None
        factory.assert_called_once()
        assert not [
            e
            for e in logs
            if e["event"]
            in (
                "metrics.decorator_registration_failed",
                "metrics.decorator_invalid_name",
            )
        ]

    def test_create_metric_safe_unexpected_error_returns_none_warns(self):
        """Non-ImportError → None + decorator_registration_failed warning."""
        # Given
        factory = create_autospec(get_or_create_counter)
        factory.side_effect = RuntimeError("registry boom")

        # When
        with capture_logs() as logs:
            result = _create_metric_safe(factory, _PREFIX + "err_total", "desc", [])

        # Then
        assert result is None
        warnings = [
            e for e in logs if e["event"] == "metrics.decorator_registration_failed"
        ]
        assert len(warnings) == 1

    def test_create_metric_safe_label_mismatch_warns_once(self):
        """Reusing one name with different label keys warns once at decoration."""
        # Given — real in-process registry; first creation fixes the label keys
        name = _PREFIX + "mismatch_total"

        # When
        with capture_logs() as logs:
            first = _create_metric_safe(get_or_create_counter, name, "desc", ["a"])
            second = _create_metric_safe(get_or_create_counter, name, "desc", ["b"])

        # Then — same collector returned, single mismatch warning
        assert first is second
        mismatch = [e for e in logs if e["event"] == "metrics.decorator_label_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0]["requested"] == ["b"]
        assert mismatch[0]["existing"] == ["a"]


class TestIdentifierValidity:
    """Module-local Prometheus legacy identifier regexes (602 D2/G5)."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("valid_name", True),
            ("_underscore", True),
            ("with:colon", True),
            ("Name123", True),
            ("bad-name", False),
            ("bad.name", False),
            ("1digit_start", False),
            ("has space", False),
            ("", False),
        ],
        ids=[
            "alnum_underscore",
            "leading_underscore",
            "colon_allowed",
            "mixed_case_digits",
            "hyphen_rejected",
            "dot_rejected",
            "leading_digit_rejected",
            "space_rejected",
            "empty_rejected",
        ],
    )
    def test_is_valid_metric_name_matches_legacy_regex(self, name, expected):
        """Metric-name regex: ^[a-zA-Z_:][a-zA-Z0-9_:]*$ (design contract)."""
        assert _is_valid_metric_name(name) is expected

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("endpoint", True),
            ("_x", True),
            ("Key123", True),
            ("bad-key", False),
            ("bad.key", False),
            ("with:colon", False),
            ("__reserved", False),
            ("1digit", False),
            ("", False),
        ],
        ids=[
            "alnum",
            "leading_underscore",
            "mixed_case_digits",
            "hyphen_rejected",
            "dot_rejected",
            "colon_rejected",
            "reserved_double_underscore_rejected",
            "leading_digit_rejected",
            "empty_rejected",
        ],
    )
    def test_is_valid_label_key_matches_legacy_regex(self, key, expected):
        """Label-key regex: ^[a-zA-Z_][a-zA-Z0-9_]*$ minus the reserved __ prefix."""
        assert _is_valid_label_key(key) is expected


class TestRecordSafe:
    """_record_safe runs the callback and fails open at DEBUG (602 D2)."""

    def test_record_safe_runs_callback(self):
        """A non-raising callback runs to completion."""
        calls = []
        _record_safe(lambda: calls.append(1))
        assert calls == [1]

    def test_record_safe_swallows_error_at_debug(self):
        """A raising callback is swallowed and logged once at DEBUG, no re-raise."""

        # Given
        def boom():
            raise ValueError("record failed")

        # When — must not raise
        with capture_logs() as logs:
            _record_safe(boom)

        # Then
        debug = [
            e
            for e in logs
            if e["event"] == "metrics.decorator_record_failed"
            and e["log_level"] == "debug"
        ]
        assert len(debug) == 1


class TestRecordHelpers:
    """_record_counter / _record_histogram collector dispatch (602 D1)."""

    def test_record_counter_none_collector_is_noop(self):
        """None collector → returns without recording (must not raise)."""
        assert (
            _record_counter(None, {}, succeeded=True, on_success=True, on_failure=False)
            is None
        )

    def test_record_histogram_none_collector_is_noop(self):
        """None collector → returns without recording (must not raise)."""
        assert _record_histogram(None, {}, 0.5) is None

    def test_record_counter_unlabeled_calls_inc_directly(self):
        """No labels → counter.inc() with no .labels() child. (chained stand-in)"""
        # MagicMock (no spec) provides the .inc/.labels chain as a collector double.
        counter = MagicMock()
        _record_counter(counter, {}, succeeded=True, on_success=True, on_failure=False)
        counter.inc.assert_called_once_with()
        counter.labels.assert_not_called()

    def test_record_counter_labeled_calls_labels_then_inc(self):
        """Labels present → counter.labels(**values).inc(). (chained stand-in)"""
        counter = MagicMock()
        _record_counter(
            counter,
            {"endpoint": "/x"},
            succeeded=True,
            on_success=True,
            on_failure=False,
        )
        counter.labels.assert_called_once_with(endpoint="/x")
        counter.labels.return_value.inc.assert_called_once_with()

    def test_record_histogram_labeled_observes_on_labeled_child(self):
        """Labels present → histogram.labels(**values).observe(duration)."""
        histogram = MagicMock()
        _record_histogram(histogram, {"type": "credit"}, 0.25)
        histogram.labels.assert_called_once_with(type="credit")
        histogram.labels.return_value.observe.assert_called_once_with(0.25)


# =============================================================================
# Async Decorator Tests
# =============================================================================


class TestAsyncTrackDLQCreation:
    """Tests for async track_dlq_creation decorator."""

    @pytest.mark.asyncio
    @patch("baldur.metrics.decorators.DLQMetricEventHandler")
    async def test_async_calls_event_handler(self, mock_handler):
        """Async decorator should call on_item_created after function succeeds."""

        @track_dlq_creation(domain="payment")
        async def async_create_dlq(failure_type: str, payload: dict):
            return {"id": 1, "failure_type": failure_type}

        result = await async_create_dlq(
            failure_type="PG_TIMEOUT", payload={"order_id": "123"}
        )

        assert result["id"] == 1
        mock_handler.on_item_created.assert_called_once_with("payment", "PG_TIMEOUT")


class TestAsyncTrackDLQResolution:
    """Tests for async track_dlq_resolution decorator."""

    @pytest.mark.asyncio
    @patch("baldur.metrics.decorators.DLQMetricEventHandler")
    async def test_async_calls_event_handler_with_duration(self, mock_handler):
        """Async decorator should measure duration and call on_item_resolved."""

        @track_dlq_resolution(domain="payment")
        async def async_resolve_dlq(dlq_item, resolution_type: str = "auto_replay"):
            return dlq_item

        await async_resolve_dlq({"id": 1}, resolution_type="manual")

        mock_handler.on_item_resolved.assert_called_once()
        call_args = mock_handler.on_item_resolved.call_args
        assert call_args.kwargs["domain"] == "payment"
        assert call_args.kwargs["resolution_type"] == "manual"
        assert call_args.kwargs["duration_seconds"] >= 0


class TestAsyncTrackReplay:
    """Tests for async track_replay decorator."""

    @pytest.mark.asyncio
    @patch("baldur.metrics.decorators.ReplayEventHandler")
    async def test_async_tracks_successful_replay(self, mock_handler):
        """Should track successful async replay completion."""

        @track_replay(domain="payment")
        async def async_replay_item(item):
            return True

        result = await async_replay_item({"id": 1})

        assert result is True
        mock_handler.on_replay_started.assert_called_once()
        mock_handler.on_replay_completed.assert_called_once()
        call_args = mock_handler.on_replay_completed.call_args
        assert call_args[0][1] is True  # success=True

    @pytest.mark.asyncio
    @patch("baldur.metrics.decorators.ReplayEventHandler")
    async def test_async_tracks_failed_replay(self, mock_handler):
        """Should track failed async replay when exception is raised."""

        @track_replay(domain="payment")
        async def async_replay_item(item):
            raise ValueError("Async replay failed")

        with pytest.raises(ValueError):
            await async_replay_item({"id": 1})

        mock_handler.on_replay_started.assert_called_once()
        mock_handler.on_replay_completed.assert_called_once()
        call_args = mock_handler.on_replay_completed.call_args
        assert call_args[0][1] is False  # success=False


class TestDecoratorFunctoolsWrapsPreservation:
    """
    Tests for functools.wraps preservation in decorators.

    리뷰 ②: Universal Decorator에서 functools.wraps가 정확히 적용되어
    inspect.iscoroutinefunction() 및 원래 함수의 메타데이터가 보존되는지 확인.
    """

    import asyncio
    import inspect

    def test_sync_decorator_preserves_function_name(self):
        """Sync decorator should preserve original function name."""

        @track_replay(domain="payment")
        def my_sync_replay_function():
            """My sync docstring."""
            return True

        assert my_sync_replay_function.__name__ == "my_sync_replay_function"
        assert my_sync_replay_function.__doc__ == "My sync docstring."

    def test_async_decorator_preserves_function_name(self):
        """Async decorator should preserve original function name."""

        @track_replay(domain="payment")
        async def my_async_replay_function():
            """My async docstring."""
            return True

        assert my_async_replay_function.__name__ == "my_async_replay_function"
        assert my_async_replay_function.__doc__ == "My async docstring."

    def test_async_wrapper_is_still_coroutine_function(self):
        """Async wrapped function should still be recognized as coroutine function."""
        import asyncio
        import inspect

        @track_replay(domain="payment")
        async def my_async_replay():
            return True

        # This is the key check from 리뷰 ②
        assert asyncio.iscoroutinefunction(my_async_replay)
        assert inspect.iscoroutinefunction(my_async_replay)

    def test_sync_wrapper_is_not_coroutine_function(self):
        """Sync wrapped function should NOT be recognized as coroutine function."""
        import asyncio
        import inspect

        @track_replay(domain="payment")
        def my_sync_replay():
            return True

        assert not asyncio.iscoroutinefunction(my_sync_replay)
        assert not inspect.iscoroutinefunction(my_sync_replay)

    def test_track_dlq_creation_preserves_metadata(self):
        """track_dlq_creation should preserve function metadata."""

        @track_dlq_creation(domain="payment")
        def create_payment_dlq(failure_type: str):
            """Create a payment DLQ item."""
            return {"id": 1}

        assert create_payment_dlq.__name__ == "create_payment_dlq"
        assert "Create a payment DLQ" in create_payment_dlq.__doc__

    def test_track_dlq_resolution_preserves_metadata(self):
        """track_dlq_resolution should preserve function metadata."""

        @track_dlq_resolution(domain="payment")
        def resolve_payment_dlq(item):
            """Resolve a payment DLQ item."""
            return item

        assert resolve_payment_dlq.__name__ == "resolve_payment_dlq"
        assert "Resolve a payment DLQ" in resolve_payment_dlq.__doc__
