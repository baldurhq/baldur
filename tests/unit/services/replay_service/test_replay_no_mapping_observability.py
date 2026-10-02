"""
Tests for ReplayService.replay_on_circuit_close() no-lane observability (#496, 809).

Verifies the 4-channel signal surface emitted when the recovered service gets
no lane (no `service_failure_type_map` entry, no replay handler registered for
its domain) and entries are parked under its domain:

1. WARNING log `replay_service.circuit_close_replay_blocked` with
   `service_name`, `block_reason`, `healing_domain`, `pending`, `remediation`
2. EventBus emit `DLQ_REPLAY_BLOCKED` with payload carrying
   `trigger=circuit_close` and the same fields
3. `ReplayEventHandler.on_replay_blocked(service_name, REASON_...)` metric
   call
4. `log_dlq_replay_blocked_audit(domain="dlq", reason=..., service_name=...,
   trigger="circuit_close", details={healing_domain, pending, remediation})`

Parametrized over the 3 upstream map shapes that converge on this branch:
- empty top-level map (`{}`)
- foreign service mapped, target service absent
- target service mapped but value is an empty list

Negative control: a domain with a replay handler falls through to the
governance check; a mapping without one does not (a mapped lane replays through
the domain's handler).

809 D4 — the branch is loud only when work is left behind: nothing parked
under the name ends it with one DEBUG line and no blocked channel; work parked
under an addressable name names the missing handler; a name with no domain of
its own names that, whatever the store holds. The retired map-unconfigured
names appear in no channel.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, create_autospec, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.core.exceptions import DLQError
from baldur.interfaces.repositories import FailedOperationRepository
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.replay_service.service import (
    _REMEDIATION_DOMAIN_NOT_ADDRESSABLE,
    _REMEDIATION_NO_REPLAY_HANDLER,
    REASON_DOMAIN_NOT_ADDRESSABLE,
    REASON_NO_REPLAY_HANDLER,
    recovery_lanes,
)
from baldur.utils.domain_validation import FALLBACK_DOMAIN, resolve_stored_domain

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_event_bus():
    return MagicMock()


@pytest.fixture
def replay_service(mock_event_bus):
    """ReplayService with one entry parked under `payment_api` and a mock bus."""
    repository = MagicMock()
    repository.get_cluster_pending_count_by_domain.return_value = 1
    svc = ReplayService(repository=repository)
    svc._event_bus = mock_event_bus
    return svc


# The 3 misconfig entry shapes that all converge on the same branch.
MISCONFIG_PARAMS = pytest.mark.parametrize(
    "service_failure_type_map",
    [
        pytest.param({}, id="empty_top_level_map"),
        pytest.param({"other_svc": ["TIMEOUT"]}, id="foreign_service_only"),
        pytest.param({"payment_api": []}, id="target_service_empty_list"),
    ],
)


# =============================================================================
# Contract — module-level constants frozen by D5 + D7
# =============================================================================


class TestNoMappingObservabilityConstantsContract:
    """Module-level constants — string equality (Contract)."""

    def test_no_handler_reason_constant_value(self):
        """REASON_NO_REPLAY_HANDLER is the no-handler block reason."""
        assert REASON_NO_REPLAY_HANDLER == "no_replay_handler_registered"

    def test_domain_not_addressable_reason_constant_value(self):
        """REASON_DOMAIN_NOT_ADDRESSABLE is the no-domain block reason."""
        assert REASON_DOMAIN_NOT_ADDRESSABLE == "domain_not_addressable"


# =============================================================================
# Behavior — 4-channel signal surface (parametrized over misconfig shapes)
# =============================================================================


class TestReplayNoMappingObservabilityBehavior:
    """Misconfig branch emits the full 4-channel signal surface (D2-D7)."""

    @MISCONFIG_PARAMS
    def test_misconfig_emits_dlq_replay_blocked_event_with_full_payload(
        self,
        service_failure_type_map,
        replay_service,
        mock_event_bus,
    ):
        """DLQ_REPLAY_BLOCKED carries the trigger, the cause and the parked count."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        blocked_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BLOCKED
        ]
        assert len(blocked_calls) == 1
        data = blocked_calls[0][1]["data"]
        assert data == {
            "trigger": "circuit_close",
            "service_name": "payment_api",
            "block_reason": REASON_NO_REPLAY_HANDLER,
            "healing_domain": "payment_api",
            "pending": 1,
            "remediation": ANY,
        }
        assert "console" in data["remediation"]

    @MISCONFIG_PARAMS
    def test_misconfig_calls_on_replay_blocked_with_service_name_and_reason(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """Metric handler called with (service_name, REASON_...) — D3 arg order."""
        # ReplayEventHandler is imported lazily inside the misconfig branch,
        # so patch its module-level home rather than the service-side import.
        with (
            patch(
                "baldur.metrics.event_handlers.ReplayEventHandler.on_replay_blocked"
            ) as mock_metric,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        mock_metric.assert_called_once_with("payment_api", REASON_NO_REPLAY_HANDLER)

    @MISCONFIG_PARAMS
    def test_misconfig_calls_log_dlq_replay_blocked_audit_with_full_kwargs(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """Audit helper called with domain/reason/service_name/trigger/details."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
        ) as mock_audit:
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        mock_audit.assert_called_once_with(
            domain="dlq",
            reason=REASON_NO_REPLAY_HANDLER,
            service_name="payment_api",
            trigger="circuit_close",
            details={
                "healing_domain": "payment_api",
                "pending": 1,
                "remediation": ANY,
            },
        )

    # 525 D4: xdist mock_leak — structlog capture_logs context races with
    # sibling tests under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    @MISCONFIG_PARAMS
    def test_misconfig_emits_warning_level_log_with_structured_fields(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """WARNING log `replay_service.circuit_close_replay_blocked` carries structured fields."""
        with (
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
            capture_logs() as cap_logs,
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        matching = [
            entry
            for entry in cap_logs
            if entry.get("event") == "replay_service.circuit_close_replay_blocked"
        ]
        assert len(matching) == 1
        log = matching[0]
        assert log["log_level"] == "warning"
        assert log["service_name"] == "payment_api"
        assert log["block_reason"] == REASON_NO_REPLAY_HANDLER
        assert log["healing_domain"] == "payment_api"
        assert log["pending"] == 1

    @MISCONFIG_PARAMS
    def test_misconfig_returns_empty_batch_replay_result(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """Misconfig branch returns BatchReplayResult() with no items processed."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
        ):
            result = replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        assert result.total == 0
        assert result.success_count == 0
        assert result.failed_count == 0
        assert result.governance_blocked is False

    @MISCONFIG_PARAMS
    def test_misconfig_bypasses_governance_check(
        self,
        service_failure_type_map,
        replay_service,
    ):
        """D1 isolation: misconfig early-return precedes check_all_governance call."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map=service_failure_type_map,
            )

        mock_governance.assert_not_called()


# =============================================================================
# Negative control — a domain with a replay handler flows through to governance
# =============================================================================


class _PaymentHandler(ReplayHandler):
    """A replay handler for `payment_api`: the precondition of every lane."""

    @property
    def domain(self) -> str:
        return "payment_api"

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id)


@pytest.fixture
def payment_handler():
    saved = dict(_replay_handlers)
    _replay_handlers.clear()
    register_replay_handler(_PaymentHandler())
    yield
    _replay_handlers.clear()
    _replay_handlers.update(saved)


class TestReplayNoMappingObservabilityNegativeControlBehavior:
    """A handler gives the domain lanes: the no-lane branch is NOT taken."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_handler_domain_does_not_emit_the_no_lane_log(
        self, replay_service, payment_handler
    ):
        """No `replay_service.circuit_close_replay_blocked` log with a handler."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
            capture_logs() as cap_logs,
        ):
            mock_governance.return_value = MagicMock(
                allowed=False, block_reason=None, block_message="stub"
            )
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
            )

        misconfig_logs = [
            e
            for e in cap_logs
            if e.get("event") == "replay_service.circuit_close_replay_blocked"
        ]
        assert misconfig_logs == []

    def test_handler_domain_does_not_call_the_blocked_audit_helper(
        self, replay_service, payment_handler
    ):
        """log_dlq_replay_blocked_audit is NOT called when the domain has lanes."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ) as mock_audit,
        ):
            mock_governance.return_value = MagicMock(
                allowed=False, block_reason=None, block_message="stub"
            )
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
            )

        mock_audit.assert_not_called()

    def test_handler_domain_invokes_governance_check(
        self, replay_service, payment_handler
    ):
        """A domain with lanes proceeds past the no-lane early-return into governance."""
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
            ) as mock_governance,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ),
        ):
            mock_governance.return_value = MagicMock(
                allowed=False, block_reason=None, block_message="stub"
            )
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
            )

        mock_governance.assert_called_once()

    def test_a_mapping_without_a_handler_still_takes_the_no_lane_branch(
        self, replay_service
    ):
        """A mapped lane replays through the domain's handler: without one the
        mapping gives no lane, and the parked work is reported as such."""
        with (
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit"
            ) as mock_audit,
        ):
            replay_service.replay_on_circuit_close(
                service_name="payment_api",
                service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
            )

        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["reason"] == REASON_NO_REPLAY_HANDLER


