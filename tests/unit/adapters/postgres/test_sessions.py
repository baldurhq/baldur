"""
Unit tests for :mod:`baldur.adapters.postgres.sessions` (515 D4).

Source: ``src/baldur/adapters/postgres/sessions.py``

The three factory functions produce the callable pair that
:class:`baldur.adapters.postgres.admin.PgAdmin` injects:

- ``django_session_factory(alias)``     → context-managed cursor via Django.
- ``django_connection_factory(alias)``  → thread-local Django connection.
- ``dbapi_session_factory(get_connection)`` → opens conn, yields cursor,
  closes both on exit.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (Django ``connections[alias]`` indexing).
- §8.4 Side effects (cursor close + conn close on exit, even when the
  body raises).
- §8.9 Lifecycle/cleanup — close failures swallowed (mirrors prod where
  pool ``.close()`` may be flaky).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from baldur.adapters.postgres.sessions import (
    dbapi_session_factory,
    django_connection_factory,
    django_session_factory,
)


@pytest.fixture
def fake_django_connections(monkeypatch):
    """Install a fake ``django.db.connections`` mapping into ``sys.modules``.

    Yields the underlying mapping so tests can introspect what alias was
    accessed and how the proxy was used.
    """
    fake_db = types.ModuleType("django.db")
    fake_connections: dict[str, MagicMock] = {}

    class _ConnectionsProxy:
        def __getitem__(self, alias):
            # Lazily mint a MagicMock for the alias so each lookup records.
            if alias not in fake_connections:
                fake_connections[alias] = MagicMock(name=f"connection[{alias}]")
            return fake_connections[alias]

    fake_db.connections = _ConnectionsProxy()
    # The Django package shell may or may not already be installed; only
    # the ``django.db`` submodule is needed for the lazy import inside the
    # factories.
    saved_django = sys.modules.get("django")
    saved_django_db = sys.modules.get("django.db")
    if saved_django is None:
        sys.modules["django"] = types.ModuleType("django")
    sys.modules["django.db"] = fake_db
    try:
        yield fake_connections
    finally:
        if saved_django is None:
            sys.modules.pop("django", None)
        else:
            sys.modules["django"] = saved_django
        if saved_django_db is None:
            sys.modules.pop("django.db", None)
        else:
            sys.modules["django.db"] = saved_django_db


class TestDjangoSessionFactoryBehavior:
    """``django_session_factory`` lazy-imports django and proxies ``connections[alias]``."""

    def test_returns_callable_without_importing_django(self, monkeypatch):
        """Factory construction must not import django (lazy)."""
        # Sentinel that proves django.db was NOT imported during factory
        # construction. We unset both keys, then call the factory builder
        # and assert they're still absent until we actually invoke the
        # returned callable.
        monkeypatch.delitem(sys.modules, "django.db", raising=False)

        callable_obj = django_session_factory("default")
        assert callable(callable_obj)
        assert "django.db" not in sys.modules

    def test_invocation_indexes_connections_with_default_alias(
        self, fake_django_connections
    ):
        """Default alias ``"default"`` indexes ``connections["default"].cursor()``."""
        get_session = django_session_factory()  # default alias

        get_session()  # invoke the callable

        assert "default" in fake_django_connections
        fake_django_connections["default"].cursor.assert_called_once()

    @pytest.mark.parametrize("alias", ["default", "replica", "shard_2"])
    def test_invocation_honors_alias_argument(self, fake_django_connections, alias):
        """The alias passed at build time is the one indexed at invoke time."""
        get_session = django_session_factory(alias)

        get_session()

        assert alias in fake_django_connections
        fake_django_connections[alias].cursor.assert_called_once()

    def test_alias_argument_isolated_across_factories(self, fake_django_connections):
        """Two factories for distinct aliases each invoke their own alias."""
        get_default = django_session_factory("default")
        get_replica = django_session_factory("replica")

        get_default()
        get_replica()

        assert {"default", "replica"} <= set(fake_django_connections)


class TestDjangoConnectionFactoryBehavior:
    """``django_connection_factory`` returns the bare ``connections[alias]`` proxy."""

    def test_returns_callable_without_importing_django(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "django.db", raising=False)
        callable_obj = django_connection_factory("default")
        assert callable(callable_obj)
        assert "django.db" not in sys.modules

    def test_invocation_returns_connections_proxy_no_io(self, fake_django_connections):
        """Returned object IS the ``connections[alias]`` proxy — no cursor opened."""
        get_connection = django_connection_factory("default")

        conn = get_connection()

        assert conn is fake_django_connections["default"]
        # No cursor was opened — caller (``PgAdmin.create_cursor``) does that.
        conn.cursor.assert_not_called()

    @pytest.mark.parametrize("alias", ["default", "replica"])
    def test_invocation_honors_alias_argument(self, fake_django_connections, alias):
        get_connection = django_connection_factory(alias)
        get_connection()
        assert alias in fake_django_connections


class TestDbapiSessionFactoryBehavior:
    """``dbapi_session_factory`` opens conn → yields cursor → closes both."""

    def test_yields_cursor_and_closes_both_on_normal_exit(self):
        """Happy path: cursor is yielded, then both cursor + conn are closed."""
        cursor = MagicMock(name="cursor")
        conn = MagicMock(name="conn")
        conn.cursor.return_value = cursor

        get_session = dbapi_session_factory(get_connection=lambda: conn)

        with get_session() as cur:
            assert cur is cursor
            cursor.close.assert_not_called()  # not closed yet
            conn.close.assert_not_called()

        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_close_called_even_when_body_raises(self):
        """Cleanup runs on exception path — pool-fronted callables must return
        the connection regardless."""
        cursor = MagicMock(name="cursor")
        conn = MagicMock(name="conn")
        conn.cursor.return_value = cursor

        get_session = dbapi_session_factory(get_connection=lambda: conn)

        with pytest.raises(RuntimeError, match="body-error"):
            with get_session():
                raise RuntimeError("body-error")

        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_flaky_close_does_not_propagate(self):
        """Pool implementations occasionally raise on close — swallow it."""
        cursor = MagicMock(name="cursor")
        cursor.close.side_effect = RuntimeError("flaky cursor close")
        conn = MagicMock(name="conn")
        conn.close.side_effect = RuntimeError("flaky conn close")
        conn.cursor.return_value = cursor

        get_session = dbapi_session_factory(get_connection=lambda: conn)

        # The context must exit cleanly even though both closes raise.
        with get_session() as cur:
            assert cur is cursor

        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_get_connection_called_once_per_with_block(self):
        """Each ``with get_session():`` opens exactly one fresh connection."""
        get_connection = MagicMock(name="get_connection")
        get_connection.return_value.cursor.return_value = MagicMock(name="cursor")

        get_session = dbapi_session_factory(get_connection=get_connection)

        with get_session():
            pass
        with get_session():
            pass

        assert get_connection.call_count == 2

    def test_get_connection_callable_is_lazy(self):
        """Factory construction must NOT call ``get_connection`` itself."""
        get_connection = MagicMock(name="get_connection")
        dbapi_session_factory(get_connection=get_connection)
        get_connection.assert_not_called()
