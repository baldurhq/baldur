"""One on-recovery pass: how it fills its lanes and where it says it stopped.

The sweep used to be the whole recovery — one budget, split across lanes, fired
once per circuit close, and everything past it waited for an operator. It is now
one *pass* of a chain, which puts three new obligations on this method and this
file pins each:

- a pass must move ``min(max_items, reachable)`` entries whatever the lane
  count, so the share an empty lane did not use is re-offered rather than parked;
- a pass must end by RETURNING at its deadline, and hand back cursors that
  resume at the entries it selected but never replayed — not at the ones it
  merely selected;
- a pass must say whether anything is left, in a form the successor can act on:
  ``capped`` and ``scan_exhausted_lanes`` for reachability, ``lane_cursors``
  for position.

The repository double honours cursors, because a mock that ignored them would
hand every fill round the same entries and quota redistribution would look
correct while re-selecting one page forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationRepository,
    ReplayablePage,
    decode_replay_cursor,
    encode_replay_cursor,
)
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, POLICY_CHAIN_CAPTURE_SOURCE
from baldur.models.governance import GovernanceCheckResult
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.models import ReplayResult
from baldur.services.replay_service.service import (
    REASON_CIRCUIT_REOPENED,
    REASON_CONTINUATION_BOUND_REACHED,
    _lane_key,
    _LaneSelection,
)

BASE = datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC)
SERVICE = "payment_api"


class _Clock:
    """Monotonic stand-in that only advances when the test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _entry(entry_id: str, *, offset: int, domain: str = SERVICE) -> FailedOperationData:
    return FailedOperationData(
        id=entry_id,
        domain=domain,
        failure_type="TYPE_A",
        status="pending",
        metadata={"source": POLICY_CHAIN_CAPTURE_SOURCE},
        created_at=BASE + timedelta(seconds=offset),
    )


def _lane_pool(prefix: str, count: int, *, domain: str = SERVICE):
    return [_entry(f"{prefix}-{i:03d}", offset=i, domain=domain) for i in range(count)]


