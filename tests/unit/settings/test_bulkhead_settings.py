"""BulkheadSettings contract tests.

Design contract values (defaults, ranges, env prefix) are hardcoded per
the Contract-test policy; the manifest row pins the metrics_enabled
flag's Core tier after the bulkhead primitives moved core-tier.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.bulkhead import BulkheadSettings


class TestBulkheadSettingsContract:
    """Field defaults / ranges / env-prefix design contract."""

    def test_env_prefix_contract(self):
        """Settings bind to the BALDUR_BULKHEAD_ env namespace."""
        assert BulkheadSettings.model_config["env_prefix"] == "BALDUR_BULKHEAD_"

    def test_per_connection_type_defaults(self):
        """Per-ConnectionType capacity defaults."""
        settings = BulkheadSettings()
        assert settings.database_max_concurrent == 10
        assert settings.cache_max_concurrent == 20
        assert settings.external_api_max_workers == 5
        assert settings.external_api_queue_size == 10
        assert settings.message_queue_max_concurrent == 15
        assert settings.default_max_concurrent == 10

    def test_metrics_updater_defaults(self):
        """Metrics updater gate defaults: enabled, 10-second interval."""
        settings = BulkheadSettings()
        assert settings.metrics_enabled is True
        assert settings.metrics_update_interval == 10.0

    def test_multi_instance_defaults(self):
        """Per-DB-alias / per-cache-instance capacity maps."""
        settings = BulkheadSettings()
        assert settings.database_aliases == {"default": 10, "replica": 15}
        assert settings.cache_instances == {"default": 20, "session": 10}

    @pytest.mark.parametrize(
        ("field", "value", "should_pass"),
        [
            ("cache_max_concurrent", 0, False),
            ("cache_max_concurrent", 1, True),
            ("cache_max_concurrent", 200, True),
            ("cache_max_concurrent", 201, False),
            ("external_api_max_workers", 0, False),
            ("external_api_max_workers", 1, True),
            ("external_api_max_workers", 50, True),
            ("external_api_max_workers", 51, False),
            ("external_api_queue_size", -1, False),
            ("external_api_queue_size", 0, True),
            ("external_api_queue_size", 100, True),
            ("external_api_queue_size", 101, False),
            ("metrics_update_interval", 0.9, False),
            ("metrics_update_interval", 1.0, True),
            ("metrics_update_interval", 300.0, True),
            ("metrics_update_interval", 300.5, False),
        ],
        ids=[
            "cache_below_min",
            "cache_at_min",
            "cache_at_max",
            "cache_above_max",
            "workers_below_min",
            "workers_at_min",
            "workers_at_max",
            "workers_above_max",
            "queue_below_min",
            "queue_at_min",
            "queue_at_max",
            "queue_above_max",
            "interval_below_min",
            "interval_at_min",
            "interval_at_max",
            "interval_above_max",
        ],
    )
    def test_field_boundary_contract(self, field, value, should_pass):
        """ge/le boundary contract: just-outside fails, at-boundary passes."""
        if should_pass:
            settings = BulkheadSettings(**{field: value})
            assert getattr(settings, field) == value
        else:
            with pytest.raises(ValidationError):
                BulkheadSettings(**{field: value})

    def test_queue_size_description_names_fallback_inertness(self):
        """external_api_queue_size is documented inert on the core fallback."""
        description = BulkheadSettings.model_fields[
            "external_api_queue_size"
        ].description
        assert description is not None
        assert "inert" in description

    def test_metrics_enabled_manifest_row_is_core_tier(self, monkeypatch):
        """The metrics_enabled flag rides the Core tier in the launch manifest."""
        from baldur.services.feature_manifest import loader as loader_module
        from baldur.services.feature_manifest.loader import load_feature_manifest

        # Pin the packaged manifest: drop any path override and the lru cache.
        monkeypatch.delenv("BALDUR_TIER_MANIFEST_PATH", raising=False)
        loader_module._cache_clear()
        try:
            entries = [
                e
                for e in load_feature_manifest()
                if e.module == "bulkhead.py" and e.field == "metrics_enabled"
            ]
        finally:
            loader_module._cache_clear()

        assert len(entries) == 1
        assert entries[0].tier == "Core"
        assert entries[0].default is True
        assert entries[0].env_var == "BALDUR_BULKHEAD_METRICS_ENABLED"
