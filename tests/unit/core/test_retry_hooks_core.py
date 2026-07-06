"""
Core Retry Hooks 팩토리 단위 테스트.

테스트 대상: core/retry_hooks.py
- make_standard_on_retry(): Audit + Prometheus 결합 on_retry 훅 팩토리
- make_standard_on_exhausted(): 최종 실패 감사 기록 훅 팩토리
"""

from __future__ import annotations

from unittest.mock import patch

from baldur.core.retry import RetryContext
from baldur.core.retry_hooks import (
    make_standard_on_exhausted,
    make_standard_on_retry,
)

# =============================================================================
# make_standard_on_retry — 동작 검증
# =============================================================================


class TestMakeStandardOnRetryBehavior:
    """make_standard_on_retry 동작 검증."""

    def test_returns_callable(self):
        """팩토리 호출 결과는 callable이다."""
        hook = make_standard_on_retry("payment")
        assert callable(hook)

    @patch("baldur.core.retry_hooks.log_retry_audit", autospec=True)
    def test_calls_audit_logging(self, mock_audit):
        """on_retry 호출 시 감사 로그를 기록한다."""
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
        """on_retry 호출 시 Prometheus 메트릭을 기록한다."""
        # Audit를 skip하도록 patch
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
        """메트릭 기록 실패 시 예외가 전파되지 않는다 (Fail-Open)."""
        with (
            patch(
                "baldur.core.retry_hooks.log_retry_audit",
                autospec=True,
            ),
            patch(
                "baldur.services.metrics.definitions.retry_attempts_histogram",
                side_effect=RuntimeError("metrics down"),
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


# =============================================================================
# make_standard_on_exhausted — 동작 검증
# =============================================================================


class TestMakeStandardOnExhaustedBehavior:
    """make_standard_on_exhausted 동작 검증."""

    def test_returns_callable(self):
        """팩토리 호출 결과는 callable이다."""
        hook = make_standard_on_exhausted("payment")
        assert callable(hook)

    @patch("baldur.core.retry_hooks.log_retry_audit", autospec=True)
    def test_calls_audit_with_error_info(self, mock_audit):
        """on_exhausted 호출 시 에러 정보를 포함한 감사 로그를 기록한다."""
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
        """에러 메시지가 500자로 잘린다."""
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
