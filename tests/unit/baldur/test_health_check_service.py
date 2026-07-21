"""
HealthCheckService unit tests.

Health Check business logic service tests.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from concurrent.futures import Future, wait
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)
from baldur.services.health_check import (
    PROBE_THREAD_NAME,
    DatabaseCheck,
    HealthCheckService,
    PoolHealthSummary,
    PoolInfo,
    ReadinessStatus,
    SystemHealthSummary,
    _spawn_probe,
    get_health_check_service,
)
from baldur.settings.health_check import reset_health_check_settings

_REGISTRY_PATH = "baldur.factory.registry.ProviderRegistry"

# Configured round budget for the bounded-probe tests. Small enough that a
# bounded round is unambiguously faster than a hung one, large enough that a
# healthy round completes inside it under load.
_TEST_PROBE_BUDGET_SECONDS = 0.2

# Wall-clock ceiling asserted against that budget, and the shared deadline for
# draining probe threads. Deliberately generous (10x the budget): the claim
# under test is that the round is bounded *at all* — an unbounded round blocks
# for _HANG_RELEASE_TIMEOUT_SECONDS — so a tight ceiling would buy nothing but
# CI flakiness on a Windows/xdist worker.
_ROUND_CEILING_SECONDS = 2.0

# Backstop for a stub probe that is never released. The provider fixture always
# releases in teardown; this only bounds the damage if a test forgets.
_HANG_RELEASE_TIMEOUT_SECONDS = 10.0


def _make_mock_db_provider(vendor="postgresql", is_usable=True, aliases=None):
    """Create a mock DatabaseHealthProvider."""
    provider = MagicMock()
    provider.check_connection.return_value = DatabaseConnectionInfo(
        alias="default",
        vendor=vendor,
        is_usable=is_usable,
    )
    provider.list_aliases.return_value = aliases or ["default"]
    return provider


class _StubDatabaseHealthProvider(DatabaseHealthProvider):
    """Hand-written provider stub with a thread-safe per-alias call counter.

    A MagicMock cannot serve here: the probe round calls ``check_connection``
    from N threads at once and ``Mock.call_count`` is incremented with a plain
    ``+=`` (read-modify-write, not atomic across bytecodes), so "exactly one
    call per alias" assertions would undercount intermittently. A real class
    also keeps the spec-less-mock budget flat (UNIT_TEST_GUIDELINES 6.2).

    Behavior is snapshotted under the lock on entry, so a probe already in
    flight keeps the verdict it was submitted with even after the test flips
    the stub — which is exactly what the stale-leftover rule needs.
    """

    def __init__(
        self,
        aliases=("default",),
        *,
        vendor="postgresql",
        is_usable=True,
        raises=None,
        hang_aliases=(),
    ):
        self.aliases = list(aliases)
        self.vendor = vendor
        self.is_usable = is_usable
        self.raises = raises
        self.hang_aliases = set(hang_aliases)
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}

    def check_connection(self, alias: str = "default") -> DatabaseConnectionInfo:
        with self._lock:
            self._calls[alias] = self._calls.get(alias, 0) + 1
            raises = self.raises
            is_usable = self.is_usable
            hangs = alias in self.hang_aliases

        if hangs:
            self.release.wait(timeout=_HANG_RELEASE_TIMEOUT_SECONDS)
        if raises is not None:
            raise raises
        return DatabaseConnectionInfo(
            alias=alias, vendor=self.vendor, is_usable=is_usable
        )

    def list_aliases(self) -> list[str]:
        return list(self.aliases)

    def close_all(self) -> None:
        return None

    def call_count(self, alias: str = "default") -> int:
        with self._lock:
            return self._calls.get(alias, 0)


class _CallCounter:
    """Thread-safe zero-argument call counter.

    Used instead of a Mock wherever the counted call happens on probe threads,
    for the same non-atomic ``call_count`` reason as the provider stub.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0

    def __call__(self) -> None:
        with self._lock:
            self.count += 1


@contextmanager
def _registered_provider(provider):
    """Serve ``provider`` from the registry's ``database_health`` slot.

    With-form rather than the decorator form used by the older tests in this
    module: a decorator patch would add spec-less debt to the G67 ratchet in
    both repos, and this seam only needs the slot's ``get()`` redirected.
    """
    with patch(f"{_REGISTRY_PATH}.database_health") as slot:
        slot.get.return_value = provider
        yield slot


def _live_probe_threads():
    """Every probe worker thread currently alive."""
    return [t for t in threading.enumerate() if t.name == PROBE_THREAD_NAME]


