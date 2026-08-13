"""The bounded admission probe, and the four sites that run it.

``RedisConnectionFactory.probe()`` answers one question — is this address
reachable — on a budget the caller can afford to pay inside its own timed
section. Before it existed, each of these sites decided admission by pinging
the process-lifetime client it was about to keep, which meant the first
protected call in a "redis-py installed, no server" process paid the full
data-path connect budget.

Two properties carry the whole design and are asserted separately here:

- The probe client is *ephemeral* and narrows *only* the connect phase. The
  read budget stays at its data-path value on purpose: a server that accepts
  the connection is reachable, and a healthy Redis that answers PING slowly
  must not be demoted for the rest of the process.
- The client the site keeps afterwards is built by a second, unnarrowed
  ``create()`` call. Narrowing the shared client instead would rewrite the
  data path — every later pool reconnect would fail at the probe budget.

The elapsed-time band that decides whether a failure earns its one retry is
driven by an explicit clock rather than by sleeping: redis-py raises the same
``TimeoutError`` for a connect timeout and a read timeout, so the band is the
only thing that separates them, and a wall-clock test of it would be both slow
and unstable on a host whose timer granularity is 15.6 ms.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import redis
from structlog.testing import capture_logs

from baldur.adapters.redis import connection_factory as connection_factory_module
from baldur.adapters.redis.connection_factory import (
    _PROBE_CONNECT_ATTEMPTS,
    RedisConnectionFactory,
)
from baldur.settings.redis import RedisSettings

# Deliberately none of the shipped defaults (0.5 / 5.0 / 5.0): an assertion
# written against a default cannot tell "reads the setting" from "hardcodes
# the number".
_PROBE_CONNECT = 0.25
_DATA_READ = 4.0
_DATA_CONNECT = 9.0


class _Clock:
    """A monotonic clock the test advances explicitly.

    The probe measures elapsed time to classify a failure. Driving that with
    real sleeps would make every band case cost its own budget in wall-clock
    and would sit within one timer tick of the boundary on Windows.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def probe_settings():
    """Real settings — the fields are real and there is no spec question."""
    return RedisSettings(
        url="redis://probe-host:6379/0",
        probe_connect_timeout=_PROBE_CONNECT,
        socket_timeout=_DATA_READ,
        socket_connect_timeout=_DATA_CONNECT,
        # True so the probe's pin to False is an assertion about the probe
        # rather than an echo of the settings default.
        retry_on_timeout=True,
    )


@pytest.fixture
def factory(probe_settings):
    return RedisConnectionFactory(settings=probe_settings)


@pytest.fixture
def clock(monkeypatch):
    """Replace the module's ``time`` binding, not the global ``time`` module.

    Patching ``time.monotonic`` itself would reach every thread in the worker
    process; rebinding the name inside this one module does not.
    """
    instance = _Clock()
    monkeypatch.setattr(
        connection_factory_module,
        "time",
        SimpleNamespace(monotonic=instance.monotonic),
    )
    return instance


def _script_create(factory, clock, monkeypatch, script, close_error=None):
    """Hand ``probe()`` a scripted client per attempt; record what it asked for.

    Each ``script`` entry is the ``(elapsed, error)`` the next ``ping()``
    produces: the clock advances by ``elapsed``, then ``error`` is raised when
    it is not None. ``close_error`` arms every client's ``close()`` to raise,
    which has to be arranged here rather than on the recorded attempt — the
    probe creates and closes within a single call.

    Returns the list of attempts, each carrying the URL, the kwargs ``create``
    received, and the client handed back.
    """
    attempts: list[SimpleNamespace] = []

    def _create(url, **kwargs):
        elapsed, error = script[len(attempts)]
        client = MagicMock(spec=redis.Redis)

        def _ping():
            clock.advance(elapsed)
            if error is not None:
                raise error

        client.ping.side_effect = _ping
        if close_error is not None:
            client.close.side_effect = close_error
        attempts.append(SimpleNamespace(url=url, kwargs=kwargs, client=client))
        return client

    monkeypatch.setattr(factory, "create", _create)
    return attempts


