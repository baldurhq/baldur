"""Unit tests for ``baldur.bootstrap._wire_registry_defaults`` (#463 / #464).

Source: ``src/baldur/bootstrap.py:_wire_registry_defaults``

Covers the framework-agnostic init() wiring + 463 D3 / D7 / D15 (cache row of
:data:`_REGISTRIES_TO_WIRE`):

- **D3 Trigger matrix** — five rows of (test_mode × is_production × URL set)
  resolve to: silent memory / ConfigurationError / WARNING + memory /
  redis default + eager backend.
- **D7 Production WAL fail-fast** — when production wiring lands but the
  WAL directory is unwritable, raise ``ConfigurationError`` instead of
  silently running memory-only.
- **D15 Legacy alias rejection** — ``BALDUR_ENVIRONMENT`` set to one of
  the four known legacy aliases (``prod`` / ``live`` / ``release`` /
  ``stable``) hard-fails at startup.

These tests cover the cache + ResilientStorageBackend slice of the new
table-driven wiring step. Group A/B per-row matrix coverage and per-helper
unit tests are written in companion files per the 464 doc.

The wiring step is exercised in isolation (``_wire_registry_defaults``
directly, not the full ``init()``) so each row can be parametrized with
deterministic env state. Full ``init()`` lifecycle is covered by the
integration tests under ``tests/self_healing/integration/``.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (cache.set_default, configure_storage_backend).
- §8.4 Side effects (WARNING log, registry default mutation).
- §8.2 Exception/edge cases (ConfigurationError raises, alias rejection).
- §6.7 parametrize for the trigger matrix and alias enumeration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.core.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def _reset_bootstrap_and_runtime():
    """Each test starts and ends with clean bootstrap + runtime state."""
    from baldur import bootstrap

    bootstrap.reset_init_state()
    yield
    bootstrap.reset_init_state()


@pytest.fixture
def isolated_cache_default():
    """Snapshot the cache registry's default + instances around the test."""
    from baldur.factory.registry import ProviderRegistry

    with ProviderRegistry.cache.snapshot():
        yield


def _stub_redis_settings(monkeypatch, url: str = "redis://stub:6379/0"):
    """Replace ``get_redis_settings`` with a fixed-URL stub."""
    settings = MagicMock()
    settings.url = url
    monkeypatch.setattr(
        "baldur.settings.redis.get_redis_settings",
        lambda: settings,
    )
    return settings


def _patch_eager_backend(
    *,
    wal_initialized: bool = True,
    wal_on_fallback_dir: bool = False,
):
    """Patch ``ResilientStorageBackend`` + ``configure_storage_backend``.

    Returns the ``configure_storage_backend`` mock so tests can assert it
    was called exactly once. The constructed backend exposes the WAL
    attributes the production boot gate reads.

    ``_wal_honors_configured_dir`` is set explicitly rather than left to
    MagicMock: an auto-resolved attribute is always truthy, so the gate
    would silently never fire and the fail-fast assertion would pass for
    the wrong reason.
    """
    backend_instance = MagicMock()
    backend_instance._wal_initialized = wal_initialized
    backend_instance._wal_on_fallback_dir = wal_on_fallback_dir
    backend_instance._wal_honors_configured_dir = (
        wal_initialized and not wal_on_fallback_dir
    )
    backend_instance.config = MagicMock(wal_dir="/tmp/baldur-wal-test")

    backend_cls = MagicMock(return_value=backend_instance)
    configure_fn = MagicMock()

    return (
        patch.multiple(
            "baldur.adapters.resilient.backend",
            ResilientStorageBackend=backend_cls,
            configure_storage_backend=configure_fn,
        ),
        configure_fn,
        backend_instance,
    )


# =============================================================================
# D3 Trigger matrix — 5 rows
# =============================================================================


class TestWireCacheAndStorageMatrixBehavior:
    """The five D3 rows: (is_test_mode × is_production × URL set) → behavior."""

    def test_test_mode_skips_wiring_silently(
        self, monkeypatch, isolated_cache_default, caplog
    ):
        """Row 1: ``BALDUR_TEST_MODE=true`` → silent memory; no WARNING, no calls."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")  # ignored
        bootstrap.reset_init_state()  # rebuild runtime with new env

        ProviderRegistry.cache.set_default("memory")  # baseline
        with caplog.at_level("INFO"):
            bootstrap._wire_registry_defaults()

        # Default not flipped to redis.
        assert ProviderRegistry.cache.get_default_name() == "memory"
        # No WARNING about registry_memory_fallback emitted.
        assert all("registry_memory_fallback" not in r.message for r in caplog.records)

    def test_production_with_url_unset_raises_configuration_error(
        self, monkeypatch, isolated_cache_default
    ):
        """Row 2: prod + URL unset → ``ConfigurationError`` blocks startup."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
            bootstrap._wire_registry_defaults()

    def test_production_with_blank_url_raises_configuration_error(
        self, monkeypatch, isolated_cache_default
    ):
        """Row 2 edge: empty/whitespace URL is treated as unset."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "   ")
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
            bootstrap._wire_registry_defaults()

    def test_production_with_url_set_wires_redis_default_and_backend(
        self, monkeypatch, isolated_cache_default
    ):
        """Row 3: prod + URL set → cache=redis + ResilientStorageBackend installed."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod-host:6379/0")
        # 464 — production also requires a SQL/Django signal so Group B
        # of the wiring step does not raise.
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://prod-host:6379/0")
        cm, configure_fn, backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.cache.get_default_name() == "redis"
        configure_fn.assert_called_once_with(backend)

    def test_non_production_with_url_unset_reports_and_falls_back_to_memory(
        self, monkeypatch, isolated_cache_default, caplog
    ):
        """Row 4: non-prod + URL unset → WARNING + memory default."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        bootstrap.reset_init_state()

        # Drift the default → wiring step must reset it to "memory".
        ProviderRegistry.cache.set_default("redis")

        with caplog.at_level("INFO"):
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.cache.get_default_name() == "memory"
        assert any("registry_memory_fallback" in r.message for r in caplog.records)

    def test_non_production_with_url_set_wires_redis_default_and_backend(
        self, monkeypatch, isolated_cache_default
    ):
        """Row 5: non-prod + URL set → redis default + lazy backend (no fail-fast)."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev-host:6379/0")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://dev-host:6379/0")
        cm, configure_fn, backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.cache.get_default_name() == "redis"
        configure_fn.assert_called_once_with(backend)


# =============================================================================
# D7 Production WAL fail-fast
# =============================================================================


