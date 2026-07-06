"""
Repository Interfaces for Baldur System

Abstract interfaces that define the contract for data access.
These interfaces allow the baldur core to be decoupled from
specific ORM implementations (Django, SQLAlchemy, etc.)

Design Principles:
1. Pure Python - no framework dependencies
2. Data classes for transfer objects
3. ABC for repository contracts
4. Optional fields use None, not Django's blank=True
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.models.cascade_event import CascadeEventData
    from baldur.models.recovery_session import RecoverySessionData

# ============================================================================
# Enums (Framework-independent)
# ============================================================================


class FailedOperationDomain(str, Enum):
    """
    Domain classification for failed operations (domain-neutral).

    Core domains are framework-agnostic. Application-specific domains
    (like 'payment', 'order') should be registered via adapter configuration.
    """

    # Domain-neutral base types
    EXTERNAL_SERVICE = "external_service"  # External API/service failures
    INTERNAL_PROCESS = "internal_process"  # Internal processing failures
    ASYNC_TASK = "async_task"  # Async/background task failures
    NOTIFICATION = "notification"  # Notification delivery failures
    DATA_SYNC = "data_sync"  # Data synchronization failures
    CUSTOM = "custom"  # Extension point for custom domains


class FailedOperationStatus(str, Enum):
    """State machine for DLQ item lifecycle"""

    PENDING = "pending"
    REPLAYING = "replaying"
    REVIEWING = "reviewing"
    REPLAYED = "replayed"
    REQUIRES_REVIEW = "requires_review"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    EXPIRED = "expired"
    PERMANENTLY_FAILED = "permanently_failed"


class ResolutionTrigger(str, Enum):
    """Trigger that initiated a DLQ replay, stamped into ``resolution_type``.

    Records *how* a successful replay was set in motion so an operator's
    console/API replay is distinguishable from a system-automatic one in the
    compliance-relevant resolution record. Distinct from ``replay_type``
    ("single"/"conditional"/"batch"), which labels the replay metric and must
    not be repurposed for provenance.

    The existing non-replay-pipeline resolution values (``duplicate_skip``,
    ``ttl_expired``, ``max_retries_exhausted``, ``manual_retry``,
    ``force_redrive``, ``manual``) are stamped directly at their own sites and
    are intentionally not members here.
    """

    MANUAL_REPLAY = "manual_replay"
    AUTO_REPLAY_CIRCUIT_CLOSE = "auto_replay_circuit_close"
    SCHEDULED_BATCH = "scheduled_batch"
    TRAFFIC_AWARE = "traffic_aware"
    THROTTLE_REPLAY = "throttle_replay"
    DLQ_CONSUMER = "dlq_consumer"


class CircuitBreakerStateEnum(str, Enum):
    """Circuit breaker states"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class SecurityIncidentType(str, Enum):
    """Types of security incidents (domain-neutral)"""

    # Domain-neutral incident types
    SIGNATURE_INVALID = "signature_invalid"  # Generic signature validation failure
    DATA_TAMPERED = "data_tampered"  # Generic data tampering detection
    TOKEN_FORGED = "token_forged"
    UNAUTHORIZED_ACCESS = "unauthorized_access"
    RATE_LIMIT_ABUSE = "rate_limit_abuse"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    REPLAY_ATTACK = "replay_attack"
    INJECTION_ATTEMPT = "injection_attempt"


class SecuritySeverity(str, Enum):
    """Severity levels for security incidents"""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"


class SecurityIncidentStatus(str, Enum):
    """Investigation status for security incidents"""

    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


# ============================================================================
# Data Transfer Objects (DTOs)
# ============================================================================


@dataclass
class FailedOperationData:
    """
    Data transfer object for FailedOperation model.

    Contains all necessary fields for DLQ operations without
    Django model dependencies.
    """

    # Identity — opaque string token (538 D1). Numeric backends translate
    # at the adapter boundary (str(pk) on read, int(id) on bind); the Redis
    # adapter carries a process-namespaced composite ID (538 D2).
    id: str

    # Domain & Classification
    domain: str
    failure_type: str
    status: str

    # Generic entity references (domain-neutral)
    # For single entity: entity_type="order", entity_id="123"
    # For multiple entities: use entity_refs dict
    entity_type: str | None = None
    entity_id: str | None = None
    entity_refs: dict[str, Any] = field(default_factory=dict)  # Legacy/extended refs
    user_id: int | None = None

    # Snapshot Data
    snapshot_data: dict[str, Any] = field(default_factory=dict)

    # Error Information
    error_code: str = ""
    error_message: str = ""

    # Retry Tracking
    retry_count: int = 0
    max_retries: int = 2
    last_retry_at: datetime | None = None

    # Forensic Context
    request_data: dict[str, Any] = field(default_factory=dict)
    response_data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Resolution
    resolved_at: datetime | None = None
    resolved_by_id: int | None = None
    resolution_type: str = ""
    resolution_note: str = ""

    # Recovery Hints
    next_action_hint: str = ""
    recommended_action: str = ""

    # Lifecycle
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        """Check if operation is pending review"""
        return self.status == FailedOperationStatus.PENDING.value

    @property
    def is_resolved(self) -> bool:
        """Check if operation is resolved"""
        return self.status == FailedOperationStatus.RESOLVED.value

    @property
    def can_retry(self) -> bool:
        """Check if operation can be retried"""
        return self.retry_count < self.max_retries


