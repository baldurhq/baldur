"""
Idempotency Models

Domain enums, key generation, and result dataclasses for idempotency operations.

Canonical location: ``baldur.services.idempotency.models``
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class IdempotencyDomain(str, Enum):
    """Domains that support idempotency checking (domain-neutral)."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Existing domains
    # ═══════════════════════════════════════════════════════════════════════════
    EXTERNAL_SERVICE = "external_service"
    """External service calls (payments, notifications, etc.)."""

    INTERNAL_PROCESS = "internal_process"
    """Internal processes (inventory decrement, point accrual, etc.)."""

    ASYNC_TASK = "async_task"
    """Asynchronous tasks (Celery Task, etc.)."""

    EVENT = "event"
    """Event handling (Webhook, message, etc.)."""

    CUSTOM = "custom"
    """Custom domain."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Chaos Engineering domains
    # Chaos experiment execution and zombie-hunter distributed-lock management
    # ═══════════════════════════════════════════════════════════════════════════
    CHAOS_EXPERIMENT = "chaos_experiment"
    """Chaos experiment execution (prevents duplicate execution of the same experiment)."""

    CHAOS_ZOMBIE_HUNTER = "chaos_zombie_hunter"
    """Zombie Hunter distributed lock (prevents duplicate rollback of orphan experiments).

    Detects orphaned experiments and cleans them up safely.
    """

    # ═══════════════════════════════════════════════════════════════════════════
    # Configuration management (priority 4 - v2.4.0)
    # ═══════════════════════════════════════════════════════════════════════════
    CONFIG_CHANGE = "config_change"
    """Configuration change (prevents duplicate application of the same change)."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Storage synchronization (priority 4 - v2.4.0)
    # ═══════════════════════════════════════════════════════════════════════════
    L2_SYNC = "l2_sync"
    """L2 storage synchronization (prevents duplicate re-sync after recovery)."""

    WAL_RECOVERY = "wal_recovery"
    """WAL recovery (prevents duplicate processing of the same entry)."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Auto Tuning (priority 4 - v2.4.0)
    # ═══════════════════════════════════════════════════════════════════════════
    AUTO_ADJUSTMENT = "auto_adjustment"
    """Autonomous adjustment (prevents duplicate application of the same adjustment)."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Multi-Region Active-Active recovery actions
    # Prevents duplicate execution of the same recovery action across regions
    # ═══════════════════════════════════════════════════════════════════════════
    RECOVERY_ACTION = "recovery_action"
    """Recovery action (CB reset, Pod restart, DLQ retry, etc.) duplicate-execution prevention."""