def _paging_repository(pools: dict, *, exhausted_lanes=()) -> MagicMock:
    """A repository whose page selector really resumes from the cursor.

    ``pools`` maps ``(failure_type, domain)`` to an ascending list of entries.
    """
    repo = MagicMock(spec=FailedOperationRepository)

    def _find_page(
        *,
        max_retries: int,
        domain: str | None = None,
        failure_type: str | None = None,
        source: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ReplayablePage:
        pool = pools.get((failure_type, domain), [])
        start = 0
        floor = decode_replay_cursor(cursor)
        if floor is not None:
            start = next(
                (
                    i + 1
                    for i, entry in enumerate(pool)
                    if (entry.created_at.timestamp(), entry.id) >= floor
                ),
                len(pool),
            )
        taken = pool[start : start + limit]
        return ReplayablePage(
            entries=taken,
            next_cursor=(
                encode_replay_cursor(taken[-1].created_at, taken[-1].id)
                if taken
                else None
            ),
            scan_exhausted=(failure_type, domain) in exhausted_lanes,
        )

    repo.find_replayable_page.side_effect = _find_page
    repo.get_by_id.return_value = None
    return repo


def _service(repo: MagicMock, clock: _Clock | None = None) -> ReplayService:
    """A sweep whose selection is real and whose per-entry replay is a no-op."""
    svc = ReplayService(repository=repo, cache=InMemoryCacheAdapter())
    svc._event_bus = MagicMock(spec=BaldurEventBus)
    gov = MagicMock(spec=GovernanceChecker)
    gov.check_all_governance.return_value = GovernanceCheckResult(allowed=True)
    svc._governance = gov
    svc._governance_resolved = True

    def _replay(entry_id, **_kwargs):
        if clock is not None:
            clock.advance(1.0)
        return ReplayResult.succeeded(entry_id, "done")

    # Wrapped rather than bare: the recorder answers to the stub's own
    # signature, so a call shape the sweep would not make fails here.
    svc._execute_replay = MagicMock(wraps=_replay)
    return svc


def _limits(repo: MagicMock) -> list[int]:
    return [call.kwargs["limit"] for call in repo.find_replayable_page.call_args_list]


def _lanes_asked(repo: MagicMock) -> list[tuple[str, str | None]]:
    return [
        (call.kwargs["failure_type"], call.kwargs["domain"])
        for call in repo.find_replayable_page.call_args_list
    ]


# =============================================================================
# Lane fill — quota split, redistribution, rotation
# =============================================================================


class TestCircuitCloseLaneFillBehavior:
    """How one pass hands its budget out, and what it does with the remainder."""

    def test_budget_is_split_across_lanes_by_divmod(self):
        """Per-type fairness: a lane with a deep backlog must not crowd out
        the others in the first round."""
        pools = {
            ("TYPE_A", None): _lane_pool("a", 50),
            ("TYPE_B", None): _lane_pool("b", 50),
            ("TYPE_C", None): _lane_pool("c", 50),
        }
        repo = _paging_repository(pools)
        svc = _service(repo)

        svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=10,
            service_failure_type_map={SERVICE: ["TYPE_A", "TYPE_B", "TYPE_C"]},
        )

        # divmod(10, 3) = (3, 1): the remainder goes to the leading lane.
        assert _limits(repo)[:3] == [4, 3, 3]

    def test_share_an_empty_lane_did_not_use_is_re_offered(self):
        """Fairness is about contention. A lane whose pool is empty is not
        contending, so leaving its share unspent would just park work."""
        pools = {("TYPE_D", None): _lane_pool("d", 300)}
        repo = _paging_repository(pools)
        svc = _service(repo)

        result = svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=100,
            service_failure_type_map={
                SERVICE: ["TYPE_A", "TYPE_B", "TYPE_C", "TYPE_D"]
            },
        )

        # 25 in the first round, then the 75 the three empty lanes left.
        assert result.total == 100

    def test_re_offered_share_never_re_selects_the_entries_already_taken(self):
        """The second fill round hands the lane its own cursor back."""
        pools = {("TYPE_D", None): _lane_pool("d", 300)}
        repo = _paging_repository(pools)
        svc = _service(repo)

        svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=100,
            service_failure_type_map={
                SERVICE: ["TYPE_A", "TYPE_B", "TYPE_C", "TYPE_D"]
            },
        )

        replayed = [call.args[0] for call in svc._execute_replay.call_args_list]
        assert len(replayed) == 100
        assert len(set(replayed)) == 100

    def test_a_lane_that_returned_less_than_its_quota_is_not_re_offered(self):
        """It has nothing left; asking again is a wasted query per pass."""
        pools = {
            ("TYPE_A", None): _lane_pool("a", 1),
            ("TYPE_B", None): _lane_pool("b", 50),
        }
        repo = _paging_repository(pools)
        svc = _service(repo)

        svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=10,
            service_failure_type_map={SERVICE: ["TYPE_A", "TYPE_B"]},
        )

        lanes = _lanes_asked(repo)
        assert lanes.count(("TYPE_A", None)) == 1
        assert lanes.count(("TYPE_B", None)) == 2

    @pytest.mark.parametrize("continuation", [0, 1, 2, 3])
    def test_continuation_counter_rotates_which_lane_leads_the_fill(self, continuation):
        """A deadline landing mid-list always cuts from the tail, so a fixed
        order would starve the same lane on every pass of a chain."""
        types = ["TYPE_A", "TYPE_B", "TYPE_C", "TYPE_D"]
        pools = {(ft, None): _lane_pool(ft.lower(), 50) for ft in types}
        repo = _paging_repository(pools)
        svc = _service(repo)

        svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=40,
            service_failure_type_map={SERVICE: types},
            continuation=continuation,
        )

        assert _lanes_asked(repo)[0] == (types[continuation % len(types)], None)

    def test_every_lane_leads_exactly_once_over_a_full_rotation(self):
        types = ["TYPE_A", "TYPE_B", "TYPE_C", "TYPE_D"]
        pools = {(ft, None): _lane_pool(ft.lower(), 50) for ft in types}
        leaders = []

        for continuation in range(len(types)):
            repo = _paging_repository(pools)
            svc = _service(repo)
            svc.replay_on_circuit_close(
                service_name=SERVICE,
                max_items=40,
                service_failure_type_map={SERVICE: types},
                continuation=continuation,
            )
            leaders.append(_lanes_asked(repo)[0][0])

        assert sorted(leaders) == sorted(types)

    def test_deadline_reached_between_lanes_stops_selecting(self):
        """Four lanes each walking their scan bound over a slow store can
        spend a whole pass in selection; a pass that reaches its wall clock
        inside a selection call ends by being killed rather than returning."""
        types = ["TYPE_A", "TYPE_B", "TYPE_C"]
        pools = {(ft, None): _lane_pool(ft.lower(), 50) for ft in types}
        clock = _Clock()
        repo = _paging_repository(pools)
        svc = _service(repo)
        # Each selection call burns a second of the pass's budget.
        original = repo.find_replayable_page.side_effect

        def _slow_page(**kwargs):
            clock.advance(1.0)
            return original(**kwargs)

        repo.find_replayable_page.side_effect = _slow_page

        with patch("baldur.services.replay_service.service.time.monotonic", new=clock):
            result = svc.replay_on_circuit_close(
                service_name=SERVICE,
                max_items=30,
                service_failure_type_map={SERVICE: types},
                deadline=clock.now + 1.5,
            )

        assert _lanes_asked(repo) == [("TYPE_A", None), ("TYPE_B", None)]
        assert result.capped is True


