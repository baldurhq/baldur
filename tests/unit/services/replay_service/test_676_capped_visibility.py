"""676 — On-recovery cap-overflow visibility (D12).

Target: ``baldur.services.replay_service.service._replay_on_circuit_close_locked``
and the on-recovery task ``baldur.celery_tasks.dlq_tasks.conditional_replay_on_circuit_close``.

A circuit-close sweep is bounded by ``on_recovery_max_items`` split into per-
failure-type quotas. When any per-type fill returns *exactly* its quota, the
cap may have left eligible entries undrained — ``BatchReplayResult.capped`` is
set True so the operator sees "why weren't all recovered" without inferring it
from queue depth. No data loss: remaining entries stay PENDING (never fetched
beyond the quota) and drain on the next CB close or a manual/scheduled replay.
The on-recovery task surfaces ``capped`` in ``dlq.circuit_recovery_completed`` and
its result dict.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.interfaces.repositories import ReplayablePage
from baldur.models.governance import GovernanceCheckResult
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.models import BatchReplayResult, ReplayResult


def _op(entry_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=entry_id,
        domain="payment",
        status="pending",
        failure_type="TYPE_A",
        created_at=None,
    )


def _page(*entries: SimpleNamespace) -> ReplayablePage:
    return ReplayablePage(entries=list(entries))


def _gov_allow() -> MagicMock:
    gov = MagicMock()
    gov.check_all_governance.return_value = GovernanceCheckResult(allowed=True)
    return gov


def _page_selector(pages: list) -> MagicMock:
    """Hand back ``pages`` in order, then empty pages forever.

    Wrapped rather than bare so the stub answers to a keyword-only call shape:
    the sweep selects by keyword, and a positional call would be a defect.
    """
    remaining = list(pages)

    def _next(**_kwargs):
        return remaining.pop(0) if remaining else _page()

    return MagicMock(wraps=_next)


def _capped_service() -> ReplayService:
    svc = ReplayService(repository=MagicMock(), cache=InMemoryCacheAdapter())
    svc._event_bus = MagicMock()
    # Governance passes; the per-entry replay is a no-op success so only the
    # quota-fill capped derivation is under test.
    svc._get_governance = MagicMock(return_value=_gov_allow())
    svc._execute_replay = MagicMock(return_value=ReplayResult.succeeded("x"))
    return svc


# =============================================================================
# BatchReplayResult.capped default
# =============================================================================


class TestBatchReplayResultCappedContract:
    def test_capped_defaults_false(self):
        assert BatchReplayResult().capped is False

    def test_capped_is_independent_of_inflight_and_governance_flags(self):
        result = BatchReplayResult(capped=True)
        assert result.capped is True
        assert result.inflight_skipped is False
        assert result.governance_blocked is False


# =============================================================================
# _replay_on_circuit_close_locked — capped derivation (boundary)
# =============================================================================


class TestCappedVisibilityBehavior:
    """``capped`` iff a per-type fill returns exactly its allotted quota."""

    def test_capped_true_when_fill_equals_quota(self):
        # 1 failure type, max_items=3 => quota 3; a full batch of 3 == quota.
        svc = _capped_service()
        svc.repository.find_replayable_page = MagicMock(
            side_effect=[_page(_op("e1"), _op("e2"), _op("e3")), _page()]
        )

        result = svc.replay_on_circuit_close(
            service_name="svc",
            max_items=3,
            service_failure_type_map={"svc": ["TYPE_A"]},
        )

        assert result.capped is True
        assert result.total == 3

    def test_capped_false_when_fill_below_quota(self):
        # One short of the quota => the cap did not bind => not capped.
        svc = _capped_service()
        svc.repository.find_replayable_page = MagicMock(
            return_value=_page(_op("e1"), _op("e2"))
        )

        result = svc.replay_on_circuit_close(
            service_name="svc",
            max_items=3,
            service_failure_type_map={"svc": ["TYPE_A"]},
        )

        assert result.capped is False
        assert result.total == 2

    def test_capped_true_if_any_single_type_fills_its_quota(self):
        # 2 types, max_items=4 => quota 2 each. Type A fills its quota (2);
        # type B does not (1). capped is True because A left entries behind.
        svc = _capped_service()
        svc.repository.find_replayable_page = MagicMock(
            side_effect=[_page(_op("a1"), _op("a2")), _page(_op("b1")), _page()]
        )

        result = svc.replay_on_circuit_close(
            service_name="svc",
            max_items=4,
            service_failure_type_map={"svc": ["TYPE_A", "TYPE_B"]},
        )

        assert result.capped is True
        assert result.total == 3

    def test_fetch_is_bounded_to_quota_so_remaining_stay_pending(self):
        # No-loss: the sweep only ever fetches up to the quota, so entries
        # beyond it are never touched (stay PENDING for a later drain).
        svc = _capped_service()
        svc.repository.find_replayable_page = MagicMock(
            side_effect=[_page(_op("e1"), _op("e2"), _op("e3")), _page()]
        )

        svc.replay_on_circuit_close(
            service_name="svc",
            max_items=3,
            service_failure_type_map={"svc": ["TYPE_A"]},
        )

        first_call = svc.repository.find_replayable_page.call_args_list[0]
        assert first_call.kwargs["limit"] == 3
        assert first_call.kwargs["failure_type"] == "TYPE_A"


# =============================================================================
# On-recovery task — capped in completion event + result dict (SC #9)
# =============================================================================


class TestCappedTaskSurfaceBehavior:
    """``conditional_replay_on_circuit_close`` surfaces ``capped`` on the
    completion event and in the returned dict.
    """

    def _run_task(self, capped: bool):
        from baldur.celery_tasks import dlq_tasks

        mock_replay = MagicMock()
        mock_replay.replay_on_circuit_close.return_value = SimpleNamespace(
            governance_blocked=False,
            governance_block_reason=None,
            total=3,
            success_count=3,
            failed_count=0,
            capped=capped,
            inflight_skipped=False,
            lane_cursors={},
            scan_exhausted_lanes=[],
            scan_exhausted=False,
        )

        with (
            patch("baldur.services.get_replay_service", return_value=mock_replay),
            capture_logs() as logs,
        ):
            eager = dlq_tasks.conditional_replay_on_circuit_close.apply(
                kwargs={"service_name": "payment-api", "max_items": 3},
                task_id="task-capped",
            )
        return eager.get(), logs

    def test_capped_true_emitted_and_returned(self):
        result, logs = self._run_task(capped=True)

        assert result["capped"] is True
        completed = [
            e for e in logs if e.get("event") == "dlq.circuit_recovery_completed"
        ]
        assert len(completed) == 1
        assert completed[0]["capped"] is True

    def test_capped_false_returned(self):
        result, _ = self._run_task(capped=False)
        assert result["capped"] is False


# =============================================================================
# capped on the completion event — one meaning on both emitting lanes
# =============================================================================


class TestBatchCompletedCappedBehavior:
    """``DLQ_REPLAY_BATCH_COMPLETED`` carries ``capped`` from both emitters.

    The two lanes share one event. A field that is derived on one of them and
    a constant on the other is worse than absent: a consumer reading it cannot
    tell "this sweep drained everything" from "this lane never computes it".
    """

    def _completion_payloads(self, svc) -> list[dict]:
        return [
            call.kwargs["data"]
            for call in svc._emit_event.call_args_list
            if "data" in call.kwargs and "total" in call.kwargs["data"]
        ]

    def _batch_service(self) -> ReplayService:
        svc = _capped_service()
        svc._emit_event = MagicMock(wraps=lambda *_a, **_kw: None)
        return svc

    def test_circuit_close_sweep_reports_capped_when_a_lane_filled_its_quota(self):
        svc = self._batch_service()
        svc.repository.find_replayable_page = _page_selector(
            [_page(_op("e1"), _op("e2")), _page()]
        )

        svc.replay_on_circuit_close(
            service_name="svc",
            max_items=2,
            service_failure_type_map={"svc": ["TYPE_A"]},
        )

        assert self._completion_payloads(svc)[-1]["capped"] is True

    def test_circuit_close_sweep_reports_uncapped_when_the_pool_ran_out(self):
        svc = self._batch_service()
        svc.repository.find_replayable_page = _page_selector([_page(_op("e1"))])

        svc.replay_on_circuit_close(
            service_name="svc",
            max_items=5,
            service_failure_type_map={"svc": ["TYPE_A"]},
        )

        assert self._completion_payloads(svc)[-1]["capped"] is False

    def test_replay_batch_reports_capped_when_the_selection_filled_its_allotment(self):
        svc = self._batch_service()
        svc.repository.find_replayable = MagicMock(
            wraps=lambda **_kwargs: [_op("e1"), _op("e2")]
        )

        result = svc.replay_batch(domain="payment", max_items=2, use_adaptive=False)

        assert result.capped is True
        assert self._completion_payloads(svc)[-1]["capped"] is True

    def test_replay_batch_reports_uncapped_when_the_selection_fell_short(self):
        svc = self._batch_service()
        svc.repository.find_replayable = MagicMock(wraps=lambda **_kwargs: [_op("e1")])

        result = svc.replay_batch(domain="payment", max_items=5, use_adaptive=False)

        assert result.capped is False
        assert self._completion_payloads(svc)[-1]["capped"] is False
