"""
AuditHook / MetricsHook / EventBusHook 단위 테스트 (#231).

테스트 대상:
- resilience/policies/hooks/audit.py (AuditHook)
- resilience/policies/hooks/metrics.py (MetricsHook, _ensure_metrics)
- resilience/policies/hooks/event_bus.py (EventBusHook)
- resilience/policies/hooks/__init__.py (re-export)

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): 하드코딩 기대값 (event_prefix 기본값, 메서드 존재)
- 동작 검증(Behavior): 소스 참조 (PolicyResult, PolicyOutcome)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.resilience_policy import (
    PolicyOutcome,
    PolicyResult,
)
from baldur.resilience.policies.hooks import (
    AuditHook,
    EventBusHook,
    MetricsHook,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def success_result():
    """성공 PolicyResult fixture."""
    return PolicyResult(
        value="ok",
        outcome=PolicyOutcome.SUCCESS,
        executed_policies=["retry", "circuit_breaker"],
        total_attempts=2,
        total_duration_ms=150.5,
    )


@pytest.fixture
def failure_result():
    """실패 PolicyResult fixture."""
    return PolicyResult(
        value=None,
        outcome=PolicyOutcome.FAILURE,
        error=RuntimeError("test failure"),
        total_attempts=3,
        total_duration_ms=500.0,
    )


# =============================================================================
# 계약 검증 — AuditHook 인터페이스
# =============================================================================


class TestAuditHookContract:
    """AuditHook PolicyHook Protocol 준수 계약 검증."""

    def test_has_on_execute(self):
        """on_execute 메서드가 존재한다."""
        assert hasattr(AuditHook, "on_execute")

    def test_has_on_success(self):
        """on_success 메서드가 존재한다."""
        assert hasattr(AuditHook, "on_success")

    def test_has_on_failure(self):
        """on_failure 메서드가 존재한다."""
        assert hasattr(AuditHook, "on_failure")

    def test_has_on_retry(self):
        """on_retry 메서드가 존재한다."""
        assert hasattr(AuditHook, "on_retry")

    def test_has_on_reject(self):
        """on_reject 메서드가 존재한다."""
        assert hasattr(AuditHook, "on_reject")


# =============================================================================
# 동작 검증 — AuditHook
# =============================================================================


class TestAuditHookBehavior:
    """AuditHook 동작 검증.

    AuditHook은 structlog.get_logger()를 사용하므로
    structlog.testing.capture_logs()로 로그를 캡처한다.
    capsys는 configure_structlog() 호출 후 stdlib 라우팅 시 캡처 불가.
    """

    def test_on_execute_logs_debug(self):
        """on_execute는 DEBUG 레벨로 로깅한다."""
        hook = AuditHook()
        with capture_logs() as cap_logs:
            hook.on_execute("composer", 1)
        assert any(e["event"] == "policy_pipeline.execution_started" for e in cap_logs)

    def test_on_success_logs_info(self, success_result):
        """on_success는 INFO 레벨로 policies, attempts, duration을 로깅한다."""
        hook = AuditHook()
        with capture_logs() as cap_logs:
            hook.on_success("composer", success_result)
        assert any(
            e["event"] == "policy_pipeline.execution_succeeded" for e in cap_logs
        )

    def test_on_failure_logs_warning(self):
        """on_failure는 WARNING 레벨로 error와 attempts를 로깅한다."""
        hook = AuditHook()
        err = RuntimeError("test error")
        with capture_logs() as cap_logs:
            hook.on_failure("composer", err, 3)
        assert any(e["event"] == "policy_pipeline.execution_failed" for e in cap_logs)

    def test_on_retry_logs_info(self):
        """on_retry는 INFO 레벨로 policy, attempt, delay를 로깅한다."""
        hook = AuditHook()
        with capture_logs() as cap_logs:
            hook.on_retry("retry_policy", 2, 1.5)
        assert any(e["event"] == "policy_pipeline.retry_scheduled" for e in cap_logs)

    def test_on_reject_logs_warning(self):
        """on_reject는 WARNING 레벨로 guard와 reason을 로깅한다."""
        hook = AuditHook()
        with capture_logs() as cap_logs:
            hook.on_reject("kill_switch", "system disabled")
        assert any(e["event"] == "policy_pipeline.execution_rejected" for e in cap_logs)
        assert any("kill_switch" in str(e.values()) for e in cap_logs)

    def test_does_not_raise(self, success_result):
        """AuditHook의 모든 메서드는 예외를 던지지 않는다."""
        hook = AuditHook()
        # 예외 없이 호출 완료
        hook.on_execute("composer", 1)
        hook.on_success("composer", success_result)
        hook.on_failure("composer", RuntimeError("err"), 1)
        hook.on_retry("policy", 1, 0.5)
        hook.on_reject("guard", "reason")


# =============================================================================
# 계약 검증 — MetricsHook 인터페이스
# =============================================================================


class TestMetricsHookContract:
    """MetricsHook PolicyHook Protocol 준수 계약 검증."""

    def test_has_on_execute(self):
        """on_execute 메서드가 존재한다."""
        assert hasattr(MetricsHook, "on_execute")

    def test_has_on_success(self):
        """on_success 메서드가 존재한다."""
        assert hasattr(MetricsHook, "on_success")

    def test_has_on_failure(self):
        """on_failure 메서드가 존재한다."""
        assert hasattr(MetricsHook, "on_failure")

    def test_has_on_retry(self):
        """on_retry 메서드가 존재한다."""
        assert hasattr(MetricsHook, "on_retry")

    def test_has_on_reject(self):
        """on_reject 메서드가 존재한다."""
        assert hasattr(MetricsHook, "on_reject")


# =============================================================================
# 동작 검증 — MetricsHook
# =============================================================================


class TestMetricsHookBehavior:
    """MetricsHook 동작 검증."""

    def test_on_execute_noop(self):
        """on_execute는 아무 동작도 하지 않는다."""
        hook = MetricsHook()
        # 예외 없이 호출
        hook.on_execute("composer", 1)

    def test_on_retry_noop(self):
        """on_retry는 Composer 레벨에서 미사용."""
        hook = MetricsHook()
        hook.on_retry("policy", 1, 0.5)

    def test_on_success_without_prometheus(self, success_result):
        """prometheus_client 미설치 시 on_success가 오류 없이 동작한다."""
        hook = MetricsHook()
        # _ensure_metrics가 False를 반환하면 메트릭 기록 건너뜀
        with patch(
            "baldur.resilience.policies.hooks.metrics._ensure_metrics",
            return_value=False,
        ):
            hook.on_success("composer", success_result)

    def test_on_failure_without_prometheus(self):
        """prometheus_client 미설치 시 on_failure가 오류 없이 동작한다."""
        hook = MetricsHook()
        with patch(
            "baldur.resilience.policies.hooks.metrics._ensure_metrics",
            return_value=False,
        ):
            hook.on_failure("composer", RuntimeError("err"), 1)

    def test_on_reject_without_prometheus(self):
        """prometheus_client 미설치 시 on_reject가 오류 없이 동작한다."""
        hook = MetricsHook()
        with patch(
            "baldur.resilience.policies.hooks.metrics._ensure_metrics",
            return_value=False,
        ):
            hook.on_reject("kill_switch", "disabled")

    def test_on_success_with_prometheus(self, success_result):
        """prometheus_client 사용 가능 시 Counter/Histogram이 호출된다."""
        hook = MetricsHook()
        mock_counter = MagicMock()
        mock_histogram = MagicMock()

        with (
            patch(
                "baldur.resilience.policies.hooks.metrics._ensure_metrics",
                return_value=True,
            ),
            patch(
                "baldur.resilience.policies.hooks.metrics._pipeline_success_total",
                mock_counter,
            ),
            patch(
                "baldur.resilience.policies.hooks.metrics._pipeline_duration_seconds",
                mock_histogram,
            ),
        ):
            hook.on_success("composer", success_result)

        mock_counter.labels.assert_called_once_with(pipeline="composer")
        mock_counter.labels().inc.assert_called_once()
        mock_histogram.labels.assert_called_once_with(pipeline="composer")
        mock_histogram.labels().observe.assert_called_once_with(
            success_result.total_duration_ms / 1000.0
        )

    def test_on_failure_with_prometheus(self):
        """prometheus_client 사용 가능 시 failure Counter가 호출된다."""
        hook = MetricsHook()
        mock_counter = MagicMock()
        err = RuntimeError("test")

        with (
            patch(
                "baldur.resilience.policies.hooks.metrics._ensure_metrics",
                return_value=True,
            ),
            patch(
                "baldur.resilience.policies.hooks.metrics._pipeline_failure_total",
                mock_counter,
            ),
        ):
            hook.on_failure("composer", err, 3)

        mock_counter.labels.assert_called_once_with(
            pipeline="composer", error_type="RuntimeError"
        )
        mock_counter.labels().inc.assert_called_once()

    def test_on_reject_with_prometheus(self):
        """prometheus_client 사용 가능 시 rejected Counter가 호출된다."""
        hook = MetricsHook()
        mock_counter = MagicMock()

        with (
            patch(
                "baldur.resilience.policies.hooks.metrics._ensure_metrics",
                return_value=True,
            ),
            patch(
                "baldur.resilience.policies.hooks.metrics._pipeline_rejected_total",
                mock_counter,
            ),
        ):
            hook.on_reject("kill_switch", "disabled")

        mock_counter.labels.assert_called_once_with(
            pipeline="composer", guard="kill_switch"
        )
        mock_counter.labels().inc.assert_called_once()


# =============================================================================
# 계약 검증 — EventBusHook
# =============================================================================


class TestEventBusHookContract:
    """EventBusHook 계약 검증."""

    def test_default_event_prefix(self):
        """기본 event_prefix는 'policy_pipeline'이다."""
        hook = EventBusHook()
        assert hook._event_prefix == "policy_pipeline"

    def test_custom_event_prefix(self):
        """생성 시 event_prefix를 지정할 수 있다."""
        hook = EventBusHook(event_prefix="custom_prefix")
        assert hook._event_prefix == "custom_prefix"

    def test_has_on_execute(self):
        """on_execute 메서드가 존재한다."""
        assert hasattr(EventBusHook, "on_execute")

    def test_has_on_success(self):
        """on_success 메서드가 존재한다."""
        assert hasattr(EventBusHook, "on_success")

    def test_has_on_failure(self):
        """on_failure 메서드가 존재한다."""
        assert hasattr(EventBusHook, "on_failure")

    def test_has_on_retry(self):
        """on_retry 메서드가 존재한다."""
        assert hasattr(EventBusHook, "on_retry")

    def test_has_on_reject(self):
        """on_reject 메서드가 존재한다."""
        assert hasattr(EventBusHook, "on_reject")


# =============================================================================
# 동작 검증 — EventBusHook
# =============================================================================


class TestEventBusHookBehavior:
    """EventBusHook 동작 검증."""

    def test_ensure_bus_import_error_returns_false(self):
        """EventBus import 실패 시 _ensure_bus()는 False를 반환한다."""
        hook = EventBusHook()
        with patch.dict("sys.modules", {"baldur.services.event_bus": None}):
            result = hook._ensure_bus()
        assert result is False
        assert hook._initialized is True
        assert hook._bus is None

    def test_ensure_bus_success(self):
        """EventBus import 성공 시 _ensure_bus()는 True를 반환한다."""
        hook = EventBusHook()
        mock_bus = MagicMock()
        mock_module = MagicMock()
        mock_module.get_event_bus.return_value = mock_bus

        with patch.dict("sys.modules", {"baldur.services.event_bus": mock_module}):
            result = hook._ensure_bus()

        assert result is True
        assert hook._bus is mock_bus

    def test_ensure_bus_cached(self):
        """_ensure_bus()는 초기화 후 캐시된다."""
        hook = EventBusHook()
        hook._initialized = True
        hook._bus = MagicMock()
        # 이미 초기화되었으므로 import 시도 없이 True 반환
        result = hook._ensure_bus()
        assert result is True

    def test_on_success_publishes_event(self, success_result):
        """on_success는 EventBus에 success 이벤트를 발행한다."""
        hook = EventBusHook(event_prefix="test_prefix")
        mock_bus = MagicMock()
        hook._initialized = True
        hook._bus = mock_bus

        hook.on_success("composer", success_result)

        mock_bus.publish.assert_called_once_with(
            "test_prefix.success",
            {
                "policies": success_result.executed_policies,
                "attempts": success_result.total_attempts,
                "duration_ms": success_result.total_duration_ms,
            },
        )

    def test_on_failure_publishes_event(self):
        """on_failure는 EventBus에 failure 이벤트를 발행한다."""
        hook = EventBusHook(event_prefix="test_prefix")
        mock_bus = MagicMock()
        hook._initialized = True
        hook._bus = mock_bus

        err = RuntimeError("test error message")
        hook.on_failure("composer", err, 3)

        mock_bus.publish.assert_called_once_with(
            "test_prefix.failure",
            {
                "error_type": "RuntimeError",
                "error_message": "test error message",
                "attempts": 3,
            },
        )

    def test_on_reject_publishes_event(self):
        """on_reject는 EventBus에 rejected 이벤트를 발행한다."""
        hook = EventBusHook(event_prefix="test_prefix")
        mock_bus = MagicMock()
        hook._initialized = True
        hook._bus = mock_bus

        hook.on_reject("kill_switch", "system disabled")

        mock_bus.publish.assert_called_once_with(
            "test_prefix.rejected",
            {
                "guard": "kill_switch",
                "reason": "system disabled",
            },
        )

    def test_on_success_no_bus_no_error(self, success_result):
        """EventBus 미사용 시 on_success가 오류 없이 동작한다."""
        hook = EventBusHook()
        hook._initialized = True
        hook._bus = None  # EventBus 없음
        # 예외 없이 호출
        hook.on_success("composer", success_result)

    def test_on_failure_no_bus_no_error(self):
        """EventBus 미사용 시 on_failure가 오류 없이 동작한다."""
        hook = EventBusHook()
        hook._initialized = True
        hook._bus = None
        hook.on_failure("composer", RuntimeError("err"), 1)

    def test_on_reject_no_bus_no_error(self):
        """EventBus 미사용 시 on_reject가 오류 없이 동작한다."""
        hook = EventBusHook()
        hook._initialized = True
        hook._bus = None
        hook.on_reject("guard", "reason")

    def test_on_execute_noop(self):
        """on_execute는 아무 동작도 하지 않는다."""
        hook = EventBusHook()
        hook.on_execute("composer", 1)

    def test_on_retry_noop(self):
        """on_retry는 Composer 레벨에서 미사용."""
        hook = EventBusHook()
        hook.on_retry("policy", 1, 0.5)

    def test_publish_exception_fail_open(self, success_result):
        """EventBus publish 실패 시 Fail-Open (예외 미전파)."""
        hook = EventBusHook()
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = RuntimeError("publish failed")
        hook._initialized = True
        hook._bus = mock_bus

        # 예외를 던지지 않아야 한다
        hook.on_success("composer", success_result)

    def test_error_message_truncated_at_500(self):
        """on_failure의 error_message는 500자로 잘린다."""
        hook = EventBusHook()
        mock_bus = MagicMock()
        hook._initialized = True
        hook._bus = mock_bus

        long_msg = "x" * 1000
        err = RuntimeError(long_msg)
        hook.on_failure("composer", err, 1)

        published_data = mock_bus.publish.call_args[0][1]
        assert len(published_data["error_message"]) <= 500


# =============================================================================
# 계약 검증 — hooks __init__.py re-export
# =============================================================================


class TestHooksInitReexportContract:
    """hooks/__init__.py re-export 계약 검증."""

    def test_audit_hook_exported(self):
        """AuditHook이 hooks 패키지에서 import 가능하다."""
        from baldur.resilience.policies.hooks import AuditHook

        assert AuditHook is not None

    def test_metrics_hook_exported(self):
        """MetricsHook이 hooks 패키지에서 import 가능하다."""
        from baldur.resilience.policies.hooks import MetricsHook

        assert MetricsHook is not None

    def test_event_bus_hook_exported(self):
        """EventBusHook이 hooks 패키지에서 import 가능하다."""
        from baldur.resilience.policies.hooks import EventBusHook

        assert EventBusHook is not None
