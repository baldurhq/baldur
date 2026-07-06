"""
Exception Hierarchy (312) 단위 테스트.

검증 대상:
- BaldurError base 클래스 및 전체 예외 계층
- extra_context() 메서드
- 각 도메인별 예외가 올바른 상속 체인을 가지는지

기법 분류:
- 계약 검증: 예외 계층 구조, extra_context() 반환값
- 동작 검증: catch-all 패턴, 메시지/코드 전달
"""

from __future__ import annotations

import pytest

from baldur.core.exceptions import (
    AdapterConnectionError,
    AdapterError,
    AdapterInitializationError,
    AdapterNotFoundError,
    BaldurError,
    CircuitBreakerError,
    CircuitBreakerTransitionError,
    ConfigurationError,
    DLQEntryNotFoundError,
    DLQError,
    DLQReplayError,
    ResilienceError,
    RetryExhaustedError,
    RunbookError,
    SettingsValidationError,
)

# =============================================================================
# 계약 검증 — 예외 계층 구조
# =============================================================================


class TestExceptionHierarchyContract:
    """예외 계층 구조가 312 설계 계약대로 구성되어 있는지 검증."""

    def test_baldur_error_inherits_from_exception(self):
        """BaldurError는 Exception을 상속해야 한다."""
        assert issubclass(BaldurError, Exception)

    def test_adapter_error_inherits_from_baldur_error(self):
        """AdapterError는 BaldurError를 상속해야 한다."""
        assert issubclass(AdapterError, BaldurError)

    def test_adapter_not_found_inherits_from_adapter_error(self):
        """AdapterNotFoundError는 AdapterError를 상속해야 한다."""
        assert issubclass(AdapterNotFoundError, AdapterError)

    def test_adapter_initialization_inherits_from_adapter_error(self):
        """AdapterInitializationError는 AdapterError를 상속해야 한다."""
        assert issubclass(AdapterInitializationError, AdapterError)

    def test_adapter_connection_inherits_from_adapter_error(self):
        """AdapterConnectionError는 AdapterError를 상속해야 한다."""
        assert issubclass(AdapterConnectionError, AdapterError)

    def test_circuit_breaker_error_inherits_from_baldur_error(self):
        """CircuitBreakerError는 BaldurError를 상속해야 한다."""
        assert issubclass(CircuitBreakerError, BaldurError)

    def test_circuit_breaker_transition_inherits_from_circuit_breaker_error(self):
        """CircuitBreakerTransitionError는 CircuitBreakerError를 상속해야 한다."""
        assert issubclass(CircuitBreakerTransitionError, CircuitBreakerError)

    def test_dlq_error_inherits_from_baldur_error(self):
        """DLQError는 BaldurError를 상속해야 한다."""
        assert issubclass(DLQError, BaldurError)

    def test_dlq_entry_not_found_inherits_from_dlq_error(self):
        """DLQEntryNotFoundError는 DLQError를 상속해야 한다."""
        assert issubclass(DLQEntryNotFoundError, DLQError)

    def test_dlq_replay_error_inherits_from_dlq_error(self):
        """DLQReplayError는 DLQError를 상속해야 한다."""
        assert issubclass(DLQReplayError, DLQError)

    def test_resilience_error_inherits_from_baldur_error(self):
        """ResilienceError는 BaldurError를 상속해야 한다."""
        assert issubclass(ResilienceError, BaldurError)

    def test_retry_exhausted_inherits_from_resilience_error(self):
        """RetryExhaustedError는 ResilienceError를 상속해야 한다."""
        assert issubclass(RetryExhaustedError, ResilienceError)

    def test_runbook_error_inherits_from_baldur_error(self):
        """RunbookError는 BaldurError를 상속해야 한다."""
        assert issubclass(RunbookError, BaldurError)

    def test_configuration_error_inherits_from_baldur_error(self):
        """ConfigurationError는 BaldurError를 상속해야 한다."""
        assert issubclass(ConfigurationError, BaldurError)

    def test_settings_validation_inherits_from_configuration_error(self):
        """SettingsValidationError는 ConfigurationError를 상속해야 한다."""
        assert issubclass(SettingsValidationError, ConfigurationError)

    def test_adapter_not_found_is_not_value_error(self):
        """AdapterNotFoundError는 ValueError가 아닌 AdapterError 계열이어야 한다."""
        err = AdapterNotFoundError("test")
        assert isinstance(err, AdapterError)
        assert isinstance(err, BaldurError)
        assert not isinstance(err, ValueError)

    def test_dlq_entry_not_found_is_not_value_error(self):
        """DLQEntryNotFoundError는 ValueError가 아닌 DLQError 계열이어야 한다."""
        err = DLQEntryNotFoundError("test")
        assert isinstance(err, DLQError)
        assert not isinstance(err, ValueError)


