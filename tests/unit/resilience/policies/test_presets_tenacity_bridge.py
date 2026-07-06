"""Unit tests for presets ``retry_policy=`` parameter (impl 451 D12).

Scope:
- ``standard_pipeline(retry_policy=...)`` — replaces native RetryPolicy.
- ``ha_pipeline(retry_policy=...)`` — same, with bulkhead+hedging stack.
- Fail-fast ValueError when ``max_retries`` / ``domain`` are non-default and ``retry_policy`` is supplied
  (D1 framework principle: silent degradation is forbidden).
"""

from __future__ import annotations

import pytest
import tenacity

from baldur.bridges.tenacity.policy import TenacityBridgePolicy
from baldur.resilience.policies.presets import standard_pipeline

# =============================================================================
# Behavior — standard_pipeline retry_policy override
# =============================================================================


class TestStandardPipelineRetryPolicyOverrideBehavior:
    """``retry_policy=`` replaces the native ``RetryPolicy`` stage."""

    def test_retry_policy_replaces_native_retry_in_chain(self):
        """The bridge appears at the retry slot in ``_policies``."""
        bridge: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(2)
        )

        pipeline = standard_pipeline("svc", retry_policy=bridge)

        # _policies layout per presets.py: [retry_stage, CircuitBreakerPolicy].
        assert pipeline._policies[0] is bridge
        # native retry name is "retry"; bridge name is "tenacity_bridge".
        assert pipeline._policies[0].name == "tenacity_bridge"

    def test_retry_policy_none_keeps_native_retry(self):
        """Default path (retry_policy=None) builds native RetryPolicy."""
        pipeline = standard_pipeline("svc")

        # First stage is a native RetryPolicy.
        assert pipeline._policies[0].name == "retry"


# =============================================================================
# Behavior — fail-fast ValueError on retry_policy + non-default param conflict
# =============================================================================


class TestStandardPipelineRetryPolicyConflictBehavior:
    """Fail-fast ValueError fires only when ``max_retries`` / ``domain`` differ from defaults."""

    @pytest.mark.parametrize(
        ("max_retries", "domain", "should_raise"),
        [
            (3, "default", False),  # both default → accepted
            (5, "default", True),  # custom max_retries → raise
            (3, "payment", True),  # custom domain → raise
            (5, "payment", True),  # both custom → raise
        ],
        ids=["both_default", "custom_max", "custom_domain", "both_custom"],
    )
    def test_raises_only_when_non_default_params(
        self, max_retries, domain, should_raise
    ):
        """ValueError carries the conflicting kwargs and the preset name."""
        bridge: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1)
        )

        if should_raise:
            with pytest.raises(ValueError) as exc_info:
                standard_pipeline(
                    "svc",
                    max_retries=max_retries,
                    domain=domain,
                    retry_policy=bridge,
                )
            assert "standard_pipeline(retry_policy=...)" in str(exc_info.value)
            if max_retries != 3:
                assert "max_retries" in str(exc_info.value)
            if domain != "default":
                assert "domain" in str(exc_info.value)
        else:
            standard_pipeline(
                "svc",
                max_retries=max_retries,
                domain=domain,
                retry_policy=bridge,
            )
