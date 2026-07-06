"""Unit tests for ``reset_init_state()`` Step 3.5 wired-registry cleanup (#464).

Source: ``src/baldur/bootstrap.py:reset_init_state`` (Step 3.5).

Covers 464 D13: every registry in :data:`_REGISTRIES_TO_WIRE` (other than
cache, which is handled by Step 2/3) gets ``clear_instances()`` +
``set_default("memory")`` on teardown. This eliminates the
test-author-discipline contract for integration tests that toggle env
vars between ``init()`` calls — a stale Redis or Database-backed
default left over from the previous ``init()`` would otherwise survive.

Companion files:
- ``tests/unit/test_bootstrap_reset_and_init_warning.py`` — reset chain
  order + cache-specific Step 1/2/3 (#463 coverage).
- ``tests/self_healing/integration/test_init_fail_loud.py`` —
  end-to-end repeated ``init() → reset_init_state() → init()`` lifecycle.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.4 Side effects (default-name + ``_instances`` reset across rows).
- §8.7 Lifecycle (post-reset state matches the cold-process baseline).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_wired_registries():
    """Snapshot every registry in ``_REGISTRIES_TO_WIRE`` around each test."""
    from contextlib import ExitStack

    from baldur import bootstrap
    from baldur.factory.registry import ProviderRegistry

    bootstrap.reset_init_state()
    with ExitStack() as stack:
        for wiring in bootstrap._REGISTRIES_TO_WIRE:
            registry = getattr(ProviderRegistry, wiring.registry_attr)
            stack.enter_context(registry.snapshot())
        yield
    bootstrap.reset_init_state()


class TestResetInitStateWiredRegistryCleanup:
    """464 D13 Step 3.5 — wired registries are restored to memory baseline."""

    def test_reset_restores_group_a_defaults_to_memory(self, monkeypatch):
        """Group A registries with non-memory defaults get reset to memory."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        # Simulate a post-init() state by manually flipping each Group A
        # registry to "redis" without going through init() (avoids the
        # eager ResilientStorageBackend construction).
        group_a_attrs = [
            "config_history_store",
            "canary_rollout_store",
            "chaos_experiment_store",
            "cross_cluster_store",
        ]
        for attr in group_a_attrs:
            getattr(ProviderRegistry, attr).set_default("redis")

        bootstrap.reset_init_state()

        for attr in group_a_attrs:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "memory", (
                f"{attr} default should be 'memory' post-reset"
            )

    def test_reset_restores_group_b_defaults_to_memory(self):
        """Group B registries get reset to memory regardless of prior default."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.cascade_event_repo.set_default("django")
        ProviderRegistry.recovery_session_repo.set_default("sql")
        ProviderRegistry.security_repo.set_default("django")

        bootstrap.reset_init_state()

        assert ProviderRegistry.cascade_event_repo.get_default_name() == "memory"
        assert ProviderRegistry.recovery_session_repo.get_default_name() == "memory"
        assert ProviderRegistry.security_repo.get_default_name() == "memory"

    def test_reset_clears_cached_instances_on_wired_registries(self):
        """Step 3.5 calls ``clear_instances()`` on every wired registry."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        # Inject a stub instance into a Group A and a Group B registry.
        stub_a = MagicMock()
        stub_b = MagicMock()
        ProviderRegistry.config_history_store.set_instance("redis", stub_a)
        ProviderRegistry.cascade_event_repo.set_instance("django", stub_b)

        # Sanity: instances are cached.
        assert ProviderRegistry.config_history_store.has_instance("redis")
        assert ProviderRegistry.cascade_event_repo.has_instance("django")

        bootstrap.reset_init_state()

        assert not ProviderRegistry.config_history_store.has_instance("redis")
        assert not ProviderRegistry.cascade_event_repo.has_instance("django")

    def test_reset_handles_rate_limit_storage_database_fallback(self):
        """D11 row: post-reset, ``rate_limit_storage`` is back at memory.

        The fallback default ``"database"`` (D11) is just another non-
        memory default to the cleanup loop — must be restored uniformly.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.rate_limit_storage.set_default("database")
        bootstrap.reset_init_state()

        assert ProviderRegistry.rate_limit_storage.get_default_name() == "memory"

    def test_reset_skips_cache_in_step_3_5_loop(self, monkeypatch):
        """Step 2/3 already handles cache; Step 3.5 must NOT re-process it.

        Verified indirectly: cache's ``close()`` is called by Step 2 only,
        and Step 3.5 should not re-invoke ``clear_instances`` on cache after
        Step 2 already cleared it. We assert the post-reset state matches
        the cold-process baseline regardless.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        # Inject a stub redis cache instance with a close() method.
        stub_cache = MagicMock()
        ProviderRegistry.cache.set_default("redis")
        ProviderRegistry.cache.set_instance("redis", stub_cache)

        bootstrap.reset_init_state()

        # Cache is back at memory baseline (Step 2/3 path).
        assert ProviderRegistry.cache.get_default_name() == "memory"
        # And the cache instance was closed exactly once (Step 2 only —
        # Step 3.5 must not have called close() again or doubled the
        # clear_instances side effect).
        assert stub_cache.close.call_count == 1

    def test_reset_step_3_5_swallows_cleanup_loop_failure(self, monkeypatch, caplog):
        """A registry that misbehaves on ``clear_instances`` does not abort
        the rest of ``reset_init_state``.

        The ``except Exception`` wrapper around the Step 3.5 loop logs a
        WARNING and falls through — runtime reset must still run.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        # Replace clear_instances on one Group A row to raise.
        original_clear = ProviderRegistry.config_history_store.clear_instances

        def _boom():
            raise RuntimeError("simulated registry teardown failure")

        monkeypatch.setattr(
            ProviderRegistry.config_history_store,
            "clear_instances",
            _boom,
        )

        from baldur import runtime as runtime_mod

        # Sanity: ensure runtime exists pre-reset so we can detect it being dropped.
        runtime_mod.get_runtime()

        with caplog.at_level("WARNING"):
            bootstrap.reset_init_state()

        # The Step 3.5 failure was logged but did not abort reset_runtime.
        assert any("wired_registry_reset_failed" in r.message for r in caplog.records)
        # Runtime was still cleared (Step 4 ran) — current_runtime returns None.
        assert runtime_mod.current_runtime() is None

        # Restore for the autouse fixture's snapshot teardown.
        monkeypatch.setattr(
            ProviderRegistry.config_history_store,
            "clear_instances",
            original_clear,
        )

    def test_repeated_init_reset_init_keeps_wired_registries_clean(self, monkeypatch):
        """Models the xdist re-entry pattern: every init() lands a fresh
        default; every reset_init_state() drops it back to memory."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)

        # Stub the eager backend so init() does not need a real Redis.
        from unittest.mock import patch as _patch

        backend = MagicMock()
        backend._wal_initialized = True
        backend.config = MagicMock(wal_dir="/tmp/baldur-wal-test")

        for cycle in range(3):
            with _patch.multiple(
                "baldur.adapters.resilient.backend",
                ResilientStorageBackend=MagicMock(return_value=backend),
                configure_storage_backend=MagicMock(),
            ):
                # Use the wiring step in isolation rather than full init() to
                # avoid the bootstrap-step cross-talk.
                bootstrap._wire_registry_defaults()

            # All Group A rows are at "redis" (URL set, non-prod, no Django).
            assert ProviderRegistry.config_history_store.get_default_name() == "redis"
            # Group B rows: no DSN/Django → memory fallback (non-prod path).
            assert ProviderRegistry.cascade_event_repo.get_default_name() == "memory"

            bootstrap.reset_init_state()

            # Post-reset, every wired registry is back at its module-load
            # baseline — the per-row ``reset_baseline`` (570 D4). Group A/B
            # rows and the event_journal memory/redis/sql hybrid reset to
            # "memory"; the probe-surface priority-chain rows reset to
            # "noop" because that is their factory/registry.py default and
            # "memory" is not a registered provider for them.
            for wiring in bootstrap._REGISTRIES_TO_WIRE:
                registry = getattr(ProviderRegistry, wiring.registry_attr)
                expected = wiring.reset_baseline
                assert registry.get_default_name() == expected, (
                    f"cycle={cycle}: {wiring.registry_attr} not reset to {expected}"
                )