# =============================================================================
# Deadline stop — cursor roll-back
# =============================================================================


class TestDeadlineCursorRollbackBehavior:
    """A pass that stops mid-replay must not step over what it left PENDING."""

    def test_rollback_names_each_lane_s_last_processed_entry(self):
        lane_a = _lane_key("TYPE_A", None)
        lane_b = _lane_key("TYPE_B", None)
        entries = _lane_pool("a", 3) + _lane_pool("b", 3)
        selection = _LaneSelection(
            selected=[(lane_a, e) for e in entries[:3]]
            + [(lane_b, e) for e in entries[3:]],
        )

        rolled = ReplayService._roll_back_lane_cursors(selection, 4, {})

        assert decode_replay_cursor(rolled[lane_a]) == (
            entries[2].created_at.timestamp(),
            entries[2].id,
        )
        assert decode_replay_cursor(rolled[lane_b]) == (
            entries[3].created_at.timestamp(),
            entries[3].id,
        )

    def test_a_lane_that_processed_nothing_keeps_the_cursor_it_came_in_with(self):
        """Rolling it to the position the *selection* reached would skip the
        entries that selection never replayed."""
        lane_a = _lane_key("TYPE_A", None)
        lane_b = _lane_key("TYPE_B", None)
        entries = _lane_pool("a", 2) + _lane_pool("b", 2)
        selection = _LaneSelection(
            selected=[(lane_a, e) for e in entries[:2]]
            + [(lane_b, e) for e in entries[2:]],
            cursors={lane_a: "9.000000|x", lane_b: "9.000000|y"},
        )

        rolled = ReplayService._roll_back_lane_cursors(
            selection, 1, {lane_b: "carried-b"}
        )

        assert rolled[lane_b] == "carried-b"

    def test_rollback_does_not_mutate_the_carried_cursors(self):
        carried = {_lane_key("TYPE_A", None): "carried"}
        selection = _LaneSelection(
            selected=[(_lane_key("TYPE_A", None), _entry("a", offset=1))]
        )

        ReplayService._roll_back_lane_cursors(selection, 1, carried)

        assert carried == {_lane_key("TYPE_A", None): "carried"}

    def test_deadline_stop_reports_only_what_it_processed(self):
        """The completion event and the daily report must not count entries
        the pass never touched."""
        pools = {("TYPE_A", None): _lane_pool("a", 100)}
        clock = _Clock()
        repo = _paging_repository(pools)
        svc = _service(repo, clock)

        with patch("baldur.services.replay_service.service.time.monotonic", new=clock):
            result = svc.replay_on_circuit_close(
                service_name=SERVICE,
                max_items=100,
                service_failure_type_map={SERVICE: ["TYPE_A"]},
                deadline=clock.now + 3.5,
            )

        assert svc._execute_replay.call_count == 4
        assert result.total == 4
        assert result.capped is True

    def test_next_pass_resumes_at_the_first_entry_the_deadline_left_behind(self):
        pools = {("TYPE_A", None): _lane_pool("a", 100)}
        clock = _Clock()
        repo = _paging_repository(pools)
        svc = _service(repo, clock)

        with patch("baldur.services.replay_service.service.time.monotonic", new=clock):
            first = svc.replay_on_circuit_close(
                service_name=SERVICE,
                max_items=100,
                service_failure_type_map={SERVICE: ["TYPE_A"]},
                deadline=clock.now + 3.5,
            )

        second_repo = _paging_repository(pools)
        second_svc = _service(second_repo)
        second_svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=10,
            service_failure_type_map={SERVICE: ["TYPE_A"]},
            lane_cursors=first.lane_cursors,
        )

        resumed = [call.args[0] for call in second_svc._execute_replay.call_args_list]
        assert resumed[0] == "a-004"

    def test_a_pass_that_ran_to_completion_carries_the_selection_cursor(self):
        """Nothing was left behind, so the cursor may advance to the end of
        what the pass selected."""
        pools = {("TYPE_A", None): _lane_pool("a", 5)}
        repo = _paging_repository(pools)
        svc = _service(repo)

        result = svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=3,
            service_failure_type_map={SERVICE: ["TYPE_A"]},
        )

        lane = _lane_key("TYPE_A", None)
        assert decode_replay_cursor(result.lane_cursors[lane]) == (
            pools[("TYPE_A", None)][2].created_at.timestamp(),
            "a-002",
        )


