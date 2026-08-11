"""752 D3 — the connection factory's failure level is the caller's call.

A creation failure used to be an unconditional ERROR with a full traceback.
On a clean install the ``redis`` package is itself an optional extra, so the
very first protected call of a zero-config run printed a traceback for a
``ModuleNotFoundError`` nobody could act on.

``unconfigured_probe`` lets a caller that KNOWS it is dialing a default
address nobody configured ask for a quiet DEBUG line instead. It defaults to
False, so every caller that does not know stays loud — including one dialing
the very same default URL on purpose.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.redis.connection_factory import RedisConnectionFactory
from baldur.settings.redis import DEFAULT_REDIS_URL, RedisSettings

_FAILURE_EVENT = "redis_factory.connection_failed"


def _events(logs: list[dict], name: str) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == name]


@pytest.fixture
def factory():
    return RedisConnectionFactory(settings=RedisSettings())


@pytest.fixture
def failing_standalone():
    """Make the standalone client constructor raise, as an absent extra does."""
    error = ModuleNotFoundError("No module named 'redis'")
    with patch.object(
        RedisConnectionFactory, "_create_standalone", side_effect=error
    ) as patched:
        yield patched


class TestConnectionFactoryProbeLevelBehavior:
    """Failure level, traceback presence, and the unchanged re-raise."""

    def test_omitting_the_keyword_keeps_the_loud_error_with_a_traceback(
        self, factory, failing_standalone
    ):
        """Default-is-loud: an uninformed caller loses no signal."""
        with capture_logs() as logs, pytest.raises(ModuleNotFoundError):
            factory.create("redis://some-host:6379/0")

        records = _events(logs, _FAILURE_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == "error"
        assert records[0]["exc_info"] is True

    def test_default_url_dialed_by_an_uninformed_caller_is_still_loud(
        self, factory, failing_standalone
    ):
        """The URL is not the signal — the caller's knowledge is.

        Somebody who configured Redis at exactly the default address, and
        anybody dialing it through a feature-local field that fell back to
        it, must keep the traceback.
        """
        with capture_logs() as logs, pytest.raises(ModuleNotFoundError):
            factory.create(DEFAULT_REDIS_URL)

        assert _events(logs, _FAILURE_EVENT)[0]["log_level"] == "error"

    def test_an_unconfigured_probe_reports_at_debug_without_a_traceback(
        self, factory, failing_standalone
    ):
        """The zero-config posture: one quiet line, no stack."""
        with capture_logs() as logs, pytest.raises(ModuleNotFoundError):
            factory.create(DEFAULT_REDIS_URL, unconfigured_probe=True)

        records = _events(logs, _FAILURE_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == "debug"
        assert "exc_info" not in records[0]

    @pytest.mark.parametrize(
        "unconfigured_probe",
        [True, False],
        ids=["quiet_probe", "loud_probe"],
    )
    def test_the_exception_is_re_raised_in_both_arms(
        self, factory, failing_standalone, unconfigured_probe
    ):
        """Only the log level changes — never the control flow."""
        with pytest.raises(ModuleNotFoundError, match="No module named 'redis'"):
            factory.create(DEFAULT_REDIS_URL, unconfigured_probe=unconfigured_probe)

    @pytest.mark.parametrize(
        ("unconfigured_probe", "expected_level"),
        [(True, "debug"), (False, "error")],
        ids=["quiet_probe", "loud_probe"],
    )
    def test_both_arms_report_the_masked_url_and_the_error_type(
        self, factory, failing_standalone, unconfigured_probe, expected_level
    ):
        """Quiet is not blind: the diagnostic payload survives the demotion."""
        with capture_logs() as logs, pytest.raises(ModuleNotFoundError):
            factory.create(
                "redis://user:secret@some-host:6379/0",
                unconfigured_probe=unconfigured_probe,
            )

        record = _events(logs, _FAILURE_EVENT)[0]
        assert record["log_level"] == expected_level
        assert record["error_type"] == "ModuleNotFoundError"
        assert "secret" not in record["url"]

    def test_the_quiet_arm_carries_the_error_message_the_traceback_would_have(
        self, factory, failing_standalone
    ):
        """Losing the stack must not mean losing what went wrong."""
        with capture_logs() as logs, pytest.raises(ModuleNotFoundError):
            factory.create(DEFAULT_REDIS_URL, unconfigured_probe=True)

        assert "No module named 'redis'" in _events(logs, _FAILURE_EVENT)[0]["error"]


class TestCacheAdapterForwardsTheProbePostureBehavior:
    """``RedisCacheAdapter`` is the seam the resilient backend probes through."""

    @pytest.mark.parametrize(
        ("kwargs", "expected_forwarded"),
        [({}, False), ({"unconfigured_probe": True}, True)],
        ids=["default_is_loud", "explicit_quiet"],
    )
    def test_adapter_forwards_the_posture_to_the_factory(
        self, kwargs, expected_forwarded
    ):
        from unittest.mock import MagicMock

        from baldur.adapters.cache.redis_adapter import RedisCacheAdapter

        factory = MagicMock(spec=RedisConnectionFactory)
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            RedisCacheAdapter(url=DEFAULT_REDIS_URL, **kwargs)

        assert (
            factory.create.call_args.kwargs["unconfigured_probe"] is expected_forwarded
        )


class TestEventBusKeepsTheLoudDefaultBehavior:
    """The regression the ``unconfigured_probe`` default exists to prevent.

    With ``BALDUR_EVENT_BUS_BACKEND=redis`` and no URL set anywhere, the
    event bus resolves the shipped default address — the same URL the quiet
    posture uses. It never asked for the quiet arm, and an operator who
    turned the Redis bus on and got no bus needs the traceback.
    """

    def test_event_bus_connect_failure_stays_at_error_with_a_traceback(
        self, monkeypatch, failing_standalone
    ):
        # Given an event bus with no URL configured anywhere
        from baldur.services.event_bus.redis_bus import RedisEventBus
        from baldur.settings.event_bus import reset_event_bus_settings
        from baldur.settings.redis import reset_redis_settings

        monkeypatch.delenv("BALDUR_EVENT_BUS_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        reset_event_bus_settings()
        reset_redis_settings()

        bus = object.__new__(RedisEventBus)
        bus._redis_client = None

        # When it tries to connect
        with capture_logs() as logs:
            connected = bus._connect_redis()

        # Then the factory kept its traceback for the default address
        assert connected is False
        records = _events(logs, _FAILURE_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == "error"
        assert records[0]["exc_info"] is True
        assert failing_standalone.call_args.args[0] == DEFAULT_REDIS_URL