class TestWireCacheAndStorageWalFailFastBehavior:
    """D7: production with unwritable WAL → ConfigurationError post-construction."""

    def test_production_with_wal_init_failure_raises_configuration_error(
        self, monkeypatch, isolated_cache_default
    ):
        """Production: ``backend._wal_initialized=False`` → ConfigurationError."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=False)

        with cm:
            with pytest.raises(ConfigurationError, match="did not honor"):
                bootstrap._wire_registry_defaults()

    def test_non_production_with_wal_init_failure_does_not_raise(
        self, monkeypatch, isolated_cache_default, caplog
    ):
        """Non-prod: WAL init failure logs but allows fall-through (dev laptop)."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, configure_fn, _backend = _patch_eager_backend(wal_initialized=False)

        with cm:
            # Must not raise.
            bootstrap._wire_registry_defaults()

        # Backend was still installed — the dev path tolerates WAL failure.
        configure_fn.assert_called_once()

    def test_production_with_wal_initialized_does_not_raise(
        self, monkeypatch, isolated_cache_default
    ):
        """Production sanity: ``_wal_initialized=True`` → no fail-fast."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        # 464 — production also requires a SQL/Django signal so Group B
        # of the wiring step does not raise.
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, configure_fn, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            # Must not raise.
            bootstrap._wire_registry_defaults()

        configure_fn.assert_called_once()

    def test_production_with_a_fallback_wal_still_raises(
        self, monkeypatch, isolated_cache_default
    ):
        """A usable WAL on a fallback dir must not satisfy the durability gate.

        The gate promises the WAL is on its *configured* directory, so a
        fallback fails it even though ``_wal_initialized`` is True — the old
        predicate would have let production boot on a possibly-ephemeral WAL.
        """
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, backend = _patch_eager_backend(
            wal_initialized=True, wal_on_fallback_dir=True
        )

        with cm:
            with pytest.raises(ConfigurationError, match="did not honor"):
                bootstrap._wire_registry_defaults()

        assert backend._wal_initialized is True

    def test_non_production_with_a_fallback_wal_boots(
        self, monkeypatch, isolated_cache_default
    ):
        """Negative: only the production promise tightened, not the dev path."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, configure_fn, _backend = _patch_eager_backend(
            wal_initialized=True, wal_on_fallback_dir=True
        )

        with cm:
            bootstrap._wire_registry_defaults()

        configure_fn.assert_called_once()


# =============================================================================
# D15 Legacy alias rejection
# =============================================================================