# =============================================================================
# 계약 검증 — extra_context() 메서드
# =============================================================================


class TestExtraContextContract:
    """BaldurError.extra_context() 설계 계약 검증."""

    def test_extra_context_with_code_returns_error_code(self):
        """code가 설정된 경우 extra_context()에 error_code 키가 포함되어야 한다."""
        err = BaldurError("test", code="E001")
        ctx = err.extra_context()
        assert ctx == {"error_code": "E001"}

    def test_extra_context_without_code_returns_empty_dict(self):
        """code가 빈 문자열이면 extra_context()는 빈 dict를 반환해야 한다."""
        err = BaldurError("test")
        assert err.extra_context() == {}

    def test_extra_context_default_code_is_empty_string(self):
        """code 기본값은 빈 문자열이어야 한다."""
        err = BaldurError("test")
        assert err.code == ""


# =============================================================================
# 동작 검증 — catch-all 패턴
# =============================================================================


class TestCatchAllPatternBehavior:
    """BaldurError로 모든 라이브러리 에러를 포괄할 수 있는지 검증."""

    @pytest.mark.parametrize(
        "error_class",
        [
            AdapterError,
            AdapterNotFoundError,
            AdapterInitializationError,
            AdapterConnectionError,
            CircuitBreakerError,
            CircuitBreakerTransitionError,
            DLQError,
            DLQEntryNotFoundError,
            DLQReplayError,
            ResilienceError,
            RetryExhaustedError,
            RunbookError,
            ConfigurationError,
            SettingsValidationError,
        ],
    )
    def test_baldur_error_catches_all_subclasses(self, error_class):
        """BaldurError로 모든 서브클래스를 catch할 수 있어야 한다."""
        with pytest.raises(BaldurError):
            raise error_class("test error")

    def test_message_preserved_through_hierarchy(self):
        """메시지가 예외 계층을 통해 보존되어야 한다."""
        msg = "adapter xyz not found"
        err = AdapterNotFoundError(msg)
        assert str(err) == msg

    def test_code_preserved_through_hierarchy(self):
        """code 인자가 서브클래스에서도 동작해야 한다."""
        err = DLQError("dlq failed", code="DLQ_001")
        assert err.code == "DLQ_001"
        assert err.extra_context() == {"error_code": "DLQ_001"}


# =============================================================================
# 동작 검증 — 외부 모듈 예외 계층 통합
# =============================================================================


class TestExternalExceptionIntegrationBehavior:
    """외부 모듈(CB, Bulkhead, Hedging)의 예외가 계층에 통합되었는지 검증."""

    def test_circuit_breaker_open_is_baldur_error(self):
        """CircuitBreakerOpenError는 BaldurError 계열이어야 한다."""
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        err = CircuitBreakerOpenError("payment")
        assert isinstance(err, CircuitBreakerError)
        assert isinstance(err, BaldurError)
        assert err.service_name == "payment"

    def test_bulkhead_full_is_resilience_error(self):
        """BulkheadFullError는 ResilienceError 계열이어야 한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadFullError

        err = BulkheadFullError("api", max_concurrent=10, active_count=10)
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_bulkhead_timeout_is_resilience_error(self):
        """BulkheadTimeoutError는 ResilienceError 계열이어야 한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadTimeoutError

        err = BulkheadTimeoutError("api", timeout=5.0)
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_hedging_error_is_resilience_error(self):
        """HedgingError는 ResilienceError 계열이어야 한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.hedging.exceptions import HedgingError

        err = HedgingError("test")
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_hedging_timeout_is_resilience_error(self):
        """HedgingTimeoutError는 ResilienceError 계열이어야 한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.hedging.exceptions import HedgingTimeoutError

        err = HedgingTimeoutError(timeout=3.0)
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_hedging_all_failed_is_resilience_error(self):
        """HedgingAllFailedError는 ResilienceError 계열이어야 한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.hedging.exceptions import HedgingAllFailedError

        err = HedgingAllFailedError(candidates_tried=3, errors=["e1", "e2", "e3"])
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_catch_resilience_error_catches_bulkhead_and_hedging(self):
        """ResilienceError로 Bulkhead와 Hedging 예외를 모두 catch할 수 있어야 한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadFullError
        from baldur_pro.services.hedging.exceptions import HedgingTimeoutError

        with pytest.raises(ResilienceError):
            raise BulkheadFullError("api", max_concurrent=5, active_count=5)

        with pytest.raises(ResilienceError):
            raise HedgingTimeoutError(timeout=1.0)

    def test_catch_circuit_breaker_error_catches_open_error(self):
        """CircuitBreakerError로 CircuitBreakerOpenError를 catch할 수 있어야 한다."""
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        with pytest.raises(CircuitBreakerError):
            raise CircuitBreakerOpenError("svc")


