"""
Unit tests for the RetryPolicyConfig settings class and RetryResult conversion.

Target: services/retry_handler/models.py
- RetryPolicyConfig: pure retry-only settings (externally dependent fields removed)
- RetryResult.to_policy_result(): conversion to the unified PolicyResult type
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.retry_handler.models import (
    RetryAction,
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


class TestRetryPolicyConfigSourcingBehavior:
    """``from_settings`` sources the 429-coordination fields on both paths.

    Sourcing is what makes the per-domain opt-out reachable at all: every
    canonical surface builds its config through ``from_settings``, so a field
    that is not read there is a field an operator cannot set. The two paths are
    covered separately because they read from different places — the PRO
    RuntimeConfigManager's retry dict, and the static per-domain override map.
    """

    def _runtime_manager(self, retry_config):
        """Stand-in for the PRO RuntimeConfigManager.

        A plain namespace rather than a mock: only two methods are consulted,
        both returning plain dicts, and a spec-less mock here would happily
        answer for a method the real manager does not have.
        """
        return SimpleNamespace(
            get_retry_config=lambda: retry_config,
            get_dlq_config=lambda: {"enabled": True},
        )

    def _static_config(self, domain_configs):
        """Minimal settings tree for the PRO-absent static path."""
        return SimpleNamespace(
            core=SimpleNamespace(
                retry=SimpleNamespace(max_attempts=3, max_delay=180, max_elapsed=None),
                backoff=SimpleNamespace(legacy_base=4, legacy_jitter_percent=25),
            ),
            services_group=SimpleNamespace(dlq=SimpleNamespace(enabled=True)),
            domain_configs=domain_configs,
        )

    def _from_settings_static(self, domain, domain_configs):
        with patch(
            "baldur.factory.registry.ProviderRegistry.runtime_config_manager"
        ) as manager_slot:
            manager_slot.safe_get.return_value = None  # force the static path
            with patch(
                "baldur.services.retry_handler.models.get_config",
                return_value=self._static_config(domain_configs),
            ):
                return RetryPolicyConfig.from_settings(domain)

    def test_static_path_reads_the_per_domain_override(self):
        """A domain that opts out of coordination gets a config that says so."""
        cfg = self._from_settings_static(
            "payment",
            {
                "payment": {
                    "retry": {
                        "rate_limit_aware": False,
                        "rate_limit_key": "stripe-api",
                    }
                }
            },
        )

        assert cfg.rate_limit_aware is False
        assert cfg.rate_limit_key == "stripe-api"

    def test_static_path_defaults_to_coordinating_with_no_override(self):
        """No per-domain entry means default-on and no key override."""
        cfg = self._from_settings_static("payment", {})

        assert cfg.rate_limit_aware is True
        assert cfg.rate_limit_key is None

    def test_runtime_config_manager_path_reads_the_retry_config(self):
        """The PRO path sources both fields off the runtime retry dict."""
        manager = self._runtime_manager(
            {
                "max_attempts": 3,
                "rate_limit_aware": False,
                "rate_limit_key": "stripe-api",
            }
        )

        with patch(
            "baldur.factory.registry.ProviderRegistry.runtime_config_manager"
        ) as manager_slot:
            manager_slot.safe_get.return_value = manager
            cfg = RetryPolicyConfig.from_settings("payment")

        assert cfg.rate_limit_aware is False
        assert cfg.rate_limit_key == "stripe-api"

    def test_runtime_config_manager_path_defaults_when_the_keys_are_absent(self):
        """An older runtime config without these keys still coordinates.

        The PRO runtime config is data, not code — a deployment carrying a dict
        written before these keys existed must land on the shipped default
        rather than on a falsy miss.
        """
        manager = self._runtime_manager({"max_attempts": 3})

        with patch(
            "baldur.factory.registry.ProviderRegistry.runtime_config_manager"
        ) as manager_slot:
            manager_slot.safe_get.return_value = manager
            cfg = RetryPolicyConfig.from_settings("payment")

        assert cfg.rate_limit_aware is True
        assert cfg.rate_limit_key is None


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
