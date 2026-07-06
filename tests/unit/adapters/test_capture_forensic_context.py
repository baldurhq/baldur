"""
ForensicCapture.capture() 동작 검증.

baldur.services.forensic_context 모듈이 구현되어 있으므로,
정상 캡처, ImportError graceful 처리, 예외 시 None 반환을 검증한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.adapters.celery.integrations.forensic_capture import (
    ForensicCapture,
)


class TestCaptureForensicContextBehavior:
    """ForensicCapture.capture() 동작 검증."""

    def test_returns_context_with_real_module(self):
        """forensic_context 모듈 존재 시 캡처 결과 반환."""
        capture = ForensicCapture()
        result = capture.capture(
            task_name="test_task",
            task_id="abc-123",
            exception=ValueError("test"),
            args=(),
            kwargs={},
            einfo=None,
        )
        assert result is not None
        assert result["task_id"] == "abc-123"
        assert result["task_name"] == "test_task"

    def test_returns_none_when_module_not_found(self):
        """forensic_context 모듈 미존재 시 None 반환 (ImportError graceful)."""
        capture = ForensicCapture()
        with patch.dict("sys.modules", {"baldur.services.forensic_context": None}):
            result = capture.capture(
                task_name="test_task",
                task_id="abc-123",
                exception=ValueError("test"),
                args=(),
                kwargs={},
                einfo=None,
            )
            assert result is None

    def test_returns_none_on_unexpected_exception(self):
        """캡처 중 예외 발생 시 None 반환."""
        capture = ForensicCapture()
        with patch.dict(
            "sys.modules",
            {
                "baldur.services.forensic_context": MagicMock(
                    capture_forensic_context=MagicMock(
                        side_effect=RuntimeError("unexpected")
                    )
                )
            },
        ):
            result = capture.capture(
                task_name="test_task",
                task_id="abc-123",
                exception=ValueError("test"),
                args=(),
                kwargs={},
                einfo=None,
            )
            assert result is None
