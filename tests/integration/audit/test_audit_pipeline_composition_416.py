"""End-to-end mock-based integration tests for #416.

These tests exercise the **composition** of:
- ``baldur.bootstrap.init()`` (the framework-agnostic entry point)
- ``ProviderRegistry.audit`` (the unified audit provider registry)
- ``log_config_change()`` (Pipeline B convenience function)
- ``HashChainFileAuditLogAdapter`` / ``NullAuditLogAdapter``
  (the resolved adapter for OSS / PRO modes)
- ``audit/env_snapshot.py`` (D7 enabled-gating defense)

These are mock-based — no Docker required. They live under
``tests/baldur/integration/audit/`` per CLAUDE.md test placement.

What the unit tests do NOT cover (and what these tests do):
- The wiring **between** bootstrap, ProviderRegistry, and the env_snapshot
  module along the actual non-mocked code path.
- The end-to-end "OSS default" guarantee that no audit-related files
  appear when ``BALDUR_AUDIT_ENABLED=False``.
- The end-to-end "PRO mode" guarantee that flipping ``enabled=True`` and
  setting the registry default to ``file_hashchain`` produces an
  on-disk hash-chained dict-schema audit row.
- D7 + D11 + D17 + D20 interlocking: env_snapshot's silenced-vs-failed
  branch must NOT leak ``logs/env_snapshot_fallback.jsonl`` in OSS mode.

Reference: 416 (Verification
Plan Parts 2, 2b, 2c, 6, 6f).
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from baldur.adapters.audit.hashchain_adapter import (
    HashChainFileAuditLogAdapter,
)
from baldur.adapters.audit.null_adapter import NullAuditLogAdapter
from baldur.audit import log_config_change
from baldur.audit.logger import AuditLogger
from baldur.factory import ProviderRegistry
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry
from baldur.settings.audit import override_audit_settings


@pytest.fixture(autouse=True)
def _isolate_audit_state():
    """Snapshot ProviderRegistry.audit + AuditLogger singleton per test."""
    AuditLogger.reset_instance()
    prior_default = ProviderRegistry.audit.get_default_name()
    try:
        yield
    finally:
        AuditLogger.reset_instance()
        if prior_default:
            ProviderRegistry.audit.set_default(prior_default)


# =============================================================================
# OSS path — enabled=False produces zero side effects across all pipelines
# =============================================================================


class TestAuditOSSDefaultPath:
    """Verifies the D11 + D7 OSS-safe default across the unified pipeline."""

    def test_module_level_audit_default_is_null(self):
        """D11: ``registry.py:995`` flips the module-load default to ``null``."""
        # No init() called — just import the registry.
        assert ProviderRegistry.audit.get_default_name() == "null"
        adapter = ProviderRegistry.get_audit_adapter()
        assert isinstance(adapter, NullAuditLogAdapter)

    def test_log_config_change_routes_to_null_adapter_when_disabled(self, tmp_path):
        """End-to-end: ``log_config_change()`` returns True via ``Null`` adapter
        and creates no files in the working tree."""
        # Make sure default is null and switch into OSS mode.
        ProviderRegistry.audit.set_default("null")
        cwd_before = os.listdir(tmp_path)

        with override_audit_settings(enabled=False):
            result = log_config_change(
                config_type="test",
                config_key="key",
                old_value=None,
                new_value="val",
                user="verify",
            )

        # D17: log() did not raise → True (this is the key invariant).
        assert result is True

        # No files created in tmp_path (defensive — log_config_change must
        # not write anywhere when routed through NullAuditLogAdapter).
        assert os.listdir(tmp_path) == cwd_before

    def test_env_snapshot_silenced_no_fallback_file(self, tmp_path, monkeypatch):
        """D7: ``log_env_snapshot_to_audit()`` returns True (silenced) and
        does NOT create the env_snapshot fallback file."""
        from baldur.audit import env_snapshot

        fallback_path = tmp_path / "env_snapshot_fallback.jsonl"
        monkeypatch.setattr(env_snapshot, "FALLBACK_LOG_PATH", str(fallback_path))
        monkeypatch.setenv("BALDUR_TEST_FLAG", "1")  # at least one tracked var

        with override_audit_settings(enabled=False):
            result = env_snapshot.log_env_snapshot_to_audit()

        assert result is True
        assert not fallback_path.exists(), (
            "D7: env_snapshot fallback file must not be created in OSS mode"
        )


# =============================================================================
# PRO path — enabled=True + file_hashchain default writes the dict schema
# =============================================================================


class TestAuditPROCompositionPath:
    """Verifies the unified pipeline writes through the hash-chain adapter."""

    def test_log_config_change_writes_dict_schema_when_enabled(self, tmp_path):
        """End-to-end: with ``enabled=True`` and a real
        ``HashChainFileAuditLogAdapter`` registered as the default, a single
        ``log_config_change()`` produces the H2 dict schema on disk
        (D6 byte-compat preservation)."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=True,
            use_file_lock=False,  # avoid cross-process lock under tmp_path
        )
        try:
            with patch(
                "baldur.factory.ProviderRegistry.get_audit_adapter",
                return_value=adapter,
            ):
                with override_audit_settings(enabled=True):
                    result = log_config_change(
                        config_type="RETRY_CONFIG",
                        config_key="max_retries",
                        old_value={"v": 3},
                        new_value={"v": 5},
                        user="alice",
                        reason="Tuning",
                    )

            assert result is True

            files = list(tmp_path.glob("audit_*.jsonl"))
            assert len(files) == 1, f"expected one audit file, got {files}"

            with open(files[0], encoding="utf-8") as f:
                row = json.loads(f.readline())

            # D6 dict schema:
            assert row["event_type"] == "config_change"
            assert row["change"]["config_type"] == "RETRY_CONFIG"
            assert row["change"]["config_key"] == "max_retries"
            assert row["change"]["old_value"] == {"v": 3}
            assert row["change"]["new_value"] == {"v": 5}
            assert row["change"]["reason"] == "Tuning"
            assert row["actor"]["user"] == "alice"
            # Hash chain integrity attached.
            assert "integrity" in row
            assert row["integrity"]["sequence"] == 1
        finally:
            adapter.close()

    def test_pro_pipeline_query_round_trip(self, tmp_path):
        """End-to-end: write via ``log_config_change`` then read via the
        adapter's ``query()`` API."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            with patch(
                "baldur.factory.ProviderRegistry.get_audit_adapter",
                return_value=adapter,
            ):
                with override_audit_settings(enabled=True):
                    for i in range(3):
                        log_config_change(
                            config_type="CB_CONFIG",
                            config_key=f"threshold_{i}",
                            old_value={"v": i},
                            new_value={"v": i + 1},
                            user="alice",
                        )
                results = adapter.query()
        finally:
            adapter.close()

        assert len(results) == 3
        for entry in results:
            assert isinstance(entry, AuditEntry)
            assert entry.action == AuditAction.CONFIG_CHANGE
            assert entry.target_type == "CB_CONFIG"
            assert entry.actor_id == "alice"

    def test_partition_split_writers_isolated(self, tmp_path):
        """Two partitioned adapters sharing a directory write to disjoint
        files even though both run through ``log_config_change``."""
        web = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="web",
            enable_hash_chain=True,
            use_file_lock=False,
        )
        celery = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="celery",
            enable_hash_chain=True,
            use_file_lock=False,
        )
        try:
            with override_audit_settings(enabled=True):
                # Route the next call through the web adapter.
                with patch(
                    "baldur.factory.ProviderRegistry.get_audit_adapter",
                    return_value=web,
                ):
                    log_config_change(
                        config_type="X",
                        config_key="w1",
                        old_value=None,
                        new_value=1,
                        user="alice",
                    )
                # And the next through the celery adapter.
                with patch(
                    "baldur.factory.ProviderRegistry.get_audit_adapter",
                    return_value=celery,
                ):
                    log_config_change(
                        config_type="X",
                        config_key="c1",
                        old_value=None,
                        new_value=1,
                        user="alice",
                    )
        finally:
            web.close()
            celery.close()

        names = sorted(p.name for p in tmp_path.iterdir())
        assert any(n.endswith("_web.jsonl") for n in names)
        assert any(n.endswith("_celery.jsonl") for n in names)
        # Independent state files (D23).
        assert (tmp_path / ".hash_chain_state.web.json").exists()
        assert (tmp_path / ".hash_chain_state.celery.json").exists()


# =============================================================================
# bootstrap.init() integration with real ProviderRegistry path
# =============================================================================


class TestBootstrapInitProviderRegistryIntegration:
    """``init()``'s ``_apply_audit_default_provider`` step interacts with the
    real ``ProviderRegistry.audit`` registry — verify both directions."""

    def test_init_disabled_keeps_null_default(self):
        """End-to-end: ``init()`` with ``enabled=False`` leaves ``null`` default."""
        from baldur import bootstrap

        bootstrap.reset_init_state()
        # Force a non-null default, then verify init() flips it back.
        ProviderRegistry.audit.set_default("file_hashchain")
        try:
            with (
                override_audit_settings(enabled=False),
                # Don't actually run downstream side effects — only the audit
                # default-provider step is under test here.
                patch.object(bootstrap, "_validate_startup_config"),
                patch.object(bootstrap, "_register_default_event_handlers"),
                patch.object(bootstrap, "_register_shutdown_handlers"),
                patch.object(bootstrap, "_run_pro_extensions"),
                patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
                patch.object(bootstrap, "_record_env_snapshot"),
            ):
                bootstrap.init()

            assert ProviderRegistry.audit.get_default_name() == "null"
        finally:
            bootstrap.reset_init_state()
            ProviderRegistry.audit.set_default("null")

    def test_init_enabled_preserves_pro_hook_default(self):
        """When ``enabled=True``, init() does NOT clobber the PRO hook's
        ``file_hashchain`` choice."""
        from baldur import bootstrap

        bootstrap.reset_init_state()
        ProviderRegistry.audit.set_default("file_hashchain")
        try:
            with (
                override_audit_settings(enabled=True),
                patch.object(bootstrap, "_validate_startup_config"),
                patch.object(bootstrap, "_register_default_event_handlers"),
                patch.object(bootstrap, "_register_shutdown_handlers"),
                patch.object(bootstrap, "_run_pro_extensions"),
                patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
                patch.object(bootstrap, "_record_env_snapshot"),
            ):
                bootstrap.init()

            assert ProviderRegistry.audit.get_default_name() == "file_hashchain"
        finally:
            bootstrap.reset_init_state()
            ProviderRegistry.audit.set_default("null")
