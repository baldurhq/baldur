"""
메트릭 is_synthetic 레이블 자동 태깅 테스트.

TestModeContext 활성화 시 메트릭에 is_synthetic 레이블이 자동 추가되는지 검증합니다.
"""

from unittest.mock import MagicMock, patch

from baldur.core.test_mode_context import TestModeContext


class TestMetricSyntheticLabel:
    """메트릭 합성 레이블 테스트."""

    def test_dlq_metric_without_synthetic_context(self):
        """합성 컨텍스트 없이 DLQ 메트릭 기록."""
        from baldur.services.metrics import recorders

        assert TestModeContext.is_synthetic() is False

        mock_metrics = MagicMock()
        with patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics):
            recorders.record_dlq_item_created(
                domain="external_service", failure_type="timeout"
            )

            mock_metrics.dlq.record_item_created.assert_called_once_with(
                "external_service", "timeout"
            )

    def test_dlq_metric_with_synthetic_context(self):
        """합성 컨텍스트에서 DLQ 메트릭 기록 — recorder가 is_synthetic 처리."""
        from baldur.services.metrics import recorders

        with TestModeContext.start(session_id="test-123"):
            mock_metrics = MagicMock()
            with patch(
                "baldur.metrics.prometheus.get_metrics", return_value=mock_metrics
            ):
                recorders.record_dlq_item_created(
                    domain="external_service", failure_type="timeout"
                )

                mock_metrics.dlq.record_item_created.assert_called_once_with(
                    "external_service", "timeout"
                )

    def test_retry_metric_synthetic_label(self):
        """Retry 메트릭 합성 레이블 테스트 — recorder가 is_synthetic 처리."""
        from baldur.services.metrics import recorders

        mock_metrics = MagicMock()
        with patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics):
            recorders.record_retry_attempt(
                domain="internal_process", attempt_count=3, outcome="success"
            )

            mock_metrics.retry.record_attempt.assert_called_once_with(
                "internal_process", 3, "success"
            )

        with TestModeContext.start():
            mock_metrics2 = MagicMock()
            with patch(
                "baldur.metrics.prometheus.get_metrics", return_value=mock_metrics2
            ):
                recorders.record_retry_attempt(
                    domain="internal_process", attempt_count=3, outcome="success"
                )

                mock_metrics2.retry.record_attempt.assert_called_once_with(
                    "internal_process", 3, "success"
                )

    def test_circuit_breaker_metric_synthetic_label(self):
        """Circuit Breaker 메트릭 합성 레이블 테스트 — recorder가 is_synthetic 처리."""
        from baldur.services.metrics import recorders

        with TestModeContext.start(session_id="cb-test"):
            mock_metrics = MagicMock()
            with patch(
                "baldur.metrics.prometheus.get_metrics", return_value=mock_metrics
            ):
                with patch(
                    "baldur.core.cb_namespace.parse_composite_cb_name",
                    return_value=("payment-gateway", ""),
                ):
                    recorders.record_circuit_breaker_state_change(
                        service="payment-gateway",
                        from_state="closed",
                        to_state="open",
                    )

                    mock_metrics.circuit_breaker.record_state_change.assert_called_once_with(
                        "payment-gateway", "closed", "open", ""
                    )

    def test_metric_is_synthetic_label(self):
        """
        문서 137 섹션 5.1 명시 테스트: 메트릭 레이블 자동 설정.

        TestModeContext 활성화 시 메트릭에 is_synthetic="true" 레이블이
        자동으로 설정되는지 검증합니다.
        """
        assert TestModeContext.get_synthetic_label_value() == "false"

        with TestModeContext.start():
            assert TestModeContext.get_synthetic_label_value() == "true"

        assert TestModeContext.get_synthetic_label_value() == "false"

    def test_replay_metric_synthetic_label(self):
        """Replay 메트릭 합성 레이블 테스트 — recorder가 is_synthetic 처리."""
        from baldur.services.metrics import recorders

        with TestModeContext.start():
            mock_metrics = MagicMock()
            with patch(
                "baldur.metrics.prometheus.get_metrics", return_value=mock_metrics
            ):
                recorders.record_replay_attempt(
                    domain="async_task", replay_type="batch", success=True
                )

                mock_metrics.replay.record_attempt.assert_called_once_with(
                    "async_task", "batch", True
                )


class TestMetricLabelTransition:
    """메트릭 레이블 상태 전환 테스트."""

    def test_label_changes_with_context(self):
        """컨텍스트 진입/종료 시 레이블 변경."""
        assert TestModeContext.get_synthetic_label_value() == "false"

        with TestModeContext.start():
            assert TestModeContext.get_synthetic_label_value() == "true"

        assert TestModeContext.get_synthetic_label_value() == "false"

    def test_nested_context_label_values(self):
        """중첩 컨텍스트에서 레이블 값."""
        assert TestModeContext.get_synthetic_label_value() == "false"

        with TestModeContext.start():
            assert TestModeContext.get_synthetic_label_value() == "true"

            with TestModeContext.start():
                assert TestModeContext.get_synthetic_label_value() == "true"

            assert TestModeContext.get_synthetic_label_value() == "true"

        assert TestModeContext.get_synthetic_label_value() == "false"
