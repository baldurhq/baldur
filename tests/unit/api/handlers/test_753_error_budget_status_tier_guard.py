"""The error-budget status endpoint keeps its 200 fail-open without PRO.

The module's documented contract is "Error Budget system failure -> default
PROCEED", a 200 on every path. On an install without the PRO distribution
the non-cached branch inverted it: resolving the service raised, and the
except arm's fail-safe *builder* imported a PRO symbol, so a
``ModuleNotFoundError`` escaped the handler and surfaced as HTTP 500 —
through a deploy gate whose whole reason for existing is not to block on its
own unavailability.

Answering tier absence on the first line restores the contract and makes the
except arm safe by construction: with the guard in front, every later
statement runs on an install that has the package, so the fail-safe builder's
import can no longer be the thing that fails. Both properties are pinned
here — the absence payload never reaches the fail-safe arm, and a
PRO-present runtime failure still does.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.api.handlers.error_budget_status import budget_status
from baldur.interfaces.web_framework import HttpMethod, RequestContext

STATUS_FAILED = "error_budget_api.status_failed"

_HANDLER_MODULE = "baldur.api.handlers.error_budget_status"


@contextmanager
def _tier(installed: bool):
    """Pin the PRO-wheel presence probe (a module-level singleton function)."""
    with patch("baldur.utils.tier.is_pro_installed", return_value=installed):
        yield


def _request(**query: str) -> RequestContext:
    return RequestContext(
        method=HttpMethod.GET,
        path="/error-budget/status/",
        query_params=dict(query),
    )


class TestBudgetStatusTierGuardBehavior:
    """Designed absence answers 200 on every branch, and says so."""

    @pytest.mark.parametrize(
        "query",
        [{}, {"nocache": "true"}, {"slo_name": "latency"}],
        ids=["cached_default_slo", "nocache_bypass", "non_default_slo"],
    )
    def test_tier_absent_answers_the_absence_payload_on_every_branch(self, query):
        """The two branches that reach ``_service()`` are the ones that used
        to 500; the cached branch is included so the guard is shown to
        precede the branch selection, not to patch one arm of it."""
        with _tier(False):
            response = budget_status(_request(**query))

        assert response.status_code == 200
        assert response.body["status"] == "unavailable"
        assert response.body["reason"] == "pro_not_installed"
        assert datetime.fromisoformat(response.body["timestamp"])

    def test_tier_absent_emits_no_status_failed_record(self):
        """Tier absence is not a system failure, so it does not log like one."""
        with _tier(False), capture_logs() as logs:
            budget_status(_request(nocache="true"))

        assert [entry for entry in logs if entry.get("event") == STATUS_FAILED] == []

    def test_tier_absent_never_reaches_the_failsafe_builder(self):
        """The proximate cause of the 200 is the guard, not the except arm.

        The fail-safe builder is the site that imported the missing package;
        a response that came from *there* would be the pre-change behavior
        with a different error, not the fix.
        """
        with (
            _tier(False),
            patch(f"{_HANDLER_MODULE}._failsafe_status", autospec=True) as failsafe,
            patch(f"{_HANDLER_MODULE}._service", autospec=True) as service,
        ):
            budget_status(_request(nocache="true"))

        failsafe.assert_not_called()
        service.assert_not_called()

    def test_a_pro_present_runtime_failure_still_takes_the_failsafe_arm(self):
        """The branch the guard must not swallow: with the capability
        installed, a service that cannot answer keeps the ERROR record and
        the fail-safe body."""
        failsafe_body = {"status": "failsafe", "decision": "PROCEED"}

        with (
            _tier(True),
            patch(
                f"{_HANDLER_MODULE}._service",
                autospec=True,
                side_effect=RuntimeError("service down"),
            ),
            patch(
                f"{_HANDLER_MODULE}._failsafe_status",
                autospec=True,
                return_value=failsafe_body,
            ) as failsafe,
            capture_logs() as logs,
        ):
            response = budget_status(_request(nocache="true"))

        assert response.status_code == 200
        assert response.body == failsafe_body
        failsafe.assert_called_once_with("service down")
        assert [
            entry
            for entry in logs
            if entry.get("event") == STATUS_FAILED and entry.get("log_level") == "error"
        ]
