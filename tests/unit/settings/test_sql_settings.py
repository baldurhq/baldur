"""
Unit tests for baldur.settings.sql.

Coverage:
- SQLDialect enum contract (postgres / mysql / sqlite values).
- infer_dialect() scheme → dialect mapping (equivalence classes).
- resolve_dsn() precedence — explicit DSN > BALDUR_POSTGRES_* fallback.
- SQLSettings field defaults + dialect validator.
- Singleton lifecycle for get_sql_settings / reset_sql_settings.

Target: 429 Part 4 C11.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings import postgres as postgres_settings
from baldur.settings.sql import (
    SQLDialect,
    SQLSettings,
    get_sql_settings,
    infer_dialect,
    reset_sql_settings,
    resolve_dsn,
)


@pytest.fixture(autouse=True)
def _reset_sql_settings_singleton():
    """Each test starts with a fresh singleton."""
    reset_sql_settings()
    yield
    reset_sql_settings()


class TestSQLDialectContract:
    """SQLDialect enum contract."""

    def test_dialect_values_match_design_contract(self):
        """Dialect values are the DSN schemes Baldur supports."""
        assert SQLDialect.POSTGRESQL.value == "postgresql"
        assert SQLDialect.MYSQL.value == "mysql"
        assert SQLDialect.SQLITE.value == "sqlite"

    def test_dialect_count_is_three(self):
        """429 Part 4 supports exactly three dialects."""
        assert len(list(SQLDialect)) == 3


class TestInferDialectBehavior:
    """infer_dialect() scheme equivalence classes."""

    @pytest.mark.parametrize(
        ("dsn", "expected"),
        [
            ("postgresql://user@host/db", SQLDialect.POSTGRESQL),
            ("postgres://user@host/db", SQLDialect.POSTGRESQL),
            ("mysql://user@host/db", SQLDialect.MYSQL),
            ("mariadb://user@host/db", SQLDialect.MYSQL),
            ("sqlite:///path/to/db", SQLDialect.SQLITE),
            ("sqlite:///:memory:", SQLDialect.SQLITE),
        ],
    )
    def test_known_schemes_map_to_expected_dialect(self, dsn, expected):
        assert infer_dialect(dsn) is expected

    def test_empty_dsn_falls_back_to_postgresql(self):
        """Empty DSN falls back to postgresql — the primary target."""
        assert infer_dialect("") is SQLDialect.POSTGRESQL

    def test_unknown_scheme_falls_back_to_postgresql(self):
        """Unknown schemes do not raise — they fall back to postgresql."""
        assert infer_dialect("clickhouse://host/db") is SQLDialect.POSTGRESQL

    def test_uppercase_scheme_is_normalized(self):
        """Schemes are case-insensitive."""
        assert infer_dialect("POSTGRESQL://host/db") is SQLDialect.POSTGRESQL


class TestSQLSettingsContract:
    """SQLSettings default + validator contract."""

    def test_default_dsn_is_empty_string(self, monkeypatch):
        """Default DSN is empty — forces fallback chain in resolve_dsn()."""
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        settings = SQLSettings()
        assert settings.dsn == ""

    def test_default_schema_managed_is_true(self, monkeypatch):
        """schema_managed defaults to True (CREATE TABLE IF NOT EXISTS)."""
        monkeypatch.delenv("BALDUR_SQL_SCHEMA_MANAGED", raising=False)
        settings = SQLSettings()
        assert settings.schema_managed is True

    def test_default_autocommit_is_false(self, monkeypatch):
        """autocommit defaults to False — Baldur manages commit/rollback."""
        monkeypatch.delenv("BALDUR_SQL_AUTOCOMMIT", raising=False)
        settings = SQLSettings()
        assert settings.autocommit is False

    def test_invalid_dialect_raises_validation_error(self):
        """Only postgresql/mysql/sqlite are accepted as dialect overrides."""
        with pytest.raises(ValidationError):
            SQLSettings(dialect="clickhouse")

    def test_empty_dialect_is_allowed(self):
        """Empty dialect is allowed — triggers DSN-based inference."""
        settings = SQLSettings(dialect="")
        assert settings.dialect == ""

    def test_dialect_override_is_lowercased(self):
        """Dialect validator normalizes to lowercase."""
        settings = SQLSettings(dialect="PostgreSQL")
        assert settings.dialect == "postgresql"


class TestSQLSettingsResolvedDialectBehavior:
    """resolved_dialect() combines explicit override + DSN inference."""

    def test_explicit_dialect_wins_over_dsn(self):
        """Explicit dialect override bypasses DSN scheme inference."""
        settings = SQLSettings(dsn="mysql://x", dialect="postgresql")
        assert settings.resolved_dialect() is SQLDialect.POSTGRESQL

    def test_dsn_drives_inference_when_dialect_unset(self):
        """Empty dialect → DSN scheme determines the resolved dialect."""
        settings = SQLSettings(dsn="sqlite:///:memory:")
        assert settings.resolved_dialect() is SQLDialect.SQLITE


class TestResolveDsnBehavior:
    """resolve_dsn precedence: explicit DSN > BALDUR_POSTGRES_* fallback."""

    def test_explicit_dsn_is_returned_as_is(self):
        """Explicit DSN wins — no fallback composition."""
        settings = SQLSettings(dsn="postgresql://custom@host/db")
        assert resolve_dsn(settings) == "postgresql://custom@host/db"

    def test_empty_dsn_falls_back_to_postgres_components(self):
        """Empty DSN composes the fallback URL from PostgresSettings."""
        postgres_settings.reset_postgres_settings()
        try:
            settings = SQLSettings(dsn="")
            dsn = resolve_dsn(settings)
            pg = postgres_settings.get_postgres_settings()
            assert dsn == f"postgresql://{pg.user}@{pg.host}:{pg.port}/{pg.database}"
        finally:
            postgres_settings.reset_postgres_settings()

    def test_resolve_uses_singleton_when_argument_missing(self):
        """Calling resolve_dsn() without an argument reads the singleton."""
        dsn_via_singleton = resolve_dsn()
        dsn_via_instance = resolve_dsn(get_sql_settings())
        assert dsn_via_singleton == dsn_via_instance


class TestSQLSettingsSingletonBehavior:
    """get_sql_settings() / reset_sql_settings() lifecycle."""

    def test_get_returns_same_instance(self):
        """get_sql_settings() caches the instance."""
        first = get_sql_settings()
        second = get_sql_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """After reset, a new instance is built on next get."""
        first = get_sql_settings()
        reset_sql_settings()
        second = get_sql_settings()
        assert first is not second
