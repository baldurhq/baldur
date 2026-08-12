"""
SystemHealthSummary watchdog fields + get_overall_health() watchdog integration (409 UU-E3).

Test targets:
    - SystemHealthSummary.watchdog_status / watchdog_components / watchdog_last_check
    - get_overall_health() watchdog degradation logic

Test categories:
    A. Contract: field existence and defaults
    B. Behavior: degradation logic, fail-open on watchdog unavailable
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.factory.registry import ProviderRegistry
from baldur.services.health_check import (
    HealthCheckService,
    SystemHealthSummary,
)
from baldur.settings.meta_watchdog import MetaWatchdogSettings

# 560 A7 log event names (single source of truth for assertions).
WATCHDOG_DECORATION_FAILED = "health_check.watchdog_decoration_failed"
WATCHDOG_ABSENT = "health_check.watchdog_absent"
ENABLED_BUT_UNREGISTERED = "meta_watchdog.enabled_but_unregistered"


def _count_event(
    cap_logs: list[dict], event_name: str, level: str | None = None
) -> int:
    """Count captured structlog records matching ``event_name`` (and ``level``).

    Within ``capture_logs`` the level key is ``log_level``.
    """
    return sum(
        1
        for e in cap_logs
        if e.get("event") == event_name
        and (level is None or e.get("log_level") == level)
    )


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestSystemHealthSummaryWatchdogFieldsContract:
    """409 UU-E3: Watchdog field existence and defaults."""

    def test_watchdog_status_field_exists(self):
        assert "watchdog_status" in SystemHealthSummary.__dataclass_fields__

    def test_watchdog_components_field_exists(self):
        assert "watchdog_components" in SystemHealthSummary.__dataclass_fields__

    def test_watchdog_last_check_field_exists(self):
        assert "watchdog_last_check" in SystemHealthSummary.__dataclass_fields__

    def test_watchdog_status_default_is_none(self):
        summary = SystemHealthSummary(status="healthy")
        assert summary.watchdog_status is None

    def test_watchdog_components_default_is_none(self):
        summary = SystemHealthSummary(status="healthy")
        assert summary.watchdog_components is None

    def test_watchdog_last_check_default_is_none(self):
        summary = SystemHealthSummary(status="healthy")
        assert summary.watchdog_last_check is None

    def test_watchdog_fields_accept_values(self):
        summary = SystemHealthSummary(
            status="healthy",
            watchdog_status="degraded",
            watchdog_components={"redis": "healthy", "dlq": "unhealthy"},
            watchdog_last_check="2026-04-04T10:00:00+00:00",
        )
        assert summary.watchdog_status == "degraded"
        assert summary.watchdog_components["dlq"] == "unhealthy"
        assert summary.watchdog_last_check == "2026-04-04T10:00:00+00:00"


# =============================================================================
# B. Behavior Tests
# =============================================================================


def _mock_db_healthy(service):
    """Patch check_database to return a healthy check.

    473 D2: get_overall_health() now branches on ``is_usable``, so the
    mock must expose that attribute. ``is_connected`` is kept for any
    call sites that still consult the legacy field.
    """
    mock_check = MagicMock()
    mock_check.is_connected = True
    mock_check.is_usable = True
    return mock_check


@contextmanager
def _cascade_env(service, *, watchdog, settings_enabled=None, settings=None):
    """Run ``get_overall_health()`` with a healthy DB and a controlled watchdog.

    Injecting via ``patch.object`` on the registry slot's ``safe_get`` exercises
    the 560 A7 branches deterministically, independent of suite ordering and
    instance caching (the registry caches resolved instances; a prior
    ``get_selfhealer_watchdog`` patch only worked when the slot happened to be
    pre-registered by an earlier suite module).

    Args:
        watchdog: one of
            ``("absent",)``             -> ``safe_get()`` returns ``None``
            ``("raises", exc)``         -> ``safe_get()`` raises ``exc``
            ``("registered", mock_wd)`` -> ``safe_get()`` returns ``mock_wd``
        settings_enabled: ``None`` leaves meta-watchdog settings unpatched;
            a bool patches ``get_meta_watchdog_settings().enabled`` by
            constructing the settings object with that keyword — which also
            makes the flag report as explicitly configured.
        settings: a ready-made ``MetaWatchdogSettings`` to serve instead.
            Wins over ``settings_enabled``, and is how a case expresses "the
            flag is riding its default" — the distinction the guard reads
            through ``model_fields_set`` and which keyword construction, by
            definition, cannot produce.
    """
    slot = ProviderRegistry.selfhealer_watchdog
    kind = watchdog[0]
    if kind == "absent":
        wd_patch = patch.object(slot, "safe_get", return_value=None)
    elif kind == "raises":
        wd_patch = patch.object(slot, "safe_get", side_effect=watchdog[1])
    elif kind == "registered":
        wd_patch = patch.object(slot, "safe_get", return_value=watchdog[1])
    else:  # pragma: no cover - guards against test typos
        raise ValueError(f"unknown watchdog kind: {kind}")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                service, "check_database", return_value=_mock_db_healthy(service)
            )
        )
        stack.enter_context(
            patch.object(service, "_get_circuit_breaker_count", return_value=5)
        )
        stack.enter_context(wd_patch)
        if settings is not None:
            stack.enter_context(
                patch(
                    "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                    return_value=settings,
                )
            )
        elif settings_enabled is not None:
            # A real settings object, not a stand-in: the guard reads
            # ``model_fields_set`` to tell an explicitly configured flag from
            # one riding its default, and a namespace without that attribute
            # sends the read into the guard's fail-open ``except`` — silently
            # turning every "warns once" assertion into a green zero on any
            # install where the tier term does not happen to carry the gate.
            # Kwarg construction reports ``{"enabled"}``, which is also the
            # honest encoding of what these cases assert.
            stack.enter_context(
                patch(
                    "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                    return_value=MetaWatchdogSettings(enabled=settings_enabled),
                )
            )
        yield


class TestGetOverallHealthWatchdogBehavior:
    """409 UU-E3: get_overall_health() watchdog integration behavior."""

    def _make_watchdog_state(self, overall_status, component_statuses=None):
        from baldur.meta.health_probe import HealthStatus

        status_map = {
            "healthy": HealthStatus.HEALTHY,
            "degraded": HealthStatus.DEGRADED,
            "unhealthy": HealthStatus.UNHEALTHY,
        }
        mock_state = MagicMock()
        mock_state.overall_status = status_map[overall_status]
        mock_state.component_statuses = {
            k: status_map[v] for k, v in (component_statuses or {}).items()
        }
        mock_state.last_check = datetime(2026, 4, 4, 10, 0, 0, tzinfo=UTC)
        return mock_state

    def _run_health_check(self, watchdog_state):
        """Run get_overall_health() with a registered watchdog (deterministic).

        Injects the watchdog by patching the registry slot's ``safe_get`` so the
        registered branch runs regardless of suite ordering / instance caching.
        """
        service = HealthCheckService()
        mock_wd = MagicMock()
        mock_wd.get_state.return_value = watchdog_state

        with _cascade_env(service, watchdog=("registered", mock_wd)):
            return service.get_overall_health()

    def test_healthy_watchdog_populates_fields(self):
        """Healthy watchdog populates all three fields."""
        wd_state = self._make_watchdog_state("healthy", {"redis": "healthy"})
        result = self._run_health_check(wd_state)

        assert result.watchdog_status == "healthy"
        assert result.watchdog_components == {"redis": "healthy"}
        assert result.watchdog_last_check == "2026-04-04T10:00:00+00:00"

    def test_degraded_watchdog_dampens_overall_to_degraded(self):
        """Watchdog DEGRADED → overall status dampened to 'degraded'."""
        wd_state = self._make_watchdog_state(
            "degraded", {"redis": "healthy", "dlq": "unhealthy"}
        )
        result = self._run_health_check(wd_state)

        assert result.status == "degraded"
        assert result.watchdog_status == "degraded"

    def test_unhealthy_watchdog_capped_at_degraded_when_db_healthy(self):
        """473 D7 axis 2 (a): when DB is healthy but watchdog is UNHEALTHY,
        overall caps at 'degraded' — only is_usable=False can drive the
        cascade to 'unhealthy'. Decouples LB depool from self-monitor noise.
        """
        wd_state = self._make_watchdog_state("unhealthy")
        result = self._run_health_check(wd_state)

        assert result.status == "degraded"
        assert result.watchdog_status == "unhealthy"

    def test_dampening_fires_when_optional_field_hydration_raises(self):
        """473 D5: dampening must run before optional decoration so a
        component_statuses.items() failure (or any subsequent hydration
        line) cannot bypass the watchdog signal.
        """
        from baldur.meta.health_probe import HealthStatus

        mock_state = MagicMock()
        mock_state.overall_status = HealthStatus.UNHEALTHY
        # Simulate the documented "watchdog_components: null" failure mode:
        # component_statuses.items() raises mid-hydration.
        broken_components = MagicMock()
        broken_components.items.side_effect = RuntimeError("hydration boom")
        mock_state.component_statuses = broken_components

        result = self._run_health_check(mock_state)

        # Dampening fired despite the hydration failure.
        assert result.status == "degraded"
        # Hydration was bypassed → optional fields stay None.
        assert result.watchdog_components is None
        assert result.watchdog_last_check is None
        # The watchdog_status itself was captured before the hydration line
        # that raised.
        assert result.watchdog_status == "unhealthy"

    def test_healthy_watchdog_does_not_change_overall_status(self):
        """Healthy watchdog does not degrade overall status."""
        wd_state = self._make_watchdog_state("healthy")
        result = self._run_health_check(wd_state)

        assert result.status == "healthy"

    def test_watchdog_unavailable_fields_are_none(self):
        """Watchdog resolution raising → fields remain None (fail-open)."""
        service = HealthCheckService()

        with _cascade_env(service, watchdog=("raises", ImportError("no meta"))):
            result = service.get_overall_health()

        assert result.watchdog_status is None
        assert result.watchdog_components is None
        assert result.watchdog_last_check is None
        assert result.status == "healthy"

    def test_audit_system_entry_decoration_succeeds_after_475_fix(self):
        """475 fix: an audit_system entry from AuditSystemProbe must decode
        cleanly through the ``v.value`` consumer at health_check.py:409.

        Cat 1.10 F2 regression: pre-fix, AuditSystemProbe.STATUS_UNHEALTHY
        was a raw ``"unhealthy"`` string. health_check.py:408-409 builds
        ``{k: v.value for k, v in component_statuses.items()}`` and the
        raw string entry raised AttributeError on ``.value`` →
        ``watchdog_components: null`` in the response body even when
        dampening fired correctly.

        This test mocks the watchdog state to include the post-475
        AuditSystemProbe.STATUS_UNHEALTHY enum directly (not a raw
        string) and verifies the decoration succeeds end-to-end.
        """
        from baldur.meta.audit_probe import AuditSystemProbe

        wd_state = self._make_watchdog_state(
            "unhealthy",
            {"redis": "healthy"},
        )
        # Directly inject what AuditSystemProbe now returns post-475:
        # a HealthStatus enum, not a raw string.
        wd_state.component_statuses["audit_system"] = AuditSystemProbe.STATUS_UNHEALTHY

        result = self._run_health_check(wd_state)

        # Decoration must succeed for ALL keys including audit_system.
        assert result.watchdog_components is not None, (
            "475 fix: hydration must not fail with AuditSystemProbe entry"
        )
        assert result.watchdog_components.get("audit_system") == "unhealthy"
        assert result.watchdog_components.get("redis") == "healthy"


# =============================================================================
# C. 560 — A7 branch split: absent (quiet) vs resolve-failure vs registered-failed
# =============================================================================


class TestWatchdogAbsentBranchBehavior:
    """560: an absent watchdog (every OSS deploy, every PRO without an active
    entitlement) is the normal state — DEBUG only, never a WARNING + traceback
    on the hot probe path. Settings disabled here to isolate the absent branch
    from the enabled-but-unregistered guard (covered separately).
    """

    def test_absent_watchdog_leaves_all_fields_none(self):
        """No registered watchdog → all watchdog_* fields stay None (fail-open)."""
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("absent",), settings_enabled=False):
            result = service.get_overall_health()

        assert result.watchdog_status is None
        assert result.watchdog_components is None
        assert result.watchdog_last_check is None

    def test_absent_watchdog_does_not_change_overall_status(self):
        """An absent watchdog contributes no dampening signal."""
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("absent",), settings_enabled=False):
            result = service.get_overall_health()

        assert result.status == "healthy"

    def test_absent_watchdog_does_not_emit_decoration_failed_warning(self):
        """560 core fix: absence must NOT emit the decoration-failed WARNING
        (no per-probe traceback spam for an expected condition).
        """
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("absent",), settings_enabled=False):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, WATCHDOG_DECORATION_FAILED) == 0

    def test_absent_watchdog_records_debug_event(self):
        """The absent branch records ``health_check.watchdog_absent`` at DEBUG."""
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("absent",), settings_enabled=False):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, WATCHDOG_ABSENT, "debug") == 1


class TestWatchdogResolveFailureBehavior:
    """560 item 2: ``safe_get()`` raising a non-AdapterNotFoundError (e.g. a
    registered callable provider failing to instantiate) is a genuine error,
    not absence — WARNING + fail-open, and NOT misclassified into the absent
    branch (the ``try/except/else`` ``else`` runs only on a clean resolve).
    """

    def test_resolve_failure_emits_decoration_failed_warning_once(self):
        """A resolve exception logs exactly one decoration-failed WARNING."""
        service = HealthCheckService()
        exc = RuntimeError("provider instantiation failed")
        with _cascade_env(service, watchdog=("raises", exc)):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, WATCHDOG_DECORATION_FAILED, "warning") == 1

    def test_resolve_failure_does_not_take_absent_branch(self):
        """With settings enabled, a resolve failure must emit neither the
        absent DEBUG nor the enabled-but-unregistered guard WARNING — proving
        the failure is not misclassified as absence.
        """
        service = HealthCheckService()
        exc = RuntimeError("provider instantiation failed")
        with _cascade_env(service, watchdog=("raises", exc), settings_enabled=True):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, WATCHDOG_ABSENT) == 0
        assert _count_event(cap_logs, ENABLED_BUT_UNREGISTERED) == 0

    def test_resolve_failure_fails_open(self):
        """The cascade still returns; watchdog_* None; overall stays healthy."""
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("raises", RuntimeError("boom"))):
            result = service.get_overall_health()

        assert result.watchdog_status is None
        assert result.status == "healthy"


class TestWatchdogRegisteredStateReadFailureBehavior:
    """560: a *registered* watchdog whose ``get_state()`` raises is genuinely
    warn-worthy — WARNING once, fail-open. This is the only case the WARNING
    is reserved for (alongside resolve failure).
    """

    def _failing_watchdog(self):
        mock_wd = MagicMock()
        mock_wd.get_state.side_effect = RuntimeError("state read failed")
        return mock_wd

    def test_state_read_failure_emits_decoration_failed_warning_once(self):
        """A registered watchdog's get_state() raising → one WARNING, fail-open."""
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("registered", self._failing_watchdog())):
            with capture_logs() as cap_logs:
                result = service.get_overall_health()

        assert _count_event(cap_logs, WATCHDOG_DECORATION_FAILED, "warning") == 1
        assert result.watchdog_status is None
        assert result.status == "healthy"

    def test_state_read_failure_does_not_emit_absent_or_guard(self):
        """A registered-but-failed watchdog is not the absent branch."""
        service = HealthCheckService()
        with _cascade_env(
            service,
            watchdog=("registered", self._failing_watchdog()),
            settings_enabled=True,
        ):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, WATCHDOG_ABSENT) == 0
        assert _count_event(cap_logs, ENABLED_BUT_UNREGISTERED) == 0