def _drain_probe_threads(timeout=_ROUND_CEILING_SECONDS):
    """Join every live probe thread under ONE shared deadline.

    A leaked probe thread that raises later can take an xdist worker down under
    ``--max-worker-restart=0``, so released probes are drained before the next
    test starts. The deadline is shared rather than per-thread: sequential
    per-thread joins would compound into N x timeout.
    """
    deadline = time.monotonic() + timeout
    for thread in _live_probe_threads():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)


def _structlog_events(caplog, event_name):
    """Return the structlog event dicts recorded under ``event_name``.

    The canonical test structlog config routes through stdlib leaving the event
    dict as ``record.msg``, so ``record.exc_info`` is *always* None and the
    traceback flag travels inside the dict as ``exc_info: True``. Asserting on
    ``record.exc_info`` would pass vacuously against both log levels and prove
    nothing — read the dict.
    """
    return [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("event") == event_name
    ]


@pytest.fixture
def provider_factory():
    """Build stub providers, guaranteeing every blocked probe is released.

    Teardown release is mandatory, not hygiene: ``daemon=True`` removes the
    pytest-won't-exit half of the hazard, but a leaked probe thread that later
    raises can still crash an xdist worker.
    """
    created: list[_StubDatabaseHealthProvider] = []

    def _make(**kwargs) -> _StubDatabaseHealthProvider:
        provider = _StubDatabaseHealthProvider(**kwargs)
        created.append(provider)
        return provider

    yield _make

    for provider in created:
        provider.release.set()
    _drain_probe_threads()


@pytest.fixture
def readiness_settings(monkeypatch):
    """Configure readiness settings by env var, resetting on both sides.

    Defaults to a short round budget with caching OFF, so a test that cares
    about the cache opts into a TTL explicitly rather than inheriting one.
    """

    def _configure(**overrides):
        for name, value in overrides.items():
            monkeypatch.setenv(f"BALDUR_HEALTH_CHECK_{name.upper()}", str(value))
        reset_health_check_settings()

    reset_health_check_settings()
    _configure(
        readiness_probe_timeout_seconds=_TEST_PROBE_BUDGET_SECONDS,
        readiness_cache_ttl_seconds=0.0,
    )
    yield _configure
    reset_health_check_settings()


