"""
368 Django Settings/DB Decoupling Integration Tests

Validates the end-to-end flow of settings decoupling:
- CascadeSettings → get_cascade_chain_config() → CascadeChainConfig
- AuditSettings → get_audit_backpressure_config() → AuditBackpressureConfig
- DatabaseHealthProvider via ProviderRegistry

Test Categories:
    A. Settings → Config chain integration
    B. ProviderRegistry database_health integration

Note: All tests use in-memory adapters - no DB/Django dependency.
"""

import os
from unittest.mock import patch

from baldur.adapters.database.noop_health import NoopDatabaseHealthAdapter
from baldur.audit.cascade_config import (
    get_audit_backpressure_config,
    get_cascade_chain_config,
)
from baldur.factory.registry import ProviderRegistry
from baldur.settings.audit import reset_audit_settings
from baldur.settings.cascade import reset_cascade_settings

# =============================================================================
# A. Settings → Config Chain Integration
# =============================================================================


class TestCascadeSettingsToConfigChain:
    """
    End-to-end: env var → CascadeSettings → get_cascade_chain_config() → CascadeChainConfig.

    Validates:
    - Environment variable override propagates to CascadeChainConfig
    - Default values flow through correctly
    """

    def setup_method(self):
        """Reset settings cache before each test."""
        reset_cascade_settings()

    def teardown_method(self):
        """Clean up settings cache."""
        reset_cascade_settings()

    @patch.dict(
        os.environ,
        {
            "BALDUR_CASCADE_MAX_DEPTH": "20",
            "BALDUR_CASCADE_WARN_DEPTH": "15",
            "BALDUR_CASCADE_BLOCK_ON_EXCEED": "false",
            "BALDUR_CASCADE_DETECT_CYCLES": "false",
        },
    )
    def test_env_vars_propagate_to_chain_config(self):
        """
        Purpose:
            Env vars override CascadeSettings, which propagate to CascadeChainConfig.
        Expected:
            - CascadeChainConfig.max_chain_depth == 20 (from env)
            - CascadeChainConfig.warn_at_depth == 15 (from env)
            - CascadeChainConfig.block_on_exceed == False (from env)
            - CascadeChainConfig.detect_cycles == False (from env)
        """
        config = get_cascade_chain_config()

        assert config.max_chain_depth == 20
        assert config.warn_at_depth == 15
        assert config.block_on_exceed is False
        assert config.detect_cycles is False

    def test_default_settings_produce_default_chain_config(self):
        """
        Purpose:
            Without env vars, default CascadeSettings → default CascadeChainConfig.
        Expected:
            - max_chain_depth == 10
            - warn_at_depth == 7
        """
        config = get_cascade_chain_config()

        assert config.max_chain_depth == 10
        assert config.warn_at_depth == 7
        assert config.block_on_exceed is True
        assert config.detect_cycles is True


class TestAuditSettingsToBackpressureConfigChain:
    """
    End-to-end: env var → AuditSettings → get_audit_backpressure_config().

    Validates:
    - Backpressure fields flow through AuditSettings to AuditBackpressureConfig
    """

    def setup_method(self):
        """Reset settings cache."""
        reset_audit_settings()

    def teardown_method(self):
        """Clean up."""
        reset_audit_settings()

    @patch.dict(
        os.environ,
        {
            "BALDUR_AUDIT_LOAD_SHEDDING_ENABLED": "false",
            "BALDUR_AUDIT_MAX_EVENTS_PER_SECOND": "500",
        },
    )
    def test_env_vars_propagate_to_backpressure_config(self):
        """
        Purpose:
            Env vars override AuditSettings backpressure fields.
        Expected:
            - load_shedding_enabled == False
            - max_events_per_second == 500
        """
        config = get_audit_backpressure_config()

        assert config.load_shedding_enabled is False
        assert config.max_events_per_second == 500


# =============================================================================
# B. ProviderRegistry database_health Integration
# =============================================================================


class TestDatabaseHealthProviderRegistryIntegration:
    """
    ProviderRegistry.database_health provides a working adapter.

    Validates:
    - Default noop adapter is accessible via ProviderRegistry
    - Override context manager works for testing
    """

    def test_default_noop_adapter_returns_unusable(self):
        """
        Purpose:
            Default database_health adapter (noop) returns is_usable=False.
        Expected:
            - health_check() returns False
        """
        adapter = ProviderRegistry.database_health.get("noop")
        assert adapter.health_check() is False

    def test_override_context_manager_restores_default(self):
        """
        Purpose:
            ProviderRegistry.database_health.override() restores original after exit.
        Expected:
            - Override provides custom adapter during context
            - Original adapter restored after context exit
        """
        original = ProviderRegistry.database_health.get("noop")

        custom = NoopDatabaseHealthAdapter()
        with ProviderRegistry.database_health.override(custom):
            overridden = ProviderRegistry.database_health.get()
            assert overridden is custom

        # After context, original instance must be restored (identity check)
        restored = ProviderRegistry.database_health.get()
        assert restored is original