class DLQCompressedStatus(str, Enum):
    """Lifecycle state machine for compressed DLQ entries."""

    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


@dataclass
class DLQCompressedEntry:
    """
    Compressed summary of evicted DLQ entries.

    Groups entries by (domain, failure_type, error_code) and retains
    aggregate statistics instead of individual records.

    Lifecycle: ACTIVE → STALE → ARCHIVED (never hard deleted).
    """

    id: str  # "compressed:{domain}:{failure_type}:{error_code}:{timestamp}"

    # Grouping key
    domain: str
    failure_type: str
    error_code: str

    # Aggregate statistics
    count: int
    first_seen: datetime
    last_seen: datetime

    # Representative sample (for debugging)
    sample_error_message: str
    sample_context: dict[str, Any] = field(default_factory=dict)

    # Lifecycle
    status: str = DLQCompressedStatus.ACTIVE.value
    compressed_at: datetime = field(default_factory=lambda: utc_now())
    stale_at: datetime | None = None
    archived_at: datetime | None = None


@dataclass
class CircuitBreakerStateData:
    """
    Data transfer object for CircuitBreakerState model.

    Represents the current state of a circuit breaker for a service.
    """

    # Identity
    service_name: str
    id: int | None = None

    # State
    state: str = CircuitBreakerStateEnum.CLOSED.value
    failure_count: int = 0
    success_count: int = 0

    # Timing
    last_failure_at: datetime | None = None
    opened_at: datetime | None = None

    # Manual Control
    manually_controlled: bool = False
    controlled_by_id: int | None = None
    control_reason: str = ""
    manual_override_expires_at: datetime | None = None

    # Half-Open Tracking
    half_open_request_count: int = 0
    half_open_window_started_at: datetime | None = None

    # Extensible Metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    # Lifecycle
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)"""
        return self.state == CircuitBreakerStateEnum.OPEN.value

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (allowing requests)"""
        return self.state == CircuitBreakerStateEnum.CLOSED.value

    @property
    def is_half_open(self) -> bool:
        """Check if circuit is half-open (testing)"""
        return self.state == CircuitBreakerStateEnum.HALF_OPEN.value


@dataclass(frozen=True, slots=True)
class CircuitBreakerCloseAttempt:
    """Result of an atomic record-success-and-check-close attempt.

    Returned by `CircuitBreakerStateRepository.record_success_with_close_check`.
    `did_close=True` indicates this caller crossed the success threshold under
    the repository lock and the storage state was atomically transitioned to
    `closed` in the same critical section. Service-layer callers must emit the
    `CIRCUIT_BREAKER_CLOSED` event only when `did_close` is True — this is the
    sole single-fire gate replacing the prior unlocked
    `state.state == "half_open"` check.
    """

    state: CircuitBreakerStateData
    did_close: bool


@dataclass(frozen=True, slots=True)
class CircuitBreakerOpenAttempt:
    """Result of an atomic record-failure-and-check-open attempt.

    Returned by `CircuitBreakerStateRepository.record_failure_with_open_check`.
    `did_open=True` indicates this caller observed `half_open` and atomically
    transitioned the storage state to `open` in the same critical section.
    Service-layer callers must emit the `CIRCUIT_BREAKER_OPENED` event (and run
    the audit / metrics side-effects) only when `did_open` is True — this is the
    single-fire gate for the HALF_OPEN->OPEN failure path, the symmetric mirror
    of `CircuitBreakerCloseAttempt.did_close`. A concurrent stale-view caller
    that also read `half_open` sees `did_open=False`.
    """

    state: CircuitBreakerStateData
    did_open: bool


@dataclass
class SecurityIncidentData:
    """
    Data transfer object for SecurityIncident model.

    Security incidents are NEVER auto-healed and require human intervention.
    """

    # Identity
    id: int

    # Classification
    incident_type: str
    severity: str
    status: str

    # Source Information
    source_ip: str | None = None
    user_agent: str = ""
    user_id: int | None = None

    # Generic entity references (domain-neutral)
    entity_refs: dict[str, int] = field(default_factory=dict)

    # Details
    description: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)

    # Investigation
    assigned_to_id: int | None = None
    investigation_notes: str = ""
    resolved_at: datetime | None = None

    # Lifecycle
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_critical(self) -> bool:
        """Check if incident is critical severity"""
        return self.severity == SecuritySeverity.CRITICAL.value

    @property
    def needs_investigation(self) -> bool:
        """Check if incident needs investigation"""
        return self.status in [
            SecurityIncidentStatus.OPEN.value,
            SecurityIncidentStatus.INVESTIGATING.value,
        ]


# ============================================================================
# Repository Interfaces
# ============================================================================


