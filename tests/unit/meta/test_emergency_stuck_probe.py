"""EmergencyStuckProbe unit tests (638 D6).

The emergency semantic-stuck probe runs a time-clock on the emergency manager's
recovery/activation timestamps and reports UNHEALTHY when:
- recovery is wedged (``is_recovering`` + ``recovery_started_at`` older than the
  threshold) — the primary, flagship case;
- an auto-triggered, non-recovering level has overstayed and its expiry lapsed —
  the secondary backstop.

Operator-held levels (``is_auto_triggered=False``, not recovering) are excluded
(intentional incident response, not a wedged healer). The level is compared only
with ``!= EmergencyLevel.NORMAL`` (equality), never the rich ``</>`` comparators,
so no ``EmergencyLevel``-vs-int ``TypeError`` is possible.

OSS-only: the probe duck-types ``EmergencyState`` (``EmergencyLevel`` is OSS), so
these tests fake the state without importing baldur_pro.

Covers:
- _age_seconds() boundary (None / empty / malformed → None; valid → correct)
- _evaluate() equivalence partitioning over level × {recovering, auto-held,
  operator-held, NORMAL}, threshold boundary, enum-not-int comparison, fail-safe
- is_applicable() state-based (manager registered/unregistered) + fail-safe
- probe() integration over the registry slot
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from baldur.factory.registry import ProviderRegistry
from baldur.meta.emergency_stuck_probe import EmergencyStuckProbe
from baldur.meta.health_probe import HealthStatus
from baldur.models.emergency import EmergencyLevel
from baldur.utils.time import utc_now

_THRESHOLD = 1800.0


def _state(
    *,
    level: EmergencyLevel = EmergencyLevel.NORMAL,
    is_recovering: bool = False,
    is_auto_triggered: bool = False,
    recovery_started_at: str | None = None,
    activated_at: str | None = None,
    expires_at: str | None = None,
) -> SimpleNamespace:
    """A duck-typed EmergencyState (all fields read via getattr in the probe)."""
    return SimpleNamespace(
        level=level,
        is_recovering=is_recovering,
        is_auto_triggered=is_auto_triggered,
        recovery_started_at=recovery_started_at,
        activated_at=activated_at,
        expires_at=expires_at,
    )


class TestEmergencyStuckProbeAgeSecondsBehavior:
    """Behavior verification for EmergencyStuckProbe._age_seconds()."""

    def test_none_input_returns_none(self):
        """A missing timestamp yields no age (clause is skipped)."""
        assert EmergencyStuckProbe._age_seconds(None, utc_now()) is None

    def test_empty_string_returns_none(self):
        """An empty timestamp string is falsy → None."""
        assert EmergencyStuckProbe._age_seconds("", utc_now()) is None

    def test_malformed_timestamp_returns_none(self):
        """An unparseable timestamp is swallowed → None (fail-safe)."""
        assert EmergencyStuckProbe._age_seconds("not-a-timestamp", utc_now()) is None

    def test_valid_timestamp_returns_elapsed_seconds(self):
        """A valid ISO timestamp yields the elapsed seconds since it."""
        now = utc_now()
        past = (now - timedelta(seconds=120)).isoformat()

        age = EmergencyStuckProbe._age_seconds(past, now)

        assert age is not None
        assert abs(age - 120.0) < 1.0


class TestEmergencyStuckProbeEvaluateBehavior:
    """Behavior verification for EmergencyStuckProbe._evaluate()."""

    def setup_method(self):
        self.probe = EmergencyStuckProbe()
        self.now = utc_now()

    def _at(self, seconds_ago: float) -> str:
        return (self.now - timedelta(seconds=seconds_ago)).isoformat()

    # --- Primary: recovery wedged ---------------------------------------

    def test_recovery_wedged_past_threshold_is_unhealthy(self):
        """is_recovering + old recovery_started_at → wedged (primary clause)."""
        state = _state(
            level=EmergencyLevel.LEVEL_2,
            is_recovering=True,
            recovery_started_at=self._at(_THRESHOLD + 600),
        )

        wedged, reason, details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is True
        assert details["clause"] == "recovery_wedged"
        assert "wedged" in reason.lower()

    def test_recovery_within_threshold_is_healthy(self):
        """Recovering but not yet past the threshold → healthy."""
        state = _state(
            level=EmergencyLevel.LEVEL_2,
            is_recovering=True,
            recovery_started_at=self._at(_THRESHOLD - 600),
        )

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is False

    def test_recovery_at_threshold_boundary_is_healthy(self):
        """age == threshold is NOT past threshold (strict >) → healthy."""
        state = _state(
            level=EmergencyLevel.LEVEL_1,
            is_recovering=True,
            recovery_started_at=self._at(_THRESHOLD),
        )

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is False

    def test_recovery_with_unparseable_timestamp_is_healthy(self):
        """A malformed recovery timestamp is fail-safe → healthy, never raises."""
        state = _state(
            level=EmergencyLevel.LEVEL_3,
            is_recovering=True,
            recovery_started_at="garbage",
        )

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is False

    # --- NORMAL level ---------------------------------------------------

    def test_normal_level_is_healthy(self):
        """A NORMAL level (not recovering) is never wedged."""
        state = _state(level=EmergencyLevel.NORMAL)

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is False

    # --- Secondary: auto-triggered overstay -----------------------------

    def test_auto_triggered_overstay_expired_is_unhealthy(self):
        """Auto-triggered, non-recovering, past threshold, expiry lapsed → wedged."""
        state = _state(
            level=EmergencyLevel.LEVEL_2,
            is_auto_triggered=True,
            activated_at=self._at(_THRESHOLD + 300),
            expires_at=self._at(60),  # already past
        )

        wedged, _reason, details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is True
        assert details["clause"] == "auto_triggered_overstay"

    def test_auto_triggered_overstay_with_none_expiry_is_unhealthy(self):
        """expires_at is None counts as 'expired or none' → wedged when overstayed."""
        state = _state(
            level=EmergencyLevel.LEVEL_1,
            is_auto_triggered=True,
            activated_at=self._at(_THRESHOLD + 300),
            expires_at=None,
        )

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is True

    def test_auto_triggered_not_yet_expired_is_healthy(self):
        """A still-valid expiry (future) blocks the secondary clause → healthy."""
        state = _state(
            level=EmergencyLevel.LEVEL_2,
            is_auto_triggered=True,
            activated_at=self._at(_THRESHOLD + 300),
            expires_at=(self.now + timedelta(seconds=600)).isoformat(),
        )

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is False

    def test_auto_triggered_within_threshold_is_healthy(self):
        """Auto-triggered but not yet past the threshold → healthy."""
        state = _state(
            level=EmergencyLevel.LEVEL_2,
            is_auto_triggered=True,
            activated_at=self._at(_THRESHOLD - 300),
            expires_at=None,
        )

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is False

    # --- Operator-held exclusion (D6 firewall) --------------------------

    def test_operator_held_level_is_excluded(self):
        """A manually-held (is_auto_triggered=False), non-recovering, long-lived
        level is intentional incident response — excluded, never wedged."""
        state = _state(
            level=EmergencyLevel.LEVEL_3,
            is_auto_triggered=False,
            activated_at=self._at(_THRESHOLD + 5000),
            expires_at=None,
        )

        wedged, _reason, _details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert wedged is False

    # --- Enum-not-int comparison ----------------------------------------

    def test_level_compared_as_enum_not_int_no_typeerror(self):
        """The level passes through != EmergencyLevel.NORMAL with no TypeError.

        Regression guard for the enum-vs-int comparison trap: the rich
        comparators raise TypeError against an int, so a stray ``level >= 1``
        would silently kill the probe. Equality comparison stays safe.
        """
        state = _state(
            level=EmergencyLevel.LEVEL_1,
            is_auto_triggered=True,
            activated_at=self._at(_THRESHOLD + 100),
            expires_at=None,
        )

        # Must not raise; level is reflected back in details as its enum value.
        _wedged, _reason, details = self.probe._evaluate(state, _THRESHOLD, self.now)

        assert details["level"] == EmergencyLevel.LEVEL_1.value


class TestEmergencyStuckProbeApplicabilityBehavior:
    """Behavior verification for EmergencyStuckProbe.is_applicable()."""

    def test_applicable_when_manager_registered(self):
        with patch.object(
            ProviderRegistry.emergency_manager, "safe_get", return_value=object()
        ):
            assert EmergencyStuckProbe().is_applicable() is True

    def test_not_applicable_when_manager_unregistered(self):
        with patch.object(
            ProviderRegistry.emergency_manager, "safe_get", return_value=None
        ):
            assert EmergencyStuckProbe().is_applicable() is False

    def test_not_applicable_on_resolution_error(self):
        with patch.object(
            ProviderRegistry.emergency_manager,
            "safe_get",
            side_effect=RuntimeError("registry down"),
        ):
            assert EmergencyStuckProbe().is_applicable() is False


class TestEmergencyStuckProbeProbeBehavior:
    """Behavior verification for EmergencyStuckProbe.probe() over the registry."""

    def test_probe_unhealthy_for_wedged_recovery(self):
        """A wedged recovery state surfaces as UNHEALTHY end-to-end."""
        old = (utc_now() - timedelta(seconds=_THRESHOLD + 3600)).isoformat()
        manager = MagicMock()
        manager.get_state.return_value = _state(
            level=EmergencyLevel.LEVEL_2,
            is_recovering=True,
            recovery_started_at=old,
        )

        with patch.object(
            ProviderRegistry.emergency_manager, "safe_get", return_value=manager
        ):
            result = EmergencyStuckProbe().probe()

        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["clause"] == "recovery_wedged"

    def test_probe_healthy_for_normal_level(self):
        """A NORMAL manager is a benign always-green component."""
        manager = MagicMock()
        manager.get_state.return_value = _state(level=EmergencyLevel.NORMAL)

        with patch.object(
            ProviderRegistry.emergency_manager, "safe_get", return_value=manager
        ):
            result = EmergencyStuckProbe().probe()

        assert result.status == HealthStatus.HEALTHY

    def test_probe_unknown_when_manager_missing(self):
        """safe_get() → None mid-probe → UNKNOWN (defensive, not a false page)."""
        with patch.object(
            ProviderRegistry.emergency_manager, "safe_get", return_value=None
        ):
            result = EmergencyStuckProbe().probe()

        assert result.status == HealthStatus.UNKNOWN
        assert result.error is not None