class TestHealthCheckService:
    """HealthCheckService unit tests."""

    def setup_method(self):
        """Create service instance before each test."""
        self.service = HealthCheckService()

    # =========================================================================
    # check_database Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_database_success(self, mock_db_health):
        """Healthy DB: is_usable=True flows through to is_connected=True."""
        mock_db_health.get.return_value = _make_mock_db_provider(is_usable=True)

        result = self.service.check_database("default")

        assert isinstance(result, DatabaseCheck)
        assert result.alias == "default"
        assert result.vendor == "postgresql"
        assert result.is_connected is True
        assert result.is_usable is True
        assert result.error is None
        assert result.latency_ms is not None
        assert result.latency_ms >= 0

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_database_unusable_returns_disconnected(self, mock_db_health):
        """473 D2: provider returns is_usable=False without raising →
        is_connected=False (single source of truth)."""
        mock_db_health.get.return_value = _make_mock_db_provider(is_usable=False)

        result = self.service.check_database("default")

        assert isinstance(result, DatabaseCheck)
        assert result.is_connected is False
        assert result.is_usable is False
        assert result.error is None  # no exception raised

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_database_connection_failure(self, mock_db_health):
        """check_connection raising an exception → is_connected=False + error."""
        provider = _make_mock_db_provider()
        provider.check_connection.side_effect = Exception("Connection refused")
        mock_db_health.get.return_value = provider

        result = self.service.check_database("default")

        assert isinstance(result, DatabaseCheck)
        assert result.alias == "default"
        assert result.is_connected is False
        assert result.is_usable is False
        assert result.error == "Connection refused"

    # =========================================================================
    # check_all_databases Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_all_databases(self, mock_db_health):
        """Iterates list_aliases and returns one DatabaseCheck per alias."""
        mock_db_health.get.return_value = _make_mock_db_provider(
            aliases=["default", "replica"],
        )

        results = self.service.check_all_databases()

        assert len(results) == 2
        assert all(isinstance(r, DatabaseCheck) for r in results)

    # =========================================================================
    # check_connection_pool Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_connection_pool_healthy(self, mock_db_health):
        """Healthy pool."""
        mock_db_health.get.return_value = _make_mock_db_provider()

        result = self.service.check_connection_pool("default")

        assert isinstance(result, PoolInfo)
        assert result.alias == "default"
        assert result.vendor == "postgresql"
        assert result.is_usable is True
        assert result.status == "healthy"
        assert result.error is None

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_connection_pool_degraded(self, mock_db_health):
        """Pool reporting is_usable=False → status='degraded'."""
        mock_db_health.get.return_value = _make_mock_db_provider(is_usable=False)

        result = self.service.check_connection_pool("default")

        assert isinstance(result, PoolInfo)
        assert result.is_usable is False
        assert result.status == "degraded"

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_connection_pool_error(self, mock_db_health):
        """Pool provider raising → status='error'."""
        mock_db_health.get.side_effect = Exception("Pool error")

        result = self.service.check_connection_pool("default")

        assert isinstance(result, PoolInfo)
        assert result.is_usable is False
        assert result.status == "error"
        assert result.error == "Pool error"

    # =========================================================================
    # get_pool_health Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_pool_health_healthy(self, mock_db_health):
        """Healthy pool summary."""
        mock_db_health.get.return_value = _make_mock_db_provider()

        result = self.service.get_pool_health()

        assert isinstance(result, PoolHealthSummary)
        assert result.status == "healthy"
        assert result.error is None

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_pool_health_error(self, mock_db_health):
        """Pool error summary."""
        mock_db_health.get.side_effect = Exception("Pool error")

        result = self.service.get_pool_health()

        assert isinstance(result, PoolHealthSummary)
        assert result.status == "error"
        assert result.error == "Pool error"

    # =========================================================================
    # get_readiness Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_readiness_ready(self, mock_db_health):
        """All DBs healthy → ready."""
        mock_db_health.get.return_value = _make_mock_db_provider(aliases=["default"])

        result = self.service.get_readiness()

        assert isinstance(result, ReadinessStatus)
        assert result.status == "ready"
        assert result.is_ready is True
        assert result.checks["database_default"] == "ready"

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_readiness_not_ready(self, mock_db_health):
        """check_connection raises → not_ready."""
        provider = _make_mock_db_provider(aliases=["default"])
        provider.check_connection.side_effect = Exception("Connection refused")
        mock_db_health.get.return_value = provider

        result = self.service.get_readiness()

        assert isinstance(result, ReadinessStatus)
        assert result.status == "not_ready"
        assert result.is_ready is False
        assert result.checks["database_default"] == "not_ready"

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_readiness_not_ready_when_db_unusable(self, mock_db_health):
        """473 D2 collateral: provider returns is_usable=False without raising
        → readiness reports not_ready (no false-positive ready emission)."""
        mock_db_health.get.return_value = _make_mock_db_provider(
            is_usable=False, aliases=["default"]
        )

        result = self.service.get_readiness()

        assert result.status == "not_ready"
        assert result.is_ready is False
        assert result.checks["database_default"] == "not_ready"

    # =========================================================================
    # get_overall_health Tests
    # =========================================================================

    @patch("baldur.utils.time.utc_now")
    @patch.object(HealthCheckService, "_get_circuit_breaker_count")
    @patch.object(HealthCheckService, "check_database")
    def test_get_overall_health_healthy(self, mock_check_db, mock_get_count, mock_now):
        """Healthy DB → status='healthy'."""
        mock_check_db.return_value = DatabaseCheck(
            alias="default",
            vendor="postgresql",
            is_connected=True,
            is_usable=True,
        )
        mock_get_count.return_value = 5
        mock_now.return_value.isoformat.return_value = "2025-12-19T00:00:00Z"

        result = self.service.get_overall_health()

        assert isinstance(result, SystemHealthSummary)
        assert result.status == "healthy"
        assert result.checks["database"] == "healthy"
        assert result.checks["circuit_breaker"] == "enabled"
        assert result.services_count == 5

    @patch("baldur.utils.time.utc_now")
    @patch.object(HealthCheckService, "check_database")
    def test_get_overall_health_unhealthy_when_db_unusable(
        self, mock_check_db, mock_now
    ):
        """473 D7 axis 1 (b): is_usable=False → status='unhealthy'."""
        mock_check_db.return_value = DatabaseCheck(
            alias="default",
            is_connected=False,
            is_usable=False,
            error="Connection refused",
        )
        mock_now.return_value.isoformat.return_value = "2025-12-19T00:00:00Z"

        with patch("baldur.services.health_check.set_health_status") as mock_set_status:
            result = self.service.get_overall_health()

        assert isinstance(result, SystemHealthSummary)
        assert result.status == "unhealthy"
        assert result.checks["database"] == "unhealthy"
        assert result.services_count == 0
        # 473 D7 mock-call assertion: set_health_status called with "unhealthy"
        # so the metric layer (_STATUS_MAP) translates to numeric 2.
        mock_set_status.assert_called_with("overall", "unhealthy")

    # =========================================================================
    # Liveness/Readiness Helper Tests
    # =========================================================================

    def test_is_alive_always_true(self):
        """is_alive is always True."""
        assert self.service.is_alive() is True

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_is_ready_true(self, mock_db_health):
        """Healthy DB → is_ready True."""
        mock_db_health.get.return_value = _make_mock_db_provider(aliases=["default"])

        assert self.service.is_ready() is True

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_is_ready_false(self, mock_db_health):
        """check_connection raises → is_ready False."""
        provider = _make_mock_db_provider(aliases=["default"])
        provider.check_connection.side_effect = Exception("Connection refused")
        mock_db_health.get.return_value = provider

        assert self.service.is_ready() is False


