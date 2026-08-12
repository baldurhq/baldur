"""752 D7 — one line that says what this install is actually running on.

A zero-config process makes several capability decisions silently: memory
instead of Redis, no metrics backend, no ``init()``. They used to be
inferable only from a scatter of warnings — and once those warnings are
correctly demoted, nothing announces them at all. One INFO line now owns the
announcement, emitted by whichever entry point runs first.

``get_runtime_posture()`` is the single derivation behind both that line and
the startup report's two capability keys, so the short announcement and the
long report cannot disagree about the same process.

This module sits at the top level following the ``test_bootstrap_*``
convention — ``baldur.bootstrap`` is a top-level module with no parent
package.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur import bootstrap
from baldur.bootstrap import (
    emit_runtime_posture_once,
    get_runtime_posture,
    reset_runtime_posture,
)
from baldur.observability.structlog_config import POSTURE_LOGGER_NAME

_POSTURE_EVENT = "baldur.runtime_posture"
_A_REDIS_URL = "redis://configured-host:6379/5"


def _posture_records(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == _POSTURE_EVENT]


@pytest.fixture(autouse=True)
def unconfigured_and_unannounced(monkeypatch):
    """Zero-config posture, latch re-armed, on both sides of each case."""
    from baldur.settings.redis import REDIS_INTENT_ENV_VARS

    for name in REDIS_INTENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    reset_runtime_posture()
    yield
    reset_runtime_posture()


class TestRuntimePostureContract:
    """The derived description: what, and why not, for storage and metrics."""

    def test_zero_config_reports_memory_storage_with_a_reason_and_a_hint(self):
        posture = get_runtime_posture()

        assert posture["storage"] == "memory"
        assert posture["storage_reason"] == "redis_not_configured"
        assert "BALDUR_REDIS_URL" in posture["storage_hint"]

    def test_a_configured_redis_reports_redis_with_no_reason(self, monkeypatch):
        """Nothing to explain when the operator already chose."""
        monkeypatch.setenv("BALDUR_REDIS_URL", _A_REDIS_URL)

        posture = get_runtime_posture()

        assert posture["storage"] == "redis"
        assert "storage_reason" not in posture
        assert "storage_hint" not in posture

    def test_storage_is_derived_from_configuration_not_from_backend_state(self):
        """The resilient backend constructs DEGRADED and connects lazily, so
        its own mode would read "memory" even on a healthy configured host."""
        from baldur.settings.redis import redis_explicitly_configured

        with patch(
            "baldur.settings.redis.redis_explicitly_configured", return_value=True
        ):
            assert get_runtime_posture()["storage"] == "redis"

        assert redis_explicitly_configured() is False

    def test_prometheus_present_reports_the_backend_with_no_reason(self):
        from baldur.metrics.registry import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed in this environment")

        posture = get_runtime_posture()

        assert posture["metrics"] == "prometheus"
        assert "metrics_reason" not in posture
        assert "metrics_hint" not in posture

    def test_absent_extra_reports_disabled_with_the_install_hint(self):
        from baldur.metrics.registry import PROMETHEUS_INSTALL_HINT

        with patch("baldur.metrics.registry.PROMETHEUS_AVAILABLE", False):
            posture = get_runtime_posture()

        assert posture["metrics"] == "disabled"
        assert posture["metrics_reason"] == "prometheus_extra_not_installed"
        assert posture["metrics_hint"] == PROMETHEUS_INSTALL_HINT

    def test_an_unimportable_metrics_registry_reports_its_own_reason(self):
        """Distinguishable from a merely-absent extra, and never raises."""
        with patch.dict(sys.modules, {"baldur.metrics.registry": None}):
            posture = get_runtime_posture()

        assert posture["metrics"] == "disabled"
        assert posture["metrics_reason"] == "metrics_registry_unavailable"
        assert "metrics_hint" not in posture

    @pytest.mark.parametrize(
        "init_done", [False, True], ids=["decorator_only", "after_init"]
    )
    def test_init_called_reflects_the_bootstrap_flag(self, monkeypatch, init_done):
        monkeypatch.setattr(bootstrap, "_init_done", init_done)

        assert get_runtime_posture()["init_called"] is init_done

    def test_the_derivation_is_pure(self):
        """No latch, no logging — the emitter owns both."""
        with capture_logs() as logs:
            first = get_runtime_posture()
            second = get_runtime_posture()

        assert logs == []
        assert first == second
        assert bootstrap._posture_emitted is False

    def test_the_startup_report_carries_the_same_two_answers(self):
        """One derivation behind both, so they cannot drift apart."""
        from baldur.bootstrap import ExtensionResult, _build_startup_report

        posture = get_runtime_posture()
        report = _build_startup_report(ExtensionResult(found=0, executed=0, failed=0))

        assert report["storage_backend"] == posture["storage"]
        assert report["metrics_backend"] == posture["metrics"]


class TestRuntimePostureLatchBehavior:
    """Exactly once per process, whichever entry point gets there first."""

    def test_the_first_call_announces_and_the_second_says_nothing(self):
        with capture_logs() as logs:
            emit_runtime_posture_once()
            emit_runtime_posture_once()

        assert len(_posture_records(logs)) == 1

    def test_the_announcement_carries_the_derived_posture(self):
        expected = get_runtime_posture()

        with capture_logs() as logs:
            emit_runtime_posture_once()

        record = _posture_records(logs)[0]
        assert record["log_level"] == "info"
        for key, value in expected.items():
            assert record[key] == value

    def test_the_announcement_goes_to_the_logger_that_carries_the_info_floor(self):
        """The root level defaults to WARNING, so the line is invisible on
        any other logger — the name is what makes it reach a handler."""
        import structlog

        with patch(
            "structlog.get_logger", wraps=structlog.get_logger
        ) as get_logger_spy:
            emit_runtime_posture_once()

        get_logger_spy.assert_called_once_with(POSTURE_LOGGER_NAME)

    def test_reset_re_arms_the_announcement(self):
        """The seam every other case in this file depends on."""
        emit_runtime_posture_once()
        reset_runtime_posture()

        with capture_logs() as logs:
            emit_runtime_posture_once()

        assert len(_posture_records(logs)) == 1

    def test_simultaneous_first_calls_still_announce_exactly_once(self):
        """The guarantee rests on the lock, not on the bool — a bare flag
        lets two threads through the check before either sets it."""
        thread_count = 12
        start = threading.Barrier(thread_count)
        errors: list[BaseException] = []

        def race() -> None:
            try:
                start.wait(timeout=10)
                emit_runtime_posture_once()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with capture_logs() as logs:
            threads = [
                threading.Thread(target=race, name=f"posture-{index}")
                for index in range(thread_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        assert errors == []
        assert len(_posture_records(logs)) == 1


@pytest.fixture
def structlog_already_configured():
    """Keep ``capture_logs`` installed across a real protect() call.

    The prelude's first act is ``configure_structlog()``, which replaces the
    whole processor chain — including the capture processor — so a capture
    opened before the call comes back empty even though the line was
    emitted. Declaring the configuration already done makes the prelude take
    its production idempotent path instead.
    """
    from baldur.observability.structlog_config import _structlog_state

    state = _structlog_state()
    previous = state.configured
    state.configured = True
    yield
    state.configured = previous


class TestPostureAnnouncedOnEveryProtectSurfaceBehavior:
    """Every documented protected-call surface announces, and only once.

    Two of the four sync/async entry points originally missed the shared
    prelude; the decorators route through them, so all seven are pinned
    here rather than a representative pair.
    """

    @staticmethod
    def _sync_surfaces():
        from baldur.decorators.dlq_protect import dlq_protect
        from baldur.protect_facade import (
            aprotect,
            aprotect_with_meta,
            aprotected,
            protect,
            protect_with_meta,
            protected,
        )

        def _ok() -> int:
            return 7

        async def _aok() -> int:
            return 7

        def _run_aprotect():
            return asyncio.run(aprotect("d752", _aok))

        def _run_aprotect_with_meta():
            return asyncio.run(aprotect_with_meta("d752", _aok))

        def _run_protected():
            return protected("d752")(_ok)()

        def _run_aprotected():
            return asyncio.run(aprotected("d752")(_aok)())

        def _run_dlq_protect():
            return dlq_protect("d752")(_ok)()

        return {
            "protect": lambda: protect("d752", _ok),
            "protect_with_meta": lambda: protect_with_meta("d752", _ok),
            "aprotect": _run_aprotect,
            "aprotect_with_meta": _run_aprotect_with_meta,
            "protected": _run_protected,
            "aprotected": _run_aprotected,
            "dlq_protect": _run_dlq_protect,
        }

    @pytest.mark.parametrize(
        "surface",
        [
            "protect",
            "protect_with_meta",
            "aprotect",
            "aprotect_with_meta",
            "protected",
            "aprotected",
            "dlq_protect",
        ],
    )
    def test_each_surface_announces_the_posture_exactly_once(
        self, surface, structlog_already_configured
    ):
        call = self._sync_surfaces()[surface]

        with capture_logs() as logs:
            call()
            call()

        assert len(_posture_records(logs)) == 1

    def test_a_deployment_with_protection_disabled_announces_nothing(self, monkeypatch):
        """Telling an operator which backend the protection they turned off
        would have used is noise, not posture."""
        from baldur.protect_facade import protect
        from baldur.settings.protect import reset_protect_settings

        monkeypatch.setenv("BALDUR_PROTECT_ENABLED", "false")
        reset_protect_settings()
        try:
            with capture_logs() as logs:
                assert protect("d752", lambda: 7) == 7
        finally:
            reset_protect_settings()

        assert _posture_records(logs) == []

    def test_the_prelude_is_the_only_logging_configuration_site(self):
        """The mechanized backstop for a future fifth entry point: the
        prelude owns the sequence, so nothing may configure logging beside
        it."""
        from pathlib import Path

        import baldur.protect_facade as facade

        source = Path(facade.__file__).read_text(encoding="utf-8")

        assert source.count("configure_structlog()") == 1


class TestRuntimePostureStatisticsBehavior:
    """753 D4 — the posture line answers what the demoted warning used to.

    Dropping the null statistics repository's announcement to DEBUG removes
    the only thing that told an operator no statistics adapter was
    registered. The answer moves to the line that already describes what this
    install is running on, so the absence is reported where someone looks for
    it rather than where it happened to be constructed.

    Deriving it must not construct anything: the registry's fallback builds a
    fresh Null repository on every call when the slot is empty, and that
    constructor flips a once-per-process latch and emits — inside a function
    documented as pure, which the startup report calls a second time before
    the posture line is even emitted.
    """

    @pytest.fixture(autouse=True)
    def restored_statistics_slot(self):
        """The slot is a class attribute; leave it as found."""
        from baldur.factory.registry import ProviderRegistry

        previous = ProviderRegistry._statistics_adapter
        ProviderRegistry._statistics_adapter = None
        yield
        ProviderRegistry._statistics_adapter = previous

    def test_an_unregistered_slot_reports_none(self):
        assert get_runtime_posture()["statistics"] == "none"

    def test_a_registered_adapter_is_reported_by_class_name(self):
        """What a host application that registered one actually sees."""
        from baldur.adapters.statistics.null import NullStatisticsRepository
        from baldur.factory.registry import ProviderRegistry

        class _HostAppStatisticsAdapter(NullStatisticsRepository):
            def __init__(self) -> None:
                """Stand in for a host adapter, minus the null announcement."""

        ProviderRegistry._statistics_adapter = _HostAppStatisticsAdapter()

        assert get_runtime_posture()["statistics"] == "_HostAppStatisticsAdapter"

    def test_the_unregistered_branch_does_not_construct_the_null_repository(self):
        """The purity negative: reading the slot, never resolving it.

        A construct-to-inspect implementation would answer correctly and
        still be wrong — it would move a latch flip and a log record into
        every ``init()``, ahead of the line this field belongs to.
        """
        from baldur.adapters.statistics.null import NullStatisticsRepository

        previous = NullStatisticsRepository._warned
        NullStatisticsRepository._warned = False
        try:
            with capture_logs() as logs:
                get_runtime_posture()

            assert NullStatisticsRepository._warned is False
            assert logs == []
        finally:
            NullStatisticsRepository._warned = previous

    def test_the_announced_line_carries_the_field(self):
        """Operator-facing or it does not count: the derivation is only half
        the claim, the announcement is the other."""
        with capture_logs() as logs:
            emit_runtime_posture_once()

        assert _posture_records(logs)[0]["statistics"] == "none"