class TestWatchdogEnabledButUnregisteredGuardBehavior:
    """560 item 3/4: the absorbed one-time latched guard. 558 made
    ``meta_watchdog.enabled`` default True, so configured-on-but-unregistered
    is a meaningful misconfiguration — surfaced exactly once per service
    instance, never per-probe.

    Every case here runs with the PRO-wheel probe pinned absent. The gate is
    a disjunction — an explicitly configured flag OR a distribution that
    could register a provider — so a tree that happens to have the PRO wheel
    installed alongside it would carry these assertions on the second term
    and report green for a reason the install this file describes does not
    have. Pinning it makes the cases assert what they claim: the
    *explicitly configured* flag is what warrants the warning.
    """

    @pytest.fixture(autouse=True)
    def pro_absent(self):
        with patch("baldur.utils.tier.is_pro_installed", return_value=False):
            yield

    def test_first_probe_emits_guard_warning_once(self):
        """enabled=True + unregistered → one enabled_but_unregistered WARNING."""
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("absent",), settings_enabled=True):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, ENABLED_BUT_UNREGISTERED, "warning") == 1

    def test_second_probe_does_not_re_emit_guard_warning(self):
        """560 item 4: the latch holds across probes on the same instance —
        WARNING fires once, the second probe is silent (no per-probe spam).
        """
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("absent",), settings_enabled=True):
            with capture_logs() as cap_first:
                service.get_overall_health()
            with capture_logs() as cap_second:
                service.get_overall_health()

        assert _count_event(cap_first, ENABLED_BUT_UNREGISTERED, "warning") == 1
        assert _count_event(cap_second, ENABLED_BUT_UNREGISTERED, "warning") == 0

    def test_disabled_watchdog_does_not_emit_guard_warning(self):
        """enabled=False → the guard stays silent even when unregistered."""
        service = HealthCheckService()
        with _cascade_env(service, watchdog=("absent",), settings_enabled=False):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, ENABLED_BUT_UNREGISTERED) == 0

    def test_settings_resolve_failure_does_not_emit_guard_or_crash(self):
        """The guard's settings read is fail-open: a resolve failure neither
        warns nor breaks the cascade.
        """
        service = HealthCheckService()
        slot = ProviderRegistry.selfhealer_watchdog
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    service, "check_database", return_value=_mock_db_healthy(service)
                )
            )
            stack.enter_context(
                patch.object(service, "_get_circuit_breaker_count", return_value=5)
            )
            stack.enter_context(patch.object(slot, "safe_get", return_value=None))
            stack.enter_context(
                patch(
                    "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                    side_effect=RuntimeError("settings boom"),
                )
            )
            with capture_logs() as cap_logs:
                result = service.get_overall_health()

        assert _count_event(cap_logs, ENABLED_BUT_UNREGISTERED) == 0
        assert result.status == "healthy"

    def test_fresh_service_instance_re_arms_guard_latch(self):
        """The latch is per-instance: a new service emits the WARNING once more."""
        first = HealthCheckService()
        with _cascade_env(first, watchdog=("absent",), settings_enabled=True):
            with capture_logs() as cap_first:
                first.get_overall_health()

        second = HealthCheckService()
        with _cascade_env(second, watchdog=("absent",), settings_enabled=True):
            with capture_logs() as cap_second:
                second.get_overall_health()

        assert _count_event(cap_first, ENABLED_BUT_UNREGISTERED, "warning") == 1
        assert _count_event(cap_second, ENABLED_BUT_UNREGISTERED, "warning") == 1


