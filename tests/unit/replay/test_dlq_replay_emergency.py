"""DLQ Replay safety-check service-wiring tests.

The actual blocking logic (Kill Switch / Emergency Level / ErrorBudgetGate)
lives in the PRO governance service and is covered under ``tests/pro/``. These
OSS tests pin only that ``ReplayService`` resolves governance through the
service layer.
"""

import pytest


class TestServiceFunctionsExist:
    """Service-layer function existence tests."""

    def test_governance_check_importable(self):
        """Governance check functions are importable when PRO is present."""
        pytest.importorskip("baldur_pro")
        try:
            from baldur_pro.services.governance.checks import (
                GovernanceCheckResult,  # noqa: F401
                check_all_governance,  # noqa: F401
            )

            imported = True
        except ImportError:
            imported = False

        assert imported is True, "governance check functions must be importable"

    def test_replay_service_uses_governance_checks(self):
        """ReplayService resolves governance via _get_governance() (518 b)."""
        from baldur.services.replay_service.service import ReplayService

        assert hasattr(ReplayService, "_get_governance"), (
            "ReplayService must resolve governance via _get_governance() "
            "(518 b migration)"
        )