class TestGetHealthCheckService:
    """get_health_check_service factory tests."""

    def test_returns_singleton(self):
        """Singleton instance returned across calls."""
        import baldur.services.health_check as module

        module._health_check_service = None

        service1 = get_health_check_service()
        service2 = get_health_check_service()

        assert service1 is service2
        assert isinstance(service1, HealthCheckService)


class TestDataClasses:
    """Data class tests."""

    def test_database_check_to_dict(self):
        """DatabaseCheck.to_dict()."""
        check = DatabaseCheck(
            alias="default",
            vendor="postgresql",
            is_connected=True,
            is_usable=True,
            latency_ms=1.5,
        )

        result = check.to_dict()

        assert result["alias"] == "default"
        assert result["vendor"] == "postgresql"
        assert result["is_connected"] is True
        assert result["latency_ms"] == 1.5

    def test_health_status_to_dict(self):
        """SystemHealthSummary.to_dict()."""
        status = SystemHealthSummary(
            status="healthy",
            checks={"database": "healthy"},
            services_count=5,
            timestamp="2025-12-19T00:00:00Z",
        )

        result = status.to_dict()

        assert result["status"] == "healthy"
        assert result["checks"]["database"] == "healthy"
        assert result["services_count"] == 5

    def test_readiness_status_to_dict(self):
        """ReadinessStatus.to_dict()."""
        status = ReadinessStatus(
            status="ready",
            checks={"database_default": "ready"},
            is_ready=True,
        )

        result = status.to_dict()

        assert result["status"] == "ready"
        assert result["is_ready"] is True

    def test_pool_health_summary_to_dict(self):
        """PoolHealthSummary.to_dict()."""
        status = PoolHealthSummary(
            status="healthy",
            pool_info={"alias": "default"},
        )

        result = status.to_dict()

        assert result["status"] == "healthy"
        assert result["pool_info"]["alias"] == "default"


# =============================================================================
# Bounded readiness probe round
# =============================================================================


class TestCheckAllDatabasesBehavior:
    """check_all_databases() probes every alias in parallel under one deadline."""

    def setup_method(self):
        self.service = HealthCheckService()

    def test_check_all_databases_hung_alias_returns_within_round_budget(
        self, provider_factory, readiness_settings
    ):
        """A database that accepts but never answers is bounded by the budget.

        Without the bound the round blocks for the driver default; the stub
        stands in for that with a much longer release backstop, so an unbounded
        round overshoots this ceiling by an order of magnitude.
        """
        # Given: an alias whose check_connection blocks until released
        provider = provider_factory(aliases=["default"], hang_aliases=["default"])

        # When: one probe round runs
        with _registered_provider(provider):
            started_at = time.monotonic()
            results = self.service.check_all_databases()
            elapsed = time.monotonic() - started_at

        # Then: it answered, bounded, naming the alias that stalled
        assert elapsed < _ROUND_CEILING_SECONDS
        assert [r.alias for r in results] == ["default"]
        assert results[0].timed_out is True
        assert results[0].is_connected is False

    def test_check_all_databases_multi_alias_probes_every_alias_exactly_once(
        self, provider_factory, readiness_settings
    ):
        """Three healthy aliases all report connected, one call each.

        Pins the per-spawn ``copy_context()`` requirement: a Context cannot be
        entered concurrently, so a single copy shared across the round would
        leave every alias after the first unprobed.
        """
        aliases = ["default", "replica", "analytics"]
        provider = provider_factory(aliases=aliases)

        with _registered_provider(provider):
            results = self.service.check_all_databases()

        assert [r.alias for r in results] == aliases
        assert all(r.is_connected for r in results)
        assert all(r.timed_out is False for r in results)
        assert [provider.call_count(alias) for alias in aliases] == [1, 1, 1]

    def test_check_all_databases_hung_alias_does_not_stall_its_healthy_sibling(
        self, provider_factory, readiness_settings
    ):
        """Parallel round: one stalled alias does not mask the others' verdicts."""
        provider = provider_factory(
            aliases=["default", "replica"], hang_aliases=["replica"]
        )

        with _registered_provider(provider):
            results = self.service.check_all_databases()

        by_alias = {r.alias: r for r in results}
        assert by_alias["default"].is_connected is True
        assert by_alias["default"].timed_out is False
        assert by_alias["replica"].timed_out is True

    def test_check_all_databases_refused_alias_is_not_classified_timed_out(
        self, provider_factory, readiness_settings
    ):
        """Negative assertion: a fast-raising alias reports an error, never a
        timeout — the two failure classes stay distinguishable to an operator."""
        provider = provider_factory(
            aliases=["default"], raises=RuntimeError("connection refused")
        )

        with _registered_provider(provider):
            results = self.service.check_all_databases()

        assert results[0].timed_out is False
        assert results[0].is_connected is False
        assert results[0].error == "connection refused"

    def test_check_all_databases_without_aliases_spawns_no_probe_thread(
        self, provider_factory, readiness_settings
    ):
        """Noop default (no databases configured): empty round, no thread, no raise."""
        provider = provider_factory(aliases=[])
        threads_before = len(_live_probe_threads())

        with _registered_provider(provider):
            results = self.service.check_all_databases()

        assert results == []
        assert len(_live_probe_threads()) == threads_before


