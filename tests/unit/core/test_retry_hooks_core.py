"""
Core retry hooks factory unit tests.

Target: core/retry_hooks.py
- make_standard_on_retry(): on_retry hook factory combining audit + Prometheus
- make_standard_on_exhausted(): final-failure audit hook factory
"""

from __future__ import annotations

from unittest.mock import patch

from baldur.core.retry import RetryContext
from baldur.core.retry_hooks import (
    make_standard_on_exhausted,
    make_standard_on_retry,
)


class _RaisingCollector:
    """A metric double that records the touch it then refuses to serve.

    Patching a Prometheus collector with ``side_effect`` does nothing: the
    production code never calls the collector, it calls ``.labels(...)`` on it,
    so the side effect never fires and the fail-open arm never runs. The double
    raises from the attribute the SUT actually reaches, and remembers that it
    was reached so the test can prove the fault happened at all.
    """

    def __init__(self) -> None:
        self.touched = False

    def labels(self, **_kwargs: object) -> None:
        self.touched = True
        raise RuntimeError("metrics registry down")


# =============================================================================
# make_standard_on_retry — behavior
# =============================================================================


class TestMakeStandardOnRetryBehavior:
    """make_standard_on_retry behavior."""

    def test_returns_callable(self):
        """The factory returns a callable."""
        hook = make_standard_on_retry("payment")
        assert callable(hook)

    @patch("baldur.core.retry_hooks.log_retry_audit", autospec=True)
    def test_calls_audit_logging(self, mock_audit):
        """Calling on_retry writes an audit record."""
        hook = make_standard_on_retry("payment")
        ctx = RetryContext(
            func_name="charge",
            attempt=1,
            max_retries=5,
            wait_time=2.0,
            elapsed_total=2.0,
        )
        hook(ctx, ValueError("fail"))

        mock_audit.assert_called_once_with(
            domain="payment",
            attempt=1,
            max_attempts=5,
            success=False,
            wait_time=2.0,
        )

    @patch(
        "baldur.services.metrics.definitions.retry_attempts_histogram",
        autospec=True,
    )
    def test_records_prometheus_metric(self, mock_histogram):
        """Calling on_retry records a Prometheus metric."""
        # Patch the audit call out of the way
        with patch(
            "baldur.core.retry_hooks.log_retry_audit",
            autospec=True,
        ):
            hook = make_standard_on_retry("payment")
            ctx = RetryContext(
                func_name="charge",
                attempt=2,
                max_retries=5,
                wait_time=1.0,
                elapsed_total=3.0,
                metric_labels={"context": "payment"},
            )
            hook(ctx, ValueError("fail"))

        mock_histogram.labels.assert_called_once_with(
            domain="payment", context="payment"
        )
        mock_histogram.labels.return_value.observe.assert_called_once_with(3)

    def test_audit_failure_is_silenced(self):
        """Audit failure does not propagate (Fail-Open).

        Post-518-a: fail-open is owned by baldur.audit.helpers._safe_delegate
        rather than a try/except in the caller. This test simulates the
        contract by patching the helper to return None (the fail-open
        result), and verifying the hook still completes without raising.
        """
        with patch(
            "baldur.core.retry_hooks.log_retry_audit",
            return_value=None,
        ):
            hook = make_standard_on_retry("payment")
            ctx = RetryContext(
                func_name="charge",
                attempt=0,
                max_retries=3,
                wait_time=1.0,
                elapsed_total=1.0,
            )
            # Should not raise
            hook(ctx, ValueError("fail"))

    def test_metrics_failure_is_silenced(self):
        """A metric recording failure does not propagate (fail-open).

        The double witnesses its own touch because this fail-open arm is a
        silent ``except Exception: pass`` — with nothing logged, the recorded
        touch is the only evidence the arm was entered rather than skipped.
        """
        collector = _RaisingCollector()
        with (
            patch(
                "baldur.core.retry_hooks.log_retry_audit",
                autospec=True,
            ),
            patch(
                "baldur.services.metrics.definitions.retry_attempts_histogram",
                collector,
            ),
        ):
            hook = make_standard_on_retry("payment")
            ctx = RetryContext(
                func_name="charge",
                attempt=0,
                max_retries=3,
                wait_time=1.0,
                elapsed_total=1.0,
            )
            # Should not raise
            hook(ctx, ValueError("fail"))

        assert collector.touched, "the fail-open arm was never reached"


# =============================================================================
# make_standard_on_exhausted — behavior
# =============================================================================


class TestMakeStandardOnExhaustedBehavior:
    """make_standard_on_exhausted behavior."""

    def test_returns_callable(self):
        """The factory returns a callable."""
        hook = make_standard_on_exhausted("payment")
        assert callable(hook)

    @patch("baldur.core.retry_hooks.log_retry_audit", autospec=True)
    def test_calls_audit_with_error_info(self, mock_audit):
        """Calling on_exhausted writes an audit record carrying the error."""
        hook = make_standard_on_exhausted("payment")
        ctx = RetryContext(
            func_name="charge",
            attempt=4,
            max_retries=5,
            wait_time=0.0,
            elapsed_total=10.0,
        )
        error = ValueError("timeout exceeded")
        hook(ctx, error)

        mock_audit.assert_called_once_with(
            domain="payment",
            attempt=4,
            max_attempts=5,
            success=False,
            error_type="ValueError",
            error_message="timeout exceeded",
        )

    @patch("baldur.core.retry_hooks.log_retry_audit", autospec=True)
    def test_error_message_truncated_to_500_chars(self, mock_audit):
        """The error message is truncated at 500 characters."""
        hook = make_standard_on_exhausted("payment")
        ctx = RetryContext(
            func_name="charge",
            attempt=2,
            max_retries=3,
            wait_time=0.0,
            elapsed_total=5.0,
        )
        long_msg = "x" * 1000
        hook(ctx, ValueError(long_msg))

        mock_audit.call_args[1] if mock_audit.call_args[1] else {}
        call_args = mock_audit.call_args
        # error_message should be truncated
        actual_msg = (
            call_args.kwargs.get("error_message", call_args[1].get("error_message", ""))
            if call_args.kwargs
            else ""
        )
        if not actual_msg:
            # Positional call - get from mock
            actual_msg = mock_audit.call_args[1]["error_message"]
        assert len(actual_msg) == 500

    def test_audit_failure_is_silenced(self):
        """Audit failure does not propagate (Fail-Open).

        Post-518-a: fail-open is owned by baldur.audit.helpers._safe_delegate.
        See TestMakeStandardOnRetryBehavior version for the full rationale.
        """
        with patch(
            "baldur.core.retry_hooks.log_retry_audit",
            return_value=None,
        ):
            hook = make_standard_on_exhausted("payment")
            ctx = RetryContext(
                func_name="charge",
                attempt=0,
                max_retries=3,
                wait_time=0.0,
                elapsed_total=0.0,
            )
            # Should not raise
            hook(ctx, ValueError("fail"))