# =============================================================================
# Phase 4: 잔존 Exception 직접 상속 클래스 마이그레이션 (312 §8)
# =============================================================================


class TestPhase4HierarchyContract:
    """Phase 4 마이그레이션 예외 계층이 312 §8.2 설계 계약대로 구성되어 있는지 검증."""

    # ── 4-A: interfaces/ ──

    def test_lock_acquisition_error_inherits_baldur_error(self):
        """LockAcquisitionError → BaldurError."""
        from baldur.interfaces.cache_provider import LockAcquisitionError

        assert issubclass(LockAcquisitionError, BaldurError)
        assert not issubclass(LockAcquisitionError, ValueError)

    def test_lock_not_owned_error_inherits_baldur_error(self):
        """LockNotOwnedError → BaldurError."""
        from baldur.interfaces.cache_provider import LockNotOwnedError

        assert issubclass(LockNotOwnedError, BaldurError)

    def test_rate_limit_storage_error_inherits_adapter_error(self):
        """RateLimitStorageError → AdapterError → BaldurError."""
        from baldur.interfaces.rate_limit_storage import RateLimitStorageError

        assert issubclass(RateLimitStorageError, AdapterError)
        assert issubclass(RateLimitStorageError, BaldurError)

    def test_rate_limit_storage_unavailable_inherits_storage_error(self):
        """RateLimitStorageUnavailableError → RateLimitStorageError chain."""
        from baldur.interfaces.rate_limit_storage import (
            RateLimitStorageError,
            RateLimitStorageUnavailableError,
        )

        assert issubclass(RateLimitStorageUnavailableError, RateLimitStorageError)
        assert issubclass(RateLimitStorageUnavailableError, BaldurError)

    def test_task_queue_error_inherits_baldur_error(self):
        """TaskQueueError → BaldurError."""
        from baldur.interfaces.task_queue import TaskQueueError

        assert issubclass(TaskQueueError, BaldurError)

    def test_task_queue_subclasses_inherit_through_chain(self):
        """TaskNotFoundError, TaskTimeoutError 등이 TaskQueueError 체인을 유지."""
        from baldur.interfaces.task_queue import (
            TaskNotFoundError,
            TaskQueueError,
            TaskRevokedError,
            TaskTimeoutError,
        )

        for cls in [TaskNotFoundError, TaskTimeoutError, TaskRevokedError]:
            assert issubclass(cls, TaskQueueError)
            assert issubclass(cls, BaldurError)

    def test_web_framework_error_inherits_adapter_error(self):
        """WebFrameworkError → AdapterError → BaldurError."""
        from baldur.interfaces.web_framework import WebFrameworkError

        assert issubclass(WebFrameworkError, AdapterError)
        assert issubclass(WebFrameworkError, BaldurError)

    def test_web_framework_subclasses_inherit_through_chain(self):
        """RouteNotFoundError 등이 WebFrameworkError 체인을 유지."""
        from baldur.interfaces.web_framework import (
            AuthenticationError,
            PermissionDeniedError,
            RouteNotFoundError,
            WebFrameworkError,
        )

        for cls in [RouteNotFoundError, AuthenticationError, PermissionDeniedError]:
            assert issubclass(cls, WebFrameworkError)
            assert issubclass(cls, BaldurError)

    # ── 4-B: adapters/ ──

    def test_ipc_error_inherits_adapter_error(self):
        """IPCError → AdapterError → BaldurError."""
        from baldur.adapters.ipc.exceptions import IPCError

        assert issubclass(IPCError, AdapterError)
        assert issubclass(IPCError, BaldurError)

    def test_ipc_subclasses_inherit_through_chain(self):
        """IPCConnectionError 등 11개 서브클래스가 IPCError 체인을 유지."""
        from baldur.adapters.ipc.exceptions import (
            IPCAuthenticationError,
            IPCCircuitBreakerOpenError,
            IPCConnectionError,
            IPCError,
            IPCInternalError,
            IPCMethodNotFoundError,
            IPCParseError,
            IPCRateLimitedError,
            IPCTimeoutError,
        )

        for cls in [
            IPCConnectionError,
            IPCTimeoutError,
            IPCAuthenticationError,
            IPCMethodNotFoundError,
            IPCParseError,
            IPCInternalError,
            IPCRateLimitedError,
            IPCCircuitBreakerOpenError,
        ]:
            assert issubclass(cls, IPCError)
            assert issubclass(cls, BaldurError)

    def test_schema_registry_not_configured_inherits_configuration_error(self):
        """SchemaRegistryNotConfiguredError → ConfigurationError."""
        pytest.importorskip("baldur_dormant.adapters.kafka.schemas")
        from baldur_dormant.adapters.kafka.schemas import (
            SchemaRegistryNotConfiguredError,
        )

        assert issubclass(SchemaRegistryNotConfiguredError, ConfigurationError)
        assert issubclass(SchemaRegistryNotConfiguredError, BaldurError)

    def test_schema_compatibility_error_inherits_adapter_error(self):
        """SchemaCompatibilityError → AdapterError."""
        pytest.importorskip("baldur_dormant.adapters.kafka.schemas")
        from baldur_dormant.adapters.kafka.schemas import SchemaCompatibilityError

        assert issubclass(SchemaCompatibilityError, AdapterError)
        assert issubclass(SchemaCompatibilityError, BaldurError)

    # ── 4-C: audit/ ──

    def test_audit_error_inherits_baldur_error(self):
        """AuditError → BaldurError (new domain base)."""
        from baldur.core.exceptions import AuditError

        assert issubclass(AuditError, BaldurError)

    def test_cascade_audit_error_inherits_audit_error(self):
        """CascadeAuditError → AuditError → BaldurError."""
        from baldur.audit.cascade_exceptions import CascadeAuditError
        from baldur.core.exceptions import AuditError

        assert issubclass(CascadeAuditError, AuditError)
        assert issubclass(CascadeAuditError, BaldurError)

    def test_cascade_subclasses_inherit_through_chain(self):
        """CascadeChainDepthExceeded 등이 CascadeAuditError 체인을 유지."""
        from baldur.audit.cascade_exceptions import (
            CascadeAuditError,
            CascadeChainDepthExceeded,
            CascadeCycleDetected,
            CascadeEventNotFound,
            CascadeIntegrityError,
        )

        for cls in [
            CascadeChainDepthExceeded,
            CascadeCycleDetected,
            CascadeEventNotFound,
            CascadeIntegrityError,
        ]:
            assert issubclass(cls, CascadeAuditError)
            assert issubclass(cls, BaldurError)

    def test_mmap_buffer_error_inherits_audit_error(self):
        """MmapBufferError → AuditError → BaldurError."""
        from baldur.audit.persistence.mmap_buffer import MmapBufferError
        from baldur.core.exceptions import AuditError

        assert issubclass(MmapBufferError, AuditError)
        assert issubclass(MmapBufferError, BaldurError)

    def test_wal_error_inherits_audit_error(self):
        """WALError → AuditError → BaldurError."""
        from baldur.audit.wal._models import WALError
        from baldur.core.exceptions import AuditError

        assert issubclass(WALError, AuditError)
        assert issubclass(WALError, BaldurError)

    def test_wal_corruption_error_inherits_wal_error(self):
        """WALCorruptionError → WALError chain."""
        from baldur.audit.wal._models import WALCorruptionError, WALError

        assert issubclass(WALCorruptionError, WALError)
        assert issubclass(WALCorruptionError, BaldurError)

    # ── 4-D: services/ ──

    def test_config_lock_error_inherits_baldur_error(self):
        """ConfigLockError → BaldurError."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.locking import ConfigLockError

        assert issubclass(ConfigLockError, BaldurError)

    def test_version_conflict_error_inherits_baldur_error(self):
        """VersionConflictError → BaldurError."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.versioning import VersionConflictError

        assert issubclass(VersionConflictError, BaldurError)

    def test_recovery_lock_error_inherits_baldur_error(self):
        """RecoveryLockError → BaldurError."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.coordination.distributed_recovery_lock import (
            RecoveryLockError,
        )

        assert issubclass(RecoveryLockError, BaldurError)

    def test_automation_blocked_error_inherits_baldur_error(self):
        """AutomationBlockedError → BaldurError."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.error_budget_gate.exceptions import (
            AutomationBlockedError,
        )

        assert issubclass(AutomationBlockedError, BaldurError)

    # ── 4-E: core/, context/ ──

    def test_fatal_config_error_inherits_configuration_error(self):
        """FatalConfigError → ConfigurationError → BaldurError."""
        from baldur.core.safe_defaults import FatalConfigError

        assert issubclass(FatalConfigError, ConfigurationError)
        assert issubclass(FatalConfigError, BaldurError)

    def test_baldur_context_error_inherits_baldur_error(self):
        """BaldurContextError → BaldurError."""
        from baldur.context.celery_context_utils import BaldurContextError

        assert issubclass(BaldurContextError, BaldurError)

    # ── 4-F: retry integration ──

    def test_max_retries_exceeded_inherits_retry_exhausted(self):
        """MaxRetriesExceededError → RetryExhaustedError → ResilienceError."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        assert issubclass(MaxRetriesExceededError, RetryExhaustedError)
        assert issubclass(MaxRetriesExceededError, ResilienceError)
        assert issubclass(MaxRetriesExceededError, BaldurError)


class TestPhase4ExtraContextBehavior:
    """Phase 4 마이그레이션 예외의 extra_context() 동작 검증."""

    def test_ipc_error_extra_context_contains_jsonrpc_code(self):
        """IPCConnectionError.extra_context()에 jsonrpc_code가 포함된다."""
        from baldur.adapters.ipc.exceptions import IPCConnectionError

        err = IPCConnectionError()
        ctx = err.extra_context()
        assert "jsonrpc_code" in ctx
        assert ctx["jsonrpc_code"] == -32003

    def test_ipc_error_extra_context_excludes_error_code_key(self):
        """IPCError는 BaldurError의 str code 로직을 사용하지 않는다."""
        from baldur.adapters.ipc.exceptions import IPCError

        err = IPCError("test", jsonrpc_code=None)
        ctx = err.extra_context()
        assert "error_code" not in ctx

    def test_cascade_chain_depth_exceeded_extra_context(self):
        """CascadeChainDepthExceeded.extra_context()가 depth/max_depth/cascade_id를 반환."""
        from baldur.audit.cascade_exceptions import CascadeChainDepthExceeded

        err = CascadeChainDepthExceeded(depth=15, max_depth=10, cascade_id="c-abc")
        ctx = err.extra_context()
        assert ctx["depth"] == 15
        assert ctx["max_depth"] == 10
        assert ctx["cascade_id"] == "c-abc"

    def test_cascade_cycle_detected_extra_context(self):
        """CascadeCycleDetected.extra_context()가 cycle_path/cascade_id를 반환."""
        from baldur.audit.cascade_exceptions import CascadeCycleDetected

        err = CascadeCycleDetected(cycle_path=["A", "B", "A"], cascade_id="c-xyz")
        ctx = err.extra_context()
        assert ctx["cycle_path"] == ["A", "B", "A"]
        assert ctx["cascade_id"] == "c-xyz"

    def test_cascade_integrity_error_extra_context(self):
        """CascadeIntegrityError.extra_context()가 integrity 정보를 반환."""
        from baldur.audit.cascade_exceptions import CascadeIntegrityError

        err = CascadeIntegrityError(
            cascade_id="c-1", error_type="hash_mismatch", details={"k": "v"}
        )
        ctx = err.extra_context()
        assert ctx["cascade_id"] == "c-1"
        assert ctx["integrity_error_type"] == "hash_mismatch"
        assert ctx["details"] == {"k": "v"}

    def test_wal_corruption_error_extra_context(self):
        """WALCorruptionError.extra_context()가 체크섬 정보를 반환."""
        from baldur.audit.wal._models import WALCorruptionError

        err = WALCorruptionError("bad", sequence=5, expected="abc", computed="xyz")
        ctx = err.extra_context()
        assert ctx["sequence"] == 5
        assert ctx["expected_checksum"] == "abc"
        assert ctx["computed_checksum"] == "xyz"

    def test_config_lock_error_extra_context(self):
        """ConfigLockError.extra_context()가 config_type/current_owner를 반환."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.locking import ConfigLockError

        err = ConfigLockError("locked", config_type="cb", current_owner="r-1")
        ctx = err.extra_context()
        assert ctx["config_type"] == "cb"
        assert ctx["current_owner"] == "r-1"

    def test_config_lock_error_extra_context_empty_fields_omitted(self):
        """ConfigLockError — 빈 필드는 extra_context()에서 제외된다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.locking import ConfigLockError

        err = ConfigLockError("locked")
        ctx = err.extra_context()
        assert "config_type" not in ctx
        assert "current_owner" not in ctx

    def test_version_conflict_error_extra_context(self):
        """VersionConflictError.extra_context()가 버전 충돌 정보를 반환."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.versioning import VersionConflictError

        err = VersionConflictError(
            expected_version=5,
            actual_version=8,
            conflicting_operator="admin",
            config_type="cb",
        )
        ctx = err.extra_context()
        assert ctx["expected_version"] == 5
        assert ctx["actual_version"] == 8
        assert ctx["conflicting_operator"] == "admin"
        assert ctx["config_type"] == "cb"

    def test_recovery_lock_error_extra_context(self):
        """RecoveryLockError.extra_context()가 namespace/current_owner를 반환."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.coordination.distributed_recovery_lock import (
            RecoveryLockError,
        )

        err = RecoveryLockError("locked", namespace="global", current_owner="s-1")
        ctx = err.extra_context()
        assert ctx["namespace"] == "global"
        assert ctx["current_owner"] == "s-1"

    def test_automation_blocked_error_extra_context(self):
        """AutomationBlockedError.extra_context()가 예산 정보를 반환."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.error_budget_gate.exceptions import (
            AutomationBlockedError,
        )

        err = AutomationBlockedError(
            "Low budget",
            error_budget_percent=5.0,
            threshold_percent=10.0,
            action="chaos",
        )
        ctx = err.extra_context()
        assert ctx["error_budget_percent"] == 5.0
        assert ctx["threshold_percent"] == 10.0
        assert ctx["action"] == "chaos"

    def test_automation_blocked_error_to_dict_backward_compat(self):
        """AutomationBlockedError.to_dict()가 기존 형태를 유지한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.error_budget_gate.exceptions import (
            AutomationBlockedError,
        )

        err = AutomationBlockedError("msg", error_budget_percent=5.0, action="test")
        d = err.to_dict()
        assert d["error"] == "AutomationBlockedError"
        assert d["manual_mode_enforced"] is True

    def test_fatal_config_error_extra_context(self):
        """FatalConfigError.extra_context()가 violations를 반환."""
        from baldur.core.safe_defaults import FatalConfigError

        violations = {"security": {"rate_limit": "too high"}}
        err = FatalConfigError(violations)
        ctx = err.extra_context()
        assert ctx["violations"] == violations

    def test_baldur_context_error_extra_context(self):
        """BaldurContextError.extra_context()가 context_name/task_name을 반환."""
        from baldur.context.celery_context_utils import BaldurContextError

        err = BaldurContextError("cell_id", "my_task")
        ctx = err.extra_context()
        assert ctx["context_name"] == "cell_id"
        assert ctx["task_name"] == "my_task"

    def test_max_retries_exceeded_extra_context(self):
        """MaxRetriesExceededError.extra_context()가 재시도 정보를 반환."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        err = MaxRetriesExceededError(
            "max retries",
            retry_count=3,
            max_retries=3,
            last_error=ValueError("timeout"),
        )
        ctx = err.extra_context()
        assert ctx["retry_count"] == 3
        assert ctx["max_retries"] == 3
        assert ctx["last_error"] == "timeout"

    def test_max_retries_exceeded_extra_context_no_last_error(self):
        """last_error가 None이면 extra_context()에서 제외된다."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        err = MaxRetriesExceededError("max", retry_count=1, max_retries=3)
        ctx = err.extra_context()
        assert "last_error" not in ctx


class TestPhase4CatchAllBehavior:
    """Phase 4 예외가 도메인 base 및 BaldurError로 catch 가능한지 검증."""

    @pytest.mark.parametrize(
        ("import_path", "class_name"),
        [
            ("baldur.interfaces.cache_provider", "LockAcquisitionError"),
            ("baldur.interfaces.cache_provider", "LockNotOwnedError"),
            ("baldur.interfaces.rate_limit_storage", "RateLimitStorageError"),
            ("baldur.interfaces.task_queue", "TaskQueueError"),
            ("baldur.interfaces.web_framework", "WebFrameworkError"),
            ("baldur.adapters.ipc.exceptions", "IPCError"),
            (
                "baldur_dormant.adapters.kafka.schemas",
                "SchemaRegistryNotConfiguredError",
            ),
            ("baldur_dormant.adapters.kafka.schemas", "SchemaCompatibilityError"),
            ("baldur.audit.cascade_exceptions", "CascadeAuditError"),
            ("baldur.audit.persistence.mmap_buffer", "MmapBufferError"),
            ("baldur.audit.wal._models", "WALError"),
            ("baldur_pro.services.canary.locking", "ConfigLockError"),
            (
                "baldur_pro.services.coordination.distributed_recovery_lock",
                "RecoveryLockError",
            ),
            (
                "baldur_pro.services.error_budget_gate.exceptions",
                "AutomationBlockedError",
            ),
        ],
    )
    def test_baldur_error_catches_phase4_class(self, import_path, class_name):
        """BaldurError로 Phase 4 예외를 catch할 수 있어야 한다."""
        import importlib

        if import_path.startswith(("baldur_pro", "baldur_dormant")):
            pytest.importorskip(import_path.split(".", 1)[0])
        module = importlib.import_module(import_path)
        error_class = getattr(module, class_name)
        with pytest.raises(BaldurError):
            raise error_class("test")

    def test_baldur_error_catches_version_conflict(self):
        """BaldurError로 VersionConflictError를 catch할 수 있어야 한다."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.versioning import VersionConflictError

        with pytest.raises(BaldurError):
            raise VersionConflictError(5, 8, "admin", "cb")

    def test_baldur_error_catches_fatal_config(self):
        """BaldurError로 FatalConfigError를 catch할 수 있어야 한다."""
        from baldur.core.safe_defaults import FatalConfigError

        with pytest.raises(BaldurError):
            raise FatalConfigError({"security": {"k": "bad"}})

    def test_baldur_error_catches_context_error(self):
        """BaldurError로 BaldurContextError를 catch할 수 있어야 한다."""
        from baldur.context.celery_context_utils import BaldurContextError

        with pytest.raises(BaldurError):
            raise BaldurContextError("cell_id", "my_task")

    def test_baldur_error_catches_max_retries_exceeded(self):
        """BaldurError로 MaxRetriesExceededError를 catch할 수 있어야 한다."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        with pytest.raises(BaldurError):
            raise MaxRetriesExceededError("max", retry_count=3, max_retries=3)

    def test_catch_audit_error_catches_all_audit_subclasses(self):
        """AuditError로 cascade/mmap/wal 예외를 모두 catch할 수 있어야 한다."""
        from baldur.audit.cascade_exceptions import CascadeChainDepthExceeded
        from baldur.audit.persistence.mmap_buffer import MmapBufferError
        from baldur.audit.wal._models import WALCorruptionError
        from baldur.core.exceptions import AuditError

        with pytest.raises(AuditError):
            raise CascadeChainDepthExceeded(depth=5, max_depth=3, cascade_id="c")

        with pytest.raises(AuditError):
            raise MmapBufferError("bad magic")

        with pytest.raises(AuditError):
            raise WALCorruptionError("bad", sequence=1, expected="a", computed="b")

    def test_catch_retry_exhausted_catches_max_retries_exceeded(self):
        """RetryExhaustedError로 MaxRetriesExceededError를 catch할 수 있어야 한다."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        with pytest.raises(RetryExhaustedError):
            raise MaxRetriesExceededError("max", retry_count=3, max_retries=3)

    def test_catch_adapter_error_catches_ipc_and_web_framework(self):
        """AdapterError로 IPCError와 WebFrameworkError를 모두 catch할 수 있어야 한다."""
        from baldur.adapters.ipc.exceptions import IPCConnectionError
        from baldur.interfaces.web_framework import RouteNotFoundError

        with pytest.raises(AdapterError):
            raise IPCConnectionError()

        with pytest.raises(AdapterError):
            raise RouteNotFoundError("not found")

    def test_catch_configuration_error_catches_fatal_and_schema(self):
        """ConfigurationError catches both FatalConfigError and SchemaRegistryNotConfiguredError."""
        pytest.importorskip("baldur_dormant.adapters.kafka.schemas")
        from baldur.core.safe_defaults import FatalConfigError
        from baldur_dormant.adapters.kafka.schemas import (
            SchemaRegistryNotConfiguredError,
        )

        with pytest.raises(ConfigurationError):
            raise FatalConfigError({"security": {"k": "bad"}})

        with pytest.raises(ConfigurationError):
            raise SchemaRegistryNotConfiguredError("no url")