class TestProbeSpawnBehavior:
    """_spawn_probe() thread properties."""

    def setup_method(self):
        self.service = HealthCheckService()

    def test_probe_threads_are_named_daemon_threads(
        self, provider_factory, readiness_settings
    ):
        """A blocked probe is a named daemon thread.

        Daemon so a leaked probe cannot block interpreter exit — the property a
        ThreadPoolExecutor cannot provide, since its atexit hook joins workers
        regardless of the flag. Named so the leak is identifiable in a dump.
        """
        provider = provider_factory(aliases=["default"], hang_aliases=["default"])

        self.service._submit_probe_round(provider, ["default"])
        probes = _live_probe_threads()

        assert probes, "expected a live probe thread while the probe is blocked"
        assert all(thread.daemon is True for thread in probes)

    def test_spawn_probe_propagates_one_context_copy_per_spawn(self):
        """The caller's context reaches each probe, in a copy the probe owns.

        Both halves matter and they pull in opposite directions. Propagation is
        required because adapters log from inside the probe body — a raw thread
        starts with an *empty* context, so without the copy the probe would see
        the ContextVar default. Per-spawn is required because a Context cannot
        be entered concurrently: the barrier holds both probes inside at once,
        so one shared copy would raise in the second and break the barrier.
        """
        # Given: a contextvar carrying caller state a probe should inherit
        var = contextvars.ContextVar("baldur_test_probe_var", default="unset")
        var.set("request-42")
        both_inside = threading.Barrier(3)
        observed: dict[str, str] = {}
        observed_lock = threading.Lock()

        def _probe(alias):
            inherited = var.get()
            var.set(alias)
            both_inside.wait(timeout=_ROUND_CEILING_SECONDS)
            with observed_lock:
                observed[alias] = f"{inherited}->{var.get()}"
            return DatabaseCheck(alias=alias)

        # When: two probes are spawned and held inside their contexts together
        futures = [_spawn_probe(_probe, alias) for alias in ("default", "replica")]
        both_inside.wait(timeout=_ROUND_CEILING_SECONDS)
        wait(futures, timeout=_ROUND_CEILING_SECONDS)

        # Then: each inherited the caller's value and kept its own write local
        assert observed == {
            "default": "request-42->default",
            "replica": "request-42->replica",
        }
        assert var.get() == "request-42"


class TestOutstandingProbeGuardBehavior:
    """The outstanding-probe guard bounds a sustained hang to one thread per alias."""

    def setup_method(self):
        self.service = HealthCheckService()

    def test_second_round_while_a_probe_is_outstanding_spawns_no_new_probe(
        self, provider_factory, readiness_settings
    ):
        """A sustained hang leaks one thread and one connection, not one per round."""
        provider = provider_factory(aliases=["default"], hang_aliases=["default"])

        with _registered_provider(provider):
            first = self.service.check_all_databases()
            started_at = time.monotonic()
            second = self.service.check_all_databases()
            second_elapsed = time.monotonic() - started_at

        assert provider.call_count("default") == 1
        assert first[0].timed_out is True
        assert second[0].timed_out is True
        # The suppressed round waits on an empty future set, so it cannot spend
        # the budget the first round did.
        assert second_elapsed < _TEST_PROBE_BUDGET_SECONDS

    def test_alias_is_reprobed_once_its_outstanding_probe_completes(
        self, provider_factory, readiness_settings
    ):
        """Suppression is transient: the alias is probed again after it answers."""
        provider = provider_factory(aliases=["default"], hang_aliases=["default"])

        with _registered_provider(provider):
            self.service.check_all_databases()
            outstanding = self.service._outstanding_probes["default"]

            provider.release.set()
            wait([outstanding], timeout=_ROUND_CEILING_SECONDS)

            results = self.service.check_all_databases()

        assert provider.call_count("default") == 2
        assert results[0].is_connected is True
        assert results[0].timed_out is False

    def test_outstanding_entry_is_dropped_when_an_alias_leaves_the_config(
        self, provider_factory, readiness_settings
    ):
        """A de-configured alias does not linger in the outstanding map forever."""
        provider = provider_factory(
            aliases=["default", "replica"], hang_aliases=["replica"]
        )

        with _registered_provider(provider):
            self.service.check_all_databases()
            assert "replica" in self.service._outstanding_probes

            provider.aliases = ["default"]
            self.service.check_all_databases()

        assert "replica" not in self.service._outstanding_probes


