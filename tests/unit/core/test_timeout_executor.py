"""Unit tests for core/timeout_executor.py — TimeoutExecutor.

Verification techniques applied:
- Contract: HEARTBEAT_INTERVAL_SECONDS, LOCK_EXTEND_SECONDS constants
- Behavior: fn execution, timeout, lock heartbeat, pre_execute_hook, cooperative cancellation
- Dependency interaction: lock.extend() calls
- Edge case: fn raising exception, lock extend failure (fail-open)
"""

from __future__ import annotations

import contextvars
import threading
from unittest.mock import MagicMock

import pytest

from baldur.core.exceptions import StepTimeoutError
from baldur.core.timeout_executor import (
    HEARTBEAT_INTERVAL_SECONDS,
    LOCK_EXTEND_SECONDS,
    TimeoutExecutor,
)

# ── Contract Tests ──────────────────────────────────────────


class TestTimeoutExecutorContract:
    """Design contract values for TimeoutExecutor constants."""

    def test_heartbeat_interval_default_is_60(self):
        """HEARTBEAT_INTERVAL_SECONDS design contract: 60."""
        assert HEARTBEAT_INTERVAL_SECONDS == 60

    def test_lock_extend_default_is_300(self):
        """LOCK_EXTEND_SECONDS design contract: 300."""
        assert LOCK_EXTEND_SECONDS == 300


# ── Behavior Tests ──────────────────────────────────────────


class TestTimeoutExecutorBehavior:
    """TimeoutExecutor execution behavior verification."""

    def test_fn_completes_within_timeout_returns_result(self):
        """fn이 타임아웃 내에 완료되면 결과를 반환한다."""
        executor = TimeoutExecutor()
        result = executor.execute(
            fn=lambda stop_event: 42,
            timeout_seconds=5.0,
        )
        assert result == 42

    def test_fn_receives_stop_event_as_argument(self):
        """fn은 threading.Event를 첫 번째 인자로 받는다."""
        received_events = []

        def capture_fn(stop_event):
            received_events.append(stop_event)
            return "ok"

        executor = TimeoutExecutor()
        executor.execute(fn=capture_fn, timeout_seconds=5.0)

        assert len(received_events) == 1
        assert isinstance(received_events[0], threading.Event)

    def test_timeout_raises_step_timeout_error(self):
        """타임아웃 초과 시 StepTimeoutError를 발생시킨다."""
        executor = TimeoutExecutor()

        with pytest.raises(StepTimeoutError) as exc_info:
            executor.execute(
                fn=lambda stop_event: stop_event.wait(10),
                timeout_seconds=0.2,
                heartbeat_interval=0.1,
            )

        assert exc_info.value.timeout_seconds == 0.2

    def test_fn_exception_propagates(self):
        """fn에서 발생한 예외가 호출자에게 전파된다."""
        executor = TimeoutExecutor()

        with pytest.raises(ValueError, match="test error"):
            executor.execute(
                fn=lambda stop_event: (_ for _ in ()).throw(ValueError("test error")),
                timeout_seconds=5.0,
            )

    def test_pre_execute_hook_called_before_and_after(self):
        """pre_execute_hook이 fn 전후로 호출된다."""
        hook = MagicMock()

        executor = TimeoutExecutor()
        executor.execute(
            fn=lambda stop_event: "result",
            timeout_seconds=5.0,
            pre_execute_hook=hook,
        )

        assert hook.call_count == 2


class TestTimeoutExecutorLockHeartbeatBehavior:
    """Lock heartbeat extension behavior verification."""

    def test_lock_extend_called_during_long_execution(self):
        """장기 실행 시 lock.extend()가 heartbeat 간격으로 호출된다."""
        mock_lock = MagicMock()
        mock_lock.extend.return_value = True

        executor = TimeoutExecutor()
        executor.execute(
            fn=lambda stop_event: stop_event.wait(0.35) or "done",
            timeout_seconds=1.0,
            lock=mock_lock,
            lock_namespace="test-ns",
            session_id="test-session",
            heartbeat_interval=0.1,
            extend_seconds=300,
        )

        assert mock_lock.extend.call_count >= 2
        mock_lock.extend.assert_called_with(
            "test-ns",
            "test-session",
            additional_seconds=300,
        )

    def test_no_lock_extend_when_lock_is_none(self):
        """lock이 None이면 extend를 호출하지 않는다."""
        executor = TimeoutExecutor()
        result = executor.execute(
            fn=lambda stop_event: stop_event.wait(0.15) or "done",
            timeout_seconds=1.0,
            lock=None,
            heartbeat_interval=0.1,
        )
        assert result == "done"

    def test_lock_extend_failure_is_failopen(self):
        """lock.extend() 실패 시 실행은 계속된다 (fail-open)."""
        mock_lock = MagicMock()
        mock_lock.extend.side_effect = RuntimeError("Redis down")

        executor = TimeoutExecutor()
        result = executor.execute(
            fn=lambda stop_event: stop_event.wait(0.25) or "survived",
            timeout_seconds=1.0,
            lock=mock_lock,
            lock_namespace="ns",
            session_id="sid",
            heartbeat_interval=0.1,
        )

        assert result == "survived"
        assert mock_lock.extend.call_count >= 1


class TestTimeoutExecutorCooperativeCancellationBehavior:
    """Cooperative cancellation via stop_event."""

    def test_stop_event_set_on_timeout(self):
        """타임아웃 시 stop_event가 설정된다."""
        received_events = []

        def slow_fn(stop_event):
            received_events.append(stop_event)
            stop_event.wait(10)
            return "never"

        executor = TimeoutExecutor()

        with pytest.raises(StepTimeoutError):
            executor.execute(
                fn=slow_fn,
                timeout_seconds=0.2,
                heartbeat_interval=0.1,
            )

        assert len(received_events) == 1
        # stop_event should be set after timeout
        assert received_events[0].is_set()


class TestTimeoutExecutorContextVarPropagation:
    """Worker thread inherits the caller's ContextVars via copy_context()."""

    def test_contextvar_propagates_to_worker_thread(self):
        """fn running in the worker thread sees the caller's ContextVar value.

        Without copy_context(), a ThreadPoolExecutor worker starts with a fresh
        empty context and would read the default ("unset"). This guards the
        structlog/deadline/cell-actor propagation contract.
        """
        var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "test_timeout_executor_var", default="unset"
        )
        var.set("parent-value")

        captured: list[str] = []
        worker_thread_ids: list[int] = []

        def read_var(stop_event):
            captured.append(var.get())
            worker_thread_ids.append(threading.get_ident())
            return "ok"

        executor = TimeoutExecutor()
        result = executor.execute(fn=read_var, timeout_seconds=5.0)

        assert result == "ok"
        # fn actually ran on a different thread (real cross-thread propagation).
        assert worker_thread_ids[0] != threading.get_ident()
        assert captured == ["parent-value"]