class TestRedisAdmissionProbeContract:
    """What the throwaway probe client is built with."""

    def test_probe_client_narrows_the_connect_phase_to_the_probe_budget(
        self, factory, monkeypatch
    ):
        """The connect timeout is the probe budget, on the real client.

        Built through the unpatched ``create``, so the assertion reads the
        connection pool the site would really get rather than the kwargs the
        factory was handed.
        """
        built = []
        original_create = factory.create

        def _spy(url, **kwargs):
            client = original_create(url, **kwargs)
            built.append(client)
            return client

        monkeypatch.setattr(factory, "create", _spy)

        with patch("redis.Redis.ping", autospec=True):
            factory.probe("redis://127.0.0.1:6399/0")

        assert (
            built[0].connection_pool.connection_kwargs["socket_connect_timeout"]
            == _PROBE_CONNECT
        )

    def test_probe_client_keeps_the_data_path_read_budget(self, factory, monkeypatch):
        """The read phase is NOT narrowed.

        A server that accepts the connection is reachable — the question the
        probe asks is already answered — so a slow PING must not be allowed to
        demote it.
        """
        built = []
        original_create = factory.create

        def _spy(url, **kwargs):
            client = original_create(url, **kwargs)
            built.append(client)
            return client

        monkeypatch.setattr(factory, "create", _spy)

        with patch("redis.Redis.ping", autospec=True):
            factory.probe("redis://127.0.0.1:6399/0")

        assert (
            built[0].connection_pool.connection_kwargs["socket_timeout"] == _DATA_READ
        )

    def test_probe_client_pins_retry_on_timeout_off(self, factory, monkeypatch):
        """False regardless of the setting: a hung host costs one read budget.

        ``probe_settings`` sets ``retry_on_timeout=True``, so this cannot pass
        by inheriting the configured value.
        """
        built = []
        original_create = factory.create

        def _spy(url, **kwargs):
            client = original_create(url, **kwargs)
            built.append(client)
            return client

        monkeypatch.setattr(factory, "create", _spy)

        with patch("redis.Redis.ping", autospec=True):
            factory.probe("redis://127.0.0.1:6399/0")

        assert built[0].connection_pool.connection_kwargs["retry_on_timeout"] is False

    def test_probe_forwards_the_connect_budget_through_standalone_routing(
        self, factory
    ):
        """redis:// reaches redis-py's own constructor with the narrowed budget."""
        with patch("redis.from_url", autospec=True) as mock_from_url:
            factory.probe("redis://host:6379/0")

        assert (
            mock_from_url.call_args.kwargs["socket_connect_timeout"] == _PROBE_CONNECT
        )

    def test_probe_forwards_the_connect_budget_through_sentinel_routing(self, factory):
        """redis+sentinel:// keeps its routing — the probe only adds budgets."""
        with patch("redis.sentinel.Sentinel", autospec=True) as mock_sentinel_cls:
            factory.probe("redis+sentinel://mymaster@s1:26379,s2:26379/0")

        assert (
            mock_sentinel_cls.call_args.kwargs["socket_connect_timeout"]
            == _PROBE_CONNECT
        )

    def test_probe_forwards_the_connect_budget_through_cluster_routing(self, factory):
        """redis+cluster:// likewise routes before the budget is applied."""
        with (
            patch("redis.cluster.ClusterNode", autospec=True),
            patch("redis.cluster.RedisCluster", autospec=True) as mock_cluster_cls,
        ):
            factory.probe("redis+cluster://n1:7000,n2:7001")

        assert (
            mock_cluster_cls.call_args.kwargs["socket_connect_timeout"]
            == _PROBE_CONNECT
        )