class TestProbeClassificationBehavior:
    """_classify_probe() maps each round outcome onto a DatabaseCheck."""

    def setup_method(self):
        self.service = HealthCheckService()

    def test_suppressed_alias_without_a_future_is_classified_timed_out(self):
        """An alias suppressed by the guard reads exactly like one that stalled."""
        check = self.service._classify_probe("default", None, 0.01)

        assert check.alias == "default"
        assert check.timed_out is True
        assert check.is_connected is False

    def test_probe_still_pending_at_the_deadline_is_classified_timed_out(self):
        pending: Future[DatabaseCheck] = Future()

        check = self.service._classify_probe("default", pending, 0.2)

        assert check.timed_out is True
        assert check.is_connected is False

    def test_exceptional_future_is_mapped_to_an_error_and_never_re_raised(self):
        """The submitted callable is wider than the probe body, so a completed
        future may still carry an exception — map it, do not raise it."""
        failed: Future[DatabaseCheck] = Future()
        failed.set_exception(RuntimeError("context run failed"))

        check = self.service._classify_probe("default", failed, 0.01)

        assert check.timed_out is False
        assert check.is_connected is False
        assert check.error == "context run failed"

    def test_completed_probe_clears_its_outstanding_entry(self):
        """A classified probe stops suppressing its alias."""
        done: Future[DatabaseCheck] = Future()
        done.set_result(
            DatabaseCheck(alias="default", is_connected=True, is_usable=True)
        )
        self.service._outstanding_probes["default"] = done

        check = self.service._classify_probe("default", done, 0.01)

        assert check.is_connected is True
        assert "default" not in self.service._outstanding_probes

    def test_stale_leftover_probe_verdict_is_discarded_not_published(
        self, provider_factory, readiness_settings
    ):
        """A leftover future's verdict describes the moment it was submitted.

        Publishing a stale "connected" would un-depool a pod whose database is
        still down, so the round discards it unread and probes afresh.
        """
        # Given: a round that timed out, leaving a probe outstanding
        provider = provider_factory(aliases=["default"], hang_aliases=["default"])

        with _registered_provider(provider):
            first = self.service.check_all_databases()
            stale = self.service._outstanding_probes["default"]

            # When: that probe completes healthy, but the database has since failed
            provider.hang_aliases = set()
            provider.raises = RuntimeError("connection refused")
            provider.release.set()
            wait([stale], timeout=_ROUND_CEILING_SECONDS)

            second = self.service.check_all_databases()

        # Then: the stale healthy verdict existed, and was not the one reported
        assert first[0].timed_out is True
        assert stale.result().is_connected is True
        assert second[0].is_connected is False
        assert second[0].error == "connection refused"


# =============================================================================
# Readiness verdict — fail direction, cache, guard
# =============================================================================


