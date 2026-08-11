"""752 D5 — availability probes that know unconfigured from broken.

Both rate-limit storage backends are constructed by auto-detection, which
probes every backend on every install. On a zero-config run that means two
guaranteed WARNINGs: a database adapter with no repository factory (it ships
none, so it can never work by construction) and a Redis adapter dialing the
shipped default address.

Each keeps its WARNING for the case it was written for — a factory that was
supplied and then failed, and a Redis somebody actually configured.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.rate_limit.database_adapter import DatabaseRateLimitStorage
from baldur.adapters.redis.connection_factory import RedisConnectionFactory

# Both storage backends are duck-typed against caller-supplied objects: this
# package ships no rate-limit repository and takes any redis-py-shaped client.
# The spec is therefore the method each probe actually reaches for, which also
# makes an unexpected extra call fail rather than auto-resolve.
_REPOSITORY_SPEC = ["get_or_create"]
_REDIS_CLIENT_SPEC = ["ping"]

_NOT_CONFIGURED_EVENT = "database_rate_limit_storage.not_configured"
_DB_UNAVAILABLE_EVENT = "database_rate_limit_storage.database_unavailable"
_REDIS_UNAVAILABLE_EVENT = "redis_rate_limit_storage.redis_unavailable"


def _events(logs: list[dict], name: str) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == name]


class TestDatabaseRateLimitProbeBehavior:
    """A bare instance is unconfigured, not broken — and knows it cheaply."""

    def test_a_bare_instance_reports_unconfigured_at_debug(self):
        storage = DatabaseRateLimitStorage()

        with capture_logs() as logs:
            available = storage.is_available()

        assert available is False
        records = _events(logs, _NOT_CONFIGURED_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == "debug"
        assert _events(logs, _DB_UNAVAILABLE_EVENT) == []

    def test_a_bare_instance_skips_the_doomed_round_trip(self):
        """``_get_repository`` raises by construction here — calling it was
        the only reason the old WARNING existed."""
        storage = DatabaseRateLimitStorage()

        with patch.object(
            DatabaseRateLimitStorage, "_get_repository", autospec=True
        ) as get_repository:
            storage.is_available()

        get_repository.assert_not_called()

    def test_a_supplied_factory_that_fails_keeps_the_warning(self):
        """Somebody wired a repository and it is broken — a real fault."""

        def _refused():
            raise RuntimeError("connection refused")

        storage = DatabaseRateLimitStorage(repository_factory=_refused)

        with capture_logs() as logs:
            available = storage.is_available()

        assert available is False
        records = _events(logs, _DB_UNAVAILABLE_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"
        assert _events(logs, _NOT_CONFIGURED_EVENT) == []

    def test_a_working_factory_reports_available_and_says_nothing(self):
        repository = MagicMock(spec=_REPOSITORY_SPEC)
        storage = DatabaseRateLimitStorage(repository_factory=lambda: repository)

        with capture_logs() as logs:
            available = storage.is_available()

        assert available is True
        assert logs == []
        repository.get_or_create.assert_called_once_with("__healthcheck__")

    @pytest.mark.parametrize(
        ("factory", "expected"),
        [(None, False), ("working", True)],
        ids=["unconfigured", "configured"],
    )
    def test_the_verdict_is_cached_after_the_first_probe(self, factory, expected):
        """Idempotency: repeated probes answer from the cache, silently."""
        repository = MagicMock(spec=_REPOSITORY_SPEC)
        storage = DatabaseRateLimitStorage(
            repository_factory=None if factory is None else (lambda: repository)
        )
        assert storage.is_available() is expected

        with capture_logs() as logs:
            assert storage.is_available() is expected
            assert storage.is_available() is expected

        assert logs == []


class TestRedisRateLimitProbeLevelBehavior:
    """The Redis adapter is auto-constructed against the default address."""

    @staticmethod
    def _storage_with_a_dead_client():
        from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage

        client = MagicMock(spec=_REDIS_CLIENT_SPEC)
        client.ping.side_effect = ConnectionError("refused")
        return RedisRateLimitStorage(client)

    @pytest.mark.parametrize(
        ("absence_expected", "expected_level"),
        [(True, "debug"), (False, "warning")],
        ids=["unconfigured_quiet", "configured_loud"],
    )
    def test_the_unavailable_level_splits_on_posture(
        self, absence_expected, expected_level
    ):
        storage = self._storage_with_a_dead_client()

        with (
            patch(
                "baldur.settings.redis.redis_absence_is_expected",
                return_value=absence_expected,
            ),
            capture_logs() as logs,
        ):
            available = storage.is_available()

        assert available is False
        records = _events(logs, _REDIS_UNAVAILABLE_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == expected_level

    def test_a_second_probe_in_fallback_mode_stays_silent_in_both_postures(self):
        """The one-per-outage latch is posture-independent."""
        storage = self._storage_with_a_dead_client()

        with patch(
            "baldur.settings.redis.redis_absence_is_expected", return_value=False
        ):
            storage.is_available()

            with capture_logs() as logs:
                storage.is_available()

        assert _events(logs, _REDIS_UNAVAILABLE_EVENT) == []


class TestRateLimitAutoDetectionProbePostureBehavior:
    """Auto-detection tells the connection factory what it is probing.

    Discovery runs on every install, so its Redis probe is the framework
    finding its own default address unreachable on a zero-config run.
    """

    @pytest.mark.parametrize(
        "absence_expected", [True, False], ids=["unconfigured", "configured"]
    )
    def test_discovery_forwards_the_posture_to_the_connection_factory(
        self, absence_expected
    ):
        from baldur.factory.adapters import discover_rate_limit_storage_adapters
        from baldur.factory.registry import ProviderRegistry

        factory = MagicMock(spec=RedisConnectionFactory)

        with (
            patch(
                "baldur.settings.redis.redis_absence_is_expected",
                return_value=absence_expected,
            ),
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                return_value=factory,
            ),
        ):
            discover_rate_limit_storage_adapters()
            ProviderRegistry.rate_limit_storage.get("redis")

        assert factory.create.call_args.kwargs["unconfigured_probe"] is absence_expected