class TestRedisAdmissionProbeBehavior:
    """What the probe does with each class of failure."""

    def test_reachable_server_returns_without_raising(
        self, factory, clock, monkeypatch
    ):
        """A PING that answers is the whole success condition."""
        attempts = _script_create(factory, clock, monkeypatch, [(0.0, None)])

        assert factory.probe("redis://host:6379/0") is None
        assert len(attempts) == 1

    def test_refused_connect_is_attempted_exactly_once(
        self, factory, clock, monkeypatch
    ):
        """A refusal is definitive — retrying it would double the common cost.

        The dominant failure in the posture this probe exists for is an
        instant RST, and it returns well inside the connect budget.
        """
        error = ConnectionError("connection refused")
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [(0.0, error), (0.0, error)],
        )

        with pytest.raises(ConnectionError):
            factory.probe("redis://host:6379/0")

        assert len(attempts) == 1

    def test_connect_timeout_then_success_yields_a_passing_probe(
        self, factory, clock, monkeypatch
    ):
        """One lost SYN must not demote an otherwise reachable host."""
        # Given: the first attempt burns the connect budget, the second answers
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [(_PROBE_CONNECT + 0.05, TimeoutError("connect")), (0.0, None)],
        )

        # When / Then: no exception escapes
        assert factory.probe("redis://host:6379/0") is None
        assert len(attempts) == _PROBE_CONNECT_ATTEMPTS

    def test_connect_timeout_twice_raises_after_the_bounded_retry(
        self, factory, clock, monkeypatch
    ):
        """A second timeout is evidence, not noise — and the retry is bounded."""
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [
                (_PROBE_CONNECT + 0.05, TimeoutError("connect")),
                (_PROBE_CONNECT + 0.05, TimeoutError("connect")),
            ],
        )

        with pytest.raises(TimeoutError):
            factory.probe("redis://host:6379/0")

        assert len(attempts) == _PROBE_CONNECT_ATTEMPTS

    def test_hung_read_is_attempted_exactly_once(self, factory, clock, monkeypatch):
        """A failure that cost the read budget was not a connect timeout.

        The host accepted the connection and then stopped answering; retrying
        would buy a second read budget for a question already answered.
        """
        error = TimeoutError("read")
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [(_DATA_READ + 0.5, error), (_DATA_READ + 0.5, error)],
        )

        with pytest.raises(TimeoutError):
            factory.probe("redis://host:6379/0")

        assert len(attempts) == 1

    def test_failure_at_exactly_the_connect_budget_is_retried(
        self, factory, clock, monkeypatch
    ):
        """Lower boundary of the retry band is inclusive."""
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [(_PROBE_CONNECT, TimeoutError("connect")), (0.0, None)],
        )

        assert factory.probe("redis://host:6379/0") is None
        assert len(attempts) == _PROBE_CONNECT_ATTEMPTS

    def test_failure_at_exactly_the_read_budget_is_not_retried(
        self, factory, clock, monkeypatch
    ):
        """Upper boundary of the retry band is exclusive."""
        error = TimeoutError("read")
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [(_DATA_READ, error), (_DATA_READ, error)],
        )

        with pytest.raises(TimeoutError):
            factory.probe("redis://host:6379/0")

        assert len(attempts) == 1

    def test_slow_but_answering_ping_still_passes_admission(
        self, factory, clock, monkeypatch
    ):
        """A fork/COW pause is not unreachability.

        The PING takes longer than the connect budget and still lands inside
        the read budget, which is exactly the case the un-narrowed read phase
        exists to protect.
        """
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [(_PROBE_CONNECT + 1.0, None)],
        )

        assert factory.probe("redis://host:6379/0") is None
        assert len(attempts) == 1

    def test_probe_client_is_closed_after_a_passing_probe(
        self, factory, clock, monkeypatch
    ):
        """Ephemeral means closed — the caller's client is a separate object."""
        attempts = _script_create(factory, clock, monkeypatch, [(0.0, None)])

        factory.probe("redis://host:6379/0")

        attempts[0].client.close.assert_called_once()

    def test_every_probe_client_is_closed_after_a_failing_probe(
        self, factory, clock, monkeypatch
    ):
        """Including the one from the attempt that earned the retry."""
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [
                (_PROBE_CONNECT + 0.05, TimeoutError("connect")),
                (_PROBE_CONNECT + 0.05, TimeoutError("connect")),
            ],
        )

        with pytest.raises(TimeoutError):
            factory.probe("redis://host:6379/0")

        assert [len(a.client.close.call_args_list) for a in attempts] == [1, 1]

    def test_unclosable_probe_client_does_not_mask_the_verdict(
        self, factory, clock, monkeypatch
    ):
        """A close that raises has nothing left the caller could act on."""
        attempts = _script_create(
            factory,
            clock,
            monkeypatch,
            [(0.0, None)],
            close_error=OSError("already gone"),
        )

        assert factory.probe("redis://host:6379/0") is None
        attempts[0].client.close.assert_called_once()

    def test_probe_logs_nothing_on_either_outcome(self, factory, clock, monkeypatch):
        """Each call site owns the level and the metrics for its own failure.

        A line emitted here would either duplicate the site's line or, worse,
        fix a level the site deliberately splits by posture.
        """
        _script_create(
            factory,
            clock,
            monkeypatch,
            [(0.0, None), (0.0, ConnectionError("refused"))],
        )

        with capture_logs() as logs:
            factory.probe("redis://host:6379/0")

        assert logs == []

        clock.now = 0.0
        _script_create(factory, clock, monkeypatch, [(0.0, ConnectionError("refused"))])

        with capture_logs() as logs:
            with pytest.raises(ConnectionError):
                factory.probe("redis://host:6379/0")

        assert logs == []


