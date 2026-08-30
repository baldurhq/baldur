"""The two backend seams a raw-client dial consults before it dials.

``probing_unconfigured_default`` is the single duck-probe over the backend's
own posture predicate. Three consumers now ask that question — the layered
wrapper's initial-load skip, the pin-guarded write's report level, and the
circuit-breaker adapter's declination guard — and a private copy in each is
how the three drift apart. It answers False for anything that is not exactly
``True``, because silence is the dangerous direction: a custom store without
the method, an unrelated object that merely carries the attribute name, and a
spec'd double whose every method answers with a truthy mock must all fall
through to the loud, dialing path.

``has_reached_redis`` is the second term of the declination guard, and the
reason that guard is not ``is_redis_available``. The latter additionally
requires the current mode to be REDIS, so it is False for the whole degraded
and recovering window of a process whose Redis is answering again — a window
in which the raw client stays perfectly usable. Declining there would drop
the store-side pin guard and the cluster-wide single-winner trip on every
blip. "Ever reached" is monotonic instead, so a store that answered once
keeps every store-side guarantee for the life of the process.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
import redis

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.adapters.resilient.backend import (
    ResilientStorageBackend,
    ResilientStorageMode,
    probing_unconfigured_default,
)
from baldur.settings.resilient_storage import ResilientStorageSettings


@pytest.fixture(autouse=True)
def clear_redis_negative_cache():
    """The shared negative cache would short-circuit the probe path."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    previous = (state.unavailable, state.fail_time)
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable, state.fail_time = previous


@pytest.fixture
def make_backend(no_redis_posture):
    """Build a zero-config backend on a throwaway WAL dir, closed on teardown.

    The posture fixture is what makes ``_probing_unconfigured_default()``
    answer True here: it clears every Redis intent variable and points the
    settings root at an address that refuses instantly, so nothing in this
    module can reach a Redis a developer happens to be running.
    """
    created: list[ResilientStorageBackend] = []

    with tempfile.TemporaryDirectory() as wal_dir:

        def _make(**settings_kwargs) -> ResilientStorageBackend:
            settings = ResilientStorageSettings(wal_dir=wal_dir, **settings_kwargs)
            backend = ResilientStorageBackend(settings=settings)
            created.append(backend)
            return backend

        yield _make

        for backend in created:
            backend.close()


def _connect(backend) -> MagicMock:
    """Drive the backend's own lazy probe to a success, without a Redis.

    The client the probe installs is returned so a caller can drive the
    later blip through the same object the raw-client seam hands out.
    """
    client = MagicMock(spec=redis.Redis)
    adapter = MagicMock(spec=RedisCacheAdapter)
    adapter.raw_client = client
    with patch("baldur.adapters.cache.RedisCacheAdapter", return_value=adapter):
        assert backend._ensure_redis() is True
    return client


class _CarriesTheName:
    """An unrelated object whose attribute is not a probe at all."""

    _probing_unconfigured_default = "not callable"


class _AnswersTruthy:
    """A custom store whose probe answers something merely truthy."""

    def __init__(self, answer):
        self._answer = answer

    def _probing_unconfigured_default(self):
        return self._answer


class TestUnconfiguredProbeBehavior:
    """The shared duck-probe's verdict for every shape it can be handed."""

    @pytest.mark.parametrize(
        "backend",
        [
            None,
            object(),
            _CarriesTheName(),
            _AnswersTruthy(1),
            _AnswersTruthy("yes"),
            _AnswersTruthy([1]),
            _AnswersTruthy(False),
            _AnswersTruthy(None),
        ],
        ids=[
            "absent_backend",
            "no_such_attribute",
            "attribute_is_not_callable",
            "answers_one",
            "answers_a_string",
            "answers_a_non_empty_list",
            "answers_false",
            "answers_none",
        ],
    )
    def test_anything_but_exactly_true_keeps_the_loud_path(self, backend):
        """The ``is True`` pin: everything unrecognised stays dialing."""
        assert probing_unconfigured_default(backend) is False

    def test_a_spec_bounded_double_keeps_the_loud_path(self):
        """The shape that makes plain truthiness silently wrong.

        ``MagicMock(spec=...)`` answers the probe with a truthy mock, so under
        plain truthiness every spec'd construction in the tree would go quiet
        at once — and none of them would reveal it.
        """
        double = MagicMock(spec=ResilientStorageBackend)

        assert double._probing_unconfigured_default() is not True
        assert probing_unconfigured_default(double) is False

    def test_a_zero_config_backend_is_reported_as_unconfigured(self, make_backend):
        """The one True case, taken from the production predicate itself."""
        backend = make_backend()

        assert backend._probing_unconfigured_default() is True
        assert probing_unconfigured_default(backend) is True

    def test_a_named_redis_is_not_reported_as_unconfigured(
        self, make_backend, no_redis_posture
    ):
        """A construction kwarg is an operator naming this backend's Redis."""
        backend = make_backend(redis_url=no_redis_posture)

        assert backend._probing_unconfigured_default() is False
        assert probing_unconfigured_default(backend) is False


class TestBackendReachMonotonicityBehavior:
    """``has_reached_redis`` across the backend's whole connection lifecycle."""

    def test_a_never_probed_backend_has_not_reached_redis(self, make_backend):
        """The backend constructs DEGRADED, before any dial has happened."""
        backend = make_backend()

        assert backend.has_reached_redis is False
        assert backend.is_redis_available is False

    def test_a_failed_first_probe_leaves_the_reach_unset(self, make_backend):
        backend = make_backend()

        with patch(
            "baldur.adapters.cache.RedisCacheAdapter",
            side_effect=ConnectionError("refused"),
        ):
            assert backend._ensure_redis() is False

        assert backend.has_reached_redis is False

    def test_a_successful_probe_records_the_reach(self, make_backend):
        backend = make_backend()

        _connect(backend)

        assert backend.has_reached_redis is True
        assert backend.is_redis_available is True

    def test_the_reach_survives_the_degraded_transition(self, make_backend):
        """The contrast the declination guard is built on.

        ``is_redis_available`` goes False the moment the mode reverts, which
        is exactly the blip window where the raw client is still usable and
        the store-side guarantees must be kept.
        """
        backend = make_backend()
        _connect(backend)

        backend._switch_to_degraded()

        assert backend.mode is ResilientStorageMode.DEGRADED
        assert backend.is_redis_available is False
        assert backend.has_reached_redis is True

    def test_the_reach_stays_set_across_repeated_outages(self, make_backend):
        """Monotonic: nothing clears it for the life of the backend."""
        backend = make_backend()
        _connect(backend)

        for _ in range(3):
            backend._switch_to_degraded()

        assert backend.has_reached_redis is True

    def test_a_degraded_backend_still_hands_out_its_client(self, make_backend):
        """The premise of keeping the guarantees: the client is still there."""
        backend = make_backend()
        client = _connect(backend)

        backend._switch_to_degraded()

        assert backend.raw_redis_client is client