# =============================================================================
# The pass result — what a successor reads
# =============================================================================


class TestReplayOnCircuitCloseBehavior:
    """The three fields a continuation decides on, and the source scoping."""

    def test_operator_mapped_lanes_select_every_capture_source(self):
        """An operator mapped the type deliberately; narrowing it to one
        capture layer would silently drop entries they asked for."""
        pools = {("TYPE_A", None): _lane_pool("a", 3)}
        repo = _paging_repository(pools)
        svc = _service(repo)

        svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=10,
            service_failure_type_map={SERVICE: ["TYPE_A"]},
        )

        assert repo.find_replayable_page.call_args_list[0].kwargs["source"] is None

    def test_the_open_circuit_lane_is_scoped_to_policy_chain_captures(self):
        """A request-boundary layer files the same failure type under a
        path-inferred domain that may name a different, still-dead circuit."""
        pools = {(OPEN_CIRCUIT_FAILURE_TYPE, SERVICE): _lane_pool("oc", 3)}
        repo = _paging_repository(pools)
        svc = _service(repo)
        svc._resolve_open_circuit_replay_domain = MagicMock(
            wraps=lambda *_a, **_kw: SERVICE
        )

        svc.replay_on_circuit_close(
            service_name=SERVICE, max_items=10, service_failure_type_map={SERVICE: []}
        )

        call = repo.find_replayable_page.call_args_list[0]
        assert call.kwargs["domain"] == SERVICE
        assert call.kwargs["source"] == POLICY_CHAIN_CAPTURE_SOURCE

    def test_result_carries_a_cursor_per_lane_keyed_by_type_and_domain(self):
        pools = {
            ("TYPE_A", None): _lane_pool("a", 5),
            ("TYPE_B", None): _lane_pool("b", 5),
        }
        repo = _paging_repository(pools)
        svc = _service(repo)

        result = svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=4,
            service_failure_type_map={SERVICE: ["TYPE_A", "TYPE_B"]},
        )

        assert set(result.lane_cursors) == {"TYPE_A|", "TYPE_B|"}

    def test_a_lane_that_stopped_on_its_scan_bound_is_named_in_the_result(self):
        """An empty page means neither "drained" nor "give up" on its own, so
        the successor needs the lane named."""
        pools = {
            ("TYPE_A", None): [],
            ("TYPE_B", None): _lane_pool("b", 2),
        }
        repo = _paging_repository(pools, exhausted_lanes={("TYPE_A", None)})
        svc = _service(repo)

        result = svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=10,
            service_failure_type_map={SERVICE: ["TYPE_A", "TYPE_B"]},
        )

        assert result.scan_exhausted_lanes == ["TYPE_A|"]
        assert result.scan_exhausted is True

    def test_no_lane_on_its_bound_reports_an_unexhausted_sweep(self):
        pools = {("TYPE_A", None): _lane_pool("a", 2)}
        repo = _paging_repository(pools)
        svc = _service(repo)

        result = svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=10,
            service_failure_type_map={SERVICE: ["TYPE_A"]},
        )

        assert result.scan_exhausted_lanes == []
        assert result.scan_exhausted is False

    def test_a_pass_handed_cursors_resumes_instead_of_re_walking(self):
        pools = {("TYPE_A", None): _lane_pool("a", 10)}
        repo = _paging_repository(pools)
        svc = _service(repo)

        first = svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=3,
            service_failure_type_map={SERVICE: ["TYPE_A"]},
        )
        svc._execute_replay.reset_mock()
        svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=3,
            service_failure_type_map={SERVICE: ["TYPE_A"]},
            lane_cursors=first.lane_cursors,
            continuation=1,
        )

        resumed = [call.args[0] for call in svc._execute_replay.call_args_list]
        assert resumed == ["a-003", "a-004", "a-005"]

    def test_completion_event_carries_capped_from_the_sweep(self):
        """``capped`` has to mean the same thing on both emitting lanes, or a
        consumer cannot read it at all."""
        pools = {("TYPE_A", None): _lane_pool("a", 10)}
        repo = _paging_repository(pools)
        svc = _service(repo)
        svc._emit_event = MagicMock(wraps=lambda *_a, **_kw: None)

        svc.replay_on_circuit_close(
            service_name=SERVICE,
            max_items=3,
            service_failure_type_map={SERVICE: ["TYPE_A"]},
        )

        payloads = [
            call.kwargs["data"]
            for call in svc._emit_event.call_args_list
            if "data" in call.kwargs and "total" in call.kwargs["data"]
        ]
        assert payloads
        assert payloads[-1]["capped"] is True


