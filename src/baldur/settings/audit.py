"""
Audit Settings - Pydantic v2.

감사 로그 및 설정 이력 관련 설정입니다.

Replaces:
- services/pending_config.py:MAX_HISTORY
- services/config_history.py:MAX_HISTORY_ENTRIES
- core/safe_defaults.py:audit_log_retention_days

Environment Variables:
    BALDUR_AUDIT_ENABLED=false           # Master switch (D18)
    BALDUR_AUDIT_PARTITION=              # Per-service partition (D23)
    BALDUR_AUDIT_USE_FILE_LOCK=true      # Cross-process file lock (D22)
    BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=false  # Redis hash chain (D22)
    BALDUR_AUDIT_MAX_HISTORY=100
    BALDUR_AUDIT_RETENTION_DAYS=90
    BALDUR_AUDIT_CONFIG_HISTORY_ENTRIES=50

Reference:
- 416 (D18, D22, D23)
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 4 [20])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §3.9
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import StrictProbability


class AuditSettings(BaseSettings):
    """
    감사 로그 및 이력 관리 설정.

    보관 정책:
    - max_history: Pending Config 변경 이력 (100개)
    - config_history_entries: 설정 버전 이력 (50개)
    - retention_days: 감사 로그 보관 기간 (90일)
    """

    model_config = make_settings_config("BALDUR_AUDIT_")

    # ==========================================================================
    # Master Switch — overrides all other audit toggles when False (D18)
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the entire audit subsystem. When False, all "
            "audit I/O is silenced (Pipeline A WAL, Pipeline B config-change "
            "events, env_snapshot fallback). Overrides fallback_enabled, "
            "metrics_enabled, load_shedding_enabled, and load_shedding/buffer "
            "thresholds — those settings only apply when enabled=True. PRO-tier "
            "consumers activate via the baldur.bootstrap_hooks entry "
            "point (D4)."
        ),
    )

    # ==========================================================================
    # Per-service partition (D23)
    # ==========================================================================
    partition: str = Field(
        default="",
        max_length=32,
        description=(
            "Audit partition identifier (D23). When non-empty, "
            "HashChainFileAuditLogAdapter writes to "
            "audit_{date}_{partition}.jsonl and uses "
            ".hash_chain_state.{partition}.json. Empty preserves the legacy "
            "audit_{date}.jsonl filename. Validation: alphanumeric + _ + - "
            "only, max 32 chars."
        ),
    )

    # ==========================================================================
    # Multi-writer safety (D22)
    # ==========================================================================
    use_file_lock: bool = Field(
        default=True,
        description=(
            "Enable cross-process file locking (D22) for "
            "HashChainFileAuditLogAdapter and HashChainManager state file. "
            "Reuses audit/checkpoint/file_lock.py. Set False only when (a) "
            "distributed_hash_chain=True with Redis, OR (b) deployment is "
            "verified single-writer."
        ),
    )
    distributed_hash_chain: bool = Field(
        default=False,
        description=(
            "Enable Redis-based distributed hash chain (D22). When True, "
            "HashChainFileAuditLogAdapter instantiates RedisHashChainManager "
            "instead of local HashChainManager. PRO multi-host deployments "
            "(K8s ≥2 pods) MUST set True — file locks do not span hosts."
        ),
    )

    @field_validator("partition")
    @classmethod
    def _validate_partition(cls, value: str) -> str:
        """Validate partition: alphanumeric + underscore + hyphen, max 32 chars.

        Empty string is allowed (legacy mode). Reason: filename safety + Redis
        key safety.
        """
        if value == "":
            return value
        if len(value) > 32:
            raise ValueError(
                f"partition must be at most 32 characters, got {len(value)}"
            )
        for ch in value:
            if not (ch.isalnum() or ch in ("_", "-")):
                raise ValueError(
                    f"partition must contain only alphanumeric, '_', or '-' "
                    f"characters; got {value!r}"
                )
        return value

    # ==========================================================================
    # Pending Config History - from pending_config.py
    # ==========================================================================
    max_history: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Maximum number of pending config change history entries",
    )

    # ==========================================================================
    # Config History - from config_history.py
    # ==========================================================================
    config_history_entries: int = Field(
        default=50,
        ge=10,
        le=500,
        description="Maximum number of config version history entries",
    )

    # ==========================================================================
    # Retention - from safe_defaults.py
    # ==========================================================================
    retention_days: int = Field(
        default=90,
        ge=30,
        le=365,
        description="Audit log retention period (days)",
    )

    # ==========================================================================
    # Event Bus History - from event_bus.py
    # ==========================================================================
    event_history_max: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum number of event bus history entries",
    )

    # ==========================================================================
    # Cascade Detector History - from cascade_detector.py
    # ==========================================================================
    cascade_history_max: int = Field(
        default=100,
        ge=50,
        le=500,
        description="Maximum number of cascade detection history entries",
    )

    # ==========================================================================
    # Self-Audit - from self_audit.py (Phase 3 리팩토링)
    # ==========================================================================
    self_audit_max_recent_events: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Maximum number of recent self-audit events to retain",
    )

    self_audit_default_limit: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Default limit for self-audit event queries",
    )

    self_audit_max_failure_rate: StrictProbability = Field(
        default=0.1,
        description="Maximum allowed failure rate for self-audit health check (0.1 = 10%)",
    )

    # ==========================================================================
    # Cascade Load Shedding - from cascade_load_shedding.py (Phase 3 리팩토링)
    # ==========================================================================
    cascade_rate_window_seconds: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="Cascade load shedding rate limit window size (seconds)",
    )

    # ==========================================================================
    # Compliance Retention - from audit/config.py
    # ==========================================================================
    compliance_max_retention_days: int = Field(
        default=365,
        ge=90,
        le=2555,
        description="Maximum retention period per legal requirements (days). Default 1 year, max 7 years.",
    )

    # ==========================================================================
    # Redis Buffer TTL - from adapters/audit/redis_buffer.py
    # ==========================================================================
    buffer_redis_ttl: int = Field(
        default=86400,
        ge=3600,
        le=604800,
        description="Redis audit buffer TTL (seconds). Default 24 hours.",
    )

    # ==========================================================================
    # Redis Buffer Drain Pipeline — flush / orphan recovery / safety ltrim
    # ==========================================================================
    buffer_redis_enabled: bool = Field(
        default=False,
        description=(
            "Enable the Redis audit-buffer drain pipeline (flush / orphan "
            "recovery / safety ltrim). Subordinate to the master `enabled` "
            "switch: the effective drain gate is `enabled AND "
            "buffer_redis_enabled`. When the effective gate is False, beat "
            "injection is suppressed and each drain task early-exits with "
            "zero Redis commands."
        ),
    )
    buffer_redis_batch_size: int = Field(
        default=500,
        ge=10,
        le=5000,
        description="Per-domain flush batch size for the Redis audit-buffer drain.",
    )
    buffer_redis_flush_interval: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description="Beat flush interval (seconds) for the Redis audit-buffer drain.",
    )

    # ==========================================================================
    # Data Access Audit - from compliance/iso27001.py (368: Django Decoupling)
    # ==========================================================================
    read_paths: list[str] = Field(
        default_factory=list,
        description="Audit middleware read paths for data access monitoring",
    )

    # ==========================================================================
    # Backpressure Settings - from audit/cascade_config.py (368: Django Decoupling)
    # ==========================================================================
    load_shedding_enabled: bool = Field(
        default=True,
        description="Enable audit load shedding under pressure",
    )
    buffer_warning_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Buffer utilization warning threshold (0.0-1.0)",
    )
    buffer_critical_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Buffer utilization critical threshold (0.0-1.0)",
    )
    max_events_per_second: int = Field(
        default=1000,
        ge=1,
        le=100000,
        description="Maximum audit events per second",
    )
    fallback_enabled: bool = Field(
        default=True,
        description="Enable fallback on audit backend failure",
    )
    metrics_enabled: bool = Field(
        default=True,
        description="Enable audit subsystem metrics",
    )

    @model_validator(mode="after")
    def _validate_buffer_thresholds(self) -> "AuditSettings":
        """Ensure buffer_warning_threshold < buffer_critical_threshold."""
        if self.buffer_warning_threshold >= self.buffer_critical_threshold:
            raise ValueError(
                f"buffer_warning_threshold ({self.buffer_warning_threshold}) "
                f"must be less than buffer_critical_threshold ({self.buffer_critical_threshold})"
            )
        return self


def get_audit_settings() -> "AuditSettings":
    from baldur.settings.root import get_config

    return get_config().audit_group.audit


def is_redis_drain_enabled() -> bool:
    """Effective drain gate for the Redis audit-buffer pipeline.

    Single source of truth consumed by the beat-injection gate
    (``include_audit_flush`` resolution), the drain-task early-exit, and
    the writer-footgun construction warning. The drain pipeline runs only
    when BOTH the master audit switch and the Redis-buffer toggle are on.
    """
    audit = get_audit_settings()
    return audit.enabled and audit.buffer_redis_enabled


def reset_audit_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["audit"]
    except KeyError:
        pass


# =============================================================================
# Mutators (D9) — distinct primitives for tests vs PRO bootstrap hook
# =============================================================================


def set_audit_settings(**kwargs: Any) -> None:
    """Permanently set audit settings in-memory (D9).

    Used by the PRO bootstrap hook (``baldur.bootstrap_hooks`` entry
    point) to flip the master switch and related defaults at startup.
    Bypasses Pydantic field validation by using ``object.__setattr__`` so
    repeat calls do not re-trigger ``model_validator`` cross-field checks.

    Tests should use ``override_audit_settings()`` instead — that helper
    is a context manager and restores prior values on exit.
    """
    audit = get_audit_settings()
    for key, value in kwargs.items():
        if not hasattr(audit, key):
            raise AttributeError(
                f"AuditSettings has no field {key!r}; "
                f"available fields: {list(AuditSettings.model_fields.keys())}"
            )
        object.__setattr__(audit, key, value)


@contextmanager
def override_audit_settings(**kwargs: Any) -> Iterator["AuditSettings"]:
    """Snapshot-and-restore context manager for tests (D9).

    Example:
        >>> with override_audit_settings(enabled=False):
        ...     # audit is silenced inside this block
        ...     do_something()
        # original enabled value restored here
    """
    audit = get_audit_settings()
    saved: dict[str, Any] = {}
    for key in kwargs:
        if not hasattr(audit, key):
            raise AttributeError(
                f"AuditSettings has no field {key!r}; "
                f"available fields: {list(AuditSettings.model_fields.keys())}"
            )
        saved[key] = getattr(audit, key)
    try:
        for key, value in kwargs.items():
            object.__setattr__(audit, key, value)
        yield audit
    finally:
        for key, value in saved.items():
            object.__setattr__(audit, key, value)