class TestReadinessFailDirectionBehavior:
    """The fail direction decides only whether a stall depools the pod."""

    def setup_method(self):
        self.service = HealthCheckService()

    @pytest.mark.parametrize(
        ("direction", "expected_ready"),
        [("not_ready", False), ("ready", True)],
    )
    def test_hung_alias_reports_timed_out_under_both_fail_directions(
        self, provider_factory, readiness_settings, direction, expected_ready
    ):
        """The check value is always "timed_out" — the operator always sees which
        alias stopped answering; only ``is_ready`` follows the knob.

        Asserting the value *is* "timed_out" also carries the negative
        assertion: a hung alias is never reported with the old "not_ready".
        """
        readiness_settings(readiness_timeout_fail_direction=direction)
        provider = provider_factory(aliases=["default"], hang_aliases=["default"])

        with _registered_provider(provider):
            status = self.service.get_readiness()

        assert status.checks["database_default"] == "timed_out"
        assert status.is_ready is expected_ready
        assert status.status == ("ready" if expected_ready else "not_ready")

    @pytest.mark.parametrize("direction", ["not_ready", "ready"])
    def test_refused_alias_reports_not_ready_under_both_fail_directions(
        self, provider_factory, readiness_settings, direction
    ):
        """A refused connection depools either way, and is never "timed_out"."""
        readiness_settings(readiness_timeout_fail_direction=direction)
        provider = provider_factory(
            aliases=["default"], raises=RuntimeError("connection refused")
        )

        with _registered_provider(provider):
            status = self.service.get_readiness()

        assert status.checks["database_default"] == "not_ready"
        assert status.is_ready is False

    @pytest.mark.parametrize("direction", ["not_ready", "ready"])
    def test_healthy_alias_reports_ready_under_both_fail_directions(
        self, provider_factory, readiness_settings, direction
    ):
        """The knob is inert when nothing stalled."""
        readiness_settings(readiness_timeout_fail_direction=direction)
        provider = provider_factory(aliases=["default"])

        with _registered_provider(provider):
            status = self.service.get_readiness()

        assert status.checks["database_default"] == "ready"
        assert status.is_ready is True

    def test_refused_alias_depools_even_when_a_hung_sibling_is_tolerated(
        self, provider_factory, readiness_settings
    ):
        """Mixed round under direction='ready': the refused alias still wins.

        The knob relaxes the timeout class only; it never makes a database that
        actively refuses connections look ready.
        """
        readiness_settings(readiness_timeout_fail_direction="ready")
        provider = provider_factory(
            aliases=["default", "replica"],
            hang_aliases=["replica"],
            raises=RuntimeError("connection refused"),
        )

        with _registered_provider(provider):
            status = self.service.get_readiness()

        assert status.checks["database_default"] == "not_ready"
        assert status.checks["database_replica"] == "timed_out"
        assert status.is_ready is False

    def test_provider_without_aliases_reports_ready_with_no_checks(
        self, provider_factory, readiness_settings
    ):
        """Zero-alias provider (the Noop default) yields ready, not a crash."""
        provider = provider_factory(aliases=[])

        with _registered_provider(provider):
            status = self.service.get_readiness()

        assert status.is_ready is True
        assert status.status == "ready"
        assert status.checks == {}


class TestReadinessCacheBehavior:
    """The readiness verdict is cached whole, for readiness_cache_ttl_seconds."""

    def setup_method(self):
        self.service = HealthCheckService()

    def test_two_calls_within_the_ttl_probe_each_alias_once(
        self, provider_factory, readiness_settings
    ):
        """Probe cadence x pod count no longer means constant SELECT 1 load."""
        readiness_settings(readiness_cache_ttl_seconds=30.0)
        provider = provider_factory(aliases=["default", "replica"])

        with _registered_provider(provider):
            first = self.service.get_readiness()
            second = self.service.get_readiness()

        assert provider.call_count("default") == 1
        assert provider.call_count("replica") == 1
        assert second is first
        assert self.service._readiness_cache.get_stats().hits == 1

    def test_zero_ttl_probes_live_on_every_sequential_call(
        self, provider_factory, readiness_settings
    ):
        """TTL=0 disables caching for sequential callers."""
        readiness_settings(readiness_cache_ttl_seconds=0.0)
        provider = provider_factory(aliases=["default"])

        with _registered_provider(provider):
            self.service.get_readiness()
            self.service.get_readiness()

        assert provider.call_count("default") == 2
        assert self.service._readiness_cache.get_stats().hits == 0

    def test_cached_verdict_outlives_a_provider_flip_within_the_ttl(
        self, provider_factory, readiness_settings
    ):
        """The accepted trade: a recovery/outage flip is seen up to a TTL late."""
        readiness_settings(readiness_cache_ttl_seconds=30.0)
        provider = provider_factory(aliases=["default"])

        with _registered_provider(provider):
            first = self.service.get_readiness()
            provider.raises = RuntimeError("connection refused")
            second = self.service.get_readiness()

        assert first.is_ready is True
        assert second.is_ready is True
        assert provider.call_count("default") == 1


