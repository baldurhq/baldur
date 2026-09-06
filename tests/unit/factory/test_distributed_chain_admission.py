"""Admission for the distributed audit hash chain: which client, or none.

Source: ``src/baldur/factory/adapters.py`` — ``_admit_distributed_chain_client``
and the two module-level memos it reads
(``_distributed_chain_redis_reachable``,
``_announce_distributed_chain_failure_once``).

Two failures wear the same settings flag and are not the same defect, so the
admission decision is a 2x3 truth table rather than a boolean:

- **No client buildable at all.** The only remaining option is a local chain,
  indistinguishable in the files from one nobody asked to be distributed.
  Stated intent refuses; an inferred one falls back, which is the status quo
  that deployment would have had anyway.
- **The client builds, the server does not answer.** Nothing is refused on the
  stated path: the chain's own fallback stamps every entry ``degraded``, so
  the substitution is labelled rather than silent. Raising here would delete
  audit records that today are written and self-describing.
- **Reachable.** The client is handed over on both paths.

The posture is published on the ``audit_distributed_chain_degraded`` series on
every outcome where a distributed chain was wanted — including ``0`` on the
healthy branch, because an absent series has to keep meaning "nobody asked".

Companion files:
``tests/unit/factory/test_hashchain_adapter_factory.py`` — the provider
factory that calls this; ``tests/unit/metrics/test_audit_backend_metrics.py``
— the gauge module itself.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.2 Exception/edge cases (the refusal, and its masked URL).
- §8.4 Side effects (gauge writes and the ERROR line, neither of which is a
  return value).
- §8.5 Dependency interaction (probe call count across resolutions).
- §8.6 Idempotency (the probe memo and the announcement latch).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import redis
from structlog.testing import capture_logs

from baldur.adapters.redis.connection_factory import RedisConnectionFactory
from baldur.core.exceptions import DistributedHashChainUnavailableError
from baldur.factory.adapters import (
    _admit_distributed_chain_client,
    _announce_distributed_chain_failure_once,
    _distributed_chain_redis_reachable,
    clear_distributed_chain_probe_cache,
)

# A URL with no credentials, so an assertion on the logged or raised value is
# about the masking helper being reached, not about this string's shape.
_CHAIN_URL = "redis://chain.example:6379/0"

# More than one, so "memoized" is distinguishable from "ran once because it
# was only called once".
_RESOLUTIONS = 3

_NO_CLIENT_EVENT = "audit.distributed_chain_resolution_failed"
_UNREACHABLE_EVENT = "audit.distributed_chain_unreachable"
_DEGRADED_GAUGE_SETTER = (
    "baldur.metrics.audit_backend_metrics.set_audit_distributed_chain_degraded"
)


@pytest.fixture(autouse=True)
def _reset_chain_memos():
    """Both memos are module-level and outlive a test."""
    clear_distributed_chain_probe_cache()
    yield
    clear_distributed_chain_probe_cache()


def _client() -> MagicMock:
    """A stand-in Redis client — admission never calls it, only routes it."""
    return MagicMock(spec=redis.Redis)


def _connection_factory(*, reachable: bool) -> MagicMock:
    """A connection factory whose ``probe()`` answers or refuses."""
    factory = MagicMock(spec=RedisConnectionFactory)
    if not reachable:
        factory.probe.side_effect = ConnectionError("no route to host")
    return factory


def _admit(*, operator_stated: bool, client, reachable: bool = True):
    """Run admission with the three collaborators pinned."""
    with (
        patch(
            "baldur.audit.config.resolve_hash_chain_redis_url",
            return_value=_CHAIN_URL,
        ),
        patch(
            "baldur.audit.config.create_hash_chain_redis_client",
            return_value=client,
        ),
        patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=_connection_factory(reachable=reachable),
        ),
    ):
        return _admit_distributed_chain_client(operator_stated=operator_stated)


def _events(logs: list[dict], name: str) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == name]


class TestDistributedChainAdmissionBehavior:
    """(stated | inferred) x (no client | unreachable | reachable)."""

    def test_stated_intent_with_no_client_refuses_rather_than_substituting(self):
        """The unlabelled substitution is the one this refuses: a local chain
        writes entries indistinguishable from a chain nobody asked to be
        distributed, so the cross-host guarantee would be silently unserved."""
        with pytest.raises(DistributedHashChainUnavailableError):
            _admit(operator_stated=True, client=None)

    def test_inferred_intent_with_no_client_falls_back_to_the_local_chain(self):
        """Nobody asked for the cross-host guarantee here — the product did.
        Falling back is the deployment's own status quo, not a broken promise,
        so it must not turn an entitled boot into a crash."""
        assert _admit(operator_stated=False, client=None) is None

    def test_stated_intent_with_an_unreachable_server_keeps_the_client(self):
        """The chain manager's fallback stamps every entry it writes
        ``degraded``, so these records survive and are self-describing.
        Refusing here would delete audit records that today get written."""
        client = _client()

        assert _admit(operator_stated=True, client=client, reachable=False) is client

    def test_inferred_intent_with_an_unreachable_server_falls_back(self):
        """An inferred chain has no operator promise to keep labelled — it
        takes the local path rather than writing every entry degraded."""
        client = _client()

        assert _admit(operator_stated=False, client=client, reachable=False) is None

    @pytest.mark.parametrize(
        "operator_stated",
        [True, False],
        ids=["stated", "inferred"],
    )
    def test_reachable_server_routes_the_client_on_both_paths(self, operator_stated):
        """Reachability is the affirmative answer both paths were waiting for."""
        client = _client()

        assert _admit(operator_stated=operator_stated, client=client) is client

    def test_refusal_carries_the_url_it_could_not_dial(self):
        """The operator has to learn *which* address failed; the exception is
        the only channel carrying it on the raising path."""
        with pytest.raises(DistributedHashChainUnavailableError) as excinfo:
            _admit(operator_stated=True, client=None)

        assert excinfo.value.redis_url == _CHAIN_URL

    def test_a_password_in_the_url_never_reaches_the_raised_exception(self):
        """The exception is rendered into logs and error context, so the
        masking helper has to be on this path, not only on the factory's."""
        secret_url = "redis://user:hunter2@chain.example:6379/0"

        with (
            patch(
                "baldur.audit.config.resolve_hash_chain_redis_url",
                return_value=secret_url,
            ),
            patch(
                "baldur.audit.config.create_hash_chain_redis_client",
                return_value=None,
            ),
            pytest.raises(DistributedHashChainUnavailableError) as excinfo,
        ):
            _admit_distributed_chain_client(operator_stated=True)

        assert "hunter2" not in excinfo.value.redis_url
        assert "***" in excinfo.value.redis_url

    def test_no_client_publishes_the_degraded_posture(self):
        with patch(_DEGRADED_GAUGE_SETTER) as mock_gauge:
            _admit(operator_stated=False, client=None)

        mock_gauge.assert_called_once_with(True)

    def test_unreachable_server_publishes_the_degraded_posture(self):
        with patch(_DEGRADED_GAUGE_SETTER) as mock_gauge:
            _admit(operator_stated=True, client=_client(), reachable=False)

        mock_gauge.assert_called_once_with(True)

    def test_reachable_server_publishes_zero_rather_than_nothing(self):
        """A healthy process publishes ``0``. Left absent, an alert on the
        series could not tell "the chain is fine" from "no process ever asked
        for one", which is the third state this gauge exists to separate."""
        with patch(_DEGRADED_GAUGE_SETTER) as mock_gauge:
            _admit(operator_stated=True, client=_client())

        mock_gauge.assert_called_once_with(False)

    def test_a_broken_gauge_does_not_break_admission(self):
        """Fail-open: an observability fault must not decide whether the audit
        trail gets a Redis client."""
        client = _client()

        with patch(
            _DEGRADED_GAUGE_SETTER,
            side_effect=RuntimeError("prometheus registry exploded"),
        ):
            assert _admit(operator_stated=True, client=client) is client

    def test_inferred_fallback_announces_nothing_at_error(self):
        """The inferred path takes the status quo — an ERROR per entitled boot
        that happens not to have a Redis would be noise that gets filtered."""
        with capture_logs() as logs:
            _admit(operator_stated=False, client=None)

        assert _events(logs, _NO_CLIENT_EVENT) == []

    def test_stated_no_client_announces_the_unbuildable_client(self):
        with (
            capture_logs() as logs,
            pytest.raises(DistributedHashChainUnavailableError),
        ):
            _admit(operator_stated=True, client=None)

        announcements = _events(logs, _NO_CLIENT_EVENT)
        assert len(announcements) == 1
        assert announcements[0]["log_level"] == "error"
        assert announcements[0]["reason"] == "no_client_buildable"
        assert announcements[0]["redis_url"] == _CHAIN_URL

    def test_stated_unreachable_announces_the_failed_probe(self):
        """A different reason on a different event name: one branch has no
        client, the other has one whose server did not answer."""
        with capture_logs() as logs:
            _admit(operator_stated=True, client=_client(), reachable=False)

        announcements = _events(logs, _UNREACHABLE_EVENT)
        assert len(announcements) == 1
        assert announcements[0]["log_level"] == "error"
        assert announcements[0]["reason"] == "admission_probe_failed"