# =============================================================================
# Behavior — duplicate failure types (D5 dedup interaction)
# =============================================================================


class TestReplayNoMappingDedupBehavior:
    """Order-preserving dedup at the operator boundary."""

    def test_duplicate_failure_types_give_one_lane(self, payment_handler):
        """`["TIMEOUT", "TIMEOUT"]` dedups to one mapped lane, not two."""
        lanes = recovery_lanes("payment_api", {"payment_api": ["TIMEOUT", "TIMEOUT"]})

        assert [lane for lane in lanes if lane[0] == "TIMEOUT"] == [
            ("TIMEOUT", "payment_api", None)
        ]


# =============================================================================
# Behavior — quiet with nothing parked, loud naming the cause otherwise (809 D4)
# =============================================================================

# An LLM endpoint identity: a domain of its own that nothing is parked under.
LLM_ENDPOINT = "llm.api_example_com.gpt_4o"
# 65 characters: over the domain length limit, so it has no domain of its own.
UNADDRESSABLE = "a" * 65

_RETIRED_LOG_EVENT = "replay_service.no_failure_types_mapped"
_RETIRED_REASON = "service_failure_type_map_unconfigured"
_RETIRED_KEY = "config_path"


def _sweep_with_no_lane(service_name: str, parked) -> SimpleNamespace:
    """Run one recovery pass for a name with no lane, capturing every channel.

    ``parked`` is what the strict pending count answers, or an exception it
    raises instead.
    """
    repository = create_autospec(FailedOperationRepository, instance=True)
    if isinstance(parked, Exception):
        repository.get_cluster_pending_count_by_domain.side_effect = parked
    else:
        repository.get_cluster_pending_count_by_domain.return_value = parked
    service = ReplayService(repository=repository, cache=InMemoryCacheAdapter())
    bus = MagicMock(spec=BaldurEventBus)
    service._event_bus = bus

    with (
        patch(
            "baldur.metrics.event_handlers.ReplayEventHandler.on_replay_blocked",
            autospec=True,
        ) as metric,
        patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
            autospec=True,
        ) as audit,
        capture_logs() as logs,
    ):
        result = service.replay_on_circuit_close(
            service_name=service_name, service_failure_type_map={}
        )

    blocked_events = [
        c.kwargs["data"]
        for c in bus.emit.call_args_list
        if c.args[0] == EventType.DLQ_REPLAY_BLOCKED
    ]
    return SimpleNamespace(
        result=result,
        repository=repository,
        logs=logs,
        blocked_events=blocked_events,
        metric=metric,
        audit=audit,
    )