class TestReadinessGuardBehavior:
    """No ordinary failure on the way to a verdict escapes as an HTTP 500."""

    def setup_method(self):
        self.service = HealthCheckService()

    def test_malformed_cache_ttl_env_var_yields_a_graceful_not_ready(
        self, provider_factory, readiness_settings, monkeypatch
    ):
        """The guard covers the settings read, not only the probe round."""
        monkeypatch.setenv(
            "BALDUR_HEALTH_CHECK_READINESS_CACHE_TTL_SECONDS", "not-a-float"
        )
        reset_health_check_settings()
        provider = provider_factory(aliases=["default"])

        with _registered_provider(provider):
            status = self.service.get_readiness()

        assert status.is_ready is False
        assert status.status == "not_ready"
        assert status.checks == {}
        # The settings read fails before any probe is submitted.
        assert provider.call_count("default") == 0

    def test_raising_provider_resolution_yields_a_graceful_not_ready(
        self, readiness_settings, caplog
    ):
        """A registry that raises depools the pod gracefully, and says so."""
        with patch(f"{_REGISTRY_PATH}.database_health") as slot:
            slot.get.side_effect = RuntimeError("registry exploded")
            with caplog.at_level(logging.WARNING):
                status = self.service.get_readiness()

        assert status.is_ready is False
        assert status.status == "not_ready"
        events = _structlog_events(caplog, "health_check.readiness_resolution_failed")
        assert len(events) == 1
        assert events[0]["error"] == "registry exploded"

    def test_raising_alias_enumeration_yields_a_graceful_not_ready(
        self, readiness_settings
    ):
        """The guard spans alias enumeration too, not just the probes."""

        class _BrokenProvider(_StubDatabaseHealthProvider):
            def list_aliases(self):
                raise RuntimeError("alias enumeration failed")

        with _registered_provider(_BrokenProvider()):
            status = self.service.get_readiness()

        assert status.is_ready is False
        assert status.checks == {}


class TestProbeConnectionReleaseBehavior:
    """The probe worker releases its per-thread DB connections every round."""

    _CLOSE_HOOK_PATH = "baldur.adapters.django.utils.close_all_django_connections"

    def setup_method(self):
        self.service = HealthCheckService()

    def test_probe_worker_releases_connections_on_the_success_path(
        self, provider_factory, readiness_settings
    ):
        """request_finished never fires for a probe, so it must release its own."""
        provider = provider_factory(aliases=["default", "replica"])
        counter = _CallCounter()

        with (
            _registered_provider(provider),
            patch(self._CLOSE_HOOK_PATH, new=counter),
        ):
            self.service.check_all_databases()

        assert counter.count == 2

    def test_probe_worker_releases_connections_on_the_exception_path(
        self, provider_factory, readiness_settings
    ):
        """A refused probe strands a connection too — the release is in a finally."""
        provider = provider_factory(
            aliases=["default"], raises=RuntimeError("connection refused")
        )
        counter = _CallCounter()

        with (
            _registered_provider(provider),
            patch(self._CLOSE_HOOK_PATH, new=counter),
        ):
            self.service.check_all_databases()

        assert counter.count == 1

    def test_failing_connection_release_does_not_fail_the_probe(
        self, provider_factory, readiness_settings
    ):
        """Fail-open: cleanup failure must not turn a completed probe into an
        exceptional Future, which would surface as a bogus not_ready."""

        def _raising_close():
            raise RuntimeError("close failed")

        provider = provider_factory(aliases=["default"])

        with (
            _registered_provider(provider),
            patch(self._CLOSE_HOOK_PATH, new=_raising_close),
        ):
            results = self.service.check_all_databases()

        assert results[0].is_connected is True
        assert results[0].error is None


class TestHealthCheckLogLevelBehavior:
    """A dependency outage logs at WARNING; the traceback is demoted to DEBUG."""

    _EVENT = "health_check.database_check_failed"

    def setup_method(self):
        self.service = HealthCheckService()

    def test_database_check_failure_emits_no_error_level_record(
        self, provider_factory, caplog
    ):
        """A sustained outage no longer emits ERROR every probe interval."""
        provider = provider_factory(raises=RuntimeError("connection refused"))

        with _registered_provider(provider), caplog.at_level(logging.DEBUG):
            self.service.check_database("default")

        failures = [
            record
            for record in caplog.records
            if isinstance(record.msg, dict) and record.msg.get("event") == self._EVENT
        ]
        assert failures
        assert all(record.levelno <= logging.WARNING for record in failures)

    def test_database_check_failure_warning_record_carries_no_traceback(
        self, provider_factory, caplog
    ):
        """Production volume is unchanged: the WARNING is a one-liner."""
        provider = provider_factory(raises=RuntimeError("connection refused"))

        with _registered_provider(provider), caplog.at_level(logging.WARNING):
            self.service.check_database("default")

        events = _structlog_events(caplog, self._EVENT)
        assert len(events) == 1
        assert "exc_info" not in events[0]
        assert events[0]["error"] == "connection refused"

    def test_database_check_failure_emits_a_debug_record_with_the_traceback(
        self, provider_factory, caplog
    ):
        """The traceback is demoted, not deleted — it is the only thing that
        says *why* the connection failed, and it cannot be recovered from
        logging configuration once dropped at the call site."""
        provider = provider_factory(raises=RuntimeError("connection refused"))

        with _registered_provider(provider), caplog.at_level(logging.DEBUG):
            self.service.check_database("default")

        with_traceback = [
            event
            for event in _structlog_events(caplog, self._EVENT)
            if event.get("exc_info")
        ]
        assert len(with_traceback) == 1
