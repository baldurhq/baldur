"""Unit tests for ``baldur.api.middleware.admission`` (591).

Scope:
    - ``AdmissionDecision``: dataclass default contract (allow + inactive).
    - ``check_admission``: the framework-free decision pipeline —
      OPTIONS passthrough, disable gate, OSS clean no-op (bulkhead-registry
      absent), the PRO allow / reject branches, the tier x decision categorical
      matrix, cell-aware bulkhead naming, and the fail-open guarantee.
    - ``_bulkhead_registry`` PRO gate: registry ``None`` -> no tier import, no
      TrafficGate token consumed.
    - ``_make_release``: idempotent release closure (double-call -> single
      ``release_bulkhead``).

The admission helper resolves all heavy dependencies lazily (``get_tier_registry``
/ ``get_traffic_gate`` / ``ProviderRegistry.bulkhead_registry``), so tests patch
those accessors to inject deterministic doubles without registering ``baldur_pro``
or touching the real singletons used by the rest of the session — mirroring the
``check_backpressure`` patch strategy in ``test_backpressure_helpers.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.middleware import admission as adm
from baldur.api.middleware.admission import (
    TIER_PRIORITY_MAP,
    AdmissionDecision,
    _make_release,
    check_admission,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext, ResponseContext
from baldur.scaling.config import BackpressureLevel
from baldur.scaling.traffic_gate import TrafficDecision, TrafficGate

# =============================================================================
# Builders
# =============================================================================


def _make_request(
    *,
    method: HttpMethod = HttpMethod.GET,
    path: str = "/api/resource/",
    client_ip: str | None = "203.0.113.5",
) -> RequestContext:
    return RequestContext(method=method, path=path, client_ip=client_ip)


def _settings(*, enabled: bool = True) -> MagicMock:
    settings = MagicMock()
    settings.enabled = enabled
    settings.get_tier_max_concurrent.return_value = 50
    settings.get_tier_bulkhead_timeout.return_value = 0.05
    return settings


def _decision(
    allowed: bool,
    *,
    bulkhead_acquired: bool = False,
    bulkhead_name: str | None = None,
    level: BackpressureLevel = BackpressureLevel.NONE,
) -> TrafficDecision:
    return TrafficDecision(
        allowed=allowed,
        reason="allowed" if allowed else "shed",
        level=level,
        gate="traffic_gate",
        bulkhead_acquired=bulkhead_acquired,
        bulkhead_name=bulkhead_name,
    )


@contextmanager
def _pro_admission(
    *,
    tier_id: str,
    decision: TrafficDecision,
    cell_id: str | None = None,
    settings: MagicMock | None = None,
    level: BackpressureLevel = BackpressureLevel.NONE,
):
    """Patch the lazy accessors so ``check_admission`` runs the PRO path.

    Yields ``(gate, tier_registry, bulkhead_registry)`` so callers can assert on
    the injected doubles.
    """
    gate = MagicMock(spec=TrafficGate)
    gate.should_allow.return_value = decision
    gate.get_level.return_value = level

    tier_registry = MagicMock()
    tier_registry.resolve_tier_with_fallback.return_value = MagicMock(tier_id=tier_id)

    bulkhead_registry = MagicMock()  # non-None == PRO present

    with (
        patch.object(
            adm, "_get_admission_settings", return_value=settings or _settings()
        ),
        patch.object(adm, "_bulkhead_registry", return_value=bulkhead_registry),
        patch("baldur.scaling.traffic_gate.get_traffic_gate", return_value=gate),
        patch("baldur.scaling.tiering.get_tier_registry", return_value=tier_registry),
        patch("baldur.context.cell_context.get_current_cell_id", return_value=cell_id),
    ):
        yield gate, tier_registry, bulkhead_registry


# =============================================================================
# AdmissionDecision — Contract
# =============================================================================


class TestAdmissionDecisionContract:
    """The dataclass defaults to 'allow + inactive' (the OSS no-op shape)."""

    def test_default_decision_is_allow_and_inactive(self):
        decision = AdmissionDecision()
        assert decision.rejection is None
        assert decision.active is False
        assert decision.release is None
        assert decision.tier_id is None

    def test_tier_priority_map_orders_critical_highest(self):
        """Lower int == higher priority (TrafficGate convention)."""
        assert TIER_PRIORITY_MAP["critical"] == 0
        assert TIER_PRIORITY_MAP["standard"] == 50
        assert TIER_PRIORITY_MAP["non_essential"] == 100


# =============================================================================
# check_admission — Behavior
# =============================================================================


class TestCheckAdmissionBehavior:
    """The framework-free decision pipeline (OPTIONS / disabled / OSS / PRO)."""

    def test_options_request_passes_through_without_consulting_settings(self):
        """CORS preflight is never shed and short-circuits before the enable gate."""
        with patch.object(adm, "_get_admission_settings") as mock_settings:
            decision = check_admission(_make_request(method=HttpMethod.OPTIONS))

        assert decision == AdmissionDecision()
        mock_settings.assert_not_called()

    def test_disabled_returns_inactive_allow_without_pro_gate(self):
        """enabled=False short-circuits before the bulkhead-registry probe."""
        with (
            patch.object(
                adm, "_get_admission_settings", return_value=_settings(enabled=False)
            ),
            patch.object(adm, "_bulkhead_registry") as mock_registry,
        ):
            decision = check_admission(_make_request())

        assert decision.active is False
        assert decision.rejection is None
        mock_registry.assert_not_called()

    def test_missing_settings_returns_inactive_allow(self):
        """settings layer unavailable (None) is treated as disabled."""
        with patch.object(adm, "_get_admission_settings", return_value=None):
            decision = check_admission(_make_request())

        assert decision.active is False
        assert decision.rejection is None

    def test_oss_noop_when_bulkhead_registry_absent(self):
        """No per-tier Bulkhead registry (OSS) -> clean no-op, no token, no tier import."""
        with (
            patch.object(adm, "_get_admission_settings", return_value=_settings()),
            patch.object(adm, "_bulkhead_registry", return_value=None),
            patch("baldur.scaling.traffic_gate.get_traffic_gate") as mock_gate_factory,
            patch("baldur.scaling.tiering.get_tier_registry") as mock_tier_factory,
        ):
            decision = check_admission(_make_request())

        assert decision.active is False
        assert decision.rejection is None
        assert decision.release is None
        mock_gate_factory.assert_not_called()
        mock_tier_factory.assert_not_called()

    def test_pro_allow_returns_active_with_release_handle(self):
        """PRO allow path: active, no rejection, a release closure, classified tier."""
        decision = _decision(
            True, bulkhead_acquired=True, bulkhead_name="tier:standard"
        )
        with _pro_admission(tier_id="standard", decision=decision) as (gate, _, _):
            result = check_admission(_make_request())

        assert result.active is True
        assert result.rejection is None
        assert result.tier_id == "standard"
        assert callable(result.release)

        # The release closure delegates to the gate (idempotent per _make_release).
        result.release()
        gate.release_bulkhead.assert_called_once_with("tier:standard")

    def test_pro_allow_without_bulkhead_has_no_release(self):
        """Rate-controller-only allow (no slot acquired) -> release is None."""
        decision = _decision(True, bulkhead_acquired=False)
        with _pro_admission(tier_id="standard", decision=decision):
            result = check_admission(_make_request())

        assert result.active is True
        assert result.rejection is None
        assert result.release is None

    def test_pro_reject_returns_503_active_without_release(self):
        """PRO reject path: 503 ResponseContext, active, no release handle."""
        decision = _decision(False, level=BackpressureLevel.HIGH)
        with _pro_admission(tier_id="non_essential", decision=decision):
            result = check_admission(_make_request())

        assert result.active is True
        assert isinstance(result.rejection, ResponseContext)
        assert result.rejection.status_code == 503
        assert result.rejection.body["code"] == "ADMISSION_CONTROL_REJECTED"
        assert result.release is None
        assert result.tier_id == "non_essential"

    @pytest.mark.parametrize(
        ("tier_id", "priority"),
        [("critical", 0), ("standard", 50), ("non_essential", 100)],
    )
    @pytest.mark.parametrize("allowed", [True, False])
    def test_tier_decision_matrix_maps_priority_and_outcome(
        self, tier_id, priority, allowed
    ):
        """tier {critical,standard,non_essential} x {allow,reject} categorical matrix."""
        decision = _decision(
            allowed,
            bulkhead_acquired=allowed,
            bulkhead_name=f"tier:{tier_id}" if allowed else None,
        )
        with _pro_admission(tier_id=tier_id, decision=decision) as (gate, _, _):
            result = check_admission(_make_request())

        # Tier classification flows to the TrafficGate priority arg.
        assert gate.should_allow.call_args.kwargs["priority"] == priority
        assert result.active is True
        assert result.tier_id == tier_id
        if allowed:
            assert result.rejection is None
        else:
            assert result.rejection.status_code == 503
            assert result.release is None

    def test_cell_aware_bulkhead_naming_when_cell_set(self):
        """An active cell id produces a cell:{id}:tier:{id} bulkhead name."""
        decision = _decision(
            True, bulkhead_acquired=True, bulkhead_name="cell:c7:tier:standard"
        )
        with _pro_admission(tier_id="standard", decision=decision, cell_id="c7") as (
            gate,
            _,
            registry,
        ):
            check_admission(_make_request())

        assert gate.should_allow.call_args.kwargs["bulkhead_name"] == (
            "cell:c7:tier:standard"
        )
        registry.get_or_create.assert_called_once_with(
            name="cell:c7:tier:standard", max_concurrent=50
        )

    def test_should_allow_receives_per_tier_timeout_and_metadata(self):
        """settings.get_tier_bulkhead_timeout + tier metadata reach should_allow."""
        decision = _decision(
            True, bulkhead_acquired=True, bulkhead_name="tier:critical"
        )
        with _pro_admission(tier_id="critical", decision=decision) as (gate, _, _):
            check_admission(_make_request())

        kwargs = gate.should_allow.call_args.kwargs
        assert kwargs["bulkhead_timeout"] == 0.05
        assert kwargs["metadata"] == {"tier_id": "critical"}

    def test_fail_open_when_classification_raises(self):
        """Tier-registry failure degrades to active=False (adapter falls back to OSS)."""
        with _pro_admission(tier_id="standard", decision=_decision(True)) as (
            _,
            tier_registry,
            _,
        ):
            tier_registry.resolve_tier_with_fallback.side_effect = RuntimeError("boom")
            result = check_admission(_make_request())

        assert result.active is False
        assert result.rejection is None

    def test_fail_open_when_should_allow_raises(self):
        """TrafficGate failure degrades to active=False, never 500s the request."""
        with _pro_admission(tier_id="standard", decision=_decision(True)) as (
            gate,
            _,
            _,
        ):
            gate.should_allow.side_effect = RuntimeError("gate exploded")
            result = check_admission(_make_request())

        assert result.active is False
        assert result.rejection is None


# =============================================================================
# check_admission — PRO gate (_bulkhead_registry)
# =============================================================================


class TestCheckAdmissionProGate:
    """The bulkhead-registry presence is the capability gate (OSS vs PRO)."""

    def test_registry_absent_skips_tier_import_and_token(self):
        """OSS: no tier classification, no TrafficGate, no token consumed."""
        with (
            patch.object(adm, "_get_admission_settings", return_value=_settings()),
            patch.object(adm, "_bulkhead_registry", return_value=None),
            patch("baldur.scaling.tiering.get_tier_registry") as mock_tier_factory,
            patch("baldur.scaling.traffic_gate.get_traffic_gate") as mock_gate_factory,
        ):
            result = check_admission(_make_request())

        assert result.active is False
        mock_tier_factory.assert_not_called()
        mock_gate_factory.assert_not_called()

    def test_registry_present_proceeds_to_should_allow(self):
        """PRO: the request reaches the TrafficGate decision exactly once."""
        with _pro_admission(
            tier_id="standard",
            decision=_decision(
                True, bulkhead_acquired=True, bulkhead_name="tier:standard"
            ),
        ) as (gate, _, _):
            check_admission(_make_request())

        gate.should_allow.assert_called_once()


# =============================================================================
# _make_release — Behavior (idempotency)
# =============================================================================


class TestAdmissionReleaseIdempotency:
    """The release closure releases the slot exactly once, however often called."""

    def test_release_invokes_release_bulkhead_with_name(self):
        gate = MagicMock(spec=TrafficGate)
        release = _make_release(gate, "tier:standard")

        release()

        gate.release_bulkhead.assert_called_once_with("tier:standard")

    def test_double_release_releases_slot_only_once(self):
        """A second invoke is a no-op (no spurious release_bulkhead_failed warning)."""
        gate = MagicMock(spec=TrafficGate)
        release = _make_release(gate, "tier:critical")

        release()
        release()
        release()

        assert gate.release_bulkhead.call_count == 1
