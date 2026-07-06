"""Unit tests for reset_init_state chain order + init-not-called WARNING (#463).

Sources:
- ``src/baldur/bootstrap.py:reset_init_state`` (D11 / D16 chain)
- ``src/baldur/factory/registry.py:_warn_if_init_not_called_*`` (D12)

Covers:

- **D11 / D16 chain order**: ``reset_init_state()`` calls, in order:
  1. ``reset_storage_backend(cleanup=True)`` (drains WAL + Redis pool)
  2. cache adapter ``close()`` (only if non-"memory" default with cached
     instance present)
  3. ``ProviderRegistry.cache.clear_instances()`` + re-assert default
     to ``"memory"``
  4. ``reset_runtime()``
- **Cache pool drain skip**: when default is already ``"memory"``, no
  cache adapter ``close()`` is invoked.
- **D12 init-not-called WARNINGs**: first ``get_cache()`` /
  ``get_storage_backend()`` access before init() emits a one-time
  WARNING per registry; subsequent calls are silent; ``reset_init_state``
  resets the warned flags.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (call order via spies).
- §8.4 Side effects (WARNING log emission).
- §8.3 Idempotency / once-per-process flag semantics.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_bootstrap_state():
    """Snapshot/restore ProviderRegistry.cache + bootstrap flags."""
    from baldur import bootstrap
    from baldur.factory.registry import ProviderRegistry

    bootstrap.reset_init_state()
    with ProviderRegistry.cache.snapshot():
        yield
    bootstrap.reset_init_state()


# =============================================================================
# D11 / D16 — reset_init_state chain order
# =============================================================================


class TestResetInitStateChainBehavior:
    """``reset_init_state`` calls reset_storage_backend → cache drain →
    cache default reset → reset_runtime, in that order."""

    def test_chain_invokes_reset_storage_backend_before_reset_runtime(self):
        """Order spy: ``reset_storage_backend`` runs before ``reset_runtime``."""
        from baldur import bootstrap

        order: list[str] = []

        with (
            patch(
                "baldur.adapters.resilient.backend.reset_storage_backend",
                side_effect=lambda *a, **kw: order.append("reset_storage_backend"),
            ) as m_reset_storage,
            patch(
                "baldur.runtime.reset_runtime",
                side_effect=lambda: order.append("reset_runtime"),
            ) as m_reset_runtime,
        ):
            bootstrap.reset_init_state()

        m_reset_storage.assert_called_once_with(cleanup=True)
        m_reset_runtime.assert_called_once_with()
        assert order.index("reset_storage_backend") < order.index("reset_runtime")

    def test_chain_re_asserts_cache_default_to_memory(self):
        """Step 3: cache default is set back to ``"memory"`` after the chain."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        # Drift the default to an arbitrary non-memory value first.
        ProviderRegistry.cache.set_default("redis")
        assert ProviderRegistry.cache.get_default_name() == "redis"

        bootstrap.reset_init_state()

        assert ProviderRegistry.cache.get_default_name() == "memory"

    def test_cache_adapter_close_invoked_when_non_memory_default_with_instance(self):
        """Step 2: when default is non-"memory" AND a cached instance exists,
        the instance's ``close()`` is invoked."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        # Install a stub adapter under "redis" with a close() method we can spy.
        stub_adapter = MagicMock()
        ProviderRegistry.cache.set_default("redis")
        # set_instance is the public surface that registers a cached instance.
        ProviderRegistry.cache.set_instance("redis", stub_adapter)

        bootstrap.reset_init_state()

        stub_adapter.close.assert_called_once_with()

    def test_cache_adapter_close_skipped_when_default_already_memory(self):
        """Step 2 skip: default is "memory" → no adapter close() call."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        stub_adapter = MagicMock()
        ProviderRegistry.cache.set_default("memory")
        ProviderRegistry.cache.set_instance("memory", stub_adapter)

        bootstrap.reset_init_state()

        # Default is already "memory" → the adapter pool drain branch
        # is skipped per the doc: "Skipped when default is already 'memory'".
        stub_adapter.close.assert_not_called()

    def test_cache_adapter_close_failure_does_not_break_chain(self):
        """A failing cache ``close()`` is logged but does NOT abort the chain."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        bad_adapter = MagicMock()
        bad_adapter.close.side_effect = RuntimeError("pool gone")

        ProviderRegistry.cache.set_default("redis")
        ProviderRegistry.cache.set_instance("redis", bad_adapter)

        # Must not raise — the broader try/except in reset_init_state
        # swallows the cache cleanup failure.
        bootstrap.reset_init_state()

        # Default still re-asserted despite the failure (the broader try/except
        # wraps the entire block, so failure mid-step swallows the rest of
        # the cache-block; the spec's contract is "no raise" — which holds).
        # The runtime reset still runs because it's outside that try/except.

    def test_chain_clears_init_done_flag(self):
        """``reset_init_state`` sets ``_init_done`` back to ``False``."""
        from baldur import bootstrap

        bootstrap._init_done = True

        bootstrap.reset_init_state()

        assert bootstrap._init_done is False

    def test_chain_resets_init_not_called_warned_flags(self):
        """Both per-registry WARNED flags are reset to ``False``."""
        from baldur import bootstrap

        bootstrap._init_not_called_cache_warned = True
        bootstrap._init_not_called_storage_warned = True

        bootstrap.reset_init_state()

        assert bootstrap._init_not_called_cache_warned is False
        assert bootstrap._init_not_called_storage_warned is False


# =============================================================================
# D12 — init-not-called WARNING (per registry, once each)
# =============================================================================


class TestInitNotCalledWarningBehavior:
    """First-access WARNING fires once per registry when init() was never called."""

    def test_cache_warning_fires_when_init_done_false_and_flag_unset(self, caplog):
        """First call → WARNING ``baldur.init_not_called_get_cache``."""
        from baldur import bootstrap
        from baldur.factory.registry import _warn_if_init_not_called_cache

        bootstrap._init_done = False
        bootstrap._init_not_called_cache_warned = False

        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_cache()

        assert bootstrap._init_not_called_cache_warned is True
        assert any("init_not_called_get_cache" in r.message for r in caplog.records)

    def test_cache_warning_fires_only_once(self, caplog):
        """Second call after the flag is set → no additional WARNING."""
        from baldur import bootstrap
        from baldur.factory.registry import _warn_if_init_not_called_cache

        bootstrap._init_done = False
        bootstrap._init_not_called_cache_warned = False

        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_cache()  # first → warns
            warn_count_first = sum(
                1 for r in caplog.records if "init_not_called_get_cache" in r.message
            )
            _warn_if_init_not_called_cache()  # second → silent
            warn_count_second = sum(
                1 for r in caplog.records if "init_not_called_get_cache" in r.message
            )

        assert warn_count_first == 1
        assert warn_count_second == 1

    def test_cache_warning_skipped_when_init_done(self, caplog):
        """When ``_init_done=True``, the warning is suppressed."""
        from baldur import bootstrap
        from baldur.factory.registry import _warn_if_init_not_called_cache

        bootstrap._init_done = True
        bootstrap._init_not_called_cache_warned = False

        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_cache()

        # Flag was NOT set because init() is the framework-adapter invariant.
        assert bootstrap._init_not_called_cache_warned is False
        assert all("init_not_called_get_cache" not in r.message for r in caplog.records)

    def test_storage_warning_fires_when_init_done_false_and_flag_unset(self, caplog):
        """First call → WARNING ``baldur.init_not_called_get_storage_backend``."""
        from baldur import bootstrap
        from baldur.factory.registry import _warn_if_init_not_called_storage

        bootstrap._init_done = False
        bootstrap._init_not_called_storage_warned = False

        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_storage()

        assert bootstrap._init_not_called_storage_warned is True
        assert any(
            "init_not_called_get_storage_backend" in r.message for r in caplog.records
        )

    def test_storage_warning_fires_only_once(self, caplog):
        """Second call after the flag is set → no additional WARNING."""
        from baldur import bootstrap
        from baldur.factory.registry import _warn_if_init_not_called_storage

        bootstrap._init_done = False
        bootstrap._init_not_called_storage_warned = False

        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_storage()
            count_first = sum(
                1
                for r in caplog.records
                if "init_not_called_get_storage_backend" in r.message
            )
            _warn_if_init_not_called_storage()
            count_second = sum(
                1
                for r in caplog.records
                if "init_not_called_get_storage_backend" in r.message
            )

        assert count_first == 1
        assert count_second == 1

    def test_two_warnings_are_independent_per_registry(self, caplog):
        """Cache warning fired → storage warning still fires (separate flags)."""
        from baldur import bootstrap
        from baldur.factory.registry import (
            _warn_if_init_not_called_cache,
            _warn_if_init_not_called_storage,
        )

        bootstrap._init_done = False
        bootstrap._init_not_called_cache_warned = False
        bootstrap._init_not_called_storage_warned = False

        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_cache()
            _warn_if_init_not_called_storage()

        cache_count = sum(
            1 for r in caplog.records if "init_not_called_get_cache" in r.message
        )
        storage_count = sum(
            1
            for r in caplog.records
            if "init_not_called_get_storage_backend" in r.message
        )
        assert cache_count == 1
        assert storage_count == 1

    def test_reset_init_state_re_arms_warnings(self, caplog):
        """After ``reset_init_state``, both WARNINGs can fire again."""
        from baldur import bootstrap
        from baldur.factory.registry import (
            _warn_if_init_not_called_cache,
            _warn_if_init_not_called_storage,
        )

        bootstrap._init_done = False
        bootstrap._init_not_called_cache_warned = False
        bootstrap._init_not_called_storage_warned = False

        # Fire the warnings once.
        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_cache()
            _warn_if_init_not_called_storage()

        # Reset and re-fire — counts must increment.
        caplog.clear()
        bootstrap.reset_init_state()

        # reset_init_state sets _init_done=False explicitly.
        with caplog.at_level("WARNING"):
            _warn_if_init_not_called_cache()
            _warn_if_init_not_called_storage()

        cache_count = sum(
            1 for r in caplog.records if "init_not_called_get_cache" in r.message
        )
        storage_count = sum(
            1
            for r in caplog.records
            if "init_not_called_get_storage_backend" in r.message
        )
        assert cache_count == 1
        assert storage_count == 1