# =============================================================================
# D. 753 — the warn gate: who is entitled to be told about the gap
# =============================================================================


class TestWatchdogWarnGateBehavior:
    """The flag defaults on, so "enabled but unregistered" is two states.

    On an install where a provider could arrive — the PRO distribution is
    present — configured-on and unregistered is the entitlement/wiring gap
    the warning was built for. On an install where none can arrive and the
    flag is merely riding its default, the same state is the permanent
    designed posture, and announcing it once per boot tells an operator that
    something they never asked for has failed.

    The gate adds the two terms the caller has not already established (it
    reached the helper through the ``wd is None`` branch, so "unregistered"
    is given): the flag on, AND either an explicitly configured flag or a
    distribution that could satisfy it. Each case below is a fresh service —
    both the warn latch and the memoized tier probe live on the instance.
    """

    @staticmethod
    def _default_on_settings(monkeypatch) -> MetaWatchdogSettings:
        """Settings whose ``enabled`` is riding its default, not configured.

        Built after clearing the variable so the object reports an empty
        ``model_fields_set`` — the environment this process runs in must not
        decide which side of the gate the case lands on.
        """
        monkeypatch.delenv("BALDUR_META_WATCHDOG_ENABLED", raising=False)
        settings = MetaWatchdogSettings()
        assert settings.enabled is True
        assert settings.model_fields_set == set()
        return settings

    def _warnings_for(self, *, pro_installed, settings=None, settings_enabled=None):
        service = HealthCheckService()
        with (
            patch("baldur.utils.tier.is_pro_installed", return_value=pro_installed),
            _cascade_env(
                service,
                watchdog=("absent",),
                settings=settings,
                settings_enabled=settings_enabled,
            ),
        ):
            with capture_logs() as cap_logs:
                service.get_overall_health()
        return _count_event(cap_logs, ENABLED_BUT_UNREGISTERED, "warning")

    def test_default_on_without_the_distribution_stays_silent(self, monkeypatch):
        """The permanent state of every install that installed only the core:
        nothing is misconfigured, so nothing is announced."""
        settings = self._default_on_settings(monkeypatch)

        assert self._warnings_for(pro_installed=False, settings=settings) == 0

    def test_default_on_with_the_distribution_present_warns_once(self, monkeypatch):
        """A provider could arrive here and has not — the gap the warning
        exists for, and the case the silence above must not swallow."""
        settings = self._default_on_settings(monkeypatch)

        assert self._warnings_for(pro_installed=True, settings=settings) == 1

    def test_an_explicitly_configured_flag_warns_even_without_the_distribution(self):
        """Intent that can never be satisfied here: silence would strand an
        operator who set the variable and is waiting for a watchdog."""
        assert self._warnings_for(pro_installed=False, settings_enabled=True) == 1

    def test_an_explicitly_disabled_flag_stays_silent_with_the_distribution(self):
        """The flag still comes first — an operator who turned it off is not
        told about a provider they declined."""
        assert self._warnings_for(pro_installed=True, settings_enabled=False) == 0

    def test_a_registered_watchdog_never_reaches_the_gate(self):
        """The precondition the caller owns: a satisfied install is decided
        before the helper runs, whatever the flag and the tier say."""
        from baldur.interfaces.meta_watchdog import SelfhealerWatchdog

        service = HealthCheckService()
        watchdog = MagicMock(spec=SelfhealerWatchdog)
        watchdog.get_state.side_effect = RuntimeError("state read failed")

        with (
            patch("baldur.utils.tier.is_pro_installed", return_value=True),
            _cascade_env(
                service, watchdog=("registered", watchdog), settings_enabled=True
            ),
        ):
            with capture_logs() as cap_logs:
                service.get_overall_health()

        assert _count_event(cap_logs, ENABLED_BUT_UNREGISTERED) == 0

    def test_the_tier_probe_is_resolved_once_per_service(self, monkeypatch):
        """On the silent posture the latch never closes, so the gate is
        re-evaluated on every health probe — the refresh pass *and* every
        uncached health request. An unmemoized probe would walk ``sys.path``
        at that cadence for the life of the process.
        """
        settings = self._default_on_settings(monkeypatch)
        service = HealthCheckService()

        with (
            patch("baldur.utils.tier.is_pro_installed", return_value=False) as probe,
            _cascade_env(service, watchdog=("absent",), settings=settings),
        ):
            service.get_overall_health()
            service.get_overall_health()

        assert probe.call_count == 1

    def test_a_settings_object_without_the_pydantic_field_set_stays_silent(self):
        """The fail-open exit the guard's own ``except`` owns, named.

        Reading ``model_fields_set`` off a stand-in that has no such
        attribute lands here — which is why the fixtures in this file build
        real settings objects. A test double that took this exit would count
        zero warnings while claiming to assert one.
        """
        from types import SimpleNamespace

        assert (
            self._warnings_for(
                pro_installed=True, settings=SimpleNamespace(enabled=True)
            )
            == 0
        )


class TestMetaWatchdogSettingsContract:
    """The pydantic-settings contract the gate's "explicitly set" term reads.

    ``model_fields_set`` is what separates "the operator asked for this" from
    "this is the shipped default". If a pydantic-settings upgrade stopped
    populating it from the environment, the gate would silently collapse to
    its tier term and the explicit-intent case would go quiet — a regression
    with no other witness in the suite.
    """

    def test_an_env_configured_flag_is_reported_as_set(self, monkeypatch):
        monkeypatch.setenv("BALDUR_META_WATCHDOG_ENABLED", "true")

        assert "enabled" in MetaWatchdogSettings().model_fields_set

    def test_a_defaulted_flag_is_not_reported_as_set(self, monkeypatch):
        monkeypatch.delenv("BALDUR_META_WATCHDOG_ENABLED", raising=False)

        assert MetaWatchdogSettings().model_fields_set == set()

    def test_a_keyword_constructed_flag_is_reported_as_set(self):
        """How the test doubles in this file express "explicitly configured"."""
        assert MetaWatchdogSettings(enabled=True).model_fields_set == {"enabled"}
