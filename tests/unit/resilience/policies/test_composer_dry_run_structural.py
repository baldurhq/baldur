"""Observe-only (dry-run) trace visibility for live structural controls — doc 603 D7.

Complement to the D5 ``intervention_suppressed`` sites: under observe-only the
automatic *healing* interventions (CB / retry / DLQ) suppress their side-effects,
but a *structural* control — a bulkhead concurrency ceiling — stays live by
design. Suppressing it would admit calls past ``max_concurrent`` and uncap
concurrency, turning observe-only into a self-inflicted overload, so the reject is
enforced even under dry-run. The composer surfaces such a surviving REJECTED /
TIMEOUT via ``execution_mode.structural_control_enforced`` so the live block is
visible in the trace instead of a silent gap.

A generic mock rejecting policy is used (not the PRO ``BulkheadPolicy``) — the
composer behaviour is tier-agnostic, which keeps the test OSS-pure. Behaviors are
computed from source (``PolicyOutcome.*``, the observe-only gate), so these are
Behavior-class tests.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    set_execution_mode,
)
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.resilience.policies.composer import compose
from tests.factories import dry_run_active

_EVENT = "execution_mode.structural_control_enforced"


class _RejectingPolicy:
    """Minimal structural-style policy that always REJECTs (bulkhead-shaped)."""

    def __init__(self, name: str = "bulkhead") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def execute(
        self,
        func: Any,
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        # error=None so the chain maps it to a clean top-level REJECTED.
        return PolicyResult(
            outcome=PolicyOutcome.REJECTED,
            executed_policies=[self._name],
            metadata={"state": {"active_count": 5, "max_concurrent": 5}},
        )


def _enforced_events(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") == _EVENT]


class TestStructuralControlTrace:
    """PolicyComposer surfaces a live structural block under observe-only."""

    def test_live_reject_is_surfaced_under_dry_run(self):
        # Given a structural control that REJECTs, composed and run under observe-only
        composed = compose(_RejectingPolicy(name="payment_bulkhead"))
        with dry_run_active(), structlog.testing.capture_logs() as logs:
            result = composed.execute(lambda: "ok")
        # Then the live block is surfaced in the dry-run trace
        events = _enforced_events(logs)
        assert len(events) == 1
        assert events[0]["policy"] == "payment_bulkhead"
        assert events[0]["outcome"] == PolicyOutcome.REJECTED.value
        assert events[0]["state"] == {"active_count": 5, "max_concurrent": 5}
        # And the control is NOT suppressed — the chain still reports REJECTED
        assert result.outcome == PolicyOutcome.REJECTED

    def test_no_trace_when_not_observe_only(self):
        # Given the same control but active (executing) mode — not observe-only
        set_execution_mode(ExecutionMode.active())
        try:
            composed = compose(_RejectingPolicy())
            with structlog.testing.capture_logs() as logs:
                result = composed.execute(lambda: "ok")
        finally:
            clear_execution_mode_override()
        # Then nothing is surfaced — the gate is observe-only-scoped
        assert _enforced_events(logs) == []
        assert result.outcome == PolicyOutcome.REJECTED