class TestLegacyAliasRejectionBehavior:
    """D15: legacy aliases of ``"production"`` hard-fail at startup."""

    @pytest.mark.parametrize(
        "alias",
        ["prod", "live", "release", "stable"],
        ids=["prod", "live", "release", "stable"],
    )
    def test_known_legacy_aliases_raise_configuration_error(
        self, monkeypatch, isolated_cache_default, alias
    ):
        """Each of the 4 known legacy aliases → ConfigurationError."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", alias)
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError, match="legacy alias"):
            bootstrap._wire_registry_defaults()

    @pytest.mark.parametrize(
        "alias",
        ["PROD", "Prod", "  prod  ", "Live", "RELEASE", "Stable"],
        ids=[
            "upper_prod",
            "title_prod",
            "padded_prod",
            "title_live",
            "upper_release",
            "title_stable",
        ],
    )
    def test_legacy_alias_check_normalizes_case_and_whitespace(
        self, monkeypatch, isolated_cache_default, alias
    ):
        """Legacy alias detection normalizes via ``.strip().lower()``."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", alias)
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError, match="legacy alias"):
            bootstrap._wire_registry_defaults()

    @pytest.mark.parametrize(
        "env_value",
        ["production", "staging", "development", "prod-eu-1", "canary-prod", "", None],
        ids=[
            "production",
            "staging",
            "development",
            "prod_eu_1",
            "canary_prod",
            "empty",
            "unset",
        ],
    )
    def test_non_alias_values_pass_legacy_check(
        self, monkeypatch, isolated_cache_default, env_value
    ):
        """Values that are NOT in the rejected set must not raise on alias check."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        if env_value is None:
            monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)
        else:
            monkeypatch.setenv("BALDUR_ENVIRONMENT", env_value)
        # Set URL so production path reaches eager construction; non-prod
        # paths take the WARNING branch — neither should raise the alias error.
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        # 464 — also stub SQL DSN so Group B passes for the production path.
        # The alias check runs first, so non-prod paths never read this var.
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            # Must not raise the legacy-alias error. (Non-prod paths still
            # work because URL is set; production path proceeds to wiring.)
            bootstrap._wire_registry_defaults()

    def test_legacy_alias_rejection_is_first_check(
        self, monkeypatch, isolated_cache_default
    ):
        """Alias rejection runs even when test mode is True.

        D15 placement: first line of the wiring step, BEFORE the test-mode
        early return. This way a CI matrix that accidentally ships
        ``BALDUR_ENVIRONMENT=prod`` is caught even in test_mode runs.
        """
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "prod")
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError, match="legacy alias"):
            bootstrap._wire_registry_defaults()


# =============================================================================
# Constant contract — _REJECTED_LEGACY_ALIASES
# =============================================================================


class TestRejectedLegacyAliasesContract:
    """Constant pinning for the four rejected aliases."""

    def test_rejected_aliases_set_contents(self):
        """The reject set is exactly {prod, live, release, stable}."""
        from baldur.bootstrap import _REJECTED_LEGACY_ALIASES

        assert _REJECTED_LEGACY_ALIASES == frozenset(
            {"prod", "live", "release", "stable"}
        )


# =============================================================================
# 464 — Group A trigger matrix (6 Redis-backed rows × D3 conditions)
# =============================================================================


GROUP_A_REGISTRY_ATTRS: tuple[str, ...] = (
    "cache",
    "config_history_store",
    "canary_rollout_store",
    "chaos_experiment_store",
    "cross_cluster_store",
    "rate_limit_storage",
)


@pytest.fixture
def isolated_all_wired_registries():
    """Snapshot every registry that ``_REGISTRIES_TO_WIRE`` mutates.

    Each test runs inside nested ``snapshot()`` context managers so the
    module-load defaults are restored on exit even if the wiring step
    flips them mid-test.
    """
    from contextlib import ExitStack

    from baldur.bootstrap import _REGISTRIES_TO_WIRE
    from baldur.factory.registry import ProviderRegistry

    with ExitStack() as stack:
        for wiring in _REGISTRIES_TO_WIRE:
            registry = getattr(ProviderRegistry, wiring.registry_attr)
            stack.enter_context(registry.snapshot())
        yield


class TestWireRegistryDefaultsGroupABehavior:
    """Group A (6 Redis-backed rows) under the D3 trigger matrix.

    Each test exercises ``_wire_registry_defaults`` end-to-end so the
    cumulative effect on all 6 Group A rows is observed.
    """

    def test_test_mode_leaves_all_group_a_rows_at_memory(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """test_mode early-return keeps every Group A registry at memory."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        for attr in GROUP_A_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "memory", (
                f"{attr} default should remain 'memory' in test mode"
            )

    def test_production_with_redis_url_set_wires_redis_for_every_group_a_row(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + URL set → every Group A row flips to ``"redis"``."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        # Group B must also pass so the function returns normally.
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://prod:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_A_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "redis", (
                f"{attr} should be wired to 'redis' under prod+URL set"
            )

    def test_production_with_redis_url_unset_raises_naming_first_failing_registry(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + URL unset → ConfigurationError mentions the registry name.

        Cache is row 1 of ``_REGISTRIES_TO_WIRE``, so it is the first row
        to trip the production fail-loud branch.
        """
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError) as exc_info:
            bootstrap._wire_registry_defaults()

        message = str(exc_info.value)
        assert "BALDUR_REDIS_URL" in message
        # The error names the registry attribute so operators can correlate
        # the error to the offending row.
        assert "ProviderRegistry.cache" in message

    def test_non_production_with_redis_url_unset_reports_and_keeps_memory_for_all(
        self, monkeypatch, isolated_all_wired_registries, caplog
    ):
        """non-prod + URL unset → WARNING per row, all 6 stay at memory."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        # Strip Django so rate_limit_storage's D11 fallback does not trigger
        # — this scenario is "no Redis AND no Django" → all 6 land at memory.
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        # Pre-flip a few rows to non-memory so the wiring step's reset is
        # observable.
        ProviderRegistry.config_history_store.set_default("redis")
        ProviderRegistry.cross_cluster_store.set_default("redis")
        bootstrap.reset_init_state()

        with caplog.at_level("INFO"):
            bootstrap._wire_registry_defaults()

        for attr in GROUP_A_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "memory", (
                f"{attr} should fall back to 'memory' in non-prod with no URL"
            )
        # At least one WARNING per Group A row is emitted (the helper logs
        # the structured event ``registry_memory_fallback`` with
        # ``reason="redis_url_unset"``).
        warning_count = sum(
            1 for r in caplog.records if "registry_memory_fallback" in r.message
        )
        assert warning_count >= 1

    def test_non_production_with_redis_url_set_wires_redis_for_every_group_a_row(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """non-prod + URL set → all 6 Group A rows flip to redis."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://dev:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_A_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "redis"


# =============================================================================
# 464 — Group B trigger matrix (3 SQL/Django-backed rows × D6 conditions)
# =============================================================================


GROUP_B_REGISTRY_ATTRS: tuple[str, ...] = (
    "recovery_session_repo",
    "security_repo",
)


class TestWireRegistryDefaultsGroupBBehavior:
    """Group B (3 rows) under the D6 trigger matrix.

    Each test sets ``BALDUR_REDIS_URL`` so the Group A phase passes
    cleanly, isolating the Group B verdict.
    """

    def test_test_mode_leaves_all_group_b_rows_at_memory(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """test_mode early-return keeps every Group B registry at memory."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://x")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        for attr in GROUP_B_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "memory"

    def test_production_with_neither_signal_set_raises_naming_sql_dsn(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + neither DSN nor Django+DATABASES → ConfigurationError on Group B."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            with pytest.raises(ConfigurationError) as exc_info:
                bootstrap._wire_registry_defaults()

        message = str(exc_info.value)
        assert "BALDUR_SQL_DSN" in message
        assert "Django DATABASES" in message
        # recovery_session_repo is the first Group B row.
        assert "ProviderRegistry.recovery_session_repo" in message

    def test_production_with_sql_dsn_set_wires_sql_for_every_group_b_row(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + DSN set (D5 priority) → all Group B rows flip to ``"sql"``."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://prod-db/baldur")
        # Pre-set Django to verify SQL wins over Django (D5).
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_B_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "sql", (
                f"{attr} should pick 'sql' under DSN-set + Django-set (D5 priority)"
            )

    def test_production_with_only_django_set_wires_django_for_every_group_b_row(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + DSN unset + Django+DATABASES set → all rows flip to ``"django"``."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_B_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "django"

    def test_non_production_with_neither_signal_set_reports_and_keeps_memory(
        self, monkeypatch, isolated_all_wired_registries, caplog
    ):
        """non-prod + neither signal → INFO per row, all stay at memory."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with caplog.at_level("INFO"), cm:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_B_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "memory"
        # Setup has redis_set=True so no Group A row reports a memory
        # fallback — only Group B's 3 rows do.
        fallback_count = sum(
            1 for r in caplog.records if "registry_memory_fallback" in r.message
        )
        # At least one record per Group B row is emitted.
        assert fallback_count >= len(GROUP_B_REGISTRY_ATTRS)

    def test_non_production_with_sql_dsn_set_wires_sql(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """non-prod + DSN set → all Group B rows flip to ``"sql"``."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://dev-db/baldur")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_B_REGISTRY_ATTRS:
            registry = getattr(ProviderRegistry, attr)
            assert registry.get_default_name() == "sql"


# =============================================================================
# 464 — D11 rate_limit_storage cross-backend fallback
# =============================================================================


class TestWireRegistryDefaultsRateLimitFallbackBehavior:
    """D11: ``rate_limit_storage`` falls through to ``"database"`` when Redis is
    unset but Django+DATABASES is configured."""

    def test_rate_limit_storage_redis_unset_django_set_falls_back_to_database(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """non-prod: Redis unset, Django configured → row picks ``"database"``."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        # rate_limit_storage uniquely picks "database"; the other Group A
        # rows still log WARNING + memory because they have no fallback.
        assert ProviderRegistry.rate_limit_storage.get_default_name() == "database"
        # Sibling Group A rows without ``fallback_target`` stay at memory.
        assert ProviderRegistry.config_history_store.get_default_name() == "memory"

    def test_rate_limit_storage_production_redis_unset_django_set_picks_database(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod: Redis unset, Django configured → ``"database"`` (no fail-loud)."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")
        # Group A's other 5 rows would raise — patch the helper for them so
        # the test is scoped to the rate_limit_storage fallback decision.
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")  # passes Group B
        bootstrap.reset_init_state()

        # Only the fallback row should land on "database"; the other Group A
        # rows would raise ConfigurationError. Confirm that by patching
        # _wire_redis_registry to skip non-fallback rows.
        from baldur import bootstrap as bs

        original_helper = bs._wire_redis_registry

        def selective_helper(
            registry, target_name, fallback_target, redis_set, django_set, runtime
        ):
            if fallback_target is None:
                # Skip — would raise in production with redis unset.
                return
            return original_helper(
                registry,
                target_name,
                fallback_target,
                redis_set,
                django_set,
                runtime,
            )

        monkeypatch.setattr(bs, "_wire_redis_registry", selective_helper)
        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.rate_limit_storage.get_default_name() == "database"

    def test_rate_limit_storage_neither_signal_in_production_raises(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod: Redis unset AND Django unset → ConfigurationError names both."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        bootstrap.reset_init_state()

        # Skip cache + the other 4 Group A rows so the rate_limit_storage row
        # is the first to evaluate the fail-loud branch.
        from baldur import bootstrap as bs

        original = bs._wire_redis_registry

        def only_fallback_row(
            registry, target_name, fallback_target, redis_set, django_set, runtime
        ):
            if fallback_target is None:
                return
            return original(
                registry,
                target_name,
                fallback_target,
                redis_set,
                django_set,
                runtime,
            )

        monkeypatch.setattr(bs, "_wire_redis_registry", only_fallback_row)

        with pytest.raises(ConfigurationError) as exc_info:
            bootstrap._wire_registry_defaults()

        message = str(exc_info.value)
        assert "BALDUR_REDIS_URL or Django DATABASES" in message
        assert "ProviderRegistry.rate_limit_storage" in message
        # Sanity: the registry was not flipped before the raise.
        assert ProviderRegistry.rate_limit_storage.get_default_name() == "memory"


# =============================================================================
# 570 — event_journal_repo PRIORITY_CHAIN row (D1) trigger matrix
# =============================================================================


class TestWireRegistryDefaultsEventJournalBehavior:
    """570 D1 — ``event_journal_repo`` wired as a PRIORITY_CHAIN row
    (``redis > sql > memory``) with ``BALDUR_EVENT_JOURNAL_BACKEND`` as the
    operator ``env_override``.

    Each test drives the full ``_wire_registry_defaults`` orchestration so
    the new row's dispatch through the (already-tested)
    ``_wire_priority_chain_registry`` helper is observed end-to-end. The
    registry registers memory/redis/sql adapters, so ``has_provider`` is
    True for each chain candidate. Cases that leave ``BALDUR_REDIS_URL``
    unset take the cache row's non-prod WARNING + memory path, so no eager
    ``ResilientStorageBackend`` is constructed; redis-set cases stub it.
    """

    def test_test_mode_leaves_event_journal_at_memory(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """test_mode early-return keeps event_journal at the memory baseline."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        monkeypatch.delenv("BALDUR_EVENT_JOURNAL_BACKEND", raising=False)
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "memory"

    def test_non_production_with_redis_url_set_wires_event_journal_to_redis(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """non-prod + ``BALDUR_REDIS_URL`` set → first chain probe wins → redis."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        monkeypatch.delenv("BALDUR_EVENT_JOURNAL_BACKEND", raising=False)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://dev:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "redis"

    def test_non_production_redis_unset_sql_set_wires_event_journal_to_sql(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """non-prod + Redis unset + SQL DSN set → chain falls through to ``"sql"``."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://dev-db/baldur")
        monkeypatch.delenv("BALDUR_EVENT_JOURNAL_BACKEND", raising=False)
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "sql"

    def test_non_production_no_signal_resets_event_journal_to_memory_terminal(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """non-prod + neither Redis nor SQL → terminal ``("memory", True)`` wins.

        Pre-drifts the default to ``"redis"`` so the reset to the chain
        terminal is observable (not a no-op pass-through).
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        for name in (
            "BALDUR_POSTGRES_HOST",
            "BALDUR_POSTGRES_PORT",
            "BALDUR_POSTGRES_DATABASE",
            "BALDUR_POSTGRES_USER",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.delenv("BALDUR_EVENT_JOURNAL_BACKEND", raising=False)
        bootstrap.reset_init_state()

        # Pre-drift so the terminal "memory" resolution is a visible reset.
        ProviderRegistry.event_journal_repo.set_default("redis")

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "memory"

    def test_env_override_forces_event_journal_backend_without_redis_url(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """``BALDUR_EVENT_JOURNAL_BACKEND=redis`` forces redis even when
        ``BALDUR_REDIS_URL`` is unset — the public operator knob (570 D1/D3)
        is preserved and beats the priority chain."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.setenv("BALDUR_EVENT_JOURNAL_BACKEND", "redis")
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "redis"

    def test_env_override_beats_priority_chain(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """When the chain would resolve ``"sql"`` (DSN set) but the operator
        sets ``BALDUR_EVENT_JOURNAL_BACKEND=memory``, the override wins."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://dev-db/baldur")
        monkeypatch.setenv("BALDUR_EVENT_JOURNAL_BACKEND", "memory")
        bootstrap.reset_init_state()

        # Pre-drift so the override → "memory" is a visible reset, AND prove
        # the override beat the "sql" the chain would otherwise have picked.
        ProviderRegistry.event_journal_repo.set_default("redis")

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "memory"

    def test_production_with_redis_url_set_wires_event_journal_to_redis(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + Redis set (+ SQL for Group B) → event_journal resolves redis.

        In production the cache row (row 1) guarantees Redis is present, so
        the ``redis`` probe matches by the time the PRIORITY_CHAIN phase
        reaches event_journal.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://prod-db/baldur")
        monkeypatch.delenv("BALDUR_EVENT_JOURNAL_BACKEND", raising=False)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://prod:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "redis"

    def test_production_redis_unset_raises_at_cache_row_event_journal_stays_memory(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + Redis unset → ConfigurationError at the cache row (Phase 1),
        BEFORE the PRIORITY_CHAIN phase — event_journal never independently
        raises and stays at the memory baseline."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_EVENT_JOURNAL_BACKEND", raising=False)
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
            bootstrap._wire_registry_defaults()

        # event_journal is a later PRIORITY_CHAIN row; the cache fail-loud
        # short-circuits Phase 1, so its default is untouched.
        assert ProviderRegistry.event_journal_repo.get_default_name() == "memory"


# =============================================================================
# 570 — postmortem_repo SQL_DJANGO row (D5) trigger matrix
# =============================================================================


class TestWireRegistryDefaultsPostmortemBehavior:
    """570 D5 — ``postmortem_repo`` wired as a Group B SQL_DJANGO row
    (``sql > django > memory``), structurally identical to
    ``recovery_session_repo`` / ``security_repo``.

    Asserts specifically on ``postmortem_repo`` because the shared
    ``GROUP_B_REGISTRY_ATTRS`` matrix predates D5 and does not include it.
    """

    def test_test_mode_leaves_postmortem_at_memory(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """test_mode early-return keeps postmortem at the memory baseline."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://x")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.postmortem_repo.get_default_name() == "memory"

    @pytest.mark.parametrize(
        ("sql_set", "django_set", "expected"),
        [
            (True, False, "sql"),
            (True, True, "sql"),
            (False, True, "django"),
            (False, False, "memory"),
        ],
        ids=["sql_only", "sql_wins_over_django", "django_only", "neither_memory"],
    )
    def test_non_production_postmortem_resolves_per_sql_django_signals(
        self, monkeypatch, isolated_all_wired_registries, sql_set, django_set, expected
    ):
        """non-prod Group B matrix on postmortem: sql > django > memory.

        Redis is left unset so the cache row takes the non-prod WARNING +
        memory path (no eager ``ResilientStorageBackend`` construction).
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        if sql_set:
            monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://dev-db/baldur")
        else:
            monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
            for name in (
                "BALDUR_POSTGRES_HOST",
                "BALDUR_POSTGRES_PORT",
                "BALDUR_POSTGRES_DATABASE",
                "BALDUR_POSTGRES_USER",
            ):
                monkeypatch.delenv(name, raising=False)
        if django_set:
            monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")
        else:
            monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.postmortem_repo.get_default_name() == expected

    def test_production_with_sql_dsn_set_wires_postmortem_to_sql(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + SQL DSN set → postmortem flips to ``"sql"`` (D5 priority)."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://prod-db/baldur")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.postmortem_repo.get_default_name() == "sql"

    def test_production_with_only_django_set_wires_postmortem_to_django(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + DSN unset + Django+DATABASES set → postmortem flips django."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        for name in (
            "BALDUR_POSTGRES_HOST",
            "BALDUR_POSTGRES_PORT",
            "BALDUR_POSTGRES_DATABASE",
            "BALDUR_POSTGRES_USER",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.postmortem_repo.get_default_name() == "django"

    def test_production_neither_signal_raises_at_first_row_postmortem_stays_memory(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + neither SQL nor Django → ConfigurationError at the FIRST
        Group B row (``recovery_session_repo``), so postmortem adds no new crash
        condition and stays at the memory baseline (570 D5 rationale)."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        for name in (
            "BALDUR_POSTGRES_HOST",
            "BALDUR_POSTGRES_PORT",
            "BALDUR_POSTGRES_DATABASE",
            "BALDUR_POSTGRES_USER",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            with pytest.raises(ConfigurationError) as exc_info:
                bootstrap._wire_registry_defaults()

        # recovery_session_repo is the first Group B row to evaluate the verdict.
        assert "ProviderRegistry.recovery_session_repo" in str(exc_info.value)
        # postmortem's row was never reached; default unchanged.
        assert ProviderRegistry.postmortem_repo.get_default_name() == "memory"


# =============================================================================
# 464 — _REGISTRIES_TO_WIRE table integrity (D9)
# =============================================================================


class TestRegistriesToWireContract:
    """Constant pinning for the declarative wiring table.

    Future-proofs against accidental row drops, attr drift, or
    fallback-target leakage when a new registry is added to
    ``factory/registry.py``.
    """

    def test_registries_to_wire_row_count(self):
        """Cache (1) + 5 Group A + 3 Group B + 5 PRIORITY_CHAIN = 14 rows."""
        from baldur.bootstrap import _REGISTRIES_TO_WIRE

        assert len(_REGISTRIES_TO_WIRE) == 14

    def test_registries_to_wire_attribute_set(self):
        """Every wired registry attribute must be listed exactly once.

        This ordered list-equality is the single source of truth for row
        *ordering*, including the Group-A-first invariant (the
        ``ResilientStorageBackend`` special case must see a consistent
        Group A verdict). The per-kind tests below filter by
        ``backend_kind`` and so do not re-verify position.
        """
        from baldur.bootstrap import _REGISTRIES_TO_WIRE

        attrs = [w.registry_attr for w in _REGISTRIES_TO_WIRE]
        expected = [
            "cache",
            "config_history_store",
            "canary_rollout_store",
            "chaos_experiment_store",
            "cross_cluster_store",
            "rate_limit_storage",
            "recovery_session_repo",
            "security_repo",
            "postmortem_repo",
            "database_health",
            "pg_admin",
            "pool_info",
            "event_journal_repo",
            "failed_op_repo",
        ]
        assert attrs == expected

    def test_registries_to_wire_group_a_kind(self):
        """All REDIS (Group A) rows, selected by ``backend_kind`` filter.

        570 D8 converted this from a brittle ``[:6]`` index slice — adding
        or removing a row now only extends this expected-attr list, never
        shifts a boundary.
        """
        from baldur.bootstrap import _REGISTRIES_TO_WIRE, _BackendKind

        group_a = [
            w for w in _REGISTRIES_TO_WIRE if w.backend_kind is _BackendKind.REDIS
        ]
        assert [w.registry_attr for w in group_a] == [
            "cache",
            "config_history_store",
            "canary_rollout_store",
            "chaos_experiment_store",
            "cross_cluster_store",
            "rate_limit_storage",
        ]

    def test_registries_to_wire_group_b_kind(self):
        """All SQL_DJANGO (Group B) rows, selected by ``backend_kind`` filter.

        570 D8 converted this from a brittle ``[6:9]`` index slice and
        added ``postmortem_repo`` (D5).
        """
        from baldur.bootstrap import _REGISTRIES_TO_WIRE, _BackendKind

        group_b = [
            w for w in _REGISTRIES_TO_WIRE if w.backend_kind is _BackendKind.SQL_DJANGO
        ]
        assert [w.registry_attr for w in group_b] == [
            "recovery_session_repo",
            "security_repo",
            "postmortem_repo",
        ]

    def test_registries_to_wire_group_c_kind(self):
        """PRIORITY_CHAIN rows: the 3 probe-surface registries (515 D6) plus
        the two memory/redis/sql hybrids — ``event_journal_repo`` (570 D1)
        and ``failed_op_repo`` (778 D1).

        The probe-surface rows follow ``django > sql > noop`` (or
        ``django > noop`` for ``pool_info``, which has no SQL implementation
        yet); both hybrids follow ``redis > sql > memory``. The contract
        asserted here is structural (``target_name==""``, non-empty chain,
        ``env_override`` present) — it does NOT assert chain *content*, so
        the differing hybrid chain order does not break it.
        """
        from baldur.bootstrap import _REGISTRIES_TO_WIRE, _BackendKind

        group_c = [
            w
            for w in _REGISTRIES_TO_WIRE
            if w.backend_kind is _BackendKind.PRIORITY_CHAIN
        ]
        assert [w.registry_attr for w in group_c] == [
            "database_health",
            "pg_admin",
            "pool_info",
            "event_journal_repo",
            "failed_op_repo",
        ]
        for w in group_c:
            assert w.target_name == ""
            assert w.priority_chain, (
                f"{w.registry_attr} PRIORITY_CHAIN row must declare "
                "a non-empty priority_chain"
            )
            assert w.env_override is not None, (
                f"{w.registry_attr} PRIORITY_CHAIN row must declare env_override"
            )

    def test_reset_baseline_matches_module_load_default(self):
        """570 D4: each row's ``reset_baseline`` equals its module-load default.

        Probe-surface PRIORITY_CHAIN rows reset to ``"noop"`` (their only
        registered default); the event_journal hybrid and the Group A/B rows
        reset to ``"memory"``; the dead-letter hybrid resets to ``"redis"``,
        which is what ``factory/registry.py`` sets at module load.
        """
        from baldur.bootstrap import _REGISTRIES_TO_WIRE

        by_attr = {w.registry_attr: w for w in _REGISTRIES_TO_WIRE}
        # Probe-surface rows reset to "noop".
        for attr in ("database_health", "pg_admin", "pool_info"):
            assert by_attr[attr].reset_baseline == "noop"
        # The hybrid PRIORITY_CHAIN row (no noop adapter) resets to "memory".
        assert by_attr["event_journal_repo"].reset_baseline == "memory"
        # The dead-letter hybrid's module-load default is "redis".
        assert by_attr["failed_op_repo"].reset_baseline == "redis"
        # Representative Group A / Group B rows reset to "memory".
        assert by_attr["cache"].reset_baseline == "memory"
        assert by_attr["postmortem_repo"].reset_baseline == "memory"

    def test_exactly_one_row_carries_database_fallback(self):
        """Only ``rate_limit_storage`` has ``fallback_target='database'`` (D11)."""
        from baldur.bootstrap import _REGISTRIES_TO_WIRE

        with_fallback = [
            w for w in _REGISTRIES_TO_WIRE if w.fallback_target == "database"
        ]
        assert len(with_fallback) == 1
        assert with_fallback[0].registry_attr == "rate_limit_storage"

    def test_other_rows_have_no_fallback_target(self):
        """All non-rate_limit rows have ``fallback_target is None``."""
        from baldur.bootstrap import _REGISTRIES_TO_WIRE

        for w in _REGISTRIES_TO_WIRE:
            if w.registry_attr == "rate_limit_storage":
                continue
            assert w.fallback_target is None

    def test_all_listed_attributes_resolve_on_provider_registry(self):
        """Each ``registry_attr`` resolves to a real ``GenericProviderRegistry``."""
        from baldur.bootstrap import _REGISTRIES_TO_WIRE
        from baldur.factory.base import GenericProviderRegistry
        from baldur.factory.registry import ProviderRegistry

        for w in _REGISTRIES_TO_WIRE:
            registry = getattr(ProviderRegistry, w.registry_attr, None)
            assert isinstance(registry, GenericProviderRegistry), (
                f"{w.registry_attr} is not a GenericProviderRegistry"
            )


# =============================================================================
# 464 — _BackendKind enum contract
# =============================================================================


class TestBackendKindContract:
    """Constant pinning for the ``_BackendKind`` enum."""

    def test_backend_kind_values(self):
        """Three members: REDIS, SQL_DJANGO, PRIORITY_CHAIN (515 D6)."""
        from baldur.bootstrap import _BackendKind

        assert _BackendKind.REDIS.value == "redis"
        assert _BackendKind.SQL_DJANGO.value == "sql_django"
        assert _BackendKind.PRIORITY_CHAIN.value == "priority_chain"
        assert {m.name for m in _BackendKind} == {
            "REDIS",
            "SQL_DJANGO",
            "PRIORITY_CHAIN",
        }

    def test_backend_kind_str_inheritance(self):
        """str-Enum inheritance enables JSON serialization without conversion."""
        from baldur.bootstrap import _BackendKind

        assert isinstance(_BackendKind.REDIS, str)


# =============================================================================
# 778 — failed_op_repo PRIORITY_CHAIN row (D1/D3) trigger matrix
# =============================================================================

# Names whose ambient presence would silently flip a probe. Every dead-letter
# wiring case clears the whole set first and re-declares only what it means to
# test, so a developer's own shell configuration cannot decide the outcome.
_DLQ_WIRING_ENV_VARS = (
    "BALDUR_TEST_MODE",
    "BALDUR_REDIS_URL",
    "BALDUR_SQL_DSN",
    "BALDUR_DLQ_BACKEND",
    "BALDUR_EVENT_JOURNAL_BACKEND",
    "DJANGO_SETTINGS_MODULE",
    "BALDUR_POSTGRES_HOST",
    "BALDUR_POSTGRES_PORT",
    "BALDUR_POSTGRES_DATABASE",
    "BALDUR_POSTGRES_USER",
)

# A DSN the stdlib can build a connection factory from. The postgres DSN the
# probe would otherwise synthesize needs a driver that may not be installed
# where this suite runs, and ``eager_validate`` really does construct the
# selected repository — so the sqlite form keeps the "dsn set" cases about
# wiring rather than about the local package set.
_SQLITE_DSN = "sqlite:///baldur-dlq-wiring-test.db"


@pytest.fixture
def dlq_wiring_env(monkeypatch):
    """Neutral non-production environment for the dead-letter wiring row."""
    for name in _DLQ_WIRING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")


def _replace_failed_op_provider(name, factory):
    """Swap one ``failed_op_repo`` provider and drop the cached instances.

    Eager validation resolves through ``registry.get()``, which returns a
    cached instance when one exists — an instance an earlier test may have
    built. Clearing makes the swapped factory the thing that actually runs.
    Both mutations are undone by ``isolated_all_wired_registries``.
    """
    from baldur.factory.registry import ProviderRegistry

    ProviderRegistry.failed_op_repo.register(name, factory)
    ProviderRegistry.failed_op_repo.clear_instances()


def _unusable_backend():
    """Stand-in for a backend that probes True and cannot be constructed."""
    raise ImportError("baldur.sql: psycopg2 is required for postgresql DSNs")


class TestWireRegistryDefaultsFailedOpRepoBehavior:
    """778 D1/D3 — ``failed_op_repo`` wired as a PRIORITY_CHAIN row
    (``redis > sql > memory``) with ``BALDUR_DLQ_BACKEND`` as the operator
    ``env_override``.

    Each case drives the full ``_wire_registry_defaults`` orchestration, so
    the row is observed where it actually runs — after the cache row's
    production gate and alongside every other row.

    Unlike the event-journal row, this one carries ``eager_validate``, so a
    resolution that lands on ``"sql"`` also constructs the repository. Cases
    that mean to select SQL therefore configure a sqlite DSN.
    """

    def test_failed_op_repo_in_test_mode_keeps_the_module_load_redis_default(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """test_mode returns before any row applies.

        The baseline this row falls back to is ``"redis"``, not ``"memory"``
        — that is what ``factory/registry.py`` sets at module load.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "redis"

    def test_failed_op_repo_with_redis_url_set_wires_to_redis(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """Redis configured → the first chain probe wins, as it does today."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://dev:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "redis"

    def test_failed_op_repo_with_redis_unset_and_dsn_set_wires_to_sql(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """The whole point of the row.

        A Redis-less deployment with a database used to land on memory,
        where a restart lost every parked call. It now lands on the durable
        store without the operator naming a backend at all.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        bootstrap.reset_init_state()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"

    def test_failed_op_repo_with_no_signal_resolves_the_memory_chain_terminal(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """Neither signal → the terminal ``("memory", True)`` member.

        Pre-drifted to ``"redis"`` so the resolution is a visible move rather
        than a pass-through over the module-load default.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        bootstrap.reset_init_state()
        ProviderRegistry.failed_op_repo.set_default("redis")

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "memory"

    def test_failed_op_repo_override_selects_sql_over_the_chains_redis_winner(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """An explicit operator choice outranks the probes.

        Redis is configured, so the chain would resolve ``"redis"``; the knob
        moves the dead-letter store to SQL without touching anything else.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "sql")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://dev:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"
        # The knob is dead-letter-scoped: the other memory/redis/sql hybrid
        # keeps resolving through its own chain.
        assert ProviderRegistry.event_journal_repo.get_default_name() == "redis"

    def test_failed_op_repo_override_selects_memory_over_the_chains_sql_winner(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """The override also works in the "less durable" direction."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "memory")
        bootstrap.reset_init_state()

        ProviderRegistry.failed_op_repo.set_default("redis")

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "memory"

    def test_failed_op_repo_unknown_override_warns_and_leaves_the_chain_in_charge(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """A typo must degrade to a warning plus a working queue.

        This is the other half of the non-raising settings validator: the
        name is rejected here, where rejecting it costs one log line, not in
        ``DLQSettings`` where it would cost the whole dead-letter queue.
        """
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "postgres")
        bootstrap.reset_init_state()

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"
        warned = [
            e
            for e in logs
            if e.get("event") == "baldur.registry_env_override_invalid"
            and e.get("registry") == "failed_op_repo"
        ]
        assert len(warned) == 1
        assert warned[0]["value"] == "postgres"
        assert warned[0]["env_var"] == "BALDUR_DLQ_BACKEND"
        assert warned[0]["log_level"] == "warning"

    def test_failed_op_repo_override_of_an_unprobed_backend_is_honored_but_announced(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """``BALDUR_DLQ_BACKEND=sql`` with no DSN configured.

        The usual cause is a half-finished configuration, so the selection
        stands — an explicit choice outranks a probe — and the mismatch is
        announced rather than silently honored.

        The provider is swapped for a trivially constructable stand-in: the
        subject here is the warning and the selection, and without a DSN the
        real factory would be resolving whatever driver happens to be
        installed on the machine running the suite.
        """
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "sql")
        bootstrap.reset_init_state()
        _replace_failed_op_provider("sql", InMemoryFailedOperationRepository)

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"
        mismatched = [
            e
            for e in logs
            if e.get("event") == "baldur.registry_env_override_probe_mismatch"
            and e.get("registry") == "failed_op_repo"
        ]
        assert len(mismatched) == 1
        assert mismatched[0]["value"] == "sql"
        assert mismatched[0]["log_level"] == "warning"

    def test_failed_op_repo_override_matching_its_probe_is_not_announced(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """The mismatch warning is a mismatch warning, not an override warning.

        Same override as the case above, this time with the DSN that makes
        its probe true — nothing to announce.
        """
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "sql")
        bootstrap.reset_init_state()

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"
        assert not [
            e
            for e in logs
            if e.get("event") == "baldur.registry_env_override_probe_mismatch"
            and e.get("registry") == "failed_op_repo"
        ]

    def test_failed_op_repo_uppercase_override_selects_its_backend(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """``BALDUR_DLQ_BACKEND=SQL`` means sql, not "unknown backend".

        The settings field lowercases the value because case is operator
        noise, not intent — and the wiring consumes the same variable, so it
        has to read it the same way. Before the wiring normalized case, an
        uppercase spelling degraded the operator's explicit choice into an
        invalid-override warning plus whatever the chain decided.
        (778 verify regression.)
        """
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "SQL")
        bootstrap.reset_init_state()

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"
        assert not [
            e
            for e in logs
            if e.get("event") == "baldur.registry_env_override_invalid"
            and e.get("registry") == "failed_op_repo"
        ]

    def test_failed_op_repo_reset_init_state_returns_the_default_to_redis(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """Enrolling the row in the wiring table enrolls it in the reset.

        A reset returns every wired registry to its own module-load
        baseline; for the dead-letter registry that baseline is ``"redis"``,
        so a wired-then-reset process starts the next init from the same
        cold-process state a fresh interpreter would.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        bootstrap.reset_init_state()
        bootstrap._wire_registry_defaults()
        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"

        bootstrap.reset_init_state()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "redis"
        assert ProviderRegistry.failed_op_repo.get_cached_instances() == {}

    def test_failed_op_repo_in_production_with_redis_url_set_wires_to_redis(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """In production the cache row guarantees Redis, so the chain's first
        member matches by the time the dead-letter row runs."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://prod:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "redis"

    def test_failed_op_repo_in_production_without_redis_raises_at_the_cache_row_first(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """Production posture is inherited, not re-derived.

        The cache row fails the boot before the chain phase is reached, so
        the dead-letter row never has to decide what a production deployment
        without Redis should mean.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "redis"


# =============================================================================
# 778 — D9 eager backend validation
# =============================================================================


class TestEagerBackendValidationBehavior:
    """778 D9 — the selected provider is constructed at wiring time.

    A probe proves an environment variable is set, never that the backend
    behind it works: ``BALDUR_SQL_DSN`` with no driver installed passes every
    probe and only raises at the first capture, where the DI fallback
    swallows it into per-worker memory — silently, production included. This
    row constructs once at wiring time so that becomes a startup verdict.
    """

    def test_failed_op_repo_constructable_backend_keeps_the_selection_and_stays_quiet(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """The happy path constructs exactly once and demotes nothing."""
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.factory.registry import ProviderRegistry

        built = []

        def _build_sql_repo():
            built.append(1)
            return InMemoryFailedOperationRepository()

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        bootstrap.reset_init_state()
        _replace_failed_op_provider("sql", _build_sql_repo)

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"
        assert len(built) == 1
        assert not [
            e
            for e in logs
            if e.get("event")
            in {
                "baldur.registry_backend_unusable",
                "baldur.registry_backend_demoted",
            }
        ]

    def test_failed_op_repo_unusable_backend_outside_production_warns_and_demotes(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """Development gets an honest posture instead of a silent lie.

        The selection falls through to the next matched chain member — the
        terminal ``memory`` always constructs — and both the cause and the
        demotion are named, so the operator can see that "durable" did not
        happen and why.
        """
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        bootstrap.reset_init_state()
        _replace_failed_op_provider("sql", _unusable_backend)

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "memory"

        unusable = [
            e for e in logs if e.get("event") == "baldur.registry_backend_unusable"
        ]
        assert len(unusable) == 1
        assert unusable[0]["registry"] == "failed_op_repo"
        assert unusable[0]["backend"] == "sql"
        assert "psycopg2" in unusable[0]["error"]
        assert unusable[0]["log_level"] == "warning"

        demoted = [
            e for e in logs if e.get("event") == "baldur.registry_backend_demoted"
        ]
        assert len(demoted) == 1
        assert demoted[0]["requested"] == "sql"
        assert demoted[0]["backend"] == "memory"
        assert demoted[0]["log_level"] == "warning"

    def test_failed_op_repo_unusable_backend_in_production_raises_naming_backend_and_knob(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """A production deployment that asked for a store it cannot build is
        a misconfigured deployment — a crash-looping pod is louder than a lie.

        The message has to carry both the backend that failed and the knob
        that selects another one, or the operator is left guessing.
        """
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "sql")
        bootstrap.reset_init_state()
        _replace_failed_op_provider("sql", _unusable_backend)

        _stub_redis_settings(monkeypatch, url="redis://prod:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm, pytest.raises(ConfigurationError) as excinfo:
            bootstrap._wire_registry_defaults()

        message = str(excinfo.value)
        assert "failed_op_repo" in message
        assert "'sql'" in message
        assert "BALDUR_DLQ_BACKEND" in message
        assert "psycopg2" in message

    def test_failed_op_repo_constructs_eagerly_while_a_flagless_row_does_not(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """The flag is opt-in, and the opt-out keeps lazy first-use behavior.

        Both hybrids resolve ``"sql"`` in this environment; only the
        dead-letter row — the one that carries the durability promise — pays
        an eager construction for it.
        """
        from baldur import bootstrap
        from baldur.adapters.memory import (
            InMemoryEventJournalRepository,
            InMemoryFailedOperationRepository,
        )
        from baldur.factory.registry import ProviderRegistry

        dlq_built: list[int] = []
        journal_built: list[int] = []

        def _build_dlq_repo():
            dlq_built.append(1)
            return InMemoryFailedOperationRepository()

        def _build_journal_repo():
            journal_built.append(1)
            return InMemoryEventJournalRepository()

        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        bootstrap.reset_init_state()
        _replace_failed_op_provider("sql", _build_dlq_repo)
        ProviderRegistry.event_journal_repo.register("sql", _build_journal_repo)
        ProviderRegistry.event_journal_repo.clear_instances()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "sql"
        assert ProviderRegistry.event_journal_repo.get_default_name() == "sql"
        assert dlq_built == [1]
        assert journal_built == []


# =============================================================================
# 778 — D3 startup report field / D7 derived env-var registration
# =============================================================================


class TestStartupReportDlqBackendBehavior:
    """778 D3 — the operator's INFO surface names the dead-letter store.

    A single-probe chain resolution logs at DEBUG, and ``storage_backend``
    describes the shared resilient backend rather than this registry, so
    without this field nothing at INFO says where captured failures land.
    """

    def test_report_names_the_wired_failed_op_repo_backend(
        self, isolated_all_wired_registries
    ):
        """The field tracks the registry default, whatever it currently is."""
        from baldur.bootstrap import ExtensionResult, _build_startup_report
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.failed_op_repo.set_default("sql")

        report = _build_startup_report(ExtensionResult())

        assert report["dlq_backend"] == "sql"
        assert report["dlq_backend"] == (
            ProviderRegistry.failed_op_repo.get_default_name()
        )

    def test_report_follows_a_change_of_the_failed_op_repo_default(
        self, isolated_all_wired_registries
    ):
        """Not a constant, and not the shared ``storage_backend`` value."""
        from baldur.bootstrap import ExtensionResult, _build_startup_report
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.failed_op_repo.set_default("memory")

        report = _build_startup_report(ExtensionResult())

        assert report["dlq_backend"] == "memory"


class TestWiringEnvOverrideRegistrationContract:
    """778 D7 — every wiring override knob is known to the startup scan.

    The knobs are read as ``os.environ.get(wiring.env_override)`` — a
    variable, not a literal — so the source scan cannot see them and the
    startup pass used to warn operators off variables the framework itself
    defined. The registration is derived from the wiring table, so a future
    row registers itself; this test is derived the same way and would fail
    for a row someone adds without one.
    """

    def test_every_wiring_env_override_resolves_as_a_known_var(self):
        import baldur.bootstrap  # noqa: F401 — the import is what registers them
        from baldur.bootstrap import _REGISTRIES_TO_WIRE
        from baldur.settings.introspection import build_prefix_index, is_known_env_var

        knobs = [w.env_override for w in _REGISTRIES_TO_WIRE if w.env_override]
        index = build_prefix_index()

        assert len(knobs) == len(set(knobs)), "an env_override is used by two rows"
        # Guard against a vacuous pass: the assertion below means nothing if
        # the table stopped carrying override knobs.
        assert "BALDUR_DLQ_BACKEND" in knobs
        unknown = [name for name in knobs if not is_known_env_var(name, index)]
        assert unknown == []

    def test_dlq_backend_is_known_through_its_settings_field_not_the_registry(self):
        """778 D6 vs D7 — the dead-letter knob resolves both ways.

        It is a real ``DLQSettings`` field, which is what lets it appear in
        the env-var reference at all; the derived registration covers the
        four knobs that back no field.
        """
        from baldur.settings.introspection import build_prefix_index, resolve_env_var

        index = build_prefix_index()

        assert resolve_env_var("BALDUR_DLQ_BACKEND", index)
        assert not resolve_env_var("BALDUR_EVENT_JOURNAL_BACKEND", index)