@dataclass
class IdempotencyKey:
    """
    Represents an idempotency key with domain context.

    The key is a combination of domain-specific identifiers that
    uniquely identify an operation.
    """

    domain: IdempotencyDomain
    key: str
    components: dict[str, Any]

    @property
    def cache_key(self) -> str:
        """Get the cache key for Redis/memcached storage."""
        return f"idempotency:{self.domain.value}:{self.key}"

    @property
    def hash(self) -> str:
        """Get a hash of the key for indexing."""
        return hashlib.sha256(self.cache_key.encode()).hexdigest()[:32]

    @classmethod
    def for_operation(
        cls,
        entity_type: str,
        entity_id: int | str,
        operation: str,
        domain: IdempotencyDomain = IdempotencyDomain.EXTERNAL_SERVICE,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for a generic operation.

        Args:
            entity_type: Type of entity (e.g., "order", "user", "product")
            entity_id: The entity ID (int PKs or string UUIDs/correlation IDs).
            operation: The operation being performed (e.g., "process", "update")
            domain: The domain category

        Returns:
            IdempotencyKey for the operation
        """
        key = f"{entity_type}:{entity_id}:{operation}"
        return cls(
            domain=domain,
            key=key,
            components={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "operation": operation,
            },
        )

    @classmethod
    def for_event(cls, event_id: str) -> IdempotencyKey:
        """
        Create an idempotency key for event processing.

        Args:
            event_id: The unique event ID

        Returns:
            IdempotencyKey for the event
        """
        return cls(
            domain=IdempotencyDomain.EVENT,
            key=event_id,
            components={"event_id": event_id},
        )

    @classmethod
    def for_resource_action(
        cls,
        resource_type: str,
        resource_id: int,
        action: str,
        amount: int | None = None,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for resource actions.

        Args:
            resource_type: Type of resource
            resource_id: The resource ID
            action: The action being performed
            amount: Optional amount for the action

        Returns:
            IdempotencyKey for the resource action
        """
        if amount is not None:
            key = f"{resource_type}:{resource_id}:{action}:{amount}"
        else:
            key = f"{resource_type}:{resource_id}:{action}"
        return cls(
            domain=IdempotencyDomain.INTERNAL_PROCESS,
            key=key,
            components={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "action": action,
                "amount": amount,
            },
        )

    @classmethod
    def custom(cls, key: str, **components: Any) -> IdempotencyKey:
        """
        Create a custom idempotency key.

        Args:
            key: The raw key string
            **components: Key components for debugging

        Returns:
            IdempotencyKey with custom domain
        """
        return cls(
            domain=IdempotencyDomain.CUSTOM,
            key=key,
            components=components,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # New factory methods (priority 5 - v2.4.0)
    # ═══════════════════════════════════════════════════════════════════════════

    @classmethod
    def for_chaos_experiment(
        cls,
        schedule_id: str,
        experiment_type: str,
        target_service: str,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for a Chaos experiment.

        Prevents experiments of the same schedule from running concurrently.

        Args:
            schedule_id: Schedule ID
            experiment_type: Experiment type (e.g., latency_injection, fault_injection)
            target_service: Target service

        Returns:
            IdempotencyKey for chaos experiment
        """
        key = f"chaos:{schedule_id}:{experiment_type}:{target_service}"
        return cls(
            domain=IdempotencyDomain.CHAOS_EXPERIMENT,
            key=key,
            components={
                "schedule_id": schedule_id,
                "experiment_type": experiment_type,
                "target_service": target_service,
            },
        )

    @classmethod
    def for_chaos_service_lock(
        cls,
        target_service: str,
    ) -> IdempotencyKey:
        """
        Service-level Chaos experiment lock.

        Prevents two or more experiments from running concurrently on the same service.
        (ensures clarity of root-cause analysis)

        A dual-lock pattern used together with the Schedule lock:
        - Service Lock: concurrency control (released when the experiment ends)
        - Schedule Lock: re-execution prevention (kept until TTL)

        Args:
            target_service: Target service name

        Returns:
            IdempotencyKey for service-level chaos lock

        Usage:
            # Example of the dual-lock pattern
            service_lock = IdempotencyKey.for_chaos_service_lock(target_service)
            schedule_lock = IdempotencyKey.for_chaos_experiment(schedule_id, exp_type, target_service)

            # 1. Acquire the service lock (concurrency control)
            if not idempotency.acquire_lock(service_lock, ttl_seconds=7200):
                return "another experiment running"

            # 2. Acquire the schedule lock (re-execution prevention)
            if not idempotency.acquire_lock(schedule_lock, ttl_seconds=86400):
                idempotency.release_lock(service_lock)  # rollback
                return "schedule already executed"

            try:
                # 3. Run the experiment
                execute_experiment()
            finally:
                # 4. Release only the service lock (the schedule lock keeps its TTL)
                idempotency.release_lock(service_lock)

        """
        key = f"chaos:service_lock:{target_service}"
        return cls(
            domain=IdempotencyDomain.CHAOS_EXPERIMENT,
            key=key,
            components={
                "lock_type": "service_level",
                "target_service": target_service,
            },
        )

    @classmethod
    def for_config_change(
        cls,
        config_key: str,
        new_value_hash: str,
        changed_by: str,
        request_id: str | None = None,
        window_id: str | None = None,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for a configuration change.

        Prevents the same configuration change from being applied twice.

        Args:
            config_key: Configuration key
            new_value_hash: Hash of the new value
            changed_by: Who made the change
            request_id: Request ID (only retries of the same request count as duplicates)
            window_id: Sliding-window ID (time-window based, recommended)

        Idempotency-scope policy:
        - request_id provided: only retries of the same request are duplicates
        - window_id provided: only the same change within the same window is a duplicate
        - neither: based on new_value_hash (legacy behavior)

        Returns:
            IdempotencyKey for config change

        Reference: Architect Review - "distinguish intentional reconfiguration vs duplicates"
        """
        if request_id:
            # Request-level idempotency (strictest)
            key = f"config:{config_key}:{request_id}"
        elif window_id:
            # Sliding-window based (recommended)
            key = f"config:{config_key}:{new_value_hash}:w{window_id}"
        else:
            # Legacy behavior (value based)
            key = f"config:{config_key}:{new_value_hash}"

        return cls(
            domain=IdempotencyDomain.CONFIG_CHANGE,
            key=key,
            components={
                "config_key": config_key,
                "new_value_hash": new_value_hash,
                "changed_by": changed_by,
                "request_id": request_id,
                "window_id": window_id,
            },
        )

    @classmethod
    def for_l2_sync(
        cls,
        service_name: str,
        record_id: str,
        intended_state: str,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for L2 synchronization.

        Prevents the same record from being synchronized twice after recovery.

        Args:
            service_name: Service name
            record_id: Record ID
            intended_state: Target state

        Returns:
            IdempotencyKey for L2 sync
        """
        key = f"l2sync:{service_name}:{record_id}"
        return cls(
            domain=IdempotencyDomain.L2_SYNC,
            key=key,
            components={
                "service_name": service_name,
                "record_id": record_id,
                "intended_state": intended_state,
            },
        )

    @classmethod
    def for_wal_recovery(
        cls,
        wal_entry_id: str,
        operation: str,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for WAL recovery.

        Prevents the same WAL entry from being processed twice.

        Args:
            wal_entry_id: WAL entry ID
            operation: Recovery-operation type

        Returns:
            IdempotencyKey for WAL recovery
        """
        key = f"wal:{wal_entry_id}:{operation}"
        return cls(
            domain=IdempotencyDomain.WAL_RECOVERY,
            key=key,
            components={
                "wal_entry_id": wal_entry_id,
                "operation": operation,
            },
        )

    @classmethod
    def for_auto_adjustment(
        cls,
        module: str,
        parameter: str,
        target_value: str,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for an autonomous adjustment.

        Prevents the same adjustment from being applied twice.

        Args:
            module: Module name (circuit_breaker, retry, etc.)
            parameter: Parameter name
            target_value: Target value

        Returns:
            IdempotencyKey for auto adjustment

        Note:
            Use AntiFlappingWindow separately for flapping checks.
            get_anti_flapping_window().check_and_record(...)
        """
        key = f"adjust:{module}:{parameter}:{target_value}"
        return cls(
            domain=IdempotencyDomain.AUTO_ADJUSTMENT,
            key=key,
            components={
                "module": module,
                "parameter": parameter,
                "target_value": target_value,
            },
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # Multi-Region Active-Active recovery actions
    # ═══════════════════════════════════════════════════════════════════════════

    @classmethod
    def for_recovery_action(
        cls,
        action_type: str,
        target: str,
        region_id: str,
        session_id: str,
    ) -> IdempotencyKey:
        """
        Create an idempotency key for a recovery action.

        Prevents duplicate execution of the same recovery action across
        instances in an active-active deployment.

        Args:
            action_type: Action type ("cb_reset", "pod_restart", "dlq_retry", etc.)
            target: Target (service name, Pod name, etc.)
            region_id: Executing region (e.g., "ap-northeast-2")
            session_id: Recovery session ID (prevents duplicates within the same recovery session)

        Returns:
            IdempotencyKey for recovery action

        Example:
            # Check idempotency before a CB reset
            key = IdempotencyKey.for_recovery_action(
                action_type="cb_reset",
                target="payment_api",
                region_id="ap-northeast-2",
                session_id="sess-12345",
            )

            result = idempotency_service.check(key)
            if result.is_duplicate:
                logger.info("idempotency.already_executed_another_region")
                return

            # Execute
            circuit_breaker.reset("payment_api")
        """
        key = f"recovery:{action_type}:{target}:{session_id}"
        return cls(
            domain=IdempotencyDomain.RECOVERY_ACTION,
            key=key,
            components={
                "action_type": action_type,
                "target": target,
                "region_id": region_id,
                "session_id": session_id,
            },
        )

    @classmethod
    def for_cb_reset(
        cls,
        service_name: str,
        region_id: str,
        trigger_id: str,
    ) -> IdempotencyKey:
        """
        Idempotency key dedicated to a Circuit Breaker reset.

        A convenience method for for_recovery_action, providing a CB-reset-specific interface.

        Args:
            service_name: Service name (e.g., "payment_api")
            region_id: Executing region (e.g., "ap-northeast-2")
            trigger_id: Trigger ID (e.g., recovery session ID)

        Returns:
            IdempotencyKey for CB reset action

        Example:
            key = IdempotencyKey.for_cb_reset(
                service_name="payment_api",
                region_id="ap-northeast-2",
                trigger_id="recovery-sess-abc123",
            )

            if not idempotency_service.check(key).is_duplicate:
                circuit_breaker.reset("payment_api")
        """
        return cls.for_recovery_action(
            action_type="cb_reset",
            target=service_name,
            region_id=region_id,
            session_id=trigger_id,
        )

    @classmethod
    def for_pod_restart(
        cls,
        pod_name: str,
        namespace: str,
        region_id: str,
        session_id: str,
    ) -> IdempotencyKey:
        """
        Idempotency key dedicated to a Pod restart.

        Args:
            pod_name: Pod name
            namespace: Kubernetes namespace
            region_id: Executing region
            session_id: Recovery session ID

        Returns:
            IdempotencyKey for pod restart action
        """
        target = f"{namespace}/{pod_name}"
        return cls.for_recovery_action(
            action_type="pod_restart",
            target=target,
            region_id=region_id,
            session_id=session_id,
        )

    @classmethod
    def for_dlq_retry(
        cls,
        queue_name: str,
        message_id: str,
        region_id: str,
        session_id: str,
    ) -> IdempotencyKey:
        """
        Idempotency key dedicated to a DLQ retry.

        Args:
            queue_name: DLQ queue name
            message_id: Message ID
            region_id: Executing region
            session_id: Recovery session ID

        Returns:
            IdempotencyKey for DLQ retry action
        """
        target = f"{queue_name}:{message_id}"
        return cls.for_recovery_action(
            action_type="dlq_retry",
            target=target,
            region_id=region_id,
            session_id=session_id,
        )

    @classmethod
    def for_dlq_replay(
        cls,
        dlq_id: int,
        domain: str,
        retry_count: int,
    ) -> IdempotencyKey:
        """DLQ replay dedup — prevents concurrent replay of same entry at same attempt.

        Uses DLQ entry PK (guaranteed unique per DB) + retry_count to scope
        idempotency per attempt. Different attempts (retry_count) get different keys,
        allowing intentional retries while blocking concurrent duplicates.
        """
        return cls.for_recovery_action(
            action_type="dlq_replay",
            target=f"{domain}:{dlq_id}",
            region_id="",
            session_id=str(retry_count),
        )


@dataclass
class IdempotencyResult(Generic[T]):
    """Result of an idempotency check."""

    is_duplicate: bool
    existing_record: T | None = None
    message: str = ""

    @property
    def should_proceed(self) -> bool:
        """Whether the operation should proceed (not a duplicate)."""
        return not self.is_duplicate
