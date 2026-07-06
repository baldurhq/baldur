"""Event type and priority definitions for the Baldur Event Bus."""

from __future__ import annotations

from enum import Enum, IntEnum

__all__ = [
    "EventType",
    "EventPriority",
]


class EventType(str, Enum):
    """Baldur system event types."""

    # Emergency Mode Events
    EMERGENCY_LEVEL_CHANGED = "emergency_level_changed"
    """Emergency level changed (global or regional).

    Subscribers processing this event MUST filter by namespace using
    `utils.event_filters.should_handle_emergency_event()` to respect
    global vs regional scope. Global events affect all pods; regional
    events should only affect pods in the matching region.

    Data fields:
    - namespace: "global" | region name (e.g., "seoul")
    - scope: "global" | "regional"
    - level: EmergencyLevel value
    - previous_level: EmergencyLevel value
    - reason: str | None
    - activated_by: str | None
    - is_active: bool
    - is_escalation: bool
    """
    EMERGENCY_ACTIVATED = "emergency_activated"
    EMERGENCY_RECOVERY_STARTED = "emergency_recovery_started"
    EMERGENCY_RECOVERY_COMPLETED = "emergency_recovery_completed"

    # Error Budget Events
    ERROR_BUDGET_CRITICAL = "error_budget_critical"
    ERROR_BUDGET_WARNING = "error_budget_warning"
    ERROR_BUDGET_RECOVERED = "error_budget_recovered"

    # Circuit Breaker Events
    CIRCUIT_BREAKER_OPENED = "circuit_breaker_opened"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker_closed"
    CIRCUIT_BREAKER_HALF_OPENED = "circuit_breaker_half_opened"

    # Mesh Coordinator Events
    MESH_OVERRIDE_INVALIDATION = "mesh_override_invalidation"
    """L1 cache invalidation broadcast across pods (infrastructure sync)."""

    # Config Events
    CONFIG_UPDATED = "config_updated"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    KILL_SWITCH_DEACTIVATED = "kill_switch_deactivated"

    # DLQ Events
    DLQ_REPLAY_BLOCKED = "dlq_replay_blocked"
    DLQ_REPLAY_COMPLETED = "dlq_replay_completed"
    """Per-item replay completion (includes dlq_id, domain, success, replay_attempt)."""
    DLQ_REPLAY_BATCH_COMPLETED = "dlq_replay_batch_completed"
    """Batch replay summary (includes domain, total, success_count, failed_count)."""
    DLQ_REPLAY_FAILED = "dlq_replay_failed"
    """DLQ replay handler crash (includes dlq_id, domain, replay_attempt, error_type, error_message)."""

    DLQ_CONSUMER_STARTED = "dlq_consumer_started"
    """DLQ Consumer process lifecycle started — leader election begun (includes resource_name).
    Emitted only after the underlying elector successfully starts (see doc 483 D4b)."""

    DLQ_CONSUMER_STOPPED = "dlq_consumer_stopped"
    """DLQ Consumer process lifecycle stopped — leader election halted (includes resource_name)."""

    DLQ_CONSUMER_LEADERSHIP_ACQUIRED = "dlq_consumer_leadership_acquired"
    """DLQ Consumer became leader and started processing (includes resource_name)."""

    DLQ_CONSUMER_LEADERSHIP_LOST = "dlq_consumer_leadership_lost"
    """DLQ Consumer lost leadership and stopped processing (includes resource_name)."""

    DLQ_OUTBOX_DROP_THRESHOLD_BREACHED = "dlq_outbox_drop_threshold_breached"
    """DLQ outbox RingBuffer drop-rate exceeded the configured threshold (data:
    capacity, size, total_dropped, drop_rate). Operator-facing leading
    indicator — emitted before drops accumulate to customer-visible loss."""

    # Daemon Worker Lifecycle Events (impl 489 D12)
    DAEMON_WORKER_DIED = "daemon_worker_died"
    """A registered daemon worker thread is no longer alive. Data fields:
    worker_name, was_respawnable, last_heartbeat_age_seconds, crash_reason
    (str | None — populated from handle.last_crash_reason if the loop
    target's wrapper captured an uncaught exception, otherwise None for
    clean thread exit). Subscribers may toggle producer-side fail-open
    flags or trigger external alerts."""

    DAEMON_WORKER_RESPAWNED = "daemon_worker_respawned"
    """A registered daemon worker thread was successfully restarted by the
    respawn coordinator. Data fields: worker_name, restart_count (the
    handle's resettable gate counter — distinct from the lifetime
    Prometheus Counter baldur_daemon_worker_restarts_total)."""

    # Chaos Events
    CHAOS_EXPERIMENT_BLOCKED = "chaos_experiment_blocked"
    CHAOS_EXPERIMENT_STARTED = "chaos_experiment_started"
    CHAOS_EXPERIMENT_STOPPED = "chaos_experiment_stopped"

    # Security Violation Events
    SECURITY_VIOLATION_DETECTED = "security_violation_detected"
    """Security violation detected."""

    SECURITY_VIOLATION_CRITICAL = "security_violation_critical"
    """CRITICAL security violation — triggers Emergency Mode and Error Budget integration."""

    CORRUPTION_VIOLATION_CRITICAL = "corruption_violation_critical"
    """CRITICAL corruption violation — edge-triggered on escalation level transition."""

    # Adaptive Throttle Events
    THROTTLE_LIMIT_CHANGED = "throttle_limit_changed"
    """Throttle limit changed (includes previous_limit, new_limit, reason)."""

    THROTTLE_SLA_WARNING = "throttle_sla_warning"
    """SLA warning threshold reached (includes current_rtt_ms, threshold_ms, current_limit)."""

    THROTTLE_SLA_CRITICAL = "throttle_sla_critical"
    """SLA critical threshold reached (includes current_rtt_ms, threshold_ms, current_limit)."""

    THROTTLE_LIMIT_RECOVERED = "throttle_limit_recovered"
    """Throttle limit recovered to normal range (includes previous_limit, new_limit)."""

    # Retry Events
    RETRY_EXHAUSTED = "retry_exhausted"
    """All retry attempts exhausted (includes domain, max_attempts, final_error_type, attempts).
    Also emitted on CB fast-fail (attempts=1, final_error_type=CircuitBreakerOpenError)."""

    # Rate Limit Coordinator Events (unified 429 response handling)
    RATE_LIMIT_429 = "rate_limit_429"
    """External API 429 response received (includes key, consecutive_429s, cooldown_until)."""

    RATE_LIMIT_COOLDOWN_START = "rate_limit_cooldown_start"
    """Rate limit cooldown started (includes key, delay, cooldown_until)."""

    RATE_LIMIT_COOLDOWN_END = "rate_limit_cooldown_end"
    """Rate limit cooldown ended (includes key, cooldown_ended_at)."""

    # Throttle + DLQ integration events
    THROTTLE_REJECTION_STORED = "throttle_rejection_stored"
    """Throttle-rejected request stored in DLQ (includes entry_id, service_name, reason, domain, throttle_limit, current_count)."""

    THROTTLE_REJECTION_REPLAY_STARTED = "throttle_rejection_replay_started"
    """DLQ replay started during throttle recovery (includes domain, batch_size, service_name, pending_count)."""

    THROTTLE_REJECTION_REPLAY_COMPLETED = "throttle_rejection_replay_completed"
    """Throttle recovery DLQ replay completed (includes domain, processed, success, failed)."""

    THROTTLE_REJECTION_REPLAY_FAILED = "throttle_rejection_replay_failed"
    """Throttle recovery DLQ replay failed (includes domain, error, error_type, service_name)."""

    # Multi-Region Events
    REGION_INSTANCE_STOPPING = "region_instance_stopping"
    """Region instance graceful shutdown started (includes region, reason, timestamp)."""

    REGION_HEARTBEAT_EXPIRED = "region_heartbeat_expired"
    """Region heartbeat TTL expired — abnormal termination detected (includes region)."""

    REGION_PRIMARY_CHANGED = "region_primary_changed"
    """Primary region changed (includes from_region, to_region)."""

    # Load Shedding Events
    LOAD_SHEDDING_LEVEL_CHANGED = "load_shedding_level_changed"
    """Load shedding level changed (includes new_level, previous_level, traffic_limit, affected_services)."""

    # Saga Orchestrator Events
    SAGA_STARTED = "saga_started"
    """Saga execution started."""

    SAGA_STEP_COMPLETED = "saga_step_completed"
    """Forward step executed successfully."""

    SAGA_STEP_FAILED = "saga_step_failed"
    """Forward step execution failed."""

    SAGA_COMPLETED = "saga_completed"
    """All forward steps succeeded, saga completed."""

    SAGA_COMPENSATING = "saga_compensating"
    """Reverse compensation step completed (emitted per individual step compensation success)."""

    SAGA_COMPENSATED = "saga_compensated"
    """All compensations completed."""

    SAGA_COMPENSATION_FAILED = "saga_compensation_failed"
    """Compensation failed. Stored in DLQ."""

    SAGA_SUSPENDED = "saga_suspended"
    """Suspended due to circuit breaker OPEN."""

    SAGA_RESUMED = "saga_resumed"
    """Suspended saga resumed."""

    SAGA_TIMED_OUT = "saga_timed_out"
    """Overall saga timeout exceeded."""

    # Runbook Executor Events
    RUNBOOK_TRIGGERED = "runbook_triggered"
    """Runbook trigger condition met, execution started."""

    RUNBOOK_STEP_COMPLETED = "runbook_step_completed"
    """Runbook individual step executed successfully."""

    RUNBOOK_STEP_FAILED = "runbook_step_failed"
    """Runbook individual step execution failed."""

    RUNBOOK_COMPLETED = "runbook_completed"
    """Runbook full execution completed successfully."""

    RUNBOOK_FAILED = "runbook_failed"
    """Runbook full execution failed (including compensation)."""

    RUNBOOK_APPROVAL_REQUIRED = "runbook_approval_required"
    """HIGH risk runbook, awaiting manual approval."""

    RUNBOOK_APPROVAL_GRANTED = "runbook_approval_granted"
    """Runbook manual approval granted."""

    RUNBOOK_APPROVAL_REJECTED = "runbook_approval_rejected"
    """Runbook manual approval rejected."""

    RUNBOOK_SKIPPED_COOLDOWN = "runbook_skipped_cooldown"
    """Runbook trigger condition met but skipped due to cooldown."""

    RUNBOOK_REGISTRY_UPDATED = "runbook_registry_updated"
    """Runbook registry changed (registration/deactivation) — for cross-node sync."""

    RUNBOOK_EXECUTION_COMPLETED = "runbook_execution_completed"
    """Runbook full execution completed successfully (emitted after Recorder records)."""

    RUNBOOK_EXECUTION_FAILED = "runbook_execution_failed"
    """Runbook full execution failed (emitted after Recorder records)."""

    # Canary Rollout Events
    CANARY_ROLLOUT_STARTED = "canary_rollout_started"
    """Canary rollout started — first stage applied (data: rollout_id, state,
    current_stage_index, config_type, previous_state)."""

    CANARY_ROLLOUT_PROMOTED = "canary_rollout_promoted"
    """Canary rollout advanced to the next mid-stage (data: rollout_id, state,
    current_stage_index, config_type, previous_state)."""

    CANARY_ROLLOUT_COMPLETED = "canary_rollout_completed"
    """Canary rollout reached the final stage and completed (data: rollout_id,
    state, current_stage_index, config_type, previous_state)."""

    CANARY_ROLLOUT_ROLLED_BACK = "canary_rollout_rolled_back"
    """Canary rollout rolled back to previous config (data: rollout_id, state,
    current_stage_index, config_type, previous_state)."""

    CANARY_ROLLOUT_PAUSED = "canary_rollout_paused"
    """Canary rollout paused (data: rollout_id, state, current_stage_index,
    config_type, previous_state)."""

    CANARY_ROLLOUT_RESUMED = "canary_rollout_resumed"
    """Canary rollout resumed from PAUSED (data: rollout_id, state,
    current_stage_index, config_type, previous_state)."""

    CANARY_ROLLOUT_CANCELLED = "canary_rollout_cancelled"
    """Canary rollout cancelled before start (data: rollout_id, state,
    current_stage_index, config_type, previous_state)."""

    # Capacity Reservation Events
    SCHEDULED_EVENT_STARTED = "scheduled_event_started"
    """Scheduled event started — pre-warming completed (includes event_id, start_time, end_time, expected_rps_multiplier, tags)."""

    SCHEDULED_EVENT_ENDED = "scheduled_event_ended"
    """Scheduled event ended — configuration rollback started (includes event_id)."""

    # Settings Recommendation Events
    RECOMMENDATION_ROLLBACK = "recommendation_rollback"
    """AutoRollbackGuard rolled back parameters from a recommendation plan."""

    # FinOps Events
    FINOPS_BUDGET_EXCEEDED = "finops_budget_exceeded"
    """Budget hard_limit exceeded (includes service_name, current_cost, budget_limit, severity).
    Note: AuditEventType (audit/event_buffer.py) has same string value for audit classification.
    These are independent enum systems — no runtime conflict. See D-12."""

    # Rollback Events
    ROLLBACK_REQUESTED = "rollback_requested"
    """Rollback request accepted, status: PENDING."""

    ROLLBACK_STARTED = "rollback_started"
    """Rollback execution started, status: IN_PROGRESS."""

    ROLLBACK_COMPLETED = "rollback_completed"
    """Rollback execution completed (includes request_id, partial: bool,
    failed_steps: list[str], total_steps: int).
    partial=True indicates some steps failed — manual intervention may be required.
    See D-5 for PARTIALLY_COMPLETED merge rationale."""

    ROLLBACK_FAILED = "rollback_failed"
    """Rollback execution failed (includes request_id, error_message, failed_steps: list[str])."""

    ROLLBACK_CANCELLED = "rollback_cancelled"
    """Rollback cancelled by operator."""

    # Learning Events
    LEARNING_PARAMETER_BLACKLISTED = "learning_parameter_blacklisted"
    """Parameter added to blacklist (includes pattern_key, blocked_values, reason)."""

    LEARNING_PATTERN_DETECTED = "learning_pattern_detected"
    """New pattern learned (includes rule_name, pattern_type)."""

    LEARNING_MANUAL_ONLY_ACTIVATED = "learning_manual_only_activated"
    """Module switched to manual-only mode — auto-adjustment disabled (includes module)."""

    LEARNING_MANUAL_ONLY_DEACTIVATED = "learning_manual_only_deactivated"
    """Module restored to autonomous mode — auto-adjustment re-enabled (includes module)."""

    # Cell Topology Events
    CELL_STATE_CHANGED = "cell_state_changed"
    """Cell state transition (includes cell_id, old_state, new_state, reason).
    Cross-pod propagation required — traffic routing depends on cell state (see doc 389)."""

    CELL_EVACUATION_STARTED = "cell_evacuation_started"
    """Cell ACTIVE → DRAINING, evacuation started (includes cell_id, reason).
    Cross-pod propagation required — upstream must stop routing to this cell (see doc 389)."""

    CELL_EVACUATION_CANCELLED = "cell_evacuation_cancelled"
    """Cell evacuation cancelled before reaching ISOLATED (includes cell_id, reason).
    Emitted when DRAINING is interrupted by manual restore or metadata mismatch."""

    CELL_EVACUATION_COMPLETED = "cell_evacuation_completed"
    """Cell DRAINING → ISOLATED, evacuation completed (includes cell_id).
    Cross-pod propagation required (see doc 389)."""

    CELL_RESTORED = "cell_restored"
    """Cell ISOLATED → ACTIVE, cell restored to service (includes cell_id, trigger: auto|manual).
    Cross-pod propagation required — downstream can resume routing (see doc 389)."""

    # Circuit Mesh Events
    CIRCUIT_MESH_OVERRIDE_APPLIED = "circuit_mesh_override_applied"
    """CB threshold override applied to upstream service."""

    CIRCUIT_MESH_OVERRIDE_EXPIRED = "circuit_mesh_override_expired"
    """CB threshold override passively expired (downstream no longer OPEN during TTL renewal)."""

    CIRCUIT_MESH_OVERRIDE_RELEASED = "circuit_mesh_override_released"
    """CB threshold override actively released (includes trigger: downstream_closed, fast_recovery_completed, manual, bulk_release)."""

    CIRCUIT_MESH_MAX_OVERRIDES_REACHED = "circuit_mesh_max_overrides_reached"
    """Concurrent override limit reached."""

    CIRCUIT_MESH_ESCALATION_TRIGGERED = "circuit_mesh_escalation_triggered"
    """Max renewals exceeded, escalation triggered (includes service_name, renewal_count).
    Note: currently notification-only — no automated follow-up action.
    Future: Runbook integration can subscribe to trigger automated remediation.
    Cross-pod propagation recommended — operator dashboard visibility (see doc 389)."""

    # --- MEDIUM priority (definitions only, publishers deferred) ---

    # Config History Events
    CONFIG_ROLLED_BACK = "config_rolled_back"
    """Config restored to previous version."""

    # Blast Radius Events
    BLAST_RADIUS_POLICY_CHANGED = "blast_radius_policy_changed"
    """Blast radius policy set for a service."""

    BLAST_RADIUS_SERVICE_ISOLATED = "blast_radius_service_isolated"
    """Service auto-isolated due to blast radius violation."""

    # Precomputed Cache Events
    CACHE_L1_L2_DRIFT_DETECTED = "cache_l1_l2_drift_detected"
    """L1↔L2 cache content drift detected."""

    PRECOMPUTED_CACHE_INVALIDATED = "precomputed_cache_invalidated"
    """Admin-initiated precomputed cache L1 invalidation (cross-pod propagation)."""

    # Daily Report Events
    DAILY_REPORT_SEND_FAILED = "daily_report_send_failed"
    """Daily report delivery failed."""

    # ML Strategy Events
    ML_STRATEGY_PROMOTED = "ml_strategy_promoted"
    """ML strategy promoted over statistical fallback (includes strategy_name, component)."""

    # Notification Events
    NOTIFICATION_DELIVERY_FAILED = "notification_delivery_failed"
    """Notification channel delivery failed."""


class EventPriority(IntEnum):
    """Event processing priority."""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4