class TestNoLaneRecoverySignalBehavior:
    """The no-lane branch speaks only for work it leaves behind."""

    def test_no_lane_with_nothing_parked_logs_debug_and_writes_no_blocked_channel(
        self,
    ):
        """A recovery of a breaker that parked nothing is a finished recovery."""
        sweep = _sweep_with_no_lane(LLM_ENDPOINT, 0)

        skipped = [
            e
            for e in sweep.logs
            if e["event"] == "replay_service.circuit_close_replay_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["log_level"] == "debug"
        assert skipped[0]["reason"] == "nothing_parked"
        assert skipped[0]["healing_domain"] == resolve_stored_domain(LLM_ENDPOINT)
        assert [e for e in sweep.logs if e["log_level"] not in ("debug", "info")] == []
        assert sweep.blocked_events == []
        sweep.metric.assert_not_called()
        sweep.audit.assert_not_called()
        assert sweep.result.total == 0

    @pytest.mark.parametrize(
        ("parked", "pending"),
        [(2, 2), (DLQError("redis_inactive"), None)],
        ids=["two_parked", "count_unavailable"],
    )
    def test_no_lane_with_work_left_names_the_missing_handler_on_every_channel(
        self, parked, pending
    ):
        """Parked work under an addressable name waits for its handler."""
        service_name = "payment_api"
        sweep = _sweep_with_no_lane(service_name, parked)
        domain = resolve_stored_domain(service_name)
        details = {
            "healing_domain": domain,
            "pending": pending,
            "remediation": _REMEDIATION_NO_REPLAY_HANDLER,
        }

        blocked = [
            e
            for e in sweep.logs
            if e["event"] == "replay_service.circuit_close_replay_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["block_reason"] == REASON_NO_REPLAY_HANDLER
        assert blocked[0]["pending"] == pending
        assert sweep.blocked_events == [
            {
                "trigger": "circuit_close",
                "service_name": service_name,
                "block_reason": REASON_NO_REPLAY_HANDLER,
                **details,
            }
        ]
        sweep.metric.assert_called_once_with(service_name, REASON_NO_REPLAY_HANDLER)
        sweep.audit.assert_called_once_with(
            domain="dlq",
            reason=REASON_NO_REPLAY_HANDLER,
            service_name=service_name,
            trigger="circuit_close",
            details=details,
        )

    def test_no_lane_name_without_a_domain_stays_loud_whatever_the_store_holds(
        self,
    ):
        """The pooled bucket is never counted, so an empty store cannot quiet it."""
        sweep = _sweep_with_no_lane(UNADDRESSABLE, 0)

        sweep.repository.get_cluster_pending_count_by_domain.assert_not_called()
        assert sweep.blocked_events == [
            {
                "trigger": "circuit_close",
                "service_name": UNADDRESSABLE,
                "block_reason": REASON_DOMAIN_NOT_ADDRESSABLE,
                "healing_domain": FALLBACK_DOMAIN,
                "pending": None,
                "remediation": _REMEDIATION_DOMAIN_NOT_ADDRESSABLE,
            }
        ]
        sweep.metric.assert_called_once_with(
            UNADDRESSABLE, REASON_DOMAIN_NOT_ADDRESSABLE
        )

    def test_no_lane_blocked_surface_carries_none_of_the_retired_names(self):
        """The map-unconfigured signal pointed at a remedy that escalates the work."""
        sweep = _sweep_with_no_lane("payment_api", 1)

        audit_kwargs = sweep.audit.call_args.kwargs
        channels = [
            *sweep.logs,
            *sweep.blocked_events,
            audit_kwargs,
            audit_kwargs["details"],
        ]
        assert [e for e in sweep.logs if e["event"] == _RETIRED_LOG_EVENT] == []
        assert all(_RETIRED_KEY not in channel for channel in channels)
        assert all(
            _RETIRED_REASON not in map(str, channel.values()) for channel in channels
        )
        assert _RETIRED_REASON not in sweep.metric.call_args.args
