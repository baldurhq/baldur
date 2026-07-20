"""
Unit tests for the RetryPolicyConfig settings class and RetryResult conversion.

Target: services/retry_handler/models.py
- RetryPolicyConfig: pure retry-only settings (externally dependent fields removed)
- RetryPolicyConfig.from_retry_config(): conversion from the legacy RetryConfig
- RetryResult.to_policy_result(): conversion to the unified PolicyResult type
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
# RetryPolicyConfig — contract
# =============================================================================


class TestRetryPolicyConfigContract:
    """Default-value contract of RetryPolicyConfig."""

    def test_max_attempts_default(self):
        """max_attempts defaults to 3."""
        assert RetryPolicyConfig().max_attempts == 3

    def test_backoff_base_default(self):
        """backoff_base defaults to 4."""
        assert RetryPolicyConfig().backoff_base == 4

    def test_backoff_max_default(self):
        """backoff_max defaults to 180."""
        assert RetryPolicyConfig().backoff_max == 180

    def test_jitter_percent_default(self):
        """jitter_percent defaults to 25."""
        assert RetryPolicyConfig().jitter_percent == 25

    def test_domain_default(self):
        """domain defaults to 'default'."""
        assert RetryPolicyConfig().domain == "default"

    def test_enable_dlq_default(self):
        """enable_dlq defaults to True."""
        assert RetryPolicyConfig().enable_dlq is True

    def test_retryable_exceptions_default(self):
        """retryable_exceptions defaults to (Exception,)."""
        assert RetryPolicyConfig().retryable_exceptions == (Exception,)

    def test_non_retryable_exceptions_default(self):
        """non_retryable_exceptions default includes CircuitBreakerError."""
        from baldur.core.exceptions import CircuitBreakerError

        assert RetryPolicyConfig().non_retryable_exceptions == (CircuitBreakerError,)

    def test_rate_limit_fields_exist_with_defaults(self):
        """The outbound 429 coordination fields live here, defaulting to on/unset.

        These fields moved onto the live config class when the synchronous retry
        stage started resolving a coordinator by default. ``rate_limit_aware`` is
        the per-policy opt-out (default on) and ``rate_limit_key`` overrides the
        coordination key (default unset, so the key falls back to ``domain``).
        """
        config = RetryPolicyConfig()
        assert config.rate_limit_aware is True
        assert config.rate_limit_key is None

    def test_no_throttle_fields(self):
        """No throttle-related fields exist."""
        assert not hasattr(RetryPolicyConfig, "throttle_aware")
        assert not hasattr(RetryPolicyConfig, "throttle_backoff_multiplier_cap")

    def test_no_critical_tier_fields(self):
        """No critical-tier fields exist."""
        assert not hasattr(RetryPolicyConfig, "critical_tier_full_stop_grace_retries")
        assert not hasattr(RetryPolicyConfig, "critical_tier_full_stop_max_delay")


# =============================================================================
# RetryPolicyConfig — behavior
# =============================================================================


class TestRetryPolicyConfigBehavior:
    """Custom-value assignment and conversion behavior of RetryPolicyConfig."""

    def test_custom_values(self):
        """Custom values are assigned correctly."""
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
        """from_retry_config() extracts only the pure retry fields from RetryConfig."""
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
# RetryResult.to_policy_result — behavior
# =============================================================================


class TestRetryResultToPolicyResultBehavior:
    """Conversion behavior from RetryResult to PolicyResult."""

    def test_success_maps_to_success_outcome(self):
        """A successful RetryResult converts to PolicyOutcome.SUCCESS."""
        result = RetryResult(
            success=True, action=RetryAction.SUCCESS, attempt=1, value="ok"
        )
        pr = result.to_policy_result()
        assert pr.outcome == PolicyOutcome.SUCCESS
        assert pr.value == "ok"
        assert pr.total_attempts == 1
        assert pr.error is None

    def test_failure_maps_to_failure_outcome(self):
        """A failed RetryResult converts to PolicyOutcome.FAILURE."""
        err = ConnectionError("timeout")
        result = RetryResult(
            success=False, action=RetryAction.ABORT, attempt=3, error=err
        )
        pr = result.to_policy_result()
        assert pr.outcome == PolicyOutcome.FAILURE
        assert pr.error is err
        assert pr.total_attempts == 3

    def test_dlq_result_includes_dlq_id_in_metadata(self):
        """A DLQ-routed result carries dlq_id in metadata."""
        result = RetryResult(
            success=False, action=RetryAction.DLQ, attempt=3, dlq_id=42
        )
        pr = result.to_policy_result()
        assert pr.outcome == PolicyOutcome.FAILURE
        assert pr.metadata["dlq_id"] == 42
        assert pr.metadata["action"] == RetryAction.DLQ.value

    def test_executed_policies_contains_retry(self):
        """The converted result's executed_policies contains 'retry'."""
        result = RetryResult(success=True, action=RetryAction.SUCCESS, attempt=1)
        pr = result.to_policy_result()
        assert "retry" in pr.executed_policies

    def test_all_action_values_in_metadata(self):
        """Every RetryAction value appears as a string in metadata['action']."""
        for action in RetryAction:
            result = RetryResult(
                success=(action == RetryAction.SUCCESS),
                action=action,
                attempt=1,
            )
            pr = result.to_policy_result()
            assert pr.metadata["action"] == action.value
