"""
Tests for ReplayService.
DLQ 재생 서비스(replay_service.py)의 단위 테스트.
거버넌스 체크, 단건/배치 재생, 핸들러 레지스트리 등을 검증합니다.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.replay_service import (
    BatchReplayResult,
    DefaultReplayHandler,
    ReplayHandler,
    ReplayResult,
    ReplayService,
    _replay_handlers,
    get_replay_handler,
    register_replay_handler,
)
from baldur_pro.services.governance.checks import GovernanceCheckResult

# =============================================================================
# Fixtures
# =============================================================================


@dataclass
class FakeFailedOperationData:
    """테스트용 FailedOperationData 대체 데이터클래스."""

    id: int
    domain: str = "payment"
    status: str = "pending"
    failure_type: str = "PG_TIMEOUT"
    retry_count: int = 0
    error_code: str = ""
    error_message: str = ""
    snapshot_data: dict = None
    request_data: dict = None
    response_data: dict = None
    metadata: dict = None

    def __post_init__(self):
        self.snapshot_data = self.snapshot_data or {}
        self.request_data = self.request_data or {}
        self.response_data = self.response_data or {}
        self.metadata = self.metadata or {}


class FakeReplayHandler(ReplayHandler):
    """테스트용 ReplayHandler 구현체."""

    def __init__(self, domain_name: str, success: bool = True):
        self._domain = domain_name
        self._success = success

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        if self._success:
            return ReplayResult.succeeded(failed_op.id, "Replayed OK")
        return ReplayResult.failed(failed_op.id, "Handler says no")


@pytest.fixture(autouse=True)
def _clear_handler_registry():
    """각 테스트 전후로 핸들러 레지스트리를 초기화."""
    _replay_handlers.clear()
    yield
    _replay_handlers.clear()


@pytest.fixture
def mock_repository():
    """Mock FailedOperationRepository를 생성."""
    repo = MagicMock()
    repo.try_acquire_for_replay.return_value = FakeFailedOperationData(id=1)
    repo.get_by_id.return_value = FakeFailedOperationData(id=1)
    repo.complete_replay.return_value = None
    repo.find_replayable.return_value = []
    return repo


# =============================================================================
# ReplayResult Tests
# =============================================================================


class TestReplayResult:
    """ReplayResult 데이터클래스 팩토리 메서드 테스트."""

    def test_succeeded_factory(self):
        """Succeeded factory
        성공 팩토리가 올바른 값을 반환하는지 확인.
        """
        result = ReplayResult.succeeded(1, "OK", data={"key": "value"})
        assert result.success is True
        assert result.dlq_id == 1
        assert result.message == "OK"
        assert result.data == {"key": "value"}

    def test_failed_factory(self):
        """Failed factory
        실패 팩토리가 올바른 값을 반환하는지 확인.
        """
        result = ReplayResult.failed(2, "timeout")
        assert result.success is False
        assert result.dlq_id == 2
        assert result.error == "timeout"

    def test_blocked_factory(self):
        """Blocked factory
        차단 팩토리가 거버넌스 정보를 포함하는지 확인.
        """
        governance = MagicMock(spec=GovernanceCheckResult)
        governance.block_message = "Kill Switch active"
        governance.block_reason = MagicMock()
        governance.block_reason.value = "kill_switch"

        result = ReplayResult.blocked(3, governance)
        assert result.success is False
        assert result.data["blocked"] is True
        assert result.data["block_reason"] == "kill_switch"


# =============================================================================
# BatchReplayResult Tests
# =============================================================================


class TestBatchReplayResult:
    """BatchReplayResult 데이터클래스 테스트."""

    def test_default_values(self):
        """Default values
        기본값이 올바르게 초기화되는지 확인.
        """
        result = BatchReplayResult()
        assert result.total == 0
        assert result.success_count == 0
        assert result.failed_count == 0
        assert result.governance_blocked is False

    def test_priority_metadata(self):
        """Priority metadata
        우선순위 기반 재생 정보가 올바르게 설정되는지 확인.
        """
        result = BatchReplayResult(
            priority_used=True,
            domains_processed=["payment", "notification"],
        )
        assert result.priority_used is True
        assert result.domains_processed == ["payment", "notification"]


# =============================================================================
# DefaultReplayHandler Tests
# =============================================================================


class TestDefaultReplayHandler:
    """DefaultReplayHandler 테스트."""

    def test_can_replay_returns_false(self):
        """Can replay returns false
        기본 핸들러는 항상 can_replay=False를 반환하는지 확인.
        """
        handler = DefaultReplayHandler("unknown")
        can, reason = handler.can_replay(FakeFailedOperationData(id=1))
        assert can is False
        assert "unknown" in reason

    def test_replay_returns_failed(self):
        """Replay returns failed
        기본 핸들러의 replay가 실패 결과를 반환하는지 확인.
        """
        handler = DefaultReplayHandler("unknown")
        result = handler.replay(FakeFailedOperationData(id=1))
        assert result.success is False
        assert "register" in result.error.lower()

    def test_domain_property(self):
        """Domain property
        domain 프로퍼티가 올바른 값을 반환하는지 확인.
        """
        handler = DefaultReplayHandler("payment")
        assert handler.domain == "payment"


# =============================================================================
# Handler Registry Tests
# =============================================================================


class TestHandlerRegistry:
    """핸들러 레지스트리 테스트."""

    def test_register_and_get(self):
        """Register and get
        핸들러 등록 후 조회가 올바르게 동작하는지 확인.
        """
        handler = FakeReplayHandler("payment")
        register_replay_handler(handler)
        retrieved = get_replay_handler("payment")
        assert retrieved is handler

    def test_get_unregistered_returns_default(self):
        """Get unregistered returns default
        등록되지 않은 도메인 조회 시 DefaultReplayHandler가 반환되는지 확인.
        """
        handler = get_replay_handler("nonexistent")
        assert isinstance(handler, DefaultReplayHandler)

    def test_overwrite_handler(self):
        """Overwrite handler
        같은 도메인으로 재등록하면 덮어쓰기 되는지 확인.
        """
        handler1 = FakeReplayHandler("payment", success=True)
        handler2 = FakeReplayHandler("payment", success=False)
        register_replay_handler(handler1)
        register_replay_handler(handler2)
        assert get_replay_handler("payment") is handler2


# =============================================================================
# ReplayService Tests
# =============================================================================


class TestReplayServiceReplaySingle:
    """ReplayService.replay_single 테스트."""

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_successful_replay(self, mock_audit, mock_gov, mock_repository):
        """Successful replay
        거버넌스 통과 + 핸들러 성공 시 ReplayResult.success=True인지 확인.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        handler = FakeReplayHandler("payment", success=True)
        register_replay_handler(handler)

        service = ReplayService(repository=mock_repository)
        result = service.replay_single(dlq_id=1)

        assert result.success is True
        mock_repository.complete_replay.assert_called_once()

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_governance_blocked(self, mock_gov, mock_repository):
        """Governance blocked
        거버넌스 차단 시 replay_single이 blocked 결과를 반환하는지 확인.
        """
        mock_gov.return_value = MagicMock(
            allowed=False,
            block_message="Kill Switch",
            block_reason=MagicMock(value="kill_switch"),
        )
        service = ReplayService(repository=mock_repository)
        result = service.replay_single(dlq_id=1)

        assert result.success is False
        assert result.data["blocked"] is True

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_entry_not_found(self, mock_audit, mock_gov, mock_repository):
        """Entry not found
        DLQ 엔트리를 찾을 수 없을 때 적절한 에러 메시지를 반환하는지 확인.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.try_acquire_for_replay.return_value = None
        mock_repository.get_by_id.return_value = None

        service = ReplayService(repository=mock_repository)
        result = service.replay_single(dlq_id=999)

        assert result.success is False
        assert "not found" in result.error

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_max_replays_exceeded(self, mock_audit, mock_gov, mock_repository):
        """Max replays exceeded
        최대 재시도 횟수 초과 시 적절한 에러 메시지를 반환하는지 확인.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.try_acquire_for_replay.return_value = None
        mock_repository.get_by_id.return_value = FakeFailedOperationData(
            id=1, status="pending"
        )

        service = ReplayService(repository=mock_repository)
        result = service.replay_single(dlq_id=1)

        assert result.success is False
        assert "max_replays_exceeded" in result.error

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_handler_crash_escalates(self, mock_audit, mock_gov, mock_repository):
        """Handler crash escalates
        핸들러가 예외를 발생시키면 에러가 적절히 처리되는지 확인.
        """
        mock_gov.return_value = MagicMock(allowed=True)

        class CrashHandler(ReplayHandler):
            @property
            def domain(self):
                return "payment"

            def can_replay(self, failed_op):
                return True, ""

            def replay(self, failed_op):
                raise RuntimeError("Handler exploded")

        register_replay_handler(CrashHandler())

        service = ReplayService(repository=mock_repository)
        result = service.replay_single(dlq_id=1)

        assert result.success is False
        assert "internal_error" in result.error
        mock_repository.complete_replay.assert_called_once()

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_non_pending_entry_rejected(self, mock_audit, mock_gov, mock_repository):
        """Non-pending entry rejected
        pending 상태가 아닌 엔트리는 재생이 거부되는지 확인.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.try_acquire_for_replay.return_value = None
        mock_repository.get_by_id.return_value = FakeFailedOperationData(
            id=1, status="resolved"
        )

        service = ReplayService(repository=mock_repository)
        result = service.replay_single(dlq_id=1)

        assert result.success is False
        assert "resolved" in result.error


class TestReplayServiceReplayBatch:
    """ReplayService.replay_batch 테스트."""

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_governance_blocked_batch(self, mock_gov, mock_repository):
        """Governance blocked batch
        거버넌스 차단 시 배치 재생이 차단되는지 확인.
        """
        mock_gov.return_value = MagicMock(
            allowed=False,
            block_message="Emergency Level",
        )
        service = ReplayService(repository=mock_repository)
        result = service.replay_batch(domain="payment")

        assert result.governance_blocked is True
        assert result.total == 0

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_empty_batch(self, mock_gov, mock_repository):
        """Empty batch
        대상 엔트리가 없을 때 빈 결과를 반환하는지 확인.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        service = ReplayService(repository=mock_repository)
        result = service.replay_batch(domain="payment")

        assert result.total == 0
        assert result.success_count == 0


