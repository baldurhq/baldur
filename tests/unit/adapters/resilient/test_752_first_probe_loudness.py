"""752 D2 — how loudly a failed first Redis probe is announced.

The resilient backend dials ``redis://localhost:6379/0`` by default, so a
zero-config first run always fails its first probe. That used to produce a
WARNING, a forensic shadow record of a sync nobody intended, and a CRITICAL
``degraded_mode_entered`` — the loudest line the framework emits, on a
perfectly healthy dev machine.

Loudness now splits on posture. When nobody configured a Redis (and this is
not production) all three effects quiet down; when anybody did, they are
byte-for-byte what they always were. The ``_degraded_critical_logged`` latch
is set in every posture, so once-per-outage semantics are unchanged.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.adapters.memory.shadow_logger import ShadowLogger
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.settings.redis import DEFAULT_REDIS_URL
from baldur.settings.resilient_storage import ResilientStorageSettings

_PROBE_EVENT = "resilient_storage.lazy_redis_probe_failed"
_DEGRADED_EVENT = "resilient_storage.degraded_mode_entered"
_CONFIGURED_URL = "redis://configured-host:6379/4"


def _events(logs: list[dict], name: str) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == name]


@pytest.fixture(autouse=True)
def unconfigured_redis_env(monkeypatch):
    """Zero-config posture unless a case says otherwise."""
    from baldur.settings.redis import REDIS_INTENT_ENV_VARS

    for name in REDIS_INTENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def non_production_runtime(monkeypatch):
    """``is_production`` is eager-read, so the runtime is rebuilt per case."""
    from baldur.runtime import reset_runtime

    monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
    monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
    reset_runtime()
    yield
    reset_runtime()


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
def make_backend():
    """Build a backend on a throwaway WAL dir, closed on teardown."""
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


class TestProbingUnconfiguredDefaultBehavior:
    """The predicate that decides the first probe's loudness.

    Two facts have to hold at once: this backend's own URL field was never
    supplied, and nothing anywhere else expressed Redis intent.
    """

    def test_zero_config_backend_is_probing_an_address_nobody_asked_for(
        self, make_backend
    ):
        backend = make_backend()

        assert backend.config.redis_url == DEFAULT_REDIS_URL
        assert backend._probing_unconfigured_default() is True

    def test_explicit_url_kwarg_marks_the_probe_configured(self, make_backend):
        """A construction kwarg is an operator naming this backend's Redis."""
        backend = make_backend(redis_url=_CONFIGURED_URL)

        assert backend._probing_unconfigured_default() is False

    def test_explicit_url_equal_to_the_default_is_still_configured(self, make_backend):
        """Choosing the default address on purpose is still choosing it."""
        backend = make_backend(redis_url=DEFAULT_REDIS_URL)

        assert backend._probing_unconfigured_default() is False

    def test_project_wide_env_var_marks_the_probe_configured(
        self, monkeypatch, make_backend
    ):
        """The URL resolves through the validator without marking the field."""
        monkeypatch.setenv("BALDUR_REDIS_URL", _CONFIGURED_URL)
        backend = make_backend()

        assert "redis_url" not in backend.config.model_fields_set
        assert backend._probing_unconfigured_default() is False

    def test_production_keeps_the_probe_configured_with_nothing_set(
        self, monkeypatch, make_backend
    ):
        """A prod process that skipped every gate needs this one signal."""
        from baldur.runtime import reset_runtime

        backend = make_backend()
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        reset_runtime()

        assert backend._probing_unconfigured_default() is False