class FailedOperationRepository(ABC):
    """
    Abstract repository for FailedOperation (DLQ) data access.

    Concrete implementations:
    - InMemoryFailedOperationRepository: in-process dict storage
    - SQLFailedOperationRepository: any DB-API 2.0 database
    - RedisDLQRepository: Redis via ResilientStorageBackend
    """

    @abstractmethod
    def create(
        self,
        domain: str,
        failure_type: str,
        error_message: str = "",
        error_code: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        entity_refs: dict[str, Any] | None = None,
        user_id: int | None = None,
        snapshot_data: dict[str, Any] | None = None,
        request_data: dict[str, Any] | None = None,
        response_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        retry_count: int = 0,
        max_retries: int = 2,
        next_action_hint: str = "",
        recommended_action: str = "",
        expires_at: datetime | None = None,
    ) -> FailedOperationData:
        """Create a new failed operation record"""
        ...

    @abstractmethod
    def get_by_id(self, id: str) -> FailedOperationData | None:
        """Get a failed operation by ID"""
        ...

    @abstractmethod
    def get_pending_by_domain(
        self,
        domain: str,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Get pending operations for a specific domain"""
        ...

    @abstractmethod
    def get_pending_count_by_domain(self, domain: str) -> int:
        """Get count of pending operations for a domain"""
        ...

    @abstractmethod
    def update_status(
        self,
        id: str,
        status: str,
        resolution_type: str = "",
        resolution_note: str = "",
        resolved_by_id: int | None = None,
        recommended_action: str = "",
    ) -> bool:
        """Update the status of a failed operation.

        recommended_action: Suggested operator action (e.g., "escalate",
        "manual_check", "replay"). Empty string preserves the existing value.
        """
        ...

    @abstractmethod
    def increment_retry_count(self, id: str) -> bool:
        """Increment retry count and update last_retry_at"""
        ...

    @abstractmethod
    def mark_as_resolved(
        self,
        id: str,
        resolution_type: str,
        resolution_note: str = "",
        resolved_by_id: int | None = None,
    ) -> bool:
        """Mark a failed operation as resolved"""
        ...

    @abstractmethod
    def get_expired_operations(
        self,
        before_date: datetime,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Get operations that have expired"""
        ...

    @abstractmethod
    def bulk_update_status(
        self,
        ids: list[str],
        status: str,
    ) -> int:
        """Bulk update status for multiple operations"""
        ...

    @abstractmethod
    def find_by_status(
        self,
        status: str,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Find operations by status with optional filters.

        Distinct contract from ``find``: positional ``status`` is required,
        results are ordered ``created_at`` ASC, and there is no offset. Used
        by replay/SLA paths that consume a single status oldest-first.
        """
        ...

    @abstractmethod
    def find(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Paginated cross-status query with optional filters.

        Results ordered by ``created_at`` DESC (newest-first). No filter
        returns all statuses — the default scope is "no filter = all", so
        escalated/terminal statuses (``permanently_failed``,
        ``requires_review``) are visible by default. Mirrors the sibling
        archive repositories' ``find(*, ..., offset, limit)`` contract.
        """
        ...

    @abstractmethod
    def count(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
    ) -> int:
        """Count operations matching filters (no filter = all statuses)."""
        ...

    @abstractmethod
    def count_created_in_window(self, start: datetime, end: datetime) -> int:
        """Count operations whose ``created_at`` is in the inclusive [start, end].

        Counts every status (no status scope): an entry created in the window
        that was later resolved/archived still consumed budget when it failed,
        so the windowed inflow count must not be status-scoped. Powers the
        Error Budget windowed inflow source.
        """
        ...

    @abstractmethod
    def find_replayable(
        self,
        max_retries: int,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """Find operations that can be replayed (pending and retry_count < max_retries)"""
        ...

    @abstractmethod
    def find_sla_breached(
        self,
        current_time: datetime,
        sla_thresholds: dict[str, timedelta],
    ) -> list[FailedOperationData]:
        """Find operations that have breached their SLA"""
        ...

    @abstractmethod
    def find_expired(
        self,
        current_time: datetime,
    ) -> list[FailedOperationData]:
        """Find operations past their retention period"""
        ...

    @abstractmethod
    def get_statistics(self) -> dict[str, Any]:
        """Get statistics about failed operations"""
        ...

    @abstractmethod
    def get_facet_counts(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Faceted status×domain counts for the admin-console DLQ filter.

        Returns ``{"by_status": {status: n, ...}, "by_domain": {domain: n, ...}}``
        with zero-count buckets dropped.

        Standard faceted-search semantics: each facet excludes its own
        selection — ``by_status`` is scoped by the ``domain`` filter and
        ``by_domain`` is scoped by the ``status`` filter, so the dimension
        being chosen keeps all of its options. An unfiltered call returns
        the complete ``by_status`` + ``by_domain``. Matching on both
        dimensions is exact (no substring/fuzzy).
        """
        ...

    # =========================================================================
    # Atomic Operations for Concurrency Safety
    # =========================================================================

    @abstractmethod
    def try_acquire_for_replay(
        self,
        id: str,
        max_retries: int,
        force: bool = False,
    ) -> FailedOperationData | None:
        """
        Atomically acquire a DLQ entry for replay.

        Normal mode (``force=False``) MUST:
        1. Check if status is PENDING and retry_count < max_retries
        2. If eligible, atomically set status to REPLAYING and increment retry_count
        3. Return the FailedOperationData if acquired, None if not eligible

        Force mode (``force=True``) is the operator-driven cap-override escape
        hatch for re-driving an at-cap entry after a root-cause fix. It MUST:
        1. Accept status in {PENDING, REQUIRES_REVIEW} and reject every other
           status (RESOLVED / ARCHIVED / REPLAYING / REVIEWING / ... -> None)
        2. Skip the ``retry_count < max_retries`` check entirely
        3. Reset retry_count to a fresh budget (the redrive attempt is attempt 1
           -> retry_count == 1 after acquire), so the entry becomes an ordinary
           under-cap entry for all downstream lifecycle logic
        4. Before resetting, stamp the persisted ``metadata`` with
           ``previous_total_retries`` (the pre-reset count accumulated across
           prior force-redrives) and ``force_redrive_count`` (incremented), so
           the budget reset preserves the forensic scar
        5. Atomically set status to REPLAYING

        The poison-pill convergence guarantee is preserved: a still-broken
        force-redriven entry re-converges to REQUIRES_REVIEW within
        ``max_retries`` further automatic attempts via ``complete_replay``.

        Implementation should use row-level locking (SELECT FOR UPDATE) or
        optimistic locking (version/updated_at check) to prevent race conditions.

        Args:
            id: The DLQ entry ID to acquire
            max_retries: Maximum allowed retry attempts
            force: Operator cap-override — bypass the cap gate and accept an
                at-cap REQUIRES_REVIEW entry (see Force mode above)

        Returns:
            FailedOperationData if successfully acquired, None otherwise

        Example Django implementation (normal mode):
            with transaction.atomic():
                entry = FailedOperation.objects.select_for_update().get(id=id)
                if entry.status != 'pending' or entry.retry_count >= max_retries:
                    return None
                entry.status = 'replaying'
                entry.retry_count += 1
                entry.last_retry_at = now()
                entry.save()
                return FailedOperationData.from_model(entry)
        """
        ...

    @abstractmethod
    def complete_replay(
        self,
        id: str,
        success: bool,
        resolution_type: str = "",
        note: str = "",
        resolved_by_id: int | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> bool:
        """
        Complete a replay operation by updating the final status.

        Should be called after replay execution to set final state:
        - success=True: Mark as RESOLVED with resolution details
        - success=False: Revert to PENDING (for retry) or REQUIRES_REVIEW (if escalated)

        This method is safe to call without transaction wrapper as it only
        updates an already-acquired entry.

        Args:
            id: The DLQ entry ID
            success: Whether the replay succeeded
            resolution_type: Type of resolution (for successful replays)
            note: Resolution note or error message
            resolved_by_id: User ID who resolved (None for system)
            error_details: Additional error context (for failed replays)

        Returns:
            True if update succeeded, False otherwise
        """
        ...

    @abstractmethod
    def release_stale_replaying(
        self,
        older_than_minutes: int = 30,
    ) -> int:
        """
        Release DLQ entries stuck in REPLAYING state.

        Entries can get stuck if the replay process crashes after acquiring
        but before completing. This method reverts them to PENDING for retry.

        Args:
            older_than_minutes: Consider entries older than this as stale

        Returns:
            Number of entries released
        """
        ...

    # =========================================================================
    # Cleanup Operations (Manual - No Auto-Delete)
    # =========================================================================

    @abstractmethod
    def archive_old_resolved(
        self,
        older_than_days: int = 30,
    ) -> int:
        """
        Archive resolved entries older than N days.

        Changes status from RESOLVED to ARCHIVED.
        This is a soft-delete operation - data is preserved.

        Args:
            older_than_days: Archive entries resolved more than this many days ago

        Returns:
            Number of entries archived
        """
        ...

    @abstractmethod
    def purge_archived(
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """
        Permanently delete archived entries.

        IMPORTANT: This is a destructive operation. Only archived entries
        can be purged. Either specify IDs or older_than_days, not both.

        With neither argument the call is a no-op (returns 0): a destructive
        purge with no selection criteria deletes nothing (fail-safe default).
        To purge every archived entry, pass ``older_than_days=0`` ("older than
        0 days" matches all archived entries).

        Args:
            ids: Specific entry IDs to purge (must be ARCHIVED status)
            older_than_days: Purge archived entries older than N days; ``0``
                purges all archived entries

        Returns:
            Number of entries permanently deleted

        Raises:
            ValueError: If trying to purge non-archived entries
        """
        ...

    @abstractmethod
    def get_cleanup_stats(self) -> dict[str, Any]:
        """
        Get statistics for cleanup operations.

        Returns:
            Dict with counts by status, age distributions, etc.
            Example:
            {
                "total": 1500,
                "by_status": {
                    "pending": 50,
                    "resolved": 1200,
                    "archived": 250,
                },
                "resolved_older_than_30_days": 800,
                "archived_older_than_90_days": 100,
            }
        """
        ...

    @abstractmethod
    def count_archived_older_than(self, older_than_days: int) -> int:
        """Count archived entries older than N days.

        Pushes counting to the repository layer where SQL adapters can use
        SELECT COUNT(*) instead of loading objects into memory.

        Args:
            older_than_days: Count archived entries resolved more than this many days ago

        Returns:
            Number of matching archived entries
        """
        ...

    # =========================================================================
    # Size Limit / Overflow Operations (329_DLQ_SIZE_LIMIT)
    # =========================================================================

    @abstractmethod
    def count_all(self) -> int:
        """Return active DLQ item count (excludes resolved/rejected/archived)."""
        ...

    @abstractmethod
    def count_by_domain(self, domain: str) -> int:
        """Return DLQ item count for a specific domain."""
        ...

    @abstractmethod
    def get_oldest_ids(self, count: int, domain: str | None = None) -> list[str]:
        """Return IDs of the oldest items (by score/timestamp)."""
        ...

    @abstractmethod
    def delete(self, entry_id: str) -> bool:
        """Delete a single DLQ entry by ID. Returns True if deleted."""
        ...

    @abstractmethod
    def evict_oldest(self, count: int, domain: str | None = None) -> int:
        """Delete the oldest items. Returns number of items actually deleted."""
        ...

    def compress_and_evict_oldest(self, count: int, domain: str | None = None) -> int:
        """Summarize then evict oldest items. Default delegates to evict_oldest."""
        return self.evict_oldest(count, domain)

    # =========================================================================
    # Compression Operations (351_DLQ_COMPRESSION)
    # =========================================================================

    def store_compressed_entry(self, entry: DLQCompressedEntry) -> bool:
        """Store a compressed summary entry. Returns True if stored."""
        raise NotImplementedError(
            "Compression storage not implemented for this adapter"
        )

    def get_compressed_entries(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DLQCompressedEntry]:
        """Return compressed DLQ entries, optionally filtered."""
        raise NotImplementedError("Compression query not implemented for this adapter")

    def get_compressed_summary(self) -> dict[str, Any]:
        """Return aggregate statistics of compressed entries."""
        raise NotImplementedError(
            "Compression summary not implemented for this adapter"
        )

    def update_compressed_status(self, entry_id: str, new_status: str) -> bool:
        """Transition compressed entry status. Returns True if updated."""
        raise NotImplementedError(
            "Compression status update not implemented for this adapter"
        )


class CircuitBreakerStateRepository(ABC):
    """
    Abstract repository for CircuitBreakerState data access.

    Manages circuit breaker state persistence and retrieval.
    """

    @abstractmethod
    def get_or_create(self, service_name: str) -> CircuitBreakerStateData:
        """Get existing state or create new one for a service"""
        ...

    @abstractmethod
    def get_by_service_name(self, service_name: str) -> CircuitBreakerStateData | None:
        """Get circuit breaker state by service name"""
        ...

    @abstractmethod
    def update_state(
        self,
        service_name: str,
        state: str,
        failure_count: int | None = None,
        success_count: int | None = None,
        opened_at: datetime | None = None,
        last_failure_at: datetime | None = None,
        half_open_request_count: int | None = None,
        reset_half_open_count: bool = False,
    ) -> bool:
        """Update circuit breaker state.

        Args:
            service_name: Service identifier
            state: New state (closed, open, half_open)
            failure_count: Optional failure count
            success_count: Optional success count
            opened_at: Optional time when circuit was opened
            half_open_request_count: Optional half-open request counter value
            reset_half_open_count: If True, atomically clear the half-open
                counter and watermark in the same write. Used by
                cold-path transitions out of HALF_OPEN (record_failure,
                record_success, force_open, force_close) so the state change
                and counter reset commit in a single round-trip.

        Returns:
            True on success
        """
        ...

    @abstractmethod
    def record_failure(self, service_name: str) -> CircuitBreakerStateData:
        """Record a failure and return updated state"""
        ...

    @abstractmethod
    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        """Record a success and return updated state"""
        ...

    def record_success_with_close_check(
        self,
        service_name: str,
        success_threshold: int,
    ) -> CircuitBreakerCloseAttempt:
        """Record a success and atomically check whether to close the circuit.

        Race-unsafe default implementation: invokes `record_success` followed
        by a separate `update_state` call when the threshold is met. Adapters
        that can perform the read-decide-write atomically (e.g., InMemory
        under a single lock acquire, Redis via Lua, SQL under a transaction)
        MUST override this to close the TOCTOU window that allows multiple
        callers to each observe a passing threshold and emit duplicate
        CIRCUIT_BREAKER_CLOSED events for the same logical transition.

        The race-unsafe default exists so non-InMemory adapters compile and
        function without change — at the cost of retaining the duplicate-emit
        race they previously had. Their distributed-safe override is tracked
        as the "Distributed Redis-backed CB race" out-of-scope item.

        Args:
            service_name: Circuit breaker identifier.
            success_threshold: Number of HALF_OPEN successes required to
                transition to CLOSED. Comes from `CircuitBreakerConfig`.

        Returns:
            `CircuitBreakerCloseAttempt(state, did_close)`. `did_close` is
            True only for the single caller that crossed the threshold under
            the adapter's atomicity guarantee — concurrent stale-view callers
            see `did_close=False`.
        """
        updated_state = self.record_success(service_name)
        if (
            updated_state.state == CircuitBreakerStateEnum.HALF_OPEN.value
            and updated_state.success_count >= success_threshold
        ):
            self.update_state(
                service_name=service_name,
                state=CircuitBreakerStateEnum.CLOSED.value,
                failure_count=0,
                success_count=0,
                opened_at=None,
                reset_half_open_count=True,
            )
            closed_state = self.get_by_service_name(service_name) or updated_state
            return CircuitBreakerCloseAttempt(state=closed_state, did_close=True)
        return CircuitBreakerCloseAttempt(state=updated_state, did_close=False)

    def record_failure_with_open_check(
        self,
        service_name: str,
    ) -> CircuitBreakerOpenAttempt:
        """Record a failure and atomically re-open the circuit if HALF_OPEN.

        Symmetric mirror of `record_success_with_close_check`, scoped to the
        HALF_OPEN->OPEN transition. A single HALF_OPEN failure re-opens
        unconditionally (no threshold), so this is simpler than the close-check.

        Race-unsafe default implementation: reads state, then issues a separate
        `update_state` when it observes HALF_OPEN. Adapters that can perform the
        read-decide-write atomically (InMemory under a single lock acquire,
        Redis via Lua, SQL under a row lock) MUST override this to close the
        TOCTOU window where multiple stale-view callers each observe HALF_OPEN
        and emit duplicate CIRCUIT_BREAKER_OPENED events for one logical re-open.

        The race-unsafe default exists so adapters that have not yet ported the
        atomic override compile and function — at the cost of retaining the
        duplicate-emit race.

        Args:
            service_name: Circuit breaker identifier.

        Returns:
            `CircuitBreakerOpenAttempt(state, did_open)`. `did_open` is True only
            for the single caller that performed the HALF_OPEN->OPEN transition
            under the adapter's atomicity guarantee — concurrent stale-view
            callers see `did_open=False`.
        """
        state = self.get_or_create(service_name)
        if state.state == CircuitBreakerStateEnum.HALF_OPEN.value:
            self.update_state(
                service_name=service_name,
                state=CircuitBreakerStateEnum.OPEN.value,
                failure_count=0,
                success_count=0,
                opened_at=utc_now(),
                reset_half_open_count=True,
            )
            open_state = self.get_by_service_name(service_name) or state
            return CircuitBreakerOpenAttempt(state=open_state, did_open=True)
        return CircuitBreakerOpenAttempt(state=state, did_open=False)

    @abstractmethod
    def set_manual_control(
        self,
        service_name: str,
        state: str,
        controlled_by_id: int | None = None,
        reason: str = "",
        expires_at: datetime | None = None,
    ) -> bool:
        """Set manual control on a circuit breaker"""
        ...

    @abstractmethod
    def clear_manual_control(
        self, service_name: str, preserve_reason: bool = False
    ) -> bool:
        """Clear manual control from a circuit breaker

        Args:
            service_name: Name of the service
            preserve_reason: If True, keep the existing control_reason value
        """
        ...

    @abstractmethod
    def get_all_states(self) -> list[CircuitBreakerStateData]:
        """Get all circuit breaker states.

        Scale bound: intended for OSS / PRO deployments where the total
        circuit-breaker count is well under ~1K (typically <= a few hundred).
        Used by admin dashboards and IPC snapshots that genuinely want a
        full picture. Callers needing larger result sets, or needing only
        a subset, should use ``get_open_states(limit=...)`` or filter via
        a future paginated API instead of growing this method.
        """
        ...

    def get_open_states(
        self, limit: int | None = None
    ) -> list[CircuitBreakerStateData]:
        """Get circuit breaker states in OPEN state.

        More efficient than get_all_states() + filter for large keyspaces.
        Default implementation filters get_all_states(); adapters may override
        with optimized queries (e.g., SCAN instead of KEYS in Redis).

        Args:
            limit: Maximum number of results. None means no limit.

        Returns:
            List of CircuitBreakerStateData with state == OPEN,
            ordered by opened_at ascending (oldest first).
        """
        all_states = self.get_all_states()
        open_states = [
            s for s in all_states if s.state == CircuitBreakerStateEnum.OPEN.value
        ]
        open_states.sort(key=lambda s: s.opened_at or datetime.min.replace(tzinfo=UTC))
        if limit is not None:
            return open_states[:limit]
        return open_states

    @abstractmethod
    def reset(self, service_name: str) -> bool:
        """Reset circuit breaker to initial closed state"""
        ...

    @abstractmethod
    def delete_state(self, service_name: str) -> bool:
        """Delete circuit breaker state entirely.

        Used by reconciliation jobs to remove orphaned CB entries.

        Args:
            service_name: Service identifier (may be Composite Key)

        Returns:
            True if deleted, False if not found
        """
        ...

    # =========================================================================
    # Atomic Operations for Concurrency Safety
    # =========================================================================

    @abstractmethod
    def atomic_force_open(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int = 90,
    ) -> tuple[bool, str, str]:
        """
        Atomically force open a circuit breaker.

        This method MUST use row-level locking to prevent concurrent modifications.
        Creates the circuit breaker if it doesn't exist.

        Args:
            service_name: Name of the service
            reason: Reason for opening
            controlled_by_id: User ID who initiated the change
            ttl_minutes: TTL for manual override

        Returns:
            Tuple of (success, previous_state, new_state)

        Example Django implementation:
            with transaction.atomic():
                state, created = CircuitBreakerState.objects.select_for_update().get_or_create(
                    service_name=service_name
                )
                previous = state.state
                state.state = 'open'
                state.manually_controlled = True
                state.save()
                return (True, previous, 'open')
        """
        ...

    @abstractmethod
    def atomic_force_close(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        """
        Atomically force close a circuit breaker.

        This method MUST use row-level locking to prevent concurrent modifications.

        Args:
            service_name: Name of the service
            reason: Reason for closing
            controlled_by_id: User ID who initiated the change

        Returns:
            Tuple of (success, previous_state, new_state)
        """
        ...

    @abstractmethod
    def atomic_reset(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        """
        Atomically reset a circuit breaker to initial state.

        This method MUST use row-level locking to prevent concurrent modifications.
        Resets all counters and clears manual control.

        Args:
            service_name: Name of the service
            reason: Reason for reset
            controlled_by_id: User ID who initiated the change

        Returns:
            Tuple of (success, previous_state, new_state)
        """
        ...

    # D8: branch 1 is the stuck-window auto-reset.
    @abstractmethod
    def try_acquire_half_open_slot(
        self,
        service_name: str,
        limit: int,
        stuck_timeout_seconds: int,
    ) -> tuple[bool, str, str]:
        """
        Atomically acquire a HALF_OPEN trial slot.

        This is the single race-free entry point for the should_allow HALF_OPEN
        branch. Implementations MUST perform the state-machine evaluation and
        the counter increment as one atomic operation (Redis Lua / SQL
        SELECT FOR UPDATE / in-memory under RLock).

        State-machine branches (in evaluation order):

        1. ``state == "half_open"`` AND ``count >= limit`` AND
           ``now - half_open_window_started_at > stuck_timeout_seconds``:
           Stuck-window auto-reset. Treat as a fresh OPEN→HALF_OPEN combo:
           reset ``half_open_request_count = 1``, ``success_count = 0``,
           refresh the window watermark. Return ``(True, "half_open", "half_open")``.
        2. ``state == "open"`` (recovery_timeout already verified by the caller):
           Atomic OPEN→HALF_OPEN transition: write ``state = "half_open"``,
           ``success_count = 0``, ``half_open_request_count = 1``, set the
           window watermark. Return ``(True, "open", "half_open")``.
        3. ``state == "half_open"`` AND ``count < limit``: increment the
           counter. Return ``(True, "half_open", "half_open")``.
        4. ``state == "half_open"`` AND ``count >= limit`` (within window):
           Reject. Return ``(False, "half_open", "half_open")``.
        5. Otherwise (CLOSED, manual override, etc.): no-op. Return
           ``(False, current_state, current_state)``.

        Args:
            service_name: Service identifier
            limit: Maximum HALF_OPEN trial slots (cluster-wide)
            stuck_timeout_seconds: Window age (seconds) past which a maxed-out
                HALF_OPEN window is considered stalled (worker died mid-trial)
                and auto-reset on the next acquire

        Returns:
            Tuple of ``(allowed, previous_state, new_state)``. The service
            emits ``CIRCUIT_BREAKER_HALF_OPENED`` iff
            ``previous_state == "open" AND new_state == "half_open"``.
        """
        ...

    # 476 G8: watermark reset paired with the HALF_OPEN counter. D9: the
    # hot-path transition prefers the single-round-trip update_state form.
    @abstractmethod
    def reset_half_open_count(self, service_name: str) -> None:
        """
        Reset the HALF_OPEN counter and clear the window watermark.

        Used by manual_control flows where a counter reset is needed without
        a state change. Hot-path transitions out of HALF_OPEN should prefer
        ``update_state(..., reset_half_open_count=True)`` (single
        round-trip).

        Args:
            service_name: Service identifier
        """
        ...


class SecurityIncidentRepository(ABC):
    """
    Abstract repository for SecurityIncident data access.

    Security incidents are stored separately and NEVER auto-replayed.
    """

    @abstractmethod
    def create(
        self,
        incident_type: str,
        severity: str,
        description: str = "",
        source_ip: str | None = None,
        user_agent: str = "",
        user_id: int | None = None,
        entity_refs: dict[str, int] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> SecurityIncidentData:
        """Create a new security incident"""
        ...

    @abstractmethod
    def get_by_id(self, id: int) -> SecurityIncidentData | None:
        """Get a security incident by ID"""
        ...

    @abstractmethod
    def get_open_incidents(
        self,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get all open (unresolved) incidents"""
        ...

    @abstractmethod
    def get_by_type(
        self,
        incident_type: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get incidents by type"""
        ...

    @abstractmethod
    def get_by_severity(
        self,
        severity: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get incidents by severity"""
        ...

    @abstractmethod
    def update_status(
        self,
        id: int,
        status: str,
        investigation_notes: str = "",
        assigned_to_id: int | None = None,
    ) -> bool:
        """Update incident status"""
        ...

    @abstractmethod
    def mark_as_resolved(
        self,
        id: int,
        investigation_notes: str = "",
    ) -> bool:
        """Mark incident as resolved"""
        ...

    @abstractmethod
    def get_recent_by_ip(
        self,
        source_ip: str,
        hours: int = 24,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get recent incidents from a specific IP"""
        ...

    @abstractmethod
    def count_by_type_since(
        self,
        incident_type: str,
        since: datetime,
    ) -> int:
        """Count incidents of a type since a given time"""
        ...


# ============================================================================
# Postmortem DTOs & Repository
# ============================================================================


def _parse_datetime(value: Any) -> datetime | None:
    """Parse datetime from ISO string or datetime object."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class PostmortemData(SerializableMixin):
    """Framework-agnostic Postmortem domain model.

    Replaces AbstractPostmortemRecord's data role.
    Django Model remains as adapter-internal persistence detail.
    """

    incident_id: str
    """Unique incident identifier."""

    started_at: datetime | None = None
    """Incident start time."""

    resolved_at: datetime | None = None
    """Incident resolution time."""

    duration_seconds: float = 0.0
    """Incident duration in seconds."""

    affected_services: list[str] = field(default_factory=list)
    """List of services affected by the incident."""

    timeline: list[dict[str, Any]] = field(default_factory=list)
    """Chronological event timeline."""

    auto_actions: list[dict[str, Any]] = field(default_factory=list)
    """Automatic recovery actions taken."""

    recommendations: list[str] = field(default_factory=list)
    """Recommended remediation steps."""

    system_snapshot: dict[str, Any] = field(default_factory=dict)
    """System state snapshot at incident time."""

    created_at: datetime | None = None
    """Record creation time."""

    source: str = "auto"
    """Record origin: 'auto' (system-generated) or 'manual'."""

    id: str = ""
    """UUID string identifier. Auto-generated if empty."""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if self.created_at is None:
            from baldur.utils.time import utc_now

            self.created_at = utc_now()

    @classmethod
    def from_incident_dict(cls, incident_data: dict[str, Any]) -> PostmortemData:
        """Create from raw incident dictionary (In-Memory format).

        Handles ISO date parsing and field extraction — replaces
        AbstractPostmortemRecord.create_from_incident_dict().
        """
        from baldur.utils.time import utc_now

        incident_id = incident_data.get("incident_id") or str(uuid.uuid4())

        started_at = _parse_datetime(incident_data.get("started_at"))
        resolved_at = _parse_datetime(incident_data.get("resolved_at"))

        duration_seconds = 0.0
        if incident_data.get("duration_seconds"):
            duration_seconds = float(incident_data["duration_seconds"])
        elif started_at and resolved_at:
            duration_seconds = (resolved_at - started_at).total_seconds()

        # Backward compatibility: support "source", "manual", and legacy "is_auto" keys
        # (mirrors AbstractPostmortemRecord.create_from_incident_dict logic)
        if (
            incident_data.get("source") == "manual"
            or incident_data.get("manual")
            or incident_data.get("is_auto") is False
        ):
            source = "manual"
        else:
            source = "auto"

        return cls(
            incident_id=incident_id,
            started_at=started_at or utc_now(),
            resolved_at=resolved_at,
            duration_seconds=duration_seconds,
            affected_services=incident_data.get("affected_services", []),
            timeline=incident_data.get("timeline", []),
            auto_actions=incident_data.get("auto_actions", []),
            recommendations=incident_data.get("recommendations", []),
            system_snapshot=incident_data.get("system_snapshot", {}),
            source=source,
        )


# Concrete hybrid (DB-primary + in-memory cache) impl lives in store.py.
class PostmortemRepository(ABC):
    """Abstract repository for Postmortem data access.

    Postmortem records are immutable audit artifacts.
    Supports hybrid storage (DB primary + In-Memory cache).
    """

    @abstractmethod
    def save(self, data: PostmortemData) -> bool:
        """Persist a postmortem record.

        Returns True if saved successfully, False otherwise.
        Implementations should handle duplicate incident_id gracefully:
        - DB-backed (Django): skip duplicate, return False (UNIQUE constraint)
        - InMemory: overwrite existing, return True (no constraint)
        """
        ...

    @abstractmethod
    def get_by_incident_id(self, incident_id: str) -> PostmortemData | None:
        """Retrieve a single postmortem by incident ID."""
        ...

    @abstractmethod
    def find(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[PostmortemData]:
        """Query postmortems with optional filters.

        Results ordered by started_at DESC.
        """
        ...

    @abstractmethod
    def count(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
    ) -> int:
        """Count postmortems matching filters."""
        ...

    @abstractmethod
    def update_fields(
        self,
        incident_id: str,
        fields: dict[str, Any],
    ) -> bool:
        """Partial update of specific fields.

        JSONField values are deep-merged with existing data.
        Returns True if the record was found and updated.
        """
        ...


# ============================================================================
# Cascade Event Archive DTOs & Repository
# ============================================================================


# ORM patterns sourced from tasks/cascade_cleanup_tasks.py.
class CascadeEventArchiveRepository(ABC):
    """Cascade Event archive persistence."""

    @abstractmethod
    def save(self, data: CascadeEventData) -> bool:
        """Persist a cascade event record.

        Idempotent — Django: IntegrityError → False, InMemory: overwrite → True.
        """
        ...

    @abstractmethod
    def get_by_cascade_id(self, cascade_id: str) -> CascadeEventData | None:
        """Retrieve a single cascade event by ID."""
        ...

    @abstractmethod
    def find(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        is_test: bool | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CascadeEventData]:
        """Query with optional filters. Results ordered by timestamp DESC."""
        ...

    @abstractmethod
    def count(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> int:
        """Count cascade events matching filters."""
        ...

    @abstractmethod
    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete archived events older than cutoff. Returns deleted count."""
        ...

    @abstractmethod
    def get_chain(
        self,
        namespace: str,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[CascadeEventData]:
        """Retrieve hash chain for integrity verification. Ordered by timestamp ASC."""
        ...


# ============================================================================
# Recovery Session Archive DTOs & Repository
# ============================================================================


# ORM patterns sourced from services/coordination/recovery_session_archive.py.
class RecoverySessionArchiveRepository(ABC):
    """Recovery Session archive persistence."""

    @abstractmethod
    def save(self, data: RecoverySessionData) -> bool:
        """Persist a recovery session record.

        Idempotent — Django: IntegrityError → False, InMemory: overwrite → True.
        """
        ...

    @abstractmethod
    def get_by_session_id(self, session_id: str) -> RecoverySessionData | None:
        """Retrieve a single session by ID."""
        ...

    @abstractmethod
    def find(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[RecoverySessionData]:
        """Query with optional filters. Results ordered by started_at DESC."""
        ...

    @abstractmethod
    def count(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count sessions matching filters."""
        ...

    @abstractmethod
    def update(self, data: RecoverySessionData) -> bool:
        """Full update of an existing session record.

        Returns True if record found and updated.
        """
        ...

    @abstractmethod
    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete archived sessions older than cutoff. Returns deleted count."""
        ...
