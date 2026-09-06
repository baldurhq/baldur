"""ConfigApplyService refuses both entries without an ACTIVE verdict.

Applying a scheduled or graceful config change is PRO behaviour: the manager
that performs it is a PRO service, and the change *creation* surface is already
unavailable without a licence because it resolves through the provider
registry. The applier reaches its manager by direct import, so it never passed
through that boundary — these tests pin the boundary the applier now carries
itself.

Two details are load-bearing and each has its own test:

- the refusal status is ``skipped``, not ``blocked``. ``blocked`` is the
  governance vocabulary, and the beat task raises a WARNING and writes an audit
  row on it; a lapsed deployment would emit both every 30s forever.
- entitlement resolves *ahead of* the governance check. Without an ACTIVE
  verdict the PRO governance provider never registers, so a governance check
  run first would answer from the permissive OSS no-op default and report
  nothing useful.

Verification techniques (UNIT_TEST_GUIDELINES §8): §8.12 branch outcomes,
§8.5 dependency interaction (the governance seam is never consulted on a
refusal), §8.13 proximate cause (the OSS-only arm must still reach its own
"manager unavailable" answer, so the refusal cannot be the presence check in
disguise).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from baldur.core.entitlement import EntitlementResult, EntitlementStatus
from baldur.services.execution_services.config_apply_service import (
    get_config_apply_service,
    reset_config_apply_service,
)

_VERDICT = "baldur.core.entitlement.get_entitlement_status"
_GOVERNANCE_SLOT = "baldur.factory.ProviderRegistry.governance"
_PRO_RUNTIME_CONFIG_MODULE = "baldur_pro.services.runtime_config"


@pytest.fixture
def service():
    """A ConfigApplyService with no cached instance carried in or out."""
    reset_config_apply_service()
    yield get_config_apply_service()
    reset_config_apply_service()


def _verdict(status: EntitlementStatus):
    return patch(_VERDICT, return_value=EntitlementResult(status=status))


class TestConfigApplyEntitlementBehavior:
    """Both entry points refuse a lapsed licence before doing anything else."""

    @pytest.mark.parametrize(
        "status",
        [EntitlementStatus.INVALID, EntitlementStatus.MISSING],
        ids=["invalid_licence", "no_licence"],
    )
    def test_apply_pending_changes_refuses_a_non_active_verdict(
        self, service, mock_pro_tier, status
    ):
        """status=skipped, reason=not_entitled — the refusal vocabulary.

        Hardcoded: the beat task branches on ``blocked`` to raise a WARNING and
        feeds ``reason`` into the audit trail, so both strings are a contract
        no matter how internal they look.
        """
        with _verdict(status):
            result = service.apply_pending_changes()

        assert result["status"] == "skipped"
        assert result["reason"] == "not_entitled"

    @pytest.mark.parametrize(
        "status",
        [EntitlementStatus.INVALID, EntitlementStatus.MISSING],
        ids=["invalid_licence", "no_licence"],
    )
    def test_apply_graceful_change_refuses_a_non_active_verdict(
        self, service, mock_pro_tier, status
    ):
        """The graceful lane has no gated beat lane in front of it at all."""
        with _verdict(status):
            result = service.apply_graceful_change(pending_id="pending-1")

        assert result["status"] == "skipped"
        assert result["reason"] == "not_entitled"

    def test_refusal_is_skipped_not_the_governance_blocked_vocabulary(
        self, service, mock_pro_tier
    ):
        """A negative assertion on the status word.

        Reusing ``blocked`` here would be indistinguishable from an emergency-
        mode block at the caller, and would make a lapsed worker write a
        WARNING plus a blocked audit row on every tick.
        """
        with _verdict(EntitlementStatus.MISSING):
            result = service.apply_pending_changes()

        assert result["status"] != "blocked"

    def test_refusal_never_consults_the_governance_seam(self, service, mock_pro_tier):
        """Entitlement resolves ahead of governance, so the seam is untouched.

        Reordering would run the check against the permissive OSS no-op default
        that stands in when the PRO governance provider never registers.
        """
        with _verdict(EntitlementStatus.MISSING), patch(_GOVERNANCE_SLOT) as mock_slot:
            service.apply_pending_changes()

        mock_slot.get.assert_not_called()

    def test_graceful_refusal_never_consults_the_governance_seam(
        self, service, mock_pro_tier
    ):
        """Same ordering on the lane that has no gate in front of it."""
        with _verdict(EntitlementStatus.MISSING), patch(_GOVERNANCE_SLOT) as mock_slot:
            service.apply_graceful_change(pending_id="pending-1")

        mock_slot.get.assert_not_called()

    def test_active_verdict_lets_the_tick_reach_governance(
        self, service, mock_pro_tier
    ):
        """The gate is a refusal, not a rewrite: an entitled tick proceeds.

        The PRO runtime-config import is pinned unavailable so the tick stops
        at the next decision instead of doing real applier work; reaching that
        answer at all is only possible past both the entitlement gate and the
        governance check.
        """
        with (
            _verdict(EntitlementStatus.ACTIVE),
            patch(_GOVERNANCE_SLOT) as mock_slot,
            patch.dict(sys.modules, {_PRO_RUNTIME_CONFIG_MODULE: None}),
        ):
            result = service.apply_pending_changes()

        mock_slot.get.assert_called_once()
        assert result["reason"] == "runtime_config_manager_unavailable"

    def test_oss_only_install_still_reports_the_manager_unavailable_answer(
        self, service, mock_oss_tier
    ):
        """Presence is answered first and is not folded into the refusal.

        An OSS-only install has no licence to be the problem, and must keep
        receiving the answer that names what is actually missing. The PRO
        import is failed by pinning ``None`` into ``sys.modules`` so the arm is
        the same in a PRO-present and a PRO-absent checkout.
        """
        with (
            patch(_VERDICT) as mock_verdict,
            patch.dict(sys.modules, {_PRO_RUNTIME_CONFIG_MODULE: None}),
        ):
            result = service.apply_pending_changes()

        assert result["status"] == "blocked"
        assert result["reason"] == "runtime_config_manager_unavailable"
        mock_verdict.assert_not_called()
