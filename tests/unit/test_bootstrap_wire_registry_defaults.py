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

import sys
import types
from types import SimpleNamespace
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

    ``_wal_initialized`` / ``_wal_on_fallback_dir`` are set explicitly
    rather than left to MagicMock: an auto-resolved attribute is always
    truthy, so the gate would silently never fire and the fail-fast
    assertion would pass for the wrong reason.
    """
    backend_instance = MagicMock()
    backend_instance._wal_initialized = wal_initialized
    backend_instance._wal_on_fallback_dir = wal_on_fallback_dir
    backend_instance._wal = (
        SimpleNamespace(wal_dir="/var/tmp/baldur-fallback-wal")
        if wal_initialized
        else None
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
            with pytest.raises(ConfigurationError, match="WAL initialization failed"):
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

    def test_production_with_a_fallback_wal_boots_with_a_warning(
        self, monkeypatch, isolated_cache_default, caplog
    ):
        """A WAL that started on a fallback dir boots production (801 D3).

        The default directory's fallback is the same durability class as
        the default itself, so the gate no longer refuses it; it announces
        it at WARNING, naming the variable that moves the WAL to a volume.
        """
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, configure_fn, _backend = _patch_eager_backend(
            wal_initialized=True, wal_on_fallback_dir=True
        )

        with cm, caplog.at_level("WARNING"):
            bootstrap._wire_registry_defaults()

        configure_fn.assert_called_once()
        relocated = [
            r.message
            for r in caplog.records
            if "resilient_storage_wal_dir_relocated" in r.message
        ]
        assert len(relocated) == 1
        assert "BALDUR_RESILIENT_STORAGE_WAL_DIR" in relocated[0]

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

    def test_production_with_neither_signal_set_keeps_memory_without_raising(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + neither DSN nor Django+DATABASES → every Group B row on memory.

        801 D2: wiring no longer refuses; production requires a SQL or Django
        store only under a PRO entitlement, enforced after the PRO hook.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch)
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_B_REGISTRY_ATTRS:
            assert getattr(ProviderRegistry, attr).get_default_name() == "memory"

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

    def test_env_override_of_an_unbuildable_redis_journal_demotes_with_a_warning(
        self, monkeypatch, isolated_all_wired_registries, caplog
    ):
        """``BALDUR_EVENT_JOURNAL_BACKEND=redis`` with ``BALDUR_REDIS_URL``
        unset selects redis, and boot validation then constructs it (801 D6:
        an operator-chosen name is always constructed). With no URL it cannot
        be built, so outside production the row demotes to memory, announced
        at WARNING, instead of keeping a default that fails every lookup."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.setenv("BALDUR_EVENT_JOURNAL_BACKEND", "redis")
        bootstrap.reset_init_state()

        with caplog.at_level("WARNING"):
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "memory"
        assert any(
            "registry_backend_demoted" in r.message
            and "event_journal_repo" in r.message
            for r in caplog.records
        )

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

    def test_production_neither_signal_wires_postmortem_to_memory_without_raising(
        self, monkeypatch, isolated_all_wired_registries
    ):
        """prod + neither SQL nor Django → postmortem on memory, no raise.

        801 D2: the SQL/Django requirement follows the PRO entitlement and is
        enforced after the PRO hook, not by wiring."""
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

        ProviderRegistry.postmortem_repo.set_default("django")

        with cm:
            bootstrap._wire_registry_defaults()

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

    def test_boot_validation_is_flagged_on_every_row_whose_selection_can_fail(self):
        """801 D6: the three incident stores, the SQL-capable probe rows and the
        two hybrids construct their selection at boot; the Group A rows (covered
        by the Redis driver check) and ``pool_info`` (Django-only) do not."""
        from baldur.bootstrap import _REGISTRIES_TO_WIRE

        flagged = {w.registry_attr for w in _REGISTRIES_TO_WIRE if w.eager_validate}

        assert flagged == {
            "recovery_session_repo",
            "security_repo",
            "postmortem_repo",
            "database_health",
            "pg_admin",
            "event_journal_repo",
            "failed_op_repo",
        }

    def test_probe_accepted_backends_are_redis_and_django(self):
        """801 D6: the two names a probe may select without boot building them."""
        from baldur.bootstrap import _PROBE_ACCEPTED_BACKENDS

        assert _PROBE_ACCEPTED_BACKENDS == frozenset({"redis", "django"})

    def test_backend_selection_env_vars_are_the_urls_plus_every_override(self):
        """801 D2 (G12): what production test mode names when it leaves them inert."""
        from baldur.bootstrap import _BACKEND_SELECTION_ENV_VARS

        assert _BACKEND_SELECTION_ENV_VARS == (
            "BALDUR_REDIS_URL",
            "BALDUR_SQL_DSN",
            "BALDUR_DATABASE_HEALTH_PROVIDER",
            "BALDUR_PG_ADMIN_PROVIDER",
            "BALDUR_POOL_INFO_PROVIDER",
            "BALDUR_EVENT_JOURNAL_BACKEND",
            "BALDUR_DLQ_BACKEND",
        )

    def test_sql_unusable_hint_names_the_dsn_variable(self):
        """801 D6: the refusal for an unbuildable ``sql`` names what to fix."""
        from baldur.bootstrap import _BACKEND_UNUSABLE_HINTS

        assert "BALDUR_SQL_DSN" in _BACKEND_UNUSABLE_HINTS["sql"]
        assert "psycopg2-binary" in _BACKEND_UNUSABLE_HINTS["sql"]


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

    def test_flagged_rows_construct_eagerly_while_a_flagless_row_does_not(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """The flag is opt-in, and the opt-out keeps lazy first-use behavior.

        Both hybrids resolve ``"sql"`` in this environment and, since 801 D6,
        both carry the flag, so both pay an eager construction. The same
        journal row with the flag switched off is left to first use.
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
        assert journal_built == [1]

        # Opt-out: the journal row without the flag keeps lazy first use.
        flagless = tuple(
            w._replace(eager_validate=False)
            if w.registry_attr == "event_journal_repo"
            else w
            for w in bootstrap._REGISTRIES_TO_WIRE
        )
        monkeypatch.setattr(bootstrap, "_REGISTRIES_TO_WIRE", flagless)
        ProviderRegistry.event_journal_repo.clear_instances()
        journal_built.clear()

        bootstrap._wire_registry_defaults()

        assert ProviderRegistry.event_journal_repo.get_default_name() == "sql"
        assert journal_built == []

    # -- 801 D6 — the decision table of _eager_validate_wired_backend --------

    def test_an_unflagged_row_constructs_nothing(self):
        from baldur import bootstrap

        built: list[str] = []
        registry = _recording_registry(built, sql=False)

        bootstrap._eager_validate_wired_backend(
            registry,
            _validation_row(eager_validate=False),
            _runtime(is_production=True),
            ["sql"],
            operator_chosen=True,
        )

        assert built == []
        assert registry.get_default_name() == "sql"

    def test_a_row_with_no_selection_constructs_nothing(self):
        """Nothing wired means nothing to validate — not an unbuildable default."""
        from baldur import bootstrap
        from baldur.factory.base import GenericProviderRegistry

        registry = GenericProviderRegistry("validation_row")

        bootstrap._eager_validate_wired_backend(
            registry,
            _validation_row(),
            _runtime(is_production=True),
            [],
            operator_chosen=False,
        )

        assert registry.get_default_name() is None

    @pytest.mark.parametrize("selected", ["redis", "django"])
    @pytest.mark.parametrize(
        "is_production", [True, False], ids=["production", "development"]
    )
    def test_a_probe_selected_redis_or_django_is_accepted_without_construction(
        self, selected, is_production
    ):
        """A cluster URL dials in the Redis constructor and a Django repository
        needs a ready app registry, so a probe's pick of either is not built —
        not even when building it would fail."""
        from baldur import bootstrap

        built: list[str] = []
        registry = _recording_registry(built, **{selected: False}, memory=True)

        bootstrap._eager_validate_wired_backend(
            registry,
            _validation_row(),
            _runtime(is_production=is_production),
            [selected, "memory"],
            operator_chosen=False,
        )

        assert built == []
        assert registry.get_default_name() == selected

    @pytest.mark.parametrize("selected", ["redis", "django", "sql", "custom"])
    def test_an_operator_chosen_name_is_constructed_whatever_it_is(self, selected):
        """A host may register its own provider under ``redis`` or ``django``;
        a name the operator chose through the override is always built."""
        from baldur import bootstrap

        built: list[str] = []
        registry = _recording_registry(built, **{selected: True})

        bootstrap._eager_validate_wired_backend(
            registry,
            _validation_row(),
            _runtime(is_production=True),
            [],
            operator_chosen=True,
        )

        assert built == [selected]
        assert registry.get_default_name() == selected

    @pytest.mark.parametrize("selected", ["redis", "django", "custom"])
    def test_an_operator_chosen_unbuildable_name_refuses_production(self, selected):
        """The refusal names the provider and the override that selected it."""
        from baldur import bootstrap

        registry = _recording_registry([], **{selected: False})

        with pytest.raises(ConfigurationError) as excinfo:
            bootstrap._eager_validate_wired_backend(
                registry,
                _validation_row(),
                _runtime(is_production=True),
                [],
                operator_chosen=True,
            )

        message = str(excinfo.value)
        assert f"Fix the provider registered as {selected!r}" in message
        assert "BALDUR_TEST_BACKEND" in message
        assert isinstance(excinfo.value.__cause__, ImportError)

    def test_an_unbuildable_sql_selection_refuses_production_naming_the_dsn(self):
        from baldur import bootstrap

        registry = _recording_registry([], sql=False, memory=True)

        with pytest.raises(ConfigurationError) as excinfo:
            bootstrap._eager_validate_wired_backend(
                registry,
                _validation_row(),
                _runtime(is_production=True),
                ["sql", "memory"],
                operator_chosen=False,
            )

        message = str(excinfo.value)
        assert "ProviderRegistry.validation_row selected backend 'sql'" in message
        assert "BALDUR_SQL_DSN" in message
        assert "select another backend via BALDUR_TEST_BACKEND" in message

    @pytest.mark.parametrize("selected", ["sql", "custom"])
    def test_a_refusal_on_a_row_without_an_override_names_no_placeholder(
        self, selected
    ):
        """The message used to print ``via None`` for rows with no override."""
        from baldur import bootstrap

        registry = _recording_registry([], **{selected: False})

        with pytest.raises(ConfigurationError) as excinfo:
            bootstrap._eager_validate_wired_backend(
                registry,
                _validation_row(env_override=None),
                _runtime(is_production=True),
                [selected],
                operator_chosen=False,
            )

        message = str(excinfo.value)
        assert "None" not in message
        assert "select another backend via" not in message

    def test_demotion_skips_redis_and_django_and_lands_on_the_first_that_builds(
        self,
    ):
        """Outside production a failed selection falls through the remaining
        candidates in order, never onto an unconstructed ``redis`` / ``django``
        (a non-production process would then raise on every lookup)."""
        from structlog.testing import capture_logs

        from baldur import bootstrap

        built: list[str] = []
        registry = _recording_registry(
            built, sql=False, redis=True, django=True, custom=False, memory=True
        )

        with capture_logs() as logs:
            bootstrap._eager_validate_wired_backend(
                registry,
                _validation_row(),
                _runtime(is_production=False),
                ["redis", "sql", "django", "custom", "memory"],
                operator_chosen=False,
            )

        assert built == ["sql", "custom", "memory"]
        assert registry.get_default_name() == "memory"
        assert [
            e["backend"]
            for e in logs
            if e.get("event") == "baldur.registry_backend_unusable"
        ] == ["sql", "custom"]
        assert [
            (e["requested"], e["backend"])
            for e in logs
            if e.get("event") == "baldur.registry_backend_demoted"
        ] == [("sql", "memory")]

    def test_the_validated_instance_is_the_one_later_lookups_receive(self):
        """Validation builds through the registry cache, not a throwaway."""
        from baldur import bootstrap

        built: list[str] = []
        registry = _recording_registry(built, sql=True)

        bootstrap._eager_validate_wired_backend(
            registry,
            _validation_row(),
            _runtime(is_production=True),
            ["sql"],
            operator_chosen=False,
        )
        instance = registry.get()

        assert built == ["sql"]
        assert registry.get() is instance

    # -- 801 D6 — every flagged row, through the wiring step -----------------

    @pytest.mark.parametrize(
        ("registry_attr", "selector_env"),
        [
            ("recovery_session_repo", {}),
            ("security_repo", {}),
            ("postmortem_repo", {}),
            ("database_health", {}),
            ("pg_admin", {}),
            ("event_journal_repo", {"BALDUR_EVENT_JOURNAL_BACKEND": "sql"}),
        ],
    )
    def test_every_flagged_row_refuses_production_when_its_sql_cannot_be_built(
        self,
        monkeypatch,
        dlq_wiring_env,
        isolated_all_wired_registries,
        registry_attr,
        selector_env,
    ):
        """A selected store that cannot be constructed never becomes process
        memory in production: the boot is refused, naming the row and the DSN.
        The journal row prefers Redis in production, so its case selects SQL
        through its override."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", _SQLITE_DSN)
        for name, value in selector_env.items():
            monkeypatch.setenv(name, value)
        bootstrap.reset_init_state()
        registry = getattr(ProviderRegistry, registry_attr)
        registry.register("sql", _unusable_backend)
        registry.clear_instances()

        _stub_redis_settings(monkeypatch, url="redis://prod:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm, pytest.raises(ConfigurationError) as excinfo:
            bootstrap._wire_registry_defaults()

        message = str(excinfo.value)
        assert f"ProviderRegistry.{registry_attr} selected backend 'sql'" in message
        assert "BALDUR_SQL_DSN" in message

    # -- 801 D6 (external review E4) — an unparsable PostgreSQL DSN ----------

    def test_an_unparsable_postgres_dsn_refuses_production_without_the_password(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        """libpq echoes the whole DSN in its parse error; the refusal, and every
        exception it chains to, must carry none of it."""
        import traceback

        from baldur import bootstrap

        _stub_echoing_psycopg2(monkeypatch)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", _TYPO_DSN)
        bootstrap.reset_init_state()

        _stub_redis_settings(monkeypatch, url="redis://prod:6379/0")
        cm, _configure, _backend = _patch_eager_backend(wal_initialized=True)

        with cm, pytest.raises(ConfigurationError) as excinfo:
            bootstrap._wire_registry_defaults()

        assert "BALDUR_SQL_DSN is not a valid PostgreSQL" in str(excinfo.value)
        rendered = "".join(traceback.format_exception(excinfo.value))
        assert _TYPO_DSN_PASSWORD not in rendered
        # ``from None`` would hide the libpq error from the rendered traceback
        # yet keep it on ``__context__``; the promise is neither link holds it.
        assert all(
            _TYPO_DSN_PASSWORD not in str(link)
            for link in _exception_chain(excinfo.value)
        )

    def test_an_unparsable_postgres_dsn_outside_production_demotes_quietly_redacted(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        _stub_echoing_psycopg2(monkeypatch)
        monkeypatch.setenv("BALDUR_SQL_DSN", _TYPO_DSN)
        bootstrap.reset_init_state()

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        assert ProviderRegistry.security_repo.get_default_name() == "memory"
        unusable = [
            e for e in logs if e.get("event") == "baldur.registry_backend_unusable"
        ]
        assert unusable, "the unparsable DSN was never reported"
        assert all("not a valid PostgreSQL" in e["error"] for e in unusable)
        assert all(_TYPO_DSN_PASSWORD not in repr(e) for e in logs)


# 801 D6 — helpers for driving ``_eager_validate_wired_backend`` directly and
# for the unparsable-DSN cases.

# A scheme typo libpq rejects; the password is what must never surface.
_TYPO_DSN_PASSWORD = "s3cretpw"
_TYPO_DSN = f"postgersql://u:{_TYPO_DSN_PASSWORD}@h/db"


def _validation_row(
    *, eager_validate: bool = True, env_override: str | None = "BALDUR_TEST_BACKEND"
):
    """A PRIORITY_CHAIN row for driving the validation directly."""
    from baldur import bootstrap

    return bootstrap._RegistryWiring(
        bootstrap._BackendKind.PRIORITY_CHAIN,
        "validation_row",
        target_name="",
        env_override=env_override,
        eager_validate=eager_validate,
    )


def _recording_registry(built: list[str], **constructs: bool):
    """A fresh registry whose providers record each construction.

    ``False`` makes that provider raise ``ImportError``. Registration follows
    keyword order, so — as in every real registry — the first name given is
    the default.
    """
    from baldur.factory.base import GenericProviderRegistry

    registry = GenericProviderRegistry("validation_row")
    for name, ok in constructs.items():
        registry.register(name, _recording_factory(built, name, ok=ok))
    return registry


def _recording_factory(built: list[str], name: str, *, ok: bool):
    def _build():
        built.append(name)
        if not ok:
            raise ImportError(f"the {name} driver is not installed")
        return object()

    return _build


def _runtime(*, is_production: bool):
    from baldur.runtime import BaldurRuntime

    runtime = MagicMock(spec=BaldurRuntime)
    runtime.is_production = is_production
    return runtime


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Every exception reachable through ``__cause__`` and ``__context__``."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        link = pending.pop()
        if link is None or any(link is s for s in seen):
            continue
        seen.append(link)
        pending.extend((link.__cause__, link.__context__))
    return seen


def _stub_echoing_psycopg2(monkeypatch):
    """Install a ``psycopg2`` whose parse error quotes the DSN, as libpq does.

    ``connect`` fails the test outright: construction must parse, never dial.
    """

    def _parse_dsn(dsn):
        raise Exception(f'missing "=" after "{dsn}" in connection info string')

    def _connect(*_args, **_kwargs):
        raise AssertionError("construction dialed the database")

    stub = types.ModuleType("psycopg2")
    stub.extensions = types.SimpleNamespace(parse_dsn=_parse_dsn)
    stub.connect = _connect
    monkeypatch.setitem(sys.modules, "psycopg2", stub)


# =============================================================================
# 801 D6 (G10) — BALDUR_REDIS_URL with no Redis driver installed
# =============================================================================


class TestRedisDriverMissingBehavior:
    """The Group A phase checks the driver once when ``BALDUR_REDIS_URL`` is set.

    Production refuses with ``ConfigurationError`` (the class every adapter's
    startup aborts on) instead of the bare ``ModuleNotFoundError`` the first
    Redis adapter used to raise. Elsewhere the Redis rows run on memory with one
    WARNING, and the chain probes count the URL as unset, so no chain selects a
    Redis adapter nothing can build. The driver is made unimportable with
    ``sys.modules["redis"] = None``, which fails the import itself.
    """

    def test_production_refuses_a_redis_url_without_the_driver(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        from baldur import bootstrap

        monkeypatch.setitem(sys.modules, "redis", None)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        bootstrap.reset_init_state()

        with pytest.raises(ConfigurationError) as excinfo:
            bootstrap._wire_registry_defaults()

        message = str(excinfo.value)
        assert "BALDUR_REDIS_URL" in message
        assert "pip install baldur-framework[redis]" in message

    def test_elsewhere_the_redis_rows_run_on_memory_with_one_warning(
        self, monkeypatch, dlq_wiring_env, isolated_all_wired_registries
    ):
        from structlog.testing import capture_logs

        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setitem(sys.modules, "redis", None)
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")
        bootstrap.reset_init_state()
        for attr in GROUP_A_REGISTRY_ATTRS:
            getattr(ProviderRegistry, attr).set_default("redis")
        cm, configure_fn, _backend = _patch_eager_backend(wal_initialized=True)

        with cm, capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        for attr in GROUP_A_REGISTRY_ATTRS:
            assert getattr(ProviderRegistry, attr).get_default_name() == "memory"
        assert ProviderRegistry.event_journal_repo.get_default_name() == "memory"
        assert ProviderRegistry.failed_op_repo.get_default_name() == "memory"
        configure_fn.assert_not_called()
        announced = [
            e for e in logs if e.get("event") == "baldur.redis_driver_unavailable"
        ]
        assert [(e["log_level"], e["env_var"]) for e in announced] == [
            ("warning", "BALDUR_REDIS_URL")
        ]

    @pytest.mark.parametrize(
        ("url", "driver_present", "configured"),
        [
            ("redis://h:6379/0", True, True),
            ("redis://h:6379/0", False, False),
            ("   ", True, False),
        ],
        ids=["url_and_driver", "url_without_driver", "blank_url"],
    )
    def test_redis_counts_as_configured_only_with_a_url_and_the_driver(
        self, monkeypatch, url, driver_present, configured
    ):
        """The chain probe behind the journal and dead-letter rows."""
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_REDIS_URL", url)
        if not driver_present:
            monkeypatch.setitem(sys.modules, "redis", None)

        assert bootstrap._redis_url_configured() is configured


# =============================================================================
# 801 D2 (G12) — production test mode names the backends it leaves inert
# =============================================================================


class TestTestModeWiringSkipBehavior:
    """``BALDUR_TEST_MODE=true`` skips wiring, so every store is process memory.

    In production, with a backend-selecting variable set, that is one WARNING
    naming the variables — never their values, since a URL can embed
    credentials. Otherwise it stays a DEBUG line.
    """

    _SKIPPED = "baldur.registry_wiring_skipped"

    @pytest.fixture
    def no_selectors(self, monkeypatch):
        from baldur.bootstrap import _BACKEND_SELECTION_ENV_VARS

        for name in _BACKEND_SELECTION_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_production_test_mode_names_the_redis_url_and_never_its_value(
        self, monkeypatch, no_selectors, isolated_all_wired_registries
    ):
        from structlog.testing import capture_logs

        from baldur import bootstrap

        url = "redis://user:pw-s3cret@cache.internal:6379/0"
        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", url)
        bootstrap.reset_init_state()

        with capture_logs() as logs:
            bootstrap._wire_registry_defaults()

        skipped = [e for e in logs if e.get("event") == self._SKIPPED]
        assert len(skipped) == 1
        assert skipped[0]["log_level"] == "warning"
        assert skipped[0]["ignored_env_vars"] == ["BALDUR_REDIS_URL"]
        assert url not in repr(skipped[0])
        assert "pw-s3cret" not in repr(skipped[0])

    def test_an_override_variable_is_named_alongside_the_dsn(
        self, monkeypatch, no_selectors
    ):
        from structlog.testing import capture_logs

        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://u:p@db/baldur")
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "sql")

        with capture_logs() as logs:
            bootstrap._announce_test_mode_wiring_skip(_runtime(is_production=True))

        skipped = [e for e in logs if e.get("event") == self._SKIPPED]
        assert [e["ignored_env_vars"] for e in skipped] == [
            ["BALDUR_SQL_DSN", "BALDUR_DLQ_BACKEND"]
        ]

    @pytest.mark.parametrize(
        ("is_production", "redis_url"),
        [(False, "redis://h:6379/0"), (True, None)],
        ids=["non_production_with_a_selector", "production_with_nothing_set"],
    )
    def test_otherwise_the_skip_stays_a_debug_line(
        self, monkeypatch, no_selectors, is_production, redis_url
    ):
        from structlog.testing import capture_logs

        from baldur import bootstrap

        if redis_url is not None:
            monkeypatch.setenv("BALDUR_REDIS_URL", redis_url)

        with capture_logs() as logs:
            bootstrap._announce_test_mode_wiring_skip(
                _runtime(is_production=is_production)
            )

        assert [e for e in logs if e.get("event") == self._SKIPPED] == []
        assert [
            e["log_level"]
            for e in logs
            if e.get("event") == "baldur.wire_registry_defaults_skipped_test_mode"
        ] == ["debug"]


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