# =============================================================================
# The chain-stop signal
# =============================================================================


class TestChainStopSignalBehavior:
    """``capped`` cannot say whether anything will come back for the rest."""

    def _emit(self, **kwargs):
        svc = _service(_paging_repository({}))
        svc._emit_event = MagicMock(wraps=lambda *_a, **_kw: None)
        with (
            patch(
                "baldur.metrics.event_handlers.ReplayEventHandler.on_replay_blocked"
            ) as metric,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ) as audit,
            capture_logs() as logs,
        ):
            svc.emit_circuit_close_chain_stopped(**kwargs)
        return svc, logs, metric, audit

    def test_all_four_block_channels_fire(self):
        svc, logs, metric, audit = self._emit(
            service_name=SERVICE, block_reason=REASON_CONTINUATION_BOUND_REACHED
        )

        assert [
            e
            for e in logs
            if e["event"] == "replay_service.circuit_close_chain_stopped"
        ]
        svc._emit_event.assert_called_once()
        metric.assert_called_once_with(SERVICE, REASON_CONTINUATION_BOUND_REACHED)
        audit.assert_called_once()

    def test_the_log_is_a_warning_because_the_lane_stopped_with_work_left(self):
        _, logs, _, _ = self._emit(
            service_name=SERVICE, block_reason=REASON_CONTINUATION_BOUND_REACHED
        )

        stopped = [
            e
            for e in logs
            if e["event"] == "replay_service.circuit_close_chain_stopped"
        ]
        assert stopped[0]["log_level"] == "warning"

    def test_payload_names_the_reason_and_the_positions_the_chain_reached(self):
        svc, logs, _, audit = self._emit(
            service_name=SERVICE,
            block_reason=REASON_CONTINUATION_BOUND_REACHED,
            scan_exhausted_lanes=["TYPE_A|"],
            lane_cursors={"TYPE_A|": "1.000000|a-1"},
        )

        event = svc._emit_event.call_args.kwargs["data"]
        assert event["block_reason"] == REASON_CONTINUATION_BOUND_REACHED
        assert event["trigger"] == "circuit_close"
        assert event["scan_exhausted_lanes"] == ["TYPE_A|"]
        assert event["lane_cursors"] == {"TYPE_A|": "1.000000|a-1"}
        assert audit.call_args.kwargs["reason"] == REASON_CONTINUATION_BOUND_REACHED

    def test_a_reopened_circuit_names_the_offending_spelling(self):
        """The projection from a protect() name onto a stored domain is
        many-to-one, so the domain alone does not identify what reopened."""
        svc, _, _, _ = self._emit(
            service_name=SERVICE,
            block_reason=REASON_CIRCUIT_REOPENED,
            offending_circuit="Payment-API",
        )

        assert (
            svc._emit_event.call_args.kwargs["data"]["offending_circuit"]
            == "Payment-API"
        )

    def test_offending_circuit_is_absent_when_no_circuit_caused_the_stop(self):
        svc, _, _, _ = self._emit(
            service_name=SERVICE, block_reason=REASON_CONTINUATION_BOUND_REACHED
        )

        assert "offending_circuit" not in svc._emit_event.call_args.kwargs["data"]

    def test_missing_lane_state_is_reported_as_empty_not_absent(self):
        """A consumer reading the payload must not have to branch on presence."""
        svc, _, _, _ = self._emit(
            service_name=SERVICE, block_reason=REASON_CONTINUATION_BOUND_REACHED
        )

        data = svc._emit_event.call_args.kwargs["data"]
        assert data["scan_exhausted_lanes"] == []
        assert data["lane_cursors"] == {}