class TestReplayServiceReplayOnCircuitClose:
    """ReplayService.replay_on_circuit_close 테스트."""

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_no_failure_types_mapped(self, mock_gov, mock_repository):
        """No failure types mapped
        서비스에 매핑된 failure_type이 없으면 빈 결과를 반환하는지 확인.
        """
        service = ReplayService(repository=mock_repository)
        result = service.replay_on_circuit_close(service_name="unknown_service")

        assert result.total == 0

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_with_failure_type_map(self, mock_audit, mock_gov, mock_repository):
        """With failure type map
        커스텀 매핑을 통해 적절한 엔트리가 재생되는지 확인.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        entry = FakeFailedOperationData(id=10, domain="payment")
        mock_repository.find_replayable.return_value = [entry]
        mock_repository.try_acquire_for_replay.return_value = entry

        handler = FakeReplayHandler("payment", success=True)
        register_replay_handler(handler)

        service = ReplayService(repository=mock_repository)
        result = service.replay_on_circuit_close(
            service_name="pg",
            service_failure_type_map={"pg": ["PG_TIMEOUT"]},
        )

        assert result.total == 1


# 518 batch (b) — the 392-C module-level `check_all_governance` fallback
# was retired. ReplayService now resolves governance via
# `ProviderRegistry.governance.get()`, which falls back to
# `NoOpGovernanceChecker` (returns `allowed_result()`) when PRO is absent.
# Fail-open behavior is covered by `tests/unit/interfaces/test_governance.py`
# (TestNoOpGovernanceCheckerBehavior).


# =============================================================================
# 442 — Phantom Method Replacement Tests
# =============================================================================


class TestReplayBatchFindReplayableBehavior:
    """Verify replay_batch() calls find_replayable() with correct parameter mapping (442-G1)."""

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_replay_batch_calls_find_replayable_with_correct_params(
        self, mock_audit, mock_gov, mock_repository
    ):
        """find_replayable() receives max_retries (not max_retry_count), domain, failure_type, limit."""
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        service = ReplayService(repository=mock_repository)
        service.replay_batch(domain="payment", failure_type="PG_TIMEOUT", max_items=25)

        mock_repository.find_replayable.assert_called_once()
        call_kwargs = mock_repository.find_replayable.call_args.kwargs
        assert "max_retries" in call_kwargs
        assert "max_retry_count" not in call_kwargs
        assert call_kwargs["domain"] == "payment"
        assert call_kwargs["failure_type"] == "PG_TIMEOUT"
        assert call_kwargs["limit"] == 25


class TestReplayOnCircuitCloseMultiTypeBehavior:
    """Verify replay_on_circuit_close() loops find_replayable() per failure_type (442-G2).

    The loop queries find_replayable() once per failure_type, aggregating results
    with a remaining counter to respect the overall max_items limit.
    """

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_single_failure_type_calls_find_replayable_once(
        self, mock_audit, mock_gov, mock_repository
    ):
        """Single failure_type in map results in exactly one find_replayable() call."""
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        service = ReplayService(repository=mock_repository)
        service.replay_on_circuit_close(
            service_name="pg",
            service_failure_type_map={"pg": ["PG_TIMEOUT"]},
        )

        mock_repository.find_replayable.assert_called_once()
        assert (
            mock_repository.find_replayable.call_args.kwargs["failure_type"]
            == "PG_TIMEOUT"
        )

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_multiple_failure_types_queries_each_type(
        self, mock_audit, mock_gov, mock_repository
    ):
        """Three failure_types produce three find_replayable() calls, one per type."""
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        service = ReplayService(repository=mock_repository)
        service.replay_on_circuit_close(
            service_name="pg",
            service_failure_type_map={"pg": ["TIMEOUT", "CONN_ERR", "DNS_FAIL"]},
        )

        assert mock_repository.find_replayable.call_count == 3
        called_types = [
            c.kwargs["failure_type"]
            for c in mock_repository.find_replayable.call_args_list
        ]
        assert called_types == ["TIMEOUT", "CONN_ERR", "DNS_FAIL"]

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_per_type_fairness_quota_caps_each_type(
        self, mock_audit, mock_gov, mock_repository
    ):
        """divmod quota allocation: first type's backlog cannot starve others.

        Pre-#472 behavior allowed the first failure_type to consume the entire
        max_items budget; the unified quota loop now allocates max_items // N
        (with divmod remainder) per type so each mapped failure_type receives
        a fair share on circuit-close replay.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        # Given: TYPE_A has more entries than its quota; TYPE_B has fewer
        entries_by_type = {
            "TYPE_A": [
                FakeFailedOperationData(id=i, domain="payment") for i in range(10)
            ],
            "TYPE_B": [
                FakeFailedOperationData(id=100 + i, domain="payment") for i in range(2)
            ],
        }
        # side_effect honors the limit kwarg so quota enforcement is verifiable
        mock_repository.find_replayable.side_effect = (
            lambda max_retries, failure_type, limit, **kw: entries_by_type[
                failure_type
            ][:limit]
        )
        mock_repository.try_acquire_for_replay.side_effect = (
            lambda id, *args, **kwargs: FakeFailedOperationData(id=id)
        )

        handler = FakeReplayHandler("payment", success=True)
        register_replay_handler(handler)

        service = ReplayService(repository=mock_repository)
        result = service.replay_on_circuit_close(
            service_name="pg",
            max_items=10,
            service_failure_type_map={"pg": ["TYPE_A", "TYPE_B"]},
        )

        # Then: both types queried with limit=5 each (10 // 2 = 5, no remainder)
        assert mock_repository.find_replayable.call_count == 2
        limits = [
            c.kwargs["limit"] for c in mock_repository.find_replayable.call_args_list
        ]
        assert limits == [5, 5]
        # TYPE_A returns its quota of 5; TYPE_B returns only 2 (under-quota)
        # Budget invariant: result.total never exceeds max_items
        assert result.total == 7
        assert result.total <= 10

    @pytest.mark.parametrize(
        ("n_types", "max_items", "expected_limits"),
        [
            (1, 50, [50]),
            (2, 10, [5, 5]),
            (3, 10, [4, 3, 3]),
            (3, 2, [1, 1]),
        ],
        ids=[
            "n1_no_remainder",
            "n2_exact_division",
            "n3_divmod_remainder",
            "n3_zero_quota_break",
        ],
    )
    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_quota_distribution_across_failure_types(
        self,
        mock_audit,
        mock_gov,
        mock_repository,
        n_types,
        max_items,
        expected_limits,
    ):
        """Per-call limit kwarg matches divmod quota distribution.

        Covers single-type fast-path equivalence, exact division, divmod
        remainder distribution (first `extra` types receive +1), and the
        max_items < N edge where trailing zero-quota iterations break early.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        failure_types = [f"TYPE_{i}" for i in range(n_types)]

        service = ReplayService(repository=mock_repository)
        service.replay_on_circuit_close(
            service_name="pg",
            max_items=max_items,
            service_failure_type_map={"pg": failure_types},
        )

        actual_limits = [
            c.kwargs["limit"] for c in mock_repository.find_replayable.call_args_list
        ]
        assert actual_limits == expected_limits

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_duplicate_failure_types_collapsed_at_boundary(
        self, mock_audit, mock_gov, mock_repository
    ):
        """Operator-supplied duplicate failure_types collapse to one quota slot.

        Without dedup, divmod would split max_items across the duplicate slots,
        diluting the budget by issuing repeated queries against the same ID pool.
        """
        mock_gov.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        service = ReplayService(repository=mock_repository)
        service.replay_on_circuit_close(
            service_name="pg",
            max_items=10,
            service_failure_type_map={"pg": ["TIMEOUT", "TIMEOUT"]},
        )

        assert mock_repository.find_replayable.call_count == 1
        assert mock_repository.find_replayable.call_args.kwargs["limit"] == 10
        assert (
            mock_repository.find_replayable.call_args.kwargs["failure_type"]
            == "TIMEOUT"
        )

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_empty_result_mid_loop_continues_to_next_type(
        self, mock_audit, mock_gov, mock_repository
    ):
        """Empty find_replayable() for one type does not decrement remaining; loop continues."""
        mock_gov.return_value = MagicMock(allowed=True)
        entry_c = FakeFailedOperationData(id=99, domain="payment")
        mock_repository.find_replayable.side_effect = [
            [],  # TYPE_A: empty
            [],  # TYPE_B: empty
            [entry_c],  # TYPE_C: one entry
        ]
        mock_repository.try_acquire_for_replay.return_value = entry_c

        handler = FakeReplayHandler("payment", success=True)
        register_replay_handler(handler)

        service = ReplayService(repository=mock_repository)
        result = service.replay_on_circuit_close(
            service_name="pg",
            max_items=50,
            service_failure_type_map={"pg": ["TYPE_A", "TYPE_B", "TYPE_C"]},
        )

        # Then: all 3 types queried, total reflects only TYPE_C's entry
        assert mock_repository.find_replayable.call_count == 3
        assert result.total == 1

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_empty_failure_types_returns_empty_result(
        self, mock_audit, mock_gov, mock_repository
    ):
        """Empty failure_types list returns BatchReplayResult with total=0."""
        service = ReplayService(repository=mock_repository)
        result = service.replay_on_circuit_close(
            service_name="pg",
            service_failure_type_map={"pg": []},
        )

        assert result.total == 0
        mock_repository.find_replayable.assert_not_called()


class TestReplayOnCircuitCloseEscalationBehavior:
    """Verify escalation uses update_status() with correct parameters (442-G3)."""

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_escalation_calls_update_status_with_requires_review(
        self, mock_audit, mock_gov, mock_repository
    ):
        """Failed replay + escalate_failures=True calls update_status with status='requires_review'."""
        mock_gov.return_value = MagicMock(allowed=True)
        # Given: one entry that fails replay
        entry = FakeFailedOperationData(id=42, domain="payment")
        mock_repository.find_replayable.return_value = [entry]
        mock_repository.get_by_id.return_value = FakeFailedOperationData(
            id=42, domain="payment", status="pending"
        )

        handler = FakeReplayHandler("payment", success=False)
        register_replay_handler(handler)

        # When
        service = ReplayService(repository=mock_repository)
        service.replay_on_circuit_close(
            service_name="pg",
            escalate_failures=True,
            service_failure_type_map={"pg": ["PG_TIMEOUT"]},
        )

        # Then
        mock_repository.update_status.assert_called_once()
        call_kwargs = mock_repository.update_status.call_args
        assert call_kwargs[0][0] == 42  # id
        assert call_kwargs[1]["status"] == "requires_review"
        assert "resolution_note" in call_kwargs[1]
        assert call_kwargs[1]["recommended_action"] == "escalate"

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_escalation_resolution_note_contains_service_and_error(
        self, mock_audit, mock_gov, mock_repository
    ):
        """resolution_note includes service_name and error message."""
        mock_gov.return_value = MagicMock(allowed=True)
        entry = FakeFailedOperationData(id=7, domain="payment")
        mock_repository.find_replayable.return_value = [entry]
        mock_repository.get_by_id.return_value = FakeFailedOperationData(
            id=7, domain="payment", status="pending"
        )

        handler = FakeReplayHandler("payment", success=False)
        register_replay_handler(handler)

        service = ReplayService(repository=mock_repository)
        service.replay_on_circuit_close(
            service_name="my_api",
            escalate_failures=True,
            service_failure_type_map={"my_api": ["CONN_ERR"]},
        )

        note = mock_repository.update_status.call_args[1]["resolution_note"]
        assert "my_api" in note
        assert "Conditional replay failed" in note

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    @patch("baldur.services.replay_service.log_dlq_replay_audit")
    def test_no_escalation_when_flag_disabled(
        self, mock_audit, mock_gov, mock_repository
    ):
        """escalate_failures=False does not call update_status on failure."""
        mock_gov.return_value = MagicMock(allowed=True)
        entry = FakeFailedOperationData(id=5, domain="payment")
        mock_repository.find_replayable.return_value = [entry]

        handler = FakeReplayHandler("payment", success=False)
        register_replay_handler(handler)

        service = ReplayService(repository=mock_repository)
        service.replay_on_circuit_close(
            service_name="pg",
            escalate_failures=False,
            service_failure_type_map={"pg": ["PG_TIMEOUT"]},
        )

        mock_repository.update_status.assert_not_called()


# =============================================================================
# ReplayService._get_governance — lazy cache + fail-open (518 batch b)
# =============================================================================


class TestReplayServiceGovernanceResolveBehavior:
    """``_get_governance()`` resolves the GovernanceChecker lazily and caches it.

    Lazy resolution (not eager in __init__) so test fixtures, REPL sessions,
    and Django auto-discovery that construct ReplayService before
    ``baldur.init()`` registers the PRO provider stay fail-open via the OSS
    NoOp default. Precedent: ``ThrottleGovernanceGuard._get_governance()``.
    """

    def test_init_does_not_resolve_governance_eagerly(self, mock_repository):
        service = ReplayService(repository=mock_repository)

        # No lookup happened during __init__ — state flag still False.
        assert service._governance is None
        assert service._governance_resolved is False

    def test_first_call_resolves_and_sets_resolved_flag(self, mock_repository):
        from baldur.interfaces.governance import GovernanceChecker

        service = ReplayService(repository=mock_repository)

        checker = service._get_governance()

        assert checker is not None
        assert isinstance(checker, GovernanceChecker)
        assert service._governance_resolved is True
        assert service._governance is checker

    def test_subsequent_calls_return_cached_instance(self, mock_repository):
        """Idempotency: ``ProviderRegistry.governance.get()`` is only consulted once."""
        service = ReplayService(repository=mock_repository)

        first = service._get_governance()
        # Poison the registry — cached call must not consult it again.
        with patch(
            "baldur.factory.registry.ProviderRegistry.governance"
        ) as mock_registry:
            mock_registry.get.side_effect = RuntimeError("would not be called")
            second = service._get_governance()

        assert second is first
        mock_registry.get.assert_not_called()

    def test_registry_failure_caches_noop_and_fails_open(self, mock_repository, caplog):
        """If ProviderRegistry.governance.get() raises, ``_governance`` becomes a
        fresh ``NoOpGovernanceChecker`` and the flag is set — the next call
        returns the cached NoOp without retrying.

        Fail-open contract: callers invoke ``.check_all_governance(...)`` directly,
        so the resolver must always return a non-None checker. NoOp returns
        ``allowed`` for every check. The WARNING log notifies operators of the
        missing real resolution.
        """
        import logging

        from baldur.interfaces.governance import NoOpGovernanceChecker

        service = ReplayService(repository=mock_repository)

        with patch(
            "baldur.factory.registry.ProviderRegistry.governance"
        ) as mock_registry:
            mock_registry.get.side_effect = RuntimeError("registry down")
            with caplog.at_level(
                logging.WARNING, logger="baldur.services.replay_service.service"
            ):
                result = service._get_governance()

        assert isinstance(result, NoOpGovernanceChecker)
        assert service._governance is result
        assert service._governance_resolved is True

        # Second call must not re-attempt resolution (cache flag short-circuit).
        with patch(
            "baldur.factory.registry.ProviderRegistry.governance"
        ) as mock_registry:
            mock_registry.get.side_effect = RuntimeError("still down")
            second = service._get_governance()
            mock_registry.get.assert_not_called()
        assert second is result