def _airgap_site(url):
    """Air-gap adapter creation, driven by its own URL channel."""
    from baldur.adapters.airgap.factory import _create_redis_adapter

    with patch.dict(os.environ, {"BALDUR_AIRGAP_REDIS_URL": url}):
        _create_redis_adapter()


def _audit_buffer_site(url):
    """Audit buffer creation, driven by its caller-supplied URL."""
    from baldur.adapters.audit.redis_buffer import create_redis_audit_buffer

    create_redis_audit_buffer(
        redis_url=url,
        fallback_log_dir=None,
        enable_graceful_shutdown=False,
    )


def _metric_source_site(url):
    """Metric source adapter creation, driven by ``RedisSettings.url``."""
    from baldur.adapters.metrics.factory import _create_redis_adapter

    _create_redis_adapter()


def _rate_limit_site(url):
    """Rate-limit storage creation, driven by ``RedisSettings.url``."""
    from baldur.factory import ProviderRegistry
    from baldur.factory.adapters import discover_rate_limit_storage_adapters

    discover_rate_limit_storage_adapters()
    ProviderRegistry.rate_limit_storage.invalidate_instance("redis")
    ProviderRegistry.rate_limit_storage.get("redis")


_SITES = [
    pytest.param(_airgap_site, id="airgap"),
    pytest.param(_audit_buffer_site, id="audit-buffer"),
    pytest.param(_metric_source_site, id="metric-source"),
    pytest.param(_rate_limit_site, id="rate-limit"),
]


class TestRedisAdmissionSiteContract:
    """Every site that keeps a process-lifetime client admits through probe().

    The two halves are asserted separately because they fail separately: a
    site could probe and then narrow its data client anyway, or keep a correct
    data client while deciding admission the old way.
    """

    @pytest.fixture
    def wired_factory(self, probe_settings, monkeypatch):
        """A real factory installed as the singleton, with ``probe`` neutered.

        ``create`` runs for real: redis-py builds a standalone client without
        opening a socket, so the connection pool the site would keep is a real
        object the assertions can read.
        """
        from baldur.adapters.redis.connection_factory import (
            configure_redis_connection_factory,
            reset_redis_connection_factory,
        )
        from baldur.settings.redis import get_redis_settings

        instance = RedisConnectionFactory(settings=probe_settings)
        probed: list[str] = []
        built: list = []
        original_create = instance.create

        def _probe(url, **kwargs):
            probed.append(url)

        def _create(url, **kwargs):
            client = original_create(url, **kwargs)
            built.append(client)
            return client

        monkeypatch.setattr(instance, "probe", _probe)
        monkeypatch.setattr(instance, "create", _create)
        configure_redis_connection_factory(instance)

        # The two settings-driven sites read this object, so it must be the
        # same one whose data-path budget the assertions name.
        monkeypatch.setattr(
            get_redis_settings.__module__ + ".get_redis_settings",
            lambda: probe_settings,
            raising=True,
        )

        yield SimpleNamespace(probed=probed, built=built, url=probe_settings.url)

        reset_redis_connection_factory()

    @pytest.mark.parametrize("site", _SITES)
    def test_site_admits_through_the_bounded_probe(self, site, wired_factory):
        """Admission is decided before the process-lifetime client is built."""
        site(wired_factory.url)

        assert wired_factory.probed == [wired_factory.url]

    @pytest.mark.parametrize("site", _SITES)
    def test_site_data_client_keeps_the_data_path_connect_budget(
        self, site, wired_factory
    ):
        """The kept client is NOT built on the probe budget.

        Narrowing it would make every later pool reconnect — a Sentinel
        failover, a BGSAVE fork stall — fail at the admission budget.
        """
        site(wired_factory.url)

        assert [
            client.connection_pool.connection_kwargs["socket_connect_timeout"]
            for client in wired_factory.built
        ] == [_DATA_CONNECT]
