"""Absent tier configuration is reported, not audited per request.

A missing tiering configuration is settled before the first request and no
request changes it. The audit trail records configuration *changes*, so a
lookup that changed nothing does not belong in it — and one row per request
buried the trail it was meant to serve. The two anomaly branches (an open
tiering circuit, an engine error) keep auditing.
"""

from __future__ import annotations

from unittest.mock import patch

from baldur.scaling.tiering import TierFallbackReason
from baldur.scaling.tiering.registry import TierRegistry

_MODULE = "baldur.scaling.tiering.registry"


class TestConfigMissingReportingBehavior:
    def setup_method(self):
        TierRegistry.reset_instance()

    def teardown_method(self):
        TierRegistry.reset_instance()

    def test_the_absent_configuration_never_reaches_the_audit_trail(self):
        registry = TierRegistry()
        with patch.object(TierRegistry, "_log_fallback_audit") as audit:
            result = registry.resolve_tier_with_fallback(path="/_health")

        assert result.fallback_reason == TierFallbackReason.CONFIG_MISSING
        audit.assert_not_called()

    def test_it_is_stated_once_however_many_requests_arrive(self):
        registry = TierRegistry()
        with patch(f"{_MODULE}.logger") as logger:
            for _ in range(5):
                registry.resolve_tier_with_fallback(path="/_health")

        reported = [
            call
            for call in logger.warning.call_args_list
            if call.args and call.args[0] == "tier_registry.tier_config_missing"
        ]
        assert len(reported) == 1