class TestFirstProbeFailureLoudnessBehavior:
    """``_report_first_probe_failure`` — level, shadow record, CRITICAL.

    Driven directly rather than through ``_ensure_redis`` so each posture is
    one act with no Redis-client stubbing in the way; the wiring that feeds
    it the posture is pinned separately below.
    """

    @pytest.mark.parametrize(
        ("unconfigured", "expected_level"),
        [(True, "debug"), (False, "warning")],
        ids=["unconfigured_quiet", "configured_loud"],
    )
    def test_probe_failure_level_splits_on_posture(
        self, make_backend, unconfigured, expected_level
    ):
        backend = make_backend()

        with capture_logs() as logs:
            backend._report_first_probe_failure(ConnectionError("nope"), unconfigured)

        records = _events(logs, _PROBE_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == expected_level
        assert "nope" in records[0]["error"]

    def test_unconfigured_failure_emits_no_critical(self, make_backend):
        backend = make_backend()

        with capture_logs() as logs:
            backend._report_first_probe_failure(ConnectionError("nope"), True)

        assert _events(logs, _DEGRADED_EVENT) == []

    def test_configured_failure_still_announces_degraded_mode_at_critical(
        self, make_backend
    ):
        """The operational signal a configured deployment must not lose."""
        backend = make_backend(redis_url=_CONFIGURED_URL)

        with capture_logs() as logs:
            backend._report_first_probe_failure(ConnectionError("nope"), False)

        records = _events(logs, _DEGRADED_EVENT)
        assert len(records) == 1
        assert records[0]["log_level"] == "critical"
        assert records[0]["reason"] == "redis_unavailable"
        assert records[0]["fallback"] == "memory_wal"

    def test_allow_memory_only_suppresses_the_critical_when_configured(
        self, make_backend
    ):
        """The pre-existing suppression the posture arm generalizes."""
        backend = make_backend(
            redis_url=_CONFIGURED_URL,
            allow_memory_only=True,
        )

        with capture_logs() as logs:
            backend._report_first_probe_failure(ConnectionError("nope"), False)

        assert _events(logs, _DEGRADED_EVENT) == []
        assert _events(logs, _PROBE_EVENT)[0]["log_level"] == "warning"

    def test_unconfigured_failure_skips_the_shadow_sync_record(self, make_backend):
        """With no Redis configured there is no sync intent to have failed."""
        backend = make_backend()
        backend._shadow = MagicMock(spec=ShadowLogger)

        backend._report_first_probe_failure(ConnectionError("nope"), True)

        backend._shadow.record_sync_failure.assert_not_called()

    def test_configured_failure_records_the_shadow_sync_failure(self, make_backend):
        backend = make_backend(redis_url=_CONFIGURED_URL)
        backend._shadow = MagicMock(spec=ShadowLogger)
        error = ConnectionError("nope")

        backend._report_first_probe_failure(error, False)

        backend._shadow.record_sync_failure.assert_called_once_with(
            service_name="redis_init",
            intended_state="connected",
            error=error,
            adapter_type="redis",
        )

    def test_allow_memory_only_operator_keeps_the_shadow_record(self, make_backend):
        """They DID name a Redis — only the CRITICAL was opted out of."""
        backend = make_backend(redis_url=_CONFIGURED_URL, allow_memory_only=True)
        backend._shadow = MagicMock(spec=ShadowLogger)

        backend._report_first_probe_failure(ConnectionError("nope"), False)

        backend._shadow.record_sync_failure.assert_called_once()

    def test_a_raising_shadow_recorder_does_not_break_the_report(self, make_backend):
        """Forensics are a side effect — they may not take the probe down."""
        backend = make_backend(redis_url=_CONFIGURED_URL)
        backend._shadow = MagicMock(spec=ShadowLogger)
        backend._shadow.record_sync_failure.side_effect = RuntimeError("shadow down")

        with capture_logs() as logs:
            backend._report_first_probe_failure(ConnectionError("nope"), False)

        assert _events(logs, "resilient_storage.shadow_record_failed")
        assert _events(logs, _DEGRADED_EVENT)

    @pytest.mark.parametrize(
        "unconfigured",
        [True, False],
        ids=["unconfigured", "configured"],
    )
    def test_the_once_per_outage_latch_is_armed_in_every_posture(
        self, make_backend, unconfigured
    ):
        """Idempotency: a second failure adds only the per-probe line."""
        backend = make_backend()
        backend._shadow = MagicMock(spec=ShadowLogger)

        backend._report_first_probe_failure(ConnectionError("first"), unconfigured)
        assert backend._degraded_critical_logged is True

        with capture_logs() as logs:
            backend._report_first_probe_failure(ConnectionError("second"), unconfigured)

        assert len(_events(logs, _PROBE_EVENT)) == 1
        assert _events(logs, _DEGRADED_EVENT) == []
        assert backend._shadow.record_sync_failure.call_count == (
            0 if unconfigured else 1
        )

    def test_the_probe_cooldown_is_armed_in_every_posture(self, make_backend):
        """Quiet must not mean "retry storm" — the 30s cooldown still arms."""
        backend = make_backend()
        assert backend._next_redis_probe == 0.0

        backend._report_first_probe_failure(ConnectionError("nope"), True)

        assert backend._next_redis_probe > 0.0


class TestEnsureRedisForwardsThePostureBehavior:
    """The wiring: one posture decision, passed down to the factory.

    The adapter's log level and the backend's have to carry the same
    meaning, so the posture is evaluated once and forwarded rather than
    re-derived a second time inside the connection factory.
    """

    @pytest.mark.parametrize(
        ("configured", "expected_forwarded"),
        [(False, True), (True, False)],
        ids=["unconfigured_quiet_probe", "configured_loud_probe"],
    )
    def test_ensure_redis_forwards_the_posture_to_the_cache_adapter(
        self, monkeypatch, make_backend, configured, expected_forwarded
    ):
        # Given
        if configured:
            monkeypatch.setenv("BALDUR_REDIS_URL", _CONFIGURED_URL)
        backend = make_backend()
        adapter_cls = MagicMock(
            spec=RedisCacheAdapter, side_effect=ConnectionError("refused")
        )

        # When
        with patch("baldur.adapters.cache.RedisCacheAdapter", adapter_cls):
            connected = backend._ensure_redis()

        # Then
        assert connected is False
        assert adapter_cls.call_args.kwargs["unconfigured_probe"] is expected_forwarded

    def test_zero_config_ensure_redis_emits_nothing_above_debug(self, make_backend):
        """The end the whole decision exists for."""
        backend = make_backend()
        adapter_cls = MagicMock(
            spec=RedisCacheAdapter, side_effect=ConnectionError("refused")
        )

        with (
            patch("baldur.adapters.cache.RedisCacheAdapter", adapter_cls),
            capture_logs() as logs,
        ):
            backend._ensure_redis()

        noisy = [
            entry
            for entry in logs
            if entry.get("log_level") in {"warning", "error", "critical"}
        ]
        assert noisy == []
