"""``dispatch_recovery_sweep``: the one dispatch path, called by each recovery (807 D8).

The CLOSED event's dispatch is pinned with the event handler
(``test_676_track1_dispatch_visibility.py``). This file pins what the other
two callers hand the chain's first pass — a successful recovery trial (its
provenance, no escalation) and an operator's close-with-replay on a breaker
already CLOSED (the operator's own chain) — and how the dispatch reads its
configuration through the replay service it is given.

The replay service runs for real; the chain task is the seam (it publishes to
the broker), and so is the arming ledger the outcome is recorded in.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.celery_tasks.dlq_tasks import conditional_replay_on_circuit_close
from baldur.interfaces.repositories import ResolutionTrigger
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.recovery import dispatch_recovery_sweep
from baldur.settings.replay_automation import (
    get_replay_automation_settings,
    reset_replay_automation_settings,
)

NAME = "payment_api"
_TASK = "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close"
_RECORD = "baldur.services.replay_service.recovery._record_dispatch_outcome"
_GET_SERVICE = "baldur.services.replay_service.service.get_replay_service"


@pytest.fixture(autouse=True)
def _settings():
    reset_replay_automation_settings()
    yield
    reset_replay_automation_settings()


@pytest.fixture
def task():
    return MagicMock(spec=conditional_replay_on_circuit_close)


def _service(config: dict | None = None):
    service = ReplayService(repository=InMemoryFailedOperationRepository())
    return service, patch.object(
        ReplayService,
        "_get_replay_automation_config",
        autospec=True,
        return_value=config,
    )


class TestDispatchRecoverySweepBehavior:
    """Each caller's provenance reaches the chain's first pass."""

    @pytest.mark.parametrize(
        ("trigger", "escalate", "operator", "trigger_value"),
        [
            (
                ResolutionTrigger.AUTO_REPLAY_RECOVERY,
                False,
                False,
                "auto_replay_recovery",
            ),
            (
                ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
                True,
                True,
                "auto_replay_circuit_close",
            ),
            ("auto_replay_recovery", False, False, "auto_replay_recovery"),
        ],
        ids=["recovery_trial", "operator_close_with_replay", "raw_string_trigger"],
    )
    def test_dispatch_recovery_sweep_hands_the_chain_its_caller_s_provenance(
        self, task, trigger, escalate, operator, trigger_value
    ):
        service, no_runtime_config = _service()
        settings = get_replay_automation_settings()

        with no_runtime_config, patch(_TASK, new=task), patch(_RECORD) as record:
            outcome = dispatch_recovery_sweep(
                NAME,
                trigger=trigger,
                escalate_failures=escalate,
                operator_requested=operator,
                service=service,
            )

        assert outcome == "dispatched"
        task.delay.assert_called_once_with(
            service_name=NAME,
            max_items=settings.on_recovery_max_items,
            max_continuations=settings.on_recovery_max_continuations,
            trigger=trigger_value,
            escalate_failures=escalate,
            operator_requested=operator,
        )
        record.assert_called_once_with("dispatched", service_name=NAME)

    def test_dispatch_recovery_sweep_reads_the_budgets_through_the_given_service(
        self, task
    ):
        service, runtime_config = _service(
            {
                "on_recovery_enabled": True,
                "on_recovery_max_items": 7,
                "on_recovery_max_continuations": 3,
            }
        )

        with (
            runtime_config,
            patch(_TASK, new=task),
            patch(_RECORD),
            patch(_GET_SERVICE, side_effect=AssertionError("must use the given one")),
        ):
            dispatch_recovery_sweep(
                NAME,
                trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
                escalate_failures=False,
                service=service,
            )

        kwargs = task.delay.call_args.kwargs
        assert (kwargs["max_items"], kwargs["max_continuations"]) == (7, 3)

    def test_dispatch_recovery_sweep_disabled_through_the_given_service(self, task):
        service, runtime_config = _service({"on_recovery_enabled": False})

        with runtime_config, patch(_TASK, new=task), patch(_RECORD) as record:
            outcome = dispatch_recovery_sweep(
                NAME,
                trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
                escalate_failures=False,
                service=service,
            )

        assert outcome == "skipped_disabled"
        task.delay.assert_not_called()
        record.assert_called_once_with("skipped_disabled", service_name=NAME)

    def test_dispatch_recovery_sweep_still_dispatches_without_a_replay_service(
        self, task
    ):
        """The configuration falls back to the settings; the dispatch goes out."""
        with (
            patch(_GET_SERVICE, side_effect=RuntimeError("registry unavailable")),
            patch(_TASK, new=task),
            patch(_RECORD) as record,
            capture_logs() as logs,
        ):
            outcome = dispatch_recovery_sweep(
                NAME,
                trigger=ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
                escalate_failures=True,
            )

        assert outcome == "dispatched"
        task.delay.assert_called_once()
        record.assert_called_once_with("dispatched", service_name=NAME)
        unavailable = [
            e
            for e in logs
            if e["event"] == "replay_service.recovery_dispatch_service_unavailable"
        ]
        assert unavailable[0]["log_level"] == "debug"

    def test_dispatch_recovery_sweep_arming_ledger_failure_never_fails_the_dispatch(
        self, task
    ):
        service, no_runtime_config = _service()

        with (
            no_runtime_config,
            patch(_TASK, new=task),
            patch(
                "baldur.services.replay_service.arming.record_dispatch_outcome",
                side_effect=RuntimeError("ledger down"),
            ) as ledger,
        ):
            outcome = dispatch_recovery_sweep(
                NAME,
                trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
                escalate_failures=False,
                service=service,
            )

        assert outcome == "dispatched"
        task.delay.assert_called_once()
        ledger.assert_called_once_with("dispatched", service_name=NAME, error=None)