class TestDistributedChainProbeMemo:
    """One connect per process per URL, and one ERROR per outage."""

    def test_probe_runs_once_across_repeated_resolutions(self):
        """A raising factory is never cached by the registry, and the audit
        adapter is resolved once per audited event across a dozen call sites —
        an unmemoized probe is one connect per event."""
        factory = _connection_factory(reachable=True)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            for _ in range(_RESOLUTIONS):
                _distributed_chain_redis_reachable(_CHAIN_URL)

        assert factory.probe.call_count == 1

    def test_a_failed_probe_is_memoized_too(self):
        """Failure-only or success-only caching both leave one branch paying a
        connect per resolution."""
        factory = _connection_factory(reachable=False)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            verdicts = [
                _distributed_chain_redis_reachable(_CHAIN_URL)
                for _ in range(_RESOLUTIONS)
            ]

        assert factory.probe.call_count == 1
        assert [reachable for reachable, _ in verdicts] == [False] * _RESOLUTIONS

    def test_only_the_first_resolution_reports_a_new_verdict(self):
        """The second element is what lets a caller announce once rather than
        once per resolution — a cached read must not claim to be new."""
        factory = _connection_factory(reachable=False)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            first = _distributed_chain_redis_reachable(_CHAIN_URL)
            second = _distributed_chain_redis_reachable(_CHAIN_URL)

        assert first == (False, True)
        assert second == (False, False)

    def test_a_successful_probe_is_not_re_run_by_a_later_failure(self):
        """The reason both polarities are cached: a resolution that succeeds
        at the probe and then fails downstream (an operator log directory on a
        read-only mount) re-enters the factory on every audited event. With a
        failure-only latch that would be one connect per event."""
        factory = _connection_factory(reachable=True)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            assert _distributed_chain_redis_reachable(_CHAIN_URL) == (True, True)
            for _ in range(_RESOLUTIONS):
                assert _distributed_chain_redis_reachable(_CHAIN_URL) == (True, False)

        assert factory.probe.call_count == 1

    def test_a_second_url_gets_its_own_verdict(self):
        """The memo is keyed by URL, not a process-wide boolean — two chains
        in one process must not inherit each other's answer."""
        factory = _connection_factory(reachable=True)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            _distributed_chain_redis_reachable(_CHAIN_URL)
            _distributed_chain_redis_reachable("redis://other.example:6379/0")

        assert factory.probe.call_count == 2

    def test_announcement_slot_is_claimed_exactly_once_per_url(self):
        claims = [
            _announce_distributed_chain_failure_once(_CHAIN_URL)
            for _ in range(_RESOLUTIONS)
        ]

        assert claims == [True] + [False] * (_RESOLUTIONS - 1)

    def test_the_no_client_branch_announces_once_across_resolutions(self):
        """The raise is the per-resolution signal; the line is the per-outage
        one. Unlatched it is one ERROR per audited event."""
        with capture_logs() as logs:
            for _ in range(_RESOLUTIONS):
                with pytest.raises(DistributedHashChainUnavailableError):
                    _admit(operator_stated=True, client=None)

        assert len(_events(logs, _NO_CLIENT_EVENT)) == 1

    def test_the_probe_failed_branch_announces_once_across_resolutions(self):
        with capture_logs() as logs:
            for _ in range(_RESOLUTIONS):
                _admit(operator_stated=True, client=_client(), reachable=False)

        assert len(_events(logs, _UNREACHABLE_EVENT)) == 1

    def test_reset_clears_the_probe_verdicts(self):
        factory = _connection_factory(reachable=True)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            _distributed_chain_redis_reachable(_CHAIN_URL)
            clear_distributed_chain_probe_cache()
            _distributed_chain_redis_reachable(_CHAIN_URL)

        assert factory.probe.call_count == 2

    def test_reset_clears_the_announcements_too(self):
        """A reset that clears only the verdicts leaves the next boot's outage
        unannounced — the two halves have to move together."""
        _announce_distributed_chain_failure_once(_CHAIN_URL)

        clear_distributed_chain_probe_cache()

        assert _announce_distributed_chain_failure_once(_CHAIN_URL) is True
