"""
Unit tests for the :class:`PoolInfoProvider` implementations (515 D5).

Sources:
- ``src/baldur/adapters/pool/django_info.py`` — Django-routed provider
- ``src/baldur/adapters/pool/sqlalchemy_info.py`` — BYO-engine provider
- ``src/baldur/adapters/pool/noop_info.py`` — empty-dict provider

Each implementation reads pool state from a fundamentally different source
(Django ``connections["default"]`` graph walk vs ``engine.pool``
directly), so the tests parametrize per provider rather than per
backend pair.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.2 Exception/edge cases — Django absent / engine None / non-pool
  attribute → empty dict (graceful degradation per CROSS_SERVICE_STANDARDS).
- §8.5 Dependency interaction — ``_extract_pool_info`` invoked with the
  correct pool object.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from baldur.adapters.pool.django_info import DjangoPoolInfoProvider
from baldur.adapters.pool.noop_info import NoopPoolInfoProvider
from baldur.adapters.pool.sqlalchemy_info import SQLAlchemyPoolInfoProvider
from baldur.interfaces.pool_info import PoolInfoProvider

# =============================================================================
# Helpers — fake SQLAlchemy QueuePool
# =============================================================================


class _FakeQueuePool:
    """Minimal SQLAlchemy-compatible pool used by ``_extract_pool_info``."""

    def __init__(
        self,
        *,
        size: int = 10,
        checkedout: int = 3,
        checkedin: int = 7,
        overflow: int = 0,
        max_overflow: int = 5,
    ) -> None:
        self._size = size
        self._checkedout = checkedout
        self._checkedin = checkedin
        self._overflow = overflow
        self._max_overflow = max_overflow

    def size(self) -> int:
        return self._size

    def checkedout(self) -> int:
        return self._checkedout

    def checkedin(self) -> int:
        return self._checkedin

    def overflow(self) -> int:
        return self._overflow


@pytest.fixture
def fake_queue_pool() -> _FakeQueuePool:
    return _FakeQueuePool()


# =============================================================================
# NoopPoolInfoProvider
# =============================================================================


class TestNoopPoolInfoProviderContract:
    """Noop provider always returns an empty dict."""

    def test_is_pool_info_provider_subclass(self):
        assert isinstance(NoopPoolInfoProvider(), PoolInfoProvider)

    def test_get_pool_info_returns_empty_dict(self):
        assert NoopPoolInfoProvider().get_pool_info() == {}

    def test_repeated_calls_return_empty_dict(self):
        provider = NoopPoolInfoProvider()
        assert provider.get_pool_info() == {}
        assert provider.get_pool_info() == {}


# =============================================================================
# SQLAlchemyPoolInfoProvider
# =============================================================================


class TestSQLAlchemyPoolInfoProviderBehavior:
    """BYO-engine provider reads ``engine.pool`` directly."""

    def test_is_pool_info_provider_subclass(self):
        provider = SQLAlchemyPoolInfoProvider(engine=None)
        assert isinstance(provider, PoolInfoProvider)

    def test_returns_extracted_pool_info(self, fake_queue_pool):
        engine = MagicMock(name="engine")
        engine.pool = fake_queue_pool

        info = SQLAlchemyPoolInfoProvider(engine).get_pool_info()

        assert info["pool_type"] == "_FakeQueuePool"
        assert info["pool_size"] == 10
        assert info["checkedin"] == 7
        assert info["checkedout"] == 3
        assert info["overflow"] == 0
        assert info["max_overflow"] == 5
        assert info["total_capacity"] == 15
        assert info["available"] == 7
        assert info["pool_exhausted"] is False

    def test_pool_exhausted_when_no_checkin_and_checkout_at_capacity(self):
        engine = MagicMock(name="engine")
        engine.pool = _FakeQueuePool(checkedin=0, checkedout=10, size=10)

        info = SQLAlchemyPoolInfoProvider(engine).get_pool_info()

        assert info["pool_exhausted"] is True

    def test_engine_none_returns_empty_dict(self):
        """Defensive: ``engine=None`` is treated as 'no pool'."""
        assert SQLAlchemyPoolInfoProvider(engine=None).get_pool_info() == {}

    def test_engine_with_none_pool_returns_empty_dict(self):
        engine = MagicMock(name="engine")
        engine.pool = None
        assert SQLAlchemyPoolInfoProvider(engine).get_pool_info() == {}

    def test_engine_pool_access_failure_returns_empty_dict(self):
        """Exception on ``engine.pool`` access → empty dict, no propagation."""

        class _ExplodingEngine:
            @property
            def pool(self):
                raise RuntimeError("pool detached")

        assert SQLAlchemyPoolInfoProvider(_ExplodingEngine()).get_pool_info() == {}


# =============================================================================
# DjangoPoolInfoProvider — three discovery branches
# =============================================================================


@pytest.fixture
def fake_django_connections(monkeypatch):
    """Install a fake ``django.db.connections`` mapping into ``sys.modules``.

    Yields the bare proxy so the test can attach ``.connection`` /
    ``.pool`` shapes on it via the returned ``configure(...)`` helper.
    """
    fake_db = types.ModuleType("django.db")
    fake_conn = MagicMock(name="django_connection[default]")
    fake_conn.ensure_connection = MagicMock()
    # Defaults: no ``.connection._pool``, no ``.pool``.
    fake_conn.connection = None
    fake_conn.pool = None

    class _ConnectionsProxy:
        def __getitem__(self, alias):
            return fake_conn

    fake_db.connections = _ConnectionsProxy()
    saved_django = sys.modules.get("django")
    saved_db = sys.modules.get("django.db")
    if saved_django is None:
        sys.modules["django"] = types.ModuleType("django")
    sys.modules["django.db"] = fake_db
    try:
        yield fake_conn
    finally:
        if saved_django is None:
            sys.modules.pop("django", None)
        else:
            sys.modules["django"] = saved_django
        if saved_db is None:
            sys.modules.pop("django.db", None)
        else:
            sys.modules["django.db"] = saved_db


class TestDjangoPoolInfoProviderBehavior:
    """Three-tier discovery: ``conn.connection._pool`` → ``dj_db_conn_pool``
    → ``conn.pool.pool``. First match wins, otherwise empty dict."""

    def test_is_pool_info_provider_subclass(self):
        assert isinstance(DjangoPoolInfoProvider(), PoolInfoProvider)

    def test_django_absent_returns_empty_dict(self, monkeypatch):
        """No django installed → empty dict, no exception."""
        monkeypatch.setitem(sys.modules, "django.db", None)
        assert DjangoPoolInfoProvider().get_pool_info() == {}

    def test_first_branch_conn_dot_connection_dot__pool(
        self, fake_django_connections, fake_queue_pool
    ):
        """``conn.connection._pool`` is the SQLAlchemy pool — first branch wins."""
        raw_conn = MagicMock(name="raw_conn")
        raw_conn._pool = fake_queue_pool
        fake_django_connections.connection = raw_conn

        info = DjangoPoolInfoProvider("default").get_pool_info()

        assert info["pool_size"] == 10
        assert info["pool_type"] == "_FakeQueuePool"
        fake_django_connections.ensure_connection.assert_called_once()

    def test_second_branch_dj_db_conn_pool_container(
        self, monkeypatch, fake_django_connections, fake_queue_pool
    ):
        """When ``raw_conn._pool`` is missing, fall through to
        ``dj_db_conn_pool.pool_container``."""

        # Make conn.connection truthy but without _pool attribute.
        class _Connection:
            pass  # no _pool

        fake_django_connections.connection = _Connection()

        # Install a fake dj_db_conn_pool module.
        pool_container = MagicMock(name="pool_container")
        pool_container.has.return_value = True
        pool_container.get.return_value = fake_queue_pool

        fake_module = types.ModuleType("dj_db_conn_pool.core.mixins.core")
        fake_module.pool_container = pool_container

        # Set the full module path so the lazy import resolves.
        monkeypatch.setitem(
            sys.modules, "dj_db_conn_pool", types.ModuleType("dj_db_conn_pool")
        )
        monkeypatch.setitem(
            sys.modules,
            "dj_db_conn_pool.core",
            types.ModuleType("dj_db_conn_pool.core"),
        )
        monkeypatch.setitem(
            sys.modules,
            "dj_db_conn_pool.core.mixins",
            types.ModuleType("dj_db_conn_pool.core.mixins"),
        )
        monkeypatch.setitem(
            sys.modules, "dj_db_conn_pool.core.mixins.core", fake_module
        )

        info = DjangoPoolInfoProvider("default").get_pool_info()

        assert info["pool_size"] == 10
        pool_container.has.assert_called_once_with("default")
        pool_container.get.assert_called_once_with("default")

    def test_third_branch_conn_dot_pool_dot_pool(
        self, fake_django_connections, fake_queue_pool
    ):
        """Final branch: ``conn.pool.pool`` (Django connection-pool fallback)."""

        class _Pool:
            pass

        pool_outer = _Pool()
        pool_outer.pool = fake_queue_pool

        # First two branches must be absent.
        fake_django_connections.connection = None  # no ``.connection._pool``
        fake_django_connections.pool = pool_outer  # ``.pool.pool`` IS set

        info = DjangoPoolInfoProvider("default").get_pool_info()

        assert info["pool_size"] == 10
        assert info["pool_type"] == "_FakeQueuePool"

    def test_no_branch_matches_returns_empty_dict(self, fake_django_connections):
        """All three branches missing → empty dict (no pool reachable)."""
        # Defaults: ``.connection = None``, ``.pool = None``.
        assert DjangoPoolInfoProvider("default").get_pool_info() == {}

    # 525 D4: xdist mock_leak — caplog/DEBUG capture races with sibling
    # tests under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    def test_unexpected_exception_returns_empty_dict(
        self, fake_django_connections, caplog
    ):
        """Defensive: exception during discovery → empty dict + DEBUG log."""
        fake_django_connections.ensure_connection.side_effect = RuntimeError(
            "transient failure"
        )

        with caplog.at_level("DEBUG"):
            info = DjangoPoolInfoProvider("default").get_pool_info()

        assert info == {}
        assert any(
            "pool_info.django_retrieve_failed" in r.message for r in caplog.records
        )

    @pytest.mark.parametrize("alias", ["default", "replica", "shard_0"])
    def test_alias_argument_used_for_connections_lookup(
        self, fake_django_connections, fake_queue_pool, alias
    ):
        """The alias passed to the constructor is the one indexed."""
        raw_conn = MagicMock()
        raw_conn._pool = fake_queue_pool
        fake_django_connections.connection = raw_conn

        info = DjangoPoolInfoProvider(alias).get_pool_info()

        assert info["pool_size"] == 10
