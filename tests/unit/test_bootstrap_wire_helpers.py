"""Per-helper unit tests for ``baldur.bootstrap`` wiring (#464 / #473 / #515).

Sources:
- ``src/baldur/bootstrap.py:_wire_redis_registry`` (Group A row dispatch — D3)
- ``src/baldur/bootstrap.py:_wire_sql_django_registry`` (Group B row dispatch — D6)
- ``src/baldur/bootstrap.py:_wire_priority_chain_registry`` (Group C row dispatch — 515 D6)
- ``src/baldur/bootstrap.py:_django_databases_configured`` (D4 signal probe)
- ``src/baldur/bootstrap.py:_postgres_dsn_configured`` (515 D6 signal probe)

These tests exercise each helper in isolation against a real
``GenericProviderRegistry`` (constructed from ``ProviderRegistry`` under
``snapshot()``) so the per-row trigger-matrix logic is verified without
the surrounding ``_wire_registry_defaults`` orchestration. Trigger-matrix
coverage of the orchestration layer lives in
``tests/unit/test_bootstrap_wire_registry_defaults.py``.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (logger calls, registry.set_default).
- §8.4 Side effects (default-name mutation, structured-log emission).
- §8.2 Exception/edge cases (ConfigurationError raise paths,
  ImportError/ImproperlyConfigured fall-through in D4 helper).
- §6.7 parametrize for the trigger matrix and signal combinations.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from baldur.core.exceptions import ConfigurationError


@pytest.fixture
def fake_runtime():
    """Return a factory producing a stubbed runtime with the two flags set."""

    def _make(*, is_production: bool = False, is_test_mode: bool = False):
        runtime = MagicMock()
        runtime.is_production = is_production
        runtime.is_test_mode = is_test_mode
        return runtime

    return _make


@pytest.fixture
def cache_registry_isolated():
    """Use ``ProviderRegistry.cache`` (a real ``GenericProviderRegistry``)
    under ``snapshot()`` — isolates the test mutation."""
    from baldur.factory.registry import ProviderRegistry

    with ProviderRegistry.cache.snapshot():
        # Reset to a known baseline so the test starts from "memory".
        ProviderRegistry.cache.set_default("memory")
        yield ProviderRegistry.cache


@pytest.fixture
def cascade_registry_isolated():
    """Use ``ProviderRegistry.cascade_event_repo`` for Group B helper tests."""
    from baldur.factory.registry import ProviderRegistry

    with ProviderRegistry.cascade_event_repo.snapshot():
        ProviderRegistry.cascade_event_repo.set_default("memory")
        yield ProviderRegistry.cascade_event_repo


@pytest.fixture
def rate_limit_registry_isolated():
    """Use ``ProviderRegistry.rate_limit_storage`` for D11 fallback tests."""
    from baldur.factory.registry import ProviderRegistry

    with ProviderRegistry.rate_limit_storage.snapshot():
        ProviderRegistry.rate_limit_storage.set_default("memory")
        yield ProviderRegistry.rate_limit_storage


@pytest.fixture
def database_health_registry_isolated():
    """Use ``ProviderRegistry.database_health`` for 473 D1 Group C tests."""
    from baldur.factory.registry import ProviderRegistry

    with ProviderRegistry.database_health.snapshot():
        ProviderRegistry.database_health.set_default("noop")
        yield ProviderRegistry.database_health


# =============================================================================
# 464 — _wire_redis_registry per-helper trigger matrix
# =============================================================================


class TestWireRedisRegistryBehavior:
    """Per-row D3 matrix for ``_wire_redis_registry``.

    Each test calls the helper directly against a real registry under
    ``snapshot()`` and verifies the post-state default plus log/raise
    behavior. Parametrized over ``fallback_target`` so the standard rows
    (``None``) and the rate_limit_storage row (``"database"``) are both
    covered.
    """

    @pytest.mark.parametrize("fallback_target", [None, "database"])
    def test_redis_set_flips_default_to_target(
        self, cache_registry_isolated, fake_runtime, fallback_target
    ):
        """Redis URL set → ``set_default(target_name)`` regardless of fallback."""
        from baldur import bootstrap

        bootstrap._wire_redis_registry(
            cache_registry_isolated,
            target_name="redis",
            fallback_target=fallback_target,
            redis_set=True,
            django_set=False,
            runtime=fake_runtime(is_production=True),
        )

        assert cache_registry_isolated.get_default_name() == "redis"

    def test_redis_set_takes_priority_over_fallback(
        self, rate_limit_registry_isolated, fake_runtime
    ):
        """When BOTH redis and django are set, redis wins (D11 priority)."""
        from baldur import bootstrap

        bootstrap._wire_redis_registry(
            rate_limit_registry_isolated,
            target_name="redis",
            fallback_target="database",
            redis_set=True,
            django_set=True,
            runtime=fake_runtime(is_production=True),
        )

        assert rate_limit_registry_isolated.get_default_name() == "redis"

    def test_redis_unset_django_set_with_fallback_picks_fallback(
        self, rate_limit_registry_isolated, fake_runtime
    ):
        """D11 fallback: Redis unset, Django set, ``fallback_target='database'``
        → registry picks ``"database"`` (production safe)."""
        from baldur import bootstrap

        bootstrap._wire_redis_registry(
            rate_limit_registry_isolated,
            target_name="redis",
            fallback_target="database",
            redis_set=False,
            django_set=True,
            runtime=fake_runtime(is_production=True),
        )

        assert rate_limit_registry_isolated.get_default_name() == "database"

    def test_redis_unset_django_set_without_fallback_in_production_raises(
        self, cache_registry_isolated, fake_runtime
    ):
        """No fallback configured + Redis unset in production → ConfigurationError.

        Django being set is irrelevant when ``fallback_target is None`` —
        only the rate_limit_storage row consults the fallback.
        """
        from baldur import bootstrap

        with pytest.raises(ConfigurationError) as exc_info:
            bootstrap._wire_redis_registry(
                cache_registry_isolated,
                target_name="redis",
                fallback_target=None,
                redis_set=False,
                django_set=True,  # ignored without fallback_target
                runtime=fake_runtime(is_production=True),
            )

        message = str(exc_info.value)
        assert "BALDUR_REDIS_URL" in message
        # No "Django DATABASES" hint when this row has no fallback.
        assert "Django DATABASES" not in message

    def test_redis_unset_no_django_with_fallback_in_production_raises(
        self, rate_limit_registry_isolated, fake_runtime
    ):
        """rate_limit_storage with neither signal in prod → ConfigurationError
        names both required signals."""
        from baldur import bootstrap

        with pytest.raises(ConfigurationError) as exc_info:
            bootstrap._wire_redis_registry(
                rate_limit_registry_isolated,
                target_name="redis",
                fallback_target="database",
                redis_set=False,
                django_set=False,
                runtime=fake_runtime(is_production=True),
            )

        message = str(exc_info.value)
        assert "BALDUR_REDIS_URL or Django DATABASES" in message
        assert "configure Django DATABASES" in message

    def test_redis_unset_in_non_production_warns_and_falls_back_to_memory(
        self, cache_registry_isolated, fake_runtime, caplog
    ):
        """non-prod + Redis unset → WARNING + ``set_default("memory")``."""
        from baldur import bootstrap

        # Pre-drift to surface that the helper resets to memory.
        cache_registry_isolated.set_default("redis")

        with caplog.at_level("WARNING"):
            bootstrap._wire_redis_registry(
                cache_registry_isolated,
                target_name="redis",
                fallback_target=None,
                redis_set=False,
                django_set=False,
                runtime=fake_runtime(is_production=False),
            )

        assert cache_registry_isolated.get_default_name() == "memory"
        # Helper emits the unified ``registry_memory_fallback`` event with
        # ``reason="redis_url_unset"`` for Group A non-prod fallback.
        assert any(
            "registry_memory_fallback" in r.message and "redis_url_unset" in r.message
            for r in caplog.records
        )

    @pytest.mark.parametrize("fallback_target", [None, "database"])
    def test_redis_unset_in_non_production_with_fallback_unused_warns(
        self,
        cache_registry_isolated,
        fake_runtime,
        caplog,
        fallback_target,
    ):
        """non-prod + neither signal → WARNING regardless of fallback (no Django)."""
        from baldur import bootstrap

        with caplog.at_level("WARNING"):
            bootstrap._wire_redis_registry(
                cache_registry_isolated,
                target_name="redis",
                fallback_target=fallback_target,
                redis_set=False,
                django_set=False,
                runtime=fake_runtime(is_production=False),
            )

        assert cache_registry_isolated.get_default_name() == "memory"


# =============================================================================
# 464 — _wire_sql_django_registry per-helper trigger matrix
# =============================================================================


class TestWireSqlDjangoRegistryBehavior:
    """Per-row D6 matrix for ``_wire_sql_django_registry``.

    Six trigger conditions × the 3 Group B rows; the helper logic is
    identical across rows so a representative ``cascade_event_repo``
    fixture covers the matrix.
    """

    def test_sql_set_picks_sql_target_regardless_of_django(
        self, cascade_registry_isolated, fake_runtime
    ):
        """D5 priority: ``BALDUR_SQL_DSN`` always wins over Django."""
        from baldur import bootstrap

        bootstrap._wire_sql_django_registry(
            cascade_registry_isolated,
            sql_target="sql",
            django_target="django",
            sql_set=True,
            django_set=True,  # ignored
            runtime=fake_runtime(is_production=True),
        )

        assert cascade_registry_isolated.get_default_name() == "sql"

    def test_only_django_set_picks_django_target(
        self, cascade_registry_isolated, fake_runtime
    ):
        """DSN unset + Django set → ``set_default(django_target)``."""
        from baldur import bootstrap

        bootstrap._wire_sql_django_registry(
            cascade_registry_isolated,
            sql_target="sql",
            django_target="django",
            sql_set=False,
            django_set=True,
            runtime=fake_runtime(is_production=True),
        )

        assert cascade_registry_isolated.get_default_name() == "django"

    def test_neither_signal_in_production_raises_configuration_error(
        self, cascade_registry_isolated, fake_runtime
    ):
        """prod + neither signal → ConfigurationError naming both."""
        from baldur import bootstrap

        with pytest.raises(ConfigurationError) as exc_info:
            bootstrap._wire_sql_django_registry(
                cascade_registry_isolated,
                sql_target="sql",
                django_target="django",
                sql_set=False,
                django_set=False,
                runtime=fake_runtime(is_production=True),
            )

        message = str(exc_info.value)
        assert "BALDUR_SQL_DSN" in message
        assert "Django DATABASES" in message
        assert "ProviderRegistry.cascade_event_repo" in message

    def test_neither_signal_in_non_production_warns_and_falls_back_to_memory(
        self, cascade_registry_isolated, fake_runtime, caplog
    ):
        """non-prod + neither → WARNING + ``set_default("memory")``."""
        from baldur import bootstrap

        with caplog.at_level("WARNING"):
            bootstrap._wire_sql_django_registry(
                cascade_registry_isolated,
                sql_target="sql",
                django_target="django",
                sql_set=False,
                django_set=False,
                runtime=fake_runtime(is_production=False),
            )

        assert cascade_registry_isolated.get_default_name() == "memory"
        # Helper emits the unified ``registry_memory_fallback`` event with
        # ``reason="sql_django_unset"`` for Group B non-prod fallback.
        assert any(
            "registry_memory_fallback" in r.message and "sql_django_unset" in r.message
            for r in caplog.records
        )

    def test_sql_set_in_non_production_picks_sql_target(
        self, cascade_registry_isolated, fake_runtime
    ):
        """non-prod + DSN set → ``"sql"`` (no special non-prod treatment)."""
        from baldur import bootstrap

        bootstrap._wire_sql_django_registry(
            cascade_registry_isolated,
            sql_target="sql",
            django_target="django",
            sql_set=True,
            django_set=False,
            runtime=fake_runtime(is_production=False),
        )

        assert cascade_registry_isolated.get_default_name() == "sql"

    def test_only_django_set_in_non_production_picks_django(
        self, cascade_registry_isolated, fake_runtime
    ):
        """non-prod + Django set → ``"django"``."""
        from baldur import bootstrap

        bootstrap._wire_sql_django_registry(
            cascade_registry_isolated,
            sql_target="sql",
            django_target="django",
            sql_set=False,
            django_set=True,
            runtime=fake_runtime(is_production=False),
        )

        assert cascade_registry_isolated.get_default_name() == "django"


# =============================================================================
# 464 — _django_databases_configured signal probe
# =============================================================================


class TestDjangoDatabasesConfiguredBehavior:
    """D4: env-guard + lazy import + attribute access fall-through.

    The helper returns True iff ALL three conditions hold:
    1. ``DJANGO_SETTINGS_MODULE`` env var is set (and truthy).
    2. ``from django.conf import settings`` succeeds.
    3. ``settings.DATABASES`` is non-empty.

    Any failure path (env unset, ImportError, ImproperlyConfigured, attr
    error) returns False and logs at DEBUG.
    """

    def test_returns_false_when_django_settings_module_unset(self, monkeypatch):
        """No env var → early-exit ``False``, no Django import attempted."""
        from baldur import bootstrap

        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        # Even if django is importable, the env-guard short-circuits first.
        assert bootstrap._django_databases_configured() is False

    def test_returns_false_when_django_settings_module_empty_string(self, monkeypatch):
        """Empty string is not truthy → early-exit ``False``."""
        from baldur import bootstrap

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "")
        assert bootstrap._django_databases_configured() is False

    def test_returns_false_when_django_import_fails(self, monkeypatch):
        """Env set + Django not importable → ``False`` (treated as not usable)."""
        from baldur import bootstrap

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "fake.settings")
        # Inject ImportError on ``django.conf`` import.
        monkeypatch.setitem(sys.modules, "django.conf", None)

        assert bootstrap._django_databases_configured() is False

    def test_returns_true_when_django_databases_populated(self, monkeypatch):
        """Env set + Django importable + non-empty DATABASES → True."""
        from baldur import bootstrap

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

        # Pre-import django.conf and stub a populated DATABASES.
        fake_conf = MagicMock()
        fake_conf.settings.DATABASES = {"default": {"ENGINE": "sqlite3"}}
        monkeypatch.setitem(sys.modules, "django.conf", fake_conf)

        assert bootstrap._django_databases_configured() is True

    def test_returns_false_when_django_databases_empty_dict(self, monkeypatch):
        """DATABASES present but empty → ``False`` (no usable backend)."""
        from baldur import bootstrap

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

        fake_conf = MagicMock()
        fake_conf.settings.DATABASES = {}
        monkeypatch.setitem(sys.modules, "django.conf", fake_conf)

        assert bootstrap._django_databases_configured() is False

    def test_returns_false_when_django_databases_attr_missing(self, monkeypatch):
        """``getattr(settings, "DATABASES", None)`` defaults to None → False."""
        from baldur import bootstrap

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

        class _Settings:
            pass  # no DATABASES attribute

        fake_conf = MagicMock()
        fake_conf.settings = _Settings()
        monkeypatch.setitem(sys.modules, "django.conf", fake_conf)

        assert bootstrap._django_databases_configured() is False

    def test_returns_false_when_attribute_access_raises(self, monkeypatch, caplog):
        """R4: malformed Django setup raising on attribute access → ``False`` + DEBUG log."""
        from baldur import bootstrap

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "fake.settings")

        # Construct a fake module whose .settings descriptor raises on access.
        class _RaisingSettings:
            @property
            def DATABASES(self):
                raise RuntimeError("ImproperlyConfigured-equivalent")

        fake_conf = MagicMock()
        fake_conf.settings = _RaisingSettings()
        monkeypatch.setitem(sys.modules, "django.conf", fake_conf)

        with caplog.at_level("DEBUG"):
            assert bootstrap._django_databases_configured() is False

        # The exception was swallowed; a structured DEBUG event records it.
        assert any("django_databases_probe_failed" in r.message for r in caplog.records)


# =============================================================================
# 515 — _wire_priority_chain_registry per-helper trigger matrix
# =============================================================================


def _build_wiring(
    registry_attr: str = "database_health",
    chain: tuple[tuple[str, object], ...] = (
        ("django", lambda: False),
        ("noop", lambda: True),
    ),
    env_override: str | None = "BALDUR_DATABASE_HEALTH_PROVIDER",
):
    """Helper to assemble a PRIORITY_CHAIN _RegistryWiring for tests."""
    from baldur.bootstrap import _BackendKind, _RegistryWiring

    return _RegistryWiring(
        _BackendKind.PRIORITY_CHAIN,
        registry_attr,
        target_name="",
        priority_chain=chain,
        env_override=env_override,
    )


class TestWirePriorityChainRegistryBehavior:
    """Per-row D6 matrix for ``_wire_priority_chain_registry`` (515).

    Probe-surface registries (``database_health``, ``pg_admin``,
    ``pool_info``) resolve to the first ``(name, probe)`` pair whose
    probe returns True AND whose provider is registered. ``env_override``
    forces a specific name when set to a registered provider. No
    production fail-loud branch — non-Django + non-Postgres deployments
    legitimately fall through to ``"noop"``.
    """

    def test_first_matching_probe_wins(
        self, database_health_registry_isolated, fake_runtime, monkeypatch
    ):
        """Django probe True → ``set_default("django")``."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_DATABASE_HEALTH_PROVIDER", raising=False)
        wiring = _build_wiring(
            chain=(
                ("django", lambda: True),
                ("sql", lambda: True),
                ("noop", lambda: True),
            )
        )

        bootstrap._wire_priority_chain_registry(
            database_health_registry_isolated,
            wiring,
            fake_runtime(is_production=True),
        )

        assert database_health_registry_isolated.get_default_name() == "django"

    def test_fallthrough_to_noop_when_all_probes_false_except_terminal(
        self, database_health_registry_isolated, fake_runtime, monkeypatch
    ):
        """When earlier probes fail, the terminal ``(noop, lambda: True)`` wins."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_DATABASE_HEALTH_PROVIDER", raising=False)
        database_health_registry_isolated.set_default("django")
        wiring = _build_wiring(
            chain=(
                ("django", lambda: False),
                ("sql", lambda: False),
                ("noop", lambda: True),
            )
        )

        bootstrap._wire_priority_chain_registry(
            database_health_registry_isolated,
            wiring,
            fake_runtime(is_production=False),
        )

        assert database_health_registry_isolated.get_default_name() == "noop"

    def test_no_match_in_production_does_not_raise(
        self, database_health_registry_isolated, fake_runtime, monkeypatch
    ):
        """515 D6: probe-surface registries do NOT fail loud in production."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_DATABASE_HEALTH_PROVIDER", raising=False)
        wiring = _build_wiring(
            chain=(
                ("django", lambda: False),
                ("noop", lambda: True),
            )
        )

        # Does not raise — probe-surface intentionally lands on noop.
        bootstrap._wire_priority_chain_registry(
            database_health_registry_isolated,
            wiring,
            fake_runtime(is_production=True),
        )
        assert database_health_registry_isolated.get_default_name() == "noop"

    def test_env_override_forces_specific_provider(
        self, database_health_registry_isolated, fake_runtime, monkeypatch
    ):
        """When env var names a registered provider, it wins over the chain."""
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_DATABASE_HEALTH_PROVIDER", "noop")
        wiring = _build_wiring(
            chain=(
                ("django", lambda: True),
                ("noop", lambda: True),
            )
        )

        bootstrap._wire_priority_chain_registry(
            database_health_registry_isolated,
            wiring,
            fake_runtime(is_production=False),
        )

        assert database_health_registry_isolated.get_default_name() == "noop"

    def test_env_override_invalid_value_logs_warning_then_falls_to_chain(
        self, database_health_registry_isolated, fake_runtime, monkeypatch, caplog
    ):
        """Unregistered env value → WARNING, chain takes over."""
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_DATABASE_HEALTH_PROVIDER", "bogus-name")
        wiring = _build_wiring(
            chain=(("noop", lambda: True),),
        )

        with caplog.at_level("WARNING"):
            bootstrap._wire_priority_chain_registry(
                database_health_registry_isolated,
                wiring,
                fake_runtime(is_production=False),
            )

        assert any("registry_env_override_invalid" in r.message for r in caplog.records)
        assert database_health_registry_isolated.get_default_name() == "noop"

    def test_multi_match_emits_info_log_with_candidates(
        self, database_health_registry_isolated, fake_runtime, monkeypatch, caplog
    ):
        """When ≥ 2 probes match, the winner + candidates are logged at INFO."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_DATABASE_HEALTH_PROVIDER", raising=False)
        wiring = _build_wiring(
            chain=(
                ("django", lambda: True),
                ("noop", lambda: True),
            )
        )

        with caplog.at_level("INFO"):
            bootstrap._wire_priority_chain_registry(
                database_health_registry_isolated,
                wiring,
                fake_runtime(is_production=False),
            )

        assert any(
            "registry_priority_chain_resolved" in r.message for r in caplog.records
        )
        assert database_health_registry_isolated.get_default_name() == "django"

    @pytest.mark.parametrize("is_test_mode", [True, False])
    def test_helper_ignores_is_test_mode(
        self,
        database_health_registry_isolated,
        fake_runtime,
        monkeypatch,
        is_test_mode,
    ):
        """Helper itself does not consult is_test_mode (caller's early return)."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_DATABASE_HEALTH_PROVIDER", raising=False)
        wiring = _build_wiring(
            chain=(
                ("django", lambda: True),
                ("noop", lambda: True),
            )
        )

        bootstrap._wire_priority_chain_registry(
            database_health_registry_isolated,
            wiring,
            fake_runtime(is_production=False, is_test_mode=is_test_mode),
        )
        assert database_health_registry_isolated.get_default_name() == "django"


# =============================================================================
# 515 — _postgres_dsn_configured signal probe
# =============================================================================


class TestPostgresDsnConfiguredProbe:
    """515 D6: probe returns True iff ``BALDUR_SQL_DSN`` OR any
    ``BALDUR_POSTGRES_*`` env is non-empty (after strip).

    Mirrors ``baldur.settings.sql.resolve_dsn`` precedence — ``BALDUR_SQL_DSN``
    wins when set; otherwise any of the four ``BALDUR_POSTGRES_*`` component
    fields satisfies the probe (postgres-only fallback).
    """

    @pytest.fixture(autouse=True)
    def _clear_all_postgres_env(self, monkeypatch):
        """Strip every relevant env var so each test starts from a clean slate."""
        for name in (
            "BALDUR_SQL_DSN",
            "BALDUR_POSTGRES_HOST",
            "BALDUR_POSTGRES_PORT",
            "BALDUR_POSTGRES_DATABASE",
            "BALDUR_POSTGRES_USER",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_returns_false_when_no_env_set(self):
        from baldur import bootstrap

        assert bootstrap._postgres_dsn_configured() is False

    def test_returns_true_when_sql_dsn_set(self, monkeypatch):
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://prod-db/baldur")
        assert bootstrap._postgres_dsn_configured() is True

    def test_returns_false_when_sql_dsn_blank(self, monkeypatch):
        """Whitespace-only DSN is treated as unset (mirrors REDIS_URL handling)."""
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_SQL_DSN", "   ")
        assert bootstrap._postgres_dsn_configured() is False

    @pytest.mark.parametrize(
        "env_var",
        [
            "BALDUR_POSTGRES_HOST",
            "BALDUR_POSTGRES_PORT",
            "BALDUR_POSTGRES_DATABASE",
            "BALDUR_POSTGRES_USER",
        ],
    )
    def test_returns_true_when_any_postgres_component_set(self, monkeypatch, env_var):
        """Any of the four component fields satisfies the probe."""
        from baldur import bootstrap

        monkeypatch.setenv(env_var, "value")
        assert bootstrap._postgres_dsn_configured() is True

    @pytest.mark.parametrize(
        "env_var",
        [
            "BALDUR_POSTGRES_HOST",
            "BALDUR_POSTGRES_PORT",
            "BALDUR_POSTGRES_DATABASE",
            "BALDUR_POSTGRES_USER",
        ],
    )
    def test_returns_false_when_component_is_whitespace(self, monkeypatch, env_var):
        """Whitespace-only values are treated as unset across all components."""
        from baldur import bootstrap

        monkeypatch.setenv(env_var, "   ")
        assert bootstrap._postgres_dsn_configured() is False

    def test_dsn_priority_over_components(self, monkeypatch):
        """Both set → DSN wins (probe still True, but verifies short-circuit).

        Even if all ``BALDUR_POSTGRES_*`` are unset, the DSN alone returns True.
        """
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://x")
        # Components stay unset by the autouse fixture.
        assert bootstrap._postgres_dsn_configured() is True


# =============================================================================
# 570 — _redis_url_configured signal probe
# =============================================================================


class TestRedisUrlConfiguredProbe:
    """570 D1: probe returns True iff ``BALDUR_REDIS_URL`` is set and
    non-empty (after ``.strip()``).

    Mirrors the inline ``redis_set`` computation in
    ``_wire_registry_defaults`` (reads ``os.environ`` directly rather than
    ``RedisSettings.url`` because the settings default
    ``redis://localhost:6379/0`` would mask the unset case). Consumed by the
    ``event_journal_repo`` PRIORITY_CHAIN row as the first probe in its
    ``redis > sql > memory`` chain — tested exactly like the sibling
    ``_postgres_dsn_configured`` probe.
    """

    @pytest.fixture(autouse=True)
    def _clear_redis_url_env(self, monkeypatch):
        """Start each test from an unset ``BALDUR_REDIS_URL``."""
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)

    def test_returns_false_when_redis_url_unset(self):
        from baldur import bootstrap

        assert bootstrap._redis_url_configured() is False

    def test_returns_true_when_redis_url_set(self, monkeypatch):
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod-host:6379/0")
        assert bootstrap._redis_url_configured() is True

    @pytest.mark.parametrize(
        "raw_value",
        ["", "   ", "\t", "\n  "],
        ids=["empty", "spaces", "tab", "newline_spaces"],
    )
    def test_returns_false_when_redis_url_blank_or_whitespace(
        self, monkeypatch, raw_value
    ):
        """Empty / whitespace-only values are treated as unset (boundary)."""
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_REDIS_URL", raw_value)
        assert bootstrap._redis_url_configured() is False


# =============================================================================
# 515 — compute_pool_status registry routing
# =============================================================================


class TestComputePoolStatusBehavior:
    """``compute_pool_status`` resolves PG-admin + pool-info via the registry.

    Source: ``src/baldur/services/precomputed_cache/compute_functions.py``

    The function consults three registry slots:
    - ``pool_info.get()``       → SQLAlchemy/Django/Noop pool stats.
    - ``database_health.get()`` → connection usability probe.
    - ``pg_admin.get()``        → pg_stat_activity counters (gated on
      ``is_available()``).

    When ``pg_admin`` resolves to ``NoopPgAdmin`` (``is_available()`` False),
    the ``pg_stats`` key is omitted from the response — fail-open
    observability behavior per CROSS_SERVICE_STANDARDS §3.
    """

    @pytest.fixture(autouse=True)
    def _disable_test_mode(self, monkeypatch):
        """``compute_pool_status`` short-circuits on BALDUR_TEST_MODE=true —
        clear it so the registry path is exercised."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)

    @pytest.fixture
    def stub_registry(self, monkeypatch):
        """Replace the three registry slots with controllable mocks.

        Returns a single object exposing ``pool_info`` / ``database_health``
        / ``pg_admin`` MagicMocks whose ``.get()`` methods can be tuned
        per test.
        """
        from baldur.factory import ProviderRegistry

        pool_info_provider = MagicMock(name="pool_info_provider")
        database_health_provider = MagicMock(name="database_health_provider")
        pg_admin_provider = MagicMock(name="pg_admin_provider")

        monkeypatch.setattr(
            ProviderRegistry.pool_info, "get", lambda: pool_info_provider
        )
        monkeypatch.setattr(
            ProviderRegistry.database_health,
            "get",
            lambda: database_health_provider,
        )
        monkeypatch.setattr(ProviderRegistry.pg_admin, "get", lambda: pg_admin_provider)

        stub = MagicMock()
        stub.pool_info = pool_info_provider
        stub.database_health = database_health_provider
        stub.pg_admin = pg_admin_provider
        return stub

    def test_healthy_when_pg_admin_available(self, stub_registry):
        """``pg_admin.is_available()=True`` → ``pg_stats`` key populated."""
        from baldur.interfaces.database_health import DatabaseConnectionInfo
        from baldur.interfaces.pg_admin import ConnectionStats
        from baldur.services.precomputed_cache.compute_functions import (
            compute_pool_status,
        )

        stub_registry.pool_info.get_pool_info.return_value = {
            "pool_size": 10,
            "checkedout": 3,
            "pool_exhausted": False,
        }
        stub_registry.database_health.check_connection.return_value = (
            DatabaseConnectionInfo(alias="default", vendor="postgresql", is_usable=True)
        )
        stub_registry.pg_admin.is_available.return_value = True
        stub_registry.pg_admin.get_connection_stats.return_value = ConnectionStats(
            total_connections=20, active=5, idle=14, idle_in_transaction=1
        )

        result = compute_pool_status()

        assert result["status"] == "healthy"
        assert result["connection_usable"] is True
        assert result["sqlalchemy_pool"]["pool_size"] == 10
        assert result["pg_stats"] == {
            "total_connections": 20,
            "active": 5,
            "idle": 14,
            "idle_in_transaction": 1,
        }

    def test_pg_stats_omitted_when_pg_admin_unavailable(self, stub_registry):
        """``pg_admin.is_available()=False`` → ``pg_stats`` key absent."""
        from baldur.interfaces.database_health import DatabaseConnectionInfo
        from baldur.services.precomputed_cache.compute_functions import (
            compute_pool_status,
        )

        stub_registry.pool_info.get_pool_info.return_value = {"pool_size": 10}
        stub_registry.database_health.check_connection.return_value = (
            DatabaseConnectionInfo(alias="default", vendor="postgresql", is_usable=True)
        )
        stub_registry.pg_admin.is_available.return_value = False

        result = compute_pool_status()

        assert "pg_stats" not in result
        # ``get_connection_stats`` must NOT be invoked when unavailable.
        stub_registry.pg_admin.get_connection_stats.assert_not_called()

    def test_exhausted_status_from_pool_info(self, stub_registry):
        """``pool_exhausted=True`` in pool_info flips the top-level status."""
        from baldur.interfaces.database_health import DatabaseConnectionInfo
        from baldur.services.precomputed_cache.compute_functions import (
            compute_pool_status,
        )

        stub_registry.pool_info.get_pool_info.return_value = {"pool_exhausted": True}
        stub_registry.database_health.check_connection.return_value = (
            DatabaseConnectionInfo(alias="default", vendor="postgresql", is_usable=True)
        )
        stub_registry.pg_admin.is_available.return_value = False

        result = compute_pool_status()

        assert result["status"] == "exhausted"

    def test_empty_pool_info_treated_as_healthy(self, stub_registry):
        """``{}`` dict (no pool reachable) → ``pool_exhausted`` defaults to False."""
        from baldur.interfaces.database_health import DatabaseConnectionInfo
        from baldur.services.precomputed_cache.compute_functions import (
            compute_pool_status,
        )

        stub_registry.pool_info.get_pool_info.return_value = {}
        stub_registry.database_health.check_connection.return_value = (
            DatabaseConnectionInfo(alias="default", vendor="postgresql", is_usable=True)
        )
        stub_registry.pg_admin.is_available.return_value = False

        result = compute_pool_status()

        assert result["status"] == "healthy"
        assert result["sqlalchemy_pool"] == {}

    def test_test_mode_short_circuits_before_registry_lookup(self, monkeypatch):
        """``BALDUR_TEST_MODE=true`` skips the entire registry-dependent path."""
        from baldur.services.precomputed_cache.compute_functions import (
            compute_pool_status,
        )

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")

        result = compute_pool_status()

        assert result["status"] == "test_mode"
        assert "sqlalchemy_pool" not in result

    def test_unexpected_exception_swallowed_into_error_dict(
        self, stub_registry, caplog
    ):
        """Any uncaught exception → ``{"status": "error", ...}`` (graceful)."""
        from baldur.services.precomputed_cache.compute_functions import (
            compute_pool_status,
        )

        stub_registry.pool_info.get_pool_info.side_effect = RuntimeError(
            "transient pool failure"
        )

        with caplog.at_level("ERROR"):
            result = compute_pool_status()

        assert result["status"] == "error"
        assert "transient pool failure" in result["error"]
        assert any(
            "precomputed_cache.pool_status_compute_failed" in r.message
            for r in caplog.records
        )
