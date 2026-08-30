"""
Exception hierarchy (312) unit tests.

Verification targets:
- The BaldurError base class and the full exception hierarchy
- The extra_context() method
- Each domain exception carries the correct inheritance chain

Technique classification:
- Contract verification: exception hierarchy structure, extra_context() return values
- Behavior verification: catch-all patterns, message/code propagation
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
    UnconfiguredStoreError,
)

# =============================================================================
# Contract verification — exception hierarchy structure
# =============================================================================


class TestExceptionHierarchyContract:
    """Verify the exception hierarchy matches the 312 design contract."""

    def test_baldur_error_inherits_from_exception(self):
        """BaldurError must inherit from Exception."""
        assert issubclass(BaldurError, Exception)

    def test_adapter_error_inherits_from_baldur_error(self):
        """AdapterError must inherit from BaldurError."""
        assert issubclass(AdapterError, BaldurError)

    def test_adapter_not_found_inherits_from_adapter_error(self):
        """AdapterNotFoundError must inherit from AdapterError."""
        assert issubclass(AdapterNotFoundError, AdapterError)

    def test_adapter_initialization_inherits_from_adapter_error(self):
        """AdapterInitializationError must inherit from AdapterError."""
        assert issubclass(AdapterInitializationError, AdapterError)

    def test_adapter_connection_inherits_from_adapter_error(self):
        """AdapterConnectionError must inherit from AdapterError."""
        assert issubclass(AdapterConnectionError, AdapterError)

    def test_circuit_breaker_error_inherits_from_baldur_error(self):
        """CircuitBreakerError must inherit from BaldurError."""
        assert issubclass(CircuitBreakerError, BaldurError)

    def test_circuit_breaker_transition_inherits_from_circuit_breaker_error(self):
        """CircuitBreakerTransitionError must inherit from CircuitBreakerError."""
        assert issubclass(CircuitBreakerTransitionError, CircuitBreakerError)

    def test_dlq_error_inherits_from_baldur_error(self):
        """DLQError must inherit from BaldurError."""
        assert issubclass(DLQError, BaldurError)

    def test_dlq_entry_not_found_inherits_from_dlq_error(self):
        """DLQEntryNotFoundError must inherit from DLQError."""
        assert issubclass(DLQEntryNotFoundError, DLQError)

    def test_dlq_replay_error_inherits_from_dlq_error(self):
        """DLQReplayError must inherit from DLQError."""
        assert issubclass(DLQReplayError, DLQError)

    def test_resilience_error_inherits_from_baldur_error(self):
        """ResilienceError must inherit from BaldurError."""
        assert issubclass(ResilienceError, BaldurError)

    def test_retry_exhausted_inherits_from_resilience_error(self):
        """RetryExhaustedError must inherit from ResilienceError."""
        assert issubclass(RetryExhaustedError, ResilienceError)

    def test_runbook_error_inherits_from_baldur_error(self):
        """RunbookError must inherit from BaldurError."""
        assert issubclass(RunbookError, BaldurError)

    def test_configuration_error_inherits_from_baldur_error(self):
        """ConfigurationError must inherit from BaldurError."""
        assert issubclass(ConfigurationError, BaldurError)

    def test_settings_validation_inherits_from_configuration_error(self):
        """SettingsValidationError must inherit from ConfigurationError."""
        assert issubclass(SettingsValidationError, ConfigurationError)

    def test_adapter_not_found_is_not_value_error(self):
        """AdapterNotFoundError must be in the AdapterError family, not ValueError."""
        err = AdapterNotFoundError("test")
        assert isinstance(err, AdapterError)
        assert isinstance(err, BaldurError)
        assert not isinstance(err, ValueError)

    def test_dlq_entry_not_found_is_not_value_error(self):
        """DLQEntryNotFoundError must be in the DLQError family, not ValueError."""
        err = DLQEntryNotFoundError("test")
        assert isinstance(err, DLQError)
        assert not isinstance(err, ValueError)

    def test_unconfigured_store_inherits_from_adapter_error(self):
        """A declined dial is an adapter concern, catchable by the base class.

        The layered wrapper catches it by name ahead of ``except Exception``;
        anything broader that catches ``AdapterError`` has to keep seeing it.
        """
        assert issubclass(UnconfiguredStoreError, AdapterError)
        assert issubclass(UnconfiguredStoreError, BaldurError)

    def test_unconfigured_store_is_distinct_from_a_failed_connection(self):
        """Nothing was dialed, so it must not read as a connection failure."""
        err = UnconfiguredStoreError(service="svc", operation="trip_to_open")

        assert not isinstance(err, AdapterConnectionError)


# =============================================================================
# Contract verification — the extra_context() method
# =============================================================================


class TestExtraContextContract:
    """BaldurError.extra_context() design-contract verification."""

    def test_extra_context_with_code_returns_error_code(self):
        """When code is set, extra_context() must include the error_code key."""
        err = BaldurError("test", code="E001")
        ctx = err.extra_context()
        assert ctx == {"error_code": "E001"}

    def test_extra_context_without_code_returns_empty_dict(self):
        """When code is an empty string, extra_context() must return an empty dict."""
        err = BaldurError("test")
        assert err.extra_context() == {}

    def test_extra_context_default_code_is_empty_string(self):
        """The code default must be an empty string."""
        err = BaldurError("test")
        assert err.code == ""

    def test_unconfigured_store_extra_context_carries_service_and_operation(self):
        """The two fields a reader needs to locate a decline."""
        err = UnconfiguredStoreError(service="payment", operation="trip_to_open")

        ctx = err.extra_context()

        assert ctx["service"] == "payment"
        assert ctx["operation"] == "trip_to_open"

    def test_unconfigured_store_extra_context_omits_empty_fields(self):
        """Empty fields are dropped rather than bound as empty strings."""
        err = UnconfiguredStoreError()

        ctx = err.extra_context()

        assert "service" not in ctx
        assert "operation" not in ctx

    def test_unconfigured_store_synthesizes_a_message_from_its_fields(self):
        """Raised without a message at every site, so the default is the message."""
        err = UnconfiguredStoreError(service="payment", operation="trip_to_open")

        assert "payment" in str(err)
        assert "trip_to_open" in str(err)

    def test_unconfigured_store_keeps_an_explicit_message(self):
        err = UnconfiguredStoreError("no store here", service="payment")

        assert str(err) == "no store here"


# =============================================================================
# Behavior verification — catch-all patterns
# =============================================================================


class TestCatchAllPatternBehavior:
    """Verify BaldurError can cover every library error."""

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
        """BaldurError must catch every subclass."""
        with pytest.raises(BaldurError):
            raise error_class("test error")

    def test_message_preserved_through_hierarchy(self):
        """Messages must be preserved through the exception hierarchy."""
        msg = "adapter xyz not found"
        err = AdapterNotFoundError(msg)
        assert str(err) == msg

    def test_code_preserved_through_hierarchy(self):
        """The code argument must work in subclasses too."""
        err = DLQError("dlq failed", code="DLQ_001")
        assert err.code == "DLQ_001"
        assert err.extra_context() == {"error_code": "DLQ_001"}


# =============================================================================
# Behavior verification — external-module exception hierarchy integration
# =============================================================================


class TestExternalExceptionIntegrationBehavior:
    """Verify external-module (CB, Bulkhead, Hedging) exceptions are integrated into the hierarchy."""

    def test_circuit_breaker_open_is_baldur_error(self):
        """CircuitBreakerOpenError must be in the BaldurError family."""
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        err = CircuitBreakerOpenError("payment")
        assert isinstance(err, CircuitBreakerError)
        assert isinstance(err, BaldurError)
        assert err.service_name == "payment"

    def test_bulkhead_full_is_resilience_error(self):
        """BulkheadFullError must be in the ResilienceError family."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadFullError

        err = BulkheadFullError("api", max_concurrent=10, active_count=10)
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_bulkhead_timeout_is_resilience_error(self):
        """BulkheadTimeoutError must be in the ResilienceError family."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadTimeoutError

        err = BulkheadTimeoutError("api", timeout=5.0)
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_hedging_error_is_resilience_error(self):
        """HedgingError must be in the ResilienceError family."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.hedging.exceptions import HedgingError

        err = HedgingError("test")
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_hedging_timeout_is_resilience_error(self):
        """HedgingTimeoutError must be in the ResilienceError family."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.hedging.exceptions import HedgingTimeoutError

        err = HedgingTimeoutError(timeout=3.0)
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_hedging_all_failed_is_resilience_error(self):
        """HedgingAllFailedError must be in the ResilienceError family."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.hedging.exceptions import HedgingAllFailedError

        err = HedgingAllFailedError(candidates_tried=3, errors=["e1", "e2", "e3"])
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_catch_resilience_error_catches_bulkhead_and_hedging(self):
        """ResilienceError must catch both Bulkhead and Hedging exceptions."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadFullError
        from baldur_pro.services.hedging.exceptions import HedgingTimeoutError

        with pytest.raises(ResilienceError):
            raise BulkheadFullError("api", max_concurrent=5, active_count=5)

        with pytest.raises(ResilienceError):
            raise HedgingTimeoutError(timeout=1.0)

    def test_catch_circuit_breaker_error_catches_open_error(self):
        """CircuitBreakerError must catch CircuitBreakerOpenError."""
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        with pytest.raises(CircuitBreakerError):
            raise CircuitBreakerOpenError("svc")


# =============================================================================
# Phase 4: migration of remaining direct-Exception subclasses (312 §8)
# =============================================================================


class TestPhase4HierarchyContract:
    """Verify the Phase 4 migration exception hierarchy matches the 312 §8.2 design contract."""

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
        """TaskNotFoundError, TaskTimeoutError, etc. keep the TaskQueueError chain."""
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
        """RouteNotFoundError, etc. keep the WebFrameworkError chain."""
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
        """The 11 IPC subclasses (IPCConnectionError, etc.) keep the IPCError chain."""
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
        """CascadeChainDepthExceeded, etc. keep the CascadeAuditError chain."""
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
    """Behavior verification of Phase 4 migration exceptions' extra_context()."""

    def test_ipc_error_extra_context_contains_jsonrpc_code(self):
        """IPCConnectionError.extra_context() includes jsonrpc_code."""
        from baldur.adapters.ipc.exceptions import IPCConnectionError

        err = IPCConnectionError()
        ctx = err.extra_context()
        assert "jsonrpc_code" in ctx
        assert ctx["jsonrpc_code"] == -32003

    def test_ipc_error_extra_context_excludes_error_code_key(self):
        """IPCError does not use BaldurError's str code logic."""
        from baldur.adapters.ipc.exceptions import IPCError

        err = IPCError("test", jsonrpc_code=None)
        ctx = err.extra_context()
        assert "error_code" not in ctx

    def test_cascade_chain_depth_exceeded_extra_context(self):
        """CascadeChainDepthExceeded.extra_context() returns depth/max_depth/cascade_id."""
        from baldur.audit.cascade_exceptions import CascadeChainDepthExceeded

        err = CascadeChainDepthExceeded(depth=15, max_depth=10, cascade_id="c-abc")
        ctx = err.extra_context()
        assert ctx["depth"] == 15
        assert ctx["max_depth"] == 10
        assert ctx["cascade_id"] == "c-abc"

    def test_cascade_cycle_detected_extra_context(self):
        """CascadeCycleDetected.extra_context() returns cycle_path/cascade_id."""
        from baldur.audit.cascade_exceptions import CascadeCycleDetected

        err = CascadeCycleDetected(cycle_path=["A", "B", "A"], cascade_id="c-xyz")
        ctx = err.extra_context()
        assert ctx["cycle_path"] == ["A", "B", "A"]
        assert ctx["cascade_id"] == "c-xyz"

    def test_cascade_integrity_error_extra_context(self):
        """CascadeIntegrityError.extra_context() returns integrity info."""
        from baldur.audit.cascade_exceptions import CascadeIntegrityError

        err = CascadeIntegrityError(
            cascade_id="c-1", error_type="hash_mismatch", details={"k": "v"}
        )
        ctx = err.extra_context()
        assert ctx["cascade_id"] == "c-1"
        assert ctx["integrity_error_type"] == "hash_mismatch"
        assert ctx["details"] == {"k": "v"}

    def test_wal_corruption_error_extra_context(self):
        """WALCorruptionError.extra_context() returns checksum info."""
        from baldur.audit.wal._models import WALCorruptionError

        err = WALCorruptionError("bad", sequence=5, expected="abc", computed="xyz")
        ctx = err.extra_context()
        assert ctx["sequence"] == 5
        assert ctx["expected_checksum"] == "abc"
        assert ctx["computed_checksum"] == "xyz"

    def test_config_lock_error_extra_context(self):
        """ConfigLockError.extra_context() returns config_type/current_owner."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.locking import ConfigLockError

        err = ConfigLockError("locked", config_type="cb", current_owner="r-1")
        ctx = err.extra_context()
        assert ctx["config_type"] == "cb"
        assert ctx["current_owner"] == "r-1"

    def test_config_lock_error_extra_context_empty_fields_omitted(self):
        """ConfigLockError — empty fields are omitted from extra_context()."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.locking import ConfigLockError

        err = ConfigLockError("locked")
        ctx = err.extra_context()
        assert "config_type" not in ctx
        assert "current_owner" not in ctx

    def test_version_conflict_error_extra_context(self):
        """VersionConflictError.extra_context() returns version-conflict info."""
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
        """RecoveryLockError.extra_context() returns namespace/current_owner."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.coordination.distributed_recovery_lock import (
            RecoveryLockError,
        )

        err = RecoveryLockError("locked", namespace="global", current_owner="s-1")
        ctx = err.extra_context()
        assert ctx["namespace"] == "global"
        assert ctx["current_owner"] == "s-1"

    def test_automation_blocked_error_extra_context(self):
        """AutomationBlockedError.extra_context() returns budget info."""
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
        """AutomationBlockedError.to_dict() keeps its existing shape."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.error_budget_gate.exceptions import (
            AutomationBlockedError,
        )

        err = AutomationBlockedError("msg", error_budget_percent=5.0, action="test")
        d = err.to_dict()
        assert d["error"] == "AutomationBlockedError"
        assert d["manual_mode_enforced"] is True

    def test_fatal_config_error_extra_context(self):
        """FatalConfigError.extra_context() returns violations."""
        from baldur.core.safe_defaults import FatalConfigError

        violations = {"security": {"rate_limit": "too high"}}
        err = FatalConfigError(violations)
        ctx = err.extra_context()
        assert ctx["violations"] == violations

    def test_baldur_context_error_extra_context(self):
        """BaldurContextError.extra_context() returns context_name/task_name."""
        from baldur.context.celery_context_utils import BaldurContextError

        err = BaldurContextError("cell_id", "my_task")
        ctx = err.extra_context()
        assert ctx["context_name"] == "cell_id"
        assert ctx["task_name"] == "my_task"

    def test_max_retries_exceeded_extra_context(self):
        """MaxRetriesExceededError.extra_context() returns retry info."""
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
        """When last_error is None, it is omitted from extra_context()."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        err = MaxRetriesExceededError("max", retry_count=1, max_retries=3)
        ctx = err.extra_context()
        assert "last_error" not in ctx


class TestPhase4CatchAllBehavior:
    """Verify Phase 4 exceptions are catchable via their domain base and BaldurError."""

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
        """BaldurError must catch Phase 4 exceptions."""
        import importlib

        if import_path.startswith(("baldur_pro", "baldur_dormant")):
            pytest.importorskip(import_path.split(".", 1)[0])
        module = importlib.import_module(import_path)
        error_class = getattr(module, class_name)
        with pytest.raises(BaldurError):
            raise error_class("test")

    def test_baldur_error_catches_version_conflict(self):
        """BaldurError must catch VersionConflictError."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.canary.versioning import VersionConflictError

        with pytest.raises(BaldurError):
            raise VersionConflictError(5, 8, "admin", "cb")

    def test_baldur_error_catches_fatal_config(self):
        """BaldurError must catch FatalConfigError."""
        from baldur.core.safe_defaults import FatalConfigError

        with pytest.raises(BaldurError):
            raise FatalConfigError({"security": {"k": "bad"}})

    def test_baldur_error_catches_context_error(self):
        """BaldurError must catch BaldurContextError."""
        from baldur.context.celery_context_utils import BaldurContextError

        with pytest.raises(BaldurError):
            raise BaldurContextError("cell_id", "my_task")

    def test_baldur_error_catches_max_retries_exceeded(self):
        """BaldurError must catch MaxRetriesExceededError."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        with pytest.raises(BaldurError):
            raise MaxRetriesExceededError("max", retry_count=3, max_retries=3)

    def test_catch_audit_error_catches_all_audit_subclasses(self):
        """AuditError must catch cascade/mmap/wal exceptions alike."""
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
        """RetryExhaustedError must catch MaxRetriesExceededError."""
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        with pytest.raises(RetryExhaustedError):
            raise MaxRetriesExceededError("max", retry_count=3, max_retries=3)

    def test_catch_adapter_error_catches_ipc_and_web_framework(self):
        """AdapterError must catch both IPCError and WebFrameworkError."""
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