# =============================================================================
# Contract — non_retryable_exceptions() (#418 P0-1)
# =============================================================================


# =============================================================================
# Contract — ConfigVersionConflictError (666 D3, OSS OCC consumer #3)
# =============================================================================


class TestConfigVersionConflictErrorContract:
    """ConfigVersionConflictError is the 3rd OSS optimistic-lock consumer.

    It subclasses ConcurrencyConflictError (the shared OSS OCC base) so the OSS
    config REST handler can ``except`` it across the PRO-manager → OSS-handler
    boundary and map it to HTTP 409, carrying the section + expected/actual
    versions as a usable client retry token (666 D3).
    """

    def test_subclasses_concurrency_conflict_error(self):
        from baldur.core.exceptions import (
            ConcurrencyConflictError,
            ConfigVersionConflictError,
        )

        assert issubclass(ConfigVersionConflictError, ConcurrencyConflictError)
        assert issubclass(ConfigVersionConflictError, BaldurError)

    def test_is_exported_in_module_all(self):
        from baldur.core import exceptions

        assert "ConfigVersionConflictError" in exceptions.__all__

    def test_versions_accessible_as_attributes_for_409_token(self):
        """The handler reads expected/actual off the exception to build the 409
        retry token, so they must be plain int attributes (not nested)."""
        from baldur.core.exceptions import ConfigVersionConflictError

        err = ConfigVersionConflictError("retry", expected_version=1, actual_version=4)

        assert err.config_type == "retry"
        assert err.expected_version == 1
        assert err.actual_version == 4

    def test_extra_context_carries_config_type_and_versions(self):
        from baldur.core.exceptions import ConfigVersionConflictError

        err = ConfigVersionConflictError(
            "circuit_breaker", expected_version=2, actual_version=5
        )
        ctx = err.extra_context()

        assert ctx["config_type"] == "circuit_breaker"
        assert ctx["expected_version"] == 2
        assert ctx["actual_version"] == 5
        # entity_id is the inherited OCC identity, set to the config section.
        assert ctx["entity_id"] == "circuit_breaker"

    def test_default_message_names_section_and_versions(self):
        from baldur.core.exceptions import ConfigVersionConflictError

        err = ConfigVersionConflictError("dlq", expected_version=0, actual_version=2)
        msg = str(err)

        assert "dlq" in msg
        assert "v0" in msg
        assert "v2" in msg

    def test_catchable_as_concurrency_conflict_error(self):
        """The OSS base catch is the boundary mechanism — a PRO-raised conflict
        is caught by an OSS ``except ConcurrencyConflictError``."""
        from baldur.core.exceptions import (
            ConcurrencyConflictError,
            ConfigVersionConflictError,
        )

        with pytest.raises(ConcurrencyConflictError):
            raise ConfigVersionConflictError(
                "slo", expected_version=1, actual_version=2
            )


class TestNonRetryableExceptionsContract:
    """non_retryable_exceptions() contract verification (#418 P0-1)."""

    def test_returns_tuple_containing_circuit_breaker_error(self):
        """non_retryable_exceptions() contains CircuitBreakerError."""
        from baldur.core.exceptions import non_retryable_exceptions

        result = non_retryable_exceptions()
        assert isinstance(result, tuple)
        assert CircuitBreakerError in result

    def test_is_exported_in_module_all(self):
        """non_retryable_exceptions is in core.exceptions.__all__."""
        from baldur.core import exceptions

        assert "non_retryable_exceptions" in exceptions.__all__

    def test_circuit_breaker_error_catches_subclasses(self):
        """CircuitBreakerTransitionError (subclass) is also non-retryable via isinstance."""
        from baldur.core.exceptions import non_retryable_exceptions

        nre = non_retryable_exceptions()
        assert isinstance(CircuitBreakerTransitionError(), nre)
