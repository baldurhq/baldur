"""
RetryPolicyConfig 설정 클래스 및 RetryResult 변환 단위 테스트.

테스트 대상: services/retry_handler/models.py
- RetryPolicyConfig: 순수 재시도 전용 설정 (외부 의존 필드 제거)
- RetryPolicyConfig.from_retry_config(): 레거시 RetryConfig에서 변환
- RetryResult.to_policy_result(): PolicyResult 통합 타입으로 변환
"""

from __future__ import annotations

from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.retry_handler.models import (
    RetryAction,
    RetryConfig,
    RetryPolicyConfig,
    RetryResult,
)

# =============================================================================
# RetryPolicyConfig — 계약 검증
# =============================================================================


class TestRetryPolicyConfigContract:
    """RetryPolicyConfig 기본값 계약 검증."""

    def test_max_attempts_default(self):
        """max_attempts 기본값은 3이다."""
        assert RetryPolicyConfig().max_attempts == 3

    def test_backoff_base_default(self):
        """backoff_base 기본값은 4이다."""
        assert RetryPolicyConfig().backoff_base == 4

    def test_backoff_max_default(self):
        """backoff_max 기본값은 180이다."""
        assert RetryPolicyConfig().backoff_max == 180

    def test_jitter_percent_default(self):
        """jitter_percent 기본값은 25이다."""
        assert RetryPolicyConfig().jitter_percent == 25

    def test_domain_default(self):
        """domain 기본값은 'default'이다."""
        assert RetryPolicyConfig().domain == "default"

    def test_enable_dlq_default(self):
        """enable_dlq 기본값은 True이다."""
        assert RetryPolicyConfig().enable_dlq is True

    def test_retryable_exceptions_default(self):
        """retryable_exceptions 기본값은 (Exception,)이다."""
        assert RetryPolicyConfig().retryable_exceptions == (Exception,)

    def test_non_retryable_exceptions_default(self):
        """non_retryable_exceptions default includes CircuitBreakerError."""
        from baldur.core.exceptions import CircuitBreakerError

        assert RetryPolicyConfig().non_retryable_exceptions == (CircuitBreakerError,)

    def test_no_rate_limit_fields(self):
        """rate_limit 관련 필드가 존재하지 않는다."""
        assert not hasattr(RetryPolicyConfig, "rate_limit_aware")
        assert not hasattr(RetryPolicyConfig, "rate_limit_key")

    def test_no_throttle_fields(self):
        """throttle 관련 필드가 존재하지 않는다."""
        assert not hasattr(RetryPolicyConfig, "throttle_aware")
        assert not hasattr(RetryPolicyConfig, "throttle_backoff_multiplier_cap")

    def test_no_critical_tier_fields(self):
        """critical_tier 관련 필드가 존재하지 않는다."""
        assert not hasattr(RetryPolicyConfig, "critical_tier_full_stop_grace_retries")
        assert not hasattr(RetryPolicyConfig, "critical_tier_full_stop_max_delay")


# =============================================================================
# RetryPolicyConfig — 동작 검증
# =============================================================================


class TestRetryPolicyConfigBehavior:
    """RetryPolicyConfig 커스텀 값 설정 및 변환 동작 검증."""

    def test_custom_values(self):
        """커스텀 값이 올바르게 설정된다."""
        config = RetryPolicyConfig(
            max_attempts=5,
            backoff_base=2,
            backoff_max=60,
            jitter_percent=10,
            domain="payment",
            enable_dlq=False,
            retryable_exceptions=(ConnectionError, TimeoutError),
            non_retryable_exceptions=(ValueError,),
        )
        assert config.max_attempts == 5
        assert config.domain == "payment"
        assert config.enable_dlq is False
        assert config.retryable_exceptions == (ConnectionError, TimeoutError)
        assert config.non_retryable_exceptions == (ValueError,)

    def test_from_retry_config_extracts_pure_retry_fields(self):
        """from_retry_config()은 RetryConfig에서 순수 재시도 필드만 추출한다."""
        legacy = RetryConfig(
            max_attempts=5,
            backoff_base=2,
            backoff_max=60,
            jitter_percent=10,
            domain="payment",
            enable_dlq=False,
            retryable_exceptions=(ConnectionError,),
            non_retryable_exceptions=(ValueError,),
            rate_limit_aware=True,
            throttle_aware=True,
            throttle_backoff_multiplier_cap=4.0,
        )
        policy_config = RetryPolicyConfig.from_retry_config(legacy)

        assert policy_config.max_attempts == legacy.max_attempts
        assert policy_config.backoff_base == legacy.backoff_base
        assert policy_config.backoff_max == legacy.backoff_max
        assert policy_config.jitter_percent == legacy.jitter_percent
        assert policy_config.domain == legacy.domain
        assert policy_config.enable_dlq == legacy.enable_dlq
        assert policy_config.retryable_exceptions == legacy.retryable_exceptions
        assert policy_config.non_retryable_exceptions == legacy.non_retryable_exceptions


# =============================================================================
# RetryResult.to_policy_result — 동작 검증
# =============================================================================


class TestRetryResultToPolicyResultBehavior:
    """RetryResult → PolicyResult 변환 동작 검증."""

    def test_success_maps_to_success_outcome(self):
        """성공 RetryResult는 PolicyOutcome.SUCCESS로 변환된다."""
        result = RetryResult(
            success=True, action=RetryAction.SUCCESS, attempt=1, value="ok"
        )
        pr = result.to_policy_result()
        assert pr.outcome == PolicyOutcome.SUCCESS
        assert pr.value == "ok"
        assert pr.total_attempts == 1
        assert pr.error is None

    def test_failure_maps_to_failure_outcome(self):
        """실패 RetryResult는 PolicyOutcome.FAILURE로 변환된다."""
        err = ConnectionError("timeout")
        result = RetryResult(
            success=False, action=RetryAction.ABORT, attempt=3, error=err
        )
        pr = result.to_policy_result()
        assert pr.outcome == PolicyOutcome.FAILURE
        assert pr.error is err
        assert pr.total_attempts == 3

    def test_dlq_result_includes_dlq_id_in_metadata(self):
        """DLQ 이동 결과는 metadata에 dlq_id를 포함한다."""
        result = RetryResult(
            success=False, action=RetryAction.DLQ, attempt=3, dlq_id=42
        )
        pr = result.to_policy_result()
        assert pr.outcome == PolicyOutcome.FAILURE
        assert pr.metadata["dlq_id"] == 42
        assert pr.metadata["action"] == RetryAction.DLQ.value

    def test_executed_policies_contains_retry(self):
        """변환 결과의 executed_policies에 'retry'가 포함된다."""
        result = RetryResult(success=True, action=RetryAction.SUCCESS, attempt=1)
        pr = result.to_policy_result()
        assert "retry" in pr.executed_policies

    def test_all_action_values_in_metadata(self):
        """모든 RetryAction 값이 metadata['action']에 문자열로 포함된다."""
        for action in RetryAction:
            result = RetryResult(
                success=(action == RetryAction.SUCCESS),
                action=action,
                attempt=1,
            )
            pr = result.to_policy_result()
            assert pr.metadata["action"] == action.value
