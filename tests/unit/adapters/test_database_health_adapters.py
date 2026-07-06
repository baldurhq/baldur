"""
Database Health Provider unit tests.

Tests for DatabaseHealthProvider interface, NoopDatabaseHealthAdapter,
and DjangoDatabaseHealthAdapter implementations.

Test Categories:
    A. Contract: DatabaseConnectionInfo defaults, health_check convenience method
    B. Behavior: Noop adapter safe defaults, Django adapter delegation
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.database.noop_health import NoopDatabaseHealthAdapter
from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
)

# =============================================================================
# A. Contract Tests
# =============================================================================


class TestDatabaseConnectionInfoContract:
    """DatabaseConnectionInfo default values and immutability."""

    def test_default_vendor_is_unknown(self):
        """Default vendor should be 'unknown'."""
        info = DatabaseConnectionInfo(alias="test")
        assert info.vendor == "unknown"

    def test_default_is_usable_is_false(self):
        """Default is_usable should be False (fail-safe)."""
        info = DatabaseConnectionInfo(alias="test")
        assert info.is_usable is False

    def test_default_metadata_is_empty_dict(self):
        """Default metadata should be empty dict."""
        info = DatabaseConnectionInfo(alias="test")
        assert info.metadata == {}

    def test_frozen_prevents_mutation(self):
        """DatabaseConnectionInfo is frozen dataclass."""
        info = DatabaseConnectionInfo(alias="test", vendor="pg", is_usable=True)
        with pytest.raises(AttributeError):
            info.vendor = "mysql"

    def test_explicit_values_preserved(self):
        """Explicit values are stored correctly."""
        info = DatabaseConnectionInfo(
            alias="replica",
            vendor="postgresql",
            is_usable=True,
            metadata={"pool_size": 10},
        )
        assert info.alias == "replica"
        assert info.vendor == "postgresql"
        assert info.is_usable is True
        assert info.metadata == {"pool_size": 10}


class TestNoopDatabaseHealthAdapterContract:
    """NoopDatabaseHealthAdapter returns safe defaults."""

    def test_check_connection_returns_unusable(self):
        """Noop adapter returns is_usable=False."""
        adapter = NoopDatabaseHealthAdapter()
        info = adapter.check_connection("default")
        assert info.alias == "default"
        assert info.vendor == "unknown"
        assert info.is_usable is False

    def test_list_aliases_returns_empty(self):
        """Noop adapter returns no aliases."""
        adapter = NoopDatabaseHealthAdapter()
        assert adapter.list_aliases() == []

    def test_close_all_is_noop(self):
        """Noop adapter close_all does nothing (no error)."""
        adapter = NoopDatabaseHealthAdapter()
        adapter.close_all()  # Should not raise

    def test_health_check_returns_false(self):
        """Noop adapter health_check returns False."""
        adapter = NoopDatabaseHealthAdapter()
        assert adapter.health_check() is False


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestDatabaseHealthProviderHealthCheckBehavior:
    """health_check() convenience method behavior."""

    def test_health_check_returns_true_when_usable(self):
        """health_check() returns True when check_connection reports is_usable=True."""
        adapter = NoopDatabaseHealthAdapter()
        # Override check_connection to return usable
        adapter.check_connection = lambda alias="default": DatabaseConnectionInfo(
            alias=alias,
            vendor="test",
            is_usable=True,
        )
        assert adapter.health_check() is True

    def test_health_check_returns_false_on_exception(self):
        """health_check() returns False when check_connection raises."""
        adapter = NoopDatabaseHealthAdapter()
        adapter.check_connection = MagicMock(side_effect=RuntimeError("db down"))
        assert adapter.health_check() is False


class TestDjangoDatabaseHealthAdapterBehavior:
    """DjangoDatabaseHealthAdapter actively probes via cursor.execute."""

    @patch("django.db.connections", autospec=False)
    def test_check_connection_active_probe_success(self, mock_connections):
        """check_connection issues SELECT 1 round-trip; success -> is_usable=True."""
        from baldur.adapters.database.django_health import (
            DjangoDatabaseHealthAdapter,
        )

        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.vendor = "postgresql"
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_connections.__getitem__.return_value = mock_conn

        adapter = DjangoDatabaseHealthAdapter()
        info = adapter.check_connection("default")

        assert info.alias == "default"
        assert info.vendor == "postgresql"
        assert info.is_usable is True
        mock_connections.__getitem__.assert_called_once_with("default")
        mock_cursor.execute.assert_called_once_with("SELECT 1")
        mock_cursor.fetchone.assert_called_once()

    @patch("django.db.connections", autospec=False)
    def test_check_connection_active_probe_failure(self, mock_connections):
        """check_connection: cursor or execute raises -> is_usable=False (no propagation)."""
        from baldur.adapters.database.django_health import (
            DjangoDatabaseHealthAdapter,
        )

        mock_conn = MagicMock()
        mock_conn.vendor = "postgresql"
        mock_conn.cursor.side_effect = RuntimeError("connection already closed")
        mock_connections.__getitem__.return_value = mock_conn

        adapter = DjangoDatabaseHealthAdapter()
        info = adapter.check_connection("default")

        assert info.alias == "default"
        assert info.vendor == "postgresql"
        assert info.is_usable is False

    @patch("django.db.connections", autospec=False)
    def test_check_connection_does_not_use_is_usable(self, mock_connections):
        """Regression: adapter must not rely on Django's is_usable() (which only
        validates the held connection and returns False on stale-closed state)."""
        from baldur.adapters.database.django_health import (
            DjangoDatabaseHealthAdapter,
        )

        mock_conn = MagicMock()
        mock_conn.vendor = "postgresql"
        # is_usable would lie: returns False for a stale closed conn even when DB is up.
        mock_conn.is_usable.return_value = False
        mock_conn.cursor.return_value.__enter__.return_value = MagicMock()
        mock_connections.__getitem__.return_value = mock_conn

        adapter = DjangoDatabaseHealthAdapter()
        info = adapter.check_connection("default")

        assert info.is_usable is True  # active probe wins over stale is_usable
        mock_conn.is_usable.assert_not_called()

    @patch("django.db.connections", autospec=False)
    def test_list_aliases_returns_django_aliases(self, mock_connections):
        """list_aliases delegates iteration to Django connections."""
        from baldur.adapters.database.django_health import (
            DjangoDatabaseHealthAdapter,
        )

        mock_connections.__iter__ = MagicMock(return_value=iter(["default", "replica"]))

        adapter = DjangoDatabaseHealthAdapter()
        aliases = adapter.list_aliases()

        assert aliases == ["default", "replica"]

    @patch("django.db.connections", autospec=False)
    def test_close_all_closes_each_connection(self, mock_connections):
        """close_all iterates all connections and calls close()."""
        from baldur.adapters.database.django_health import (
            DjangoDatabaseHealthAdapter,
        )

        mock_conn1 = MagicMock()
        mock_conn2 = MagicMock()
        mock_connections.all.return_value = [mock_conn1, mock_conn2]

        adapter = DjangoDatabaseHealthAdapter()
        adapter.close_all()

        mock_conn1.close.assert_called_once()
        mock_conn2.close.assert_called_once()


class TestNoopDatabaseHealthAdapterCheckConnectionAliasBehavior:
    """Noop adapter correctly passes through alias parameter."""

    def test_custom_alias_returned_in_info(self):
        """check_connection passes custom alias to returned info."""
        adapter = NoopDatabaseHealthAdapter()
        info = adapter.check_connection("replica")
        assert info.alias == "replica"
