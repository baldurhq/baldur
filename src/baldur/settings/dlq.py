"""
DLQ (Dead Letter Queue) Settings - Pydantic v2.

Single Source of Truth for DLQ configuration.

Replaces:
- core/config.py:DLQConfig
- core/safe_defaults.py:SAFE_DEFAULTS["dlq"]
- core/safe_defaults.py:VALIDATION_RULES["dlq"]

Environment Variables:
    BALDUR_DLQ_ENABLED=true
    BALDUR_DLQ_RETRY_DELAY=60
    ... etc
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    IntervalDuration,
    LargeCount,
    TinyCount,
)
from baldur.settings.validators import warn_below


class DLQSettings(BaseSettings):
    """
    Dead Letter Queue configuration with validation.

    All defaults match core/config.py:DLQConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["dlq"]
    """

    model_config = make_settings_config("BALDUR_DLQ_")

    # ==========================================================================
    # Core Settings (from core/config.py:DLQConfig)
    # Validation rules from core/safe_defaults.py:VALIDATION_RULES["dlq"]
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable Dead Letter Queue",
    )
    retry_delay: IntervalDuration = Field(
        default=60,
        description="Delay between retries in seconds",
    )
    expiry_hours: int = Field(
        default=72,
        ge=1,
        le=720,
        description="Hours until DLQ entry expires",
    )
    retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Days to retain DLQ entries",
    )
    batch_size: LargeCount = Field(
        default=10,
        description="Number of entries to process in a batch",
    )
    max_replay_attempts: TinyCount = Field(
        default=2,
        description="Maximum replay attempts for DLQ entries",
    )

    # ==========================================================================
    # Size Limit Settings (329_DLQ_SIZE_LIMIT)
    # ==========================================================================
    max_size: int = Field(
        default=100_000,
        ge=1_000,
        le=10_000_000,
        description="DLQ max total item count. Overflow strategy applied when exceeded.",
    )
    max_size_per_domain: int = Field(
        default=20_000,
        ge=100,
        le=1_000_000,
        description="Per-domain max item count. Prevents noisy neighbor.",
    )
    overflow_strategy: str = Field(
        default="drop_oldest",
        pattern=r"^(drop_oldest|reject|compress_oldest)$",
        description=(
            "Strategy when limit exceeded: "
            "drop_oldest=evict oldest (synchronously at store time on OSS; "
            "PRO defers eviction to a background worker), "
            "reject=reject new items (503), "
            "compress_oldest=summarize then evict (PRO; "
            "OSS degrades to drop_oldest)"
        ),
    )
    emergency_purge_threshold: float = Field(
        default=0.8,
        ge=0.5,
        le=1.0,
        description="Trigger aggressive cleanup when current_size/max_size exceeds this ratio.",
    )
    overflow_evict_batch_size: int = Field(
        default=500,
        ge=100,
        le=50_000,
        description="Number of items to evict per background eviction chunk.",
    )
    overflow_check_interval: int = Field(
        default=10,
        ge=1,
        le=1000,
        description=(
            "Periodic-N skip interval for the synchronous overflow check on "
            "store_failure (#485 D4/G6). Below the emergency_purge_threshold "
            "ratio every Nth store performs the full ZCARD check; above the "
            "threshold every store checks. Drift bound = N * concurrent_writers "
            "in the steady-state regime only."
        ),
    )

    # ==========================================================================
    # Compression Settings (351_DLQ_COMPRESSION)
    # ==========================================================================
    compress_stale_after_days: int = Field(
        default=30,
        ge=7,
        le=365,
        description="Days until compressed entry transitions to STALE status.",
    )
    compress_archive_after_days: int = Field(
        default=90,
        ge=30,
        le=730,
        description="Days until STALE compressed entry transitions to ARCHIVED.",
    )
    compress_summary_scan_cap: int = Field(
        default=5000,
        ge=100,
        le=100_000,
        description=(
            "Upper bound on the compressed-summary walk. When the compressed "
            "index holds more than this many entries, the summary covers the "
            "newest ``compress_summary_scan_cap`` of them, sets "
            "``summary_truncated`` on the response, and logs a WARNING. Below "
            "the cap the summary is exact. Trades summary exactness against a "
            "bounded interactive read cost on very large histories."
        ),
    )

    # ==========================================================================
    # Stale Replaying Recovery (443_LIFECYCLE_CLEANUP_GAPS D4)
    # ==========================================================================
    stale_replaying_timeout_minutes: int = Field(
        default=30,
        ge=5,
        le=1440,
        description="Minutes before REPLAYING entries are considered stale and released back to PENDING",
    )

    # ==========================================================================
    # Circuit Close Inflight Lock (497 D4)
    # ==========================================================================
    circuit_close_inflight_ttl_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description=(
            "TTL (seconds) for the per-service in-flight lock guarding "
            "ReplayService.replay_on_circuit_close. Stale entries beyond this "
            "TTL are reclaimed via cache.delete + setnx retry."
        ),
    )

    # ==========================================================================
    # Entry Size & Forensic Truncation (502 DLQ_ENTRY_SIZE)
    # ==========================================================================
    entry_payload_compression_enabled: bool = Field(
        default=True,
        description=(
            "Whole-entry zlib compression on the DLQ write path. "
            "Reduces per-entry storage cost ~50-70% by compressing the "
            "orjson-encoded payload. Magic-byte (0x78) auto-detection on "
            "the read path keeps decode lossless across the toggle."
        ),
    )
    request_data_max_bytes: int = Field(
        default=4096,
        ge=256,
        le=1_048_576,
        description=(
            "Maximum bytes for the request_data field after JSON encoding. "
            "Oversize values are replaced with a {_truncated, original_size, "
            "preview} marker; the original payload is dropped to bound "
            "per-entry storage cost."
        ),
    )
    field_max_bytes: int = Field(
        default=4096,
        ge=256,
        le=1_048_576,
        description=(
            "Maximum bytes for snapshot_data / response_data / metadata "
            "fields after JSON encoding. Same truncation marker shape as "
            "request_data_max_bytes. entity_refs is excluded — typically "
            "a small {entity_id} dict."
        ),
    )
    truncate_blocks_replay: bool = Field(
        default=True,
        description=(
            "When True, entries with a truncated request_data field are "
            "blocked from auto-replay (ReplayService and AdaptiveThrottle). "
            "Truncated request_data cannot reconstruct the original call, "
            "so silent replay would corrupt downstream state."
        ),
    )

    # ==========================================================================
    # Composite Index (544 D3 — domain registry cardinality alert)
    # ==========================================================================
    domain_cardinality_alert_threshold: int = Field(
        default=1024,
        ge=10,
        le=100_000,
        description=(
            "Log WARNING when ZCARD dlq:domains exceeds this value. "
            "Enforced at the input layer by 545 "
            "(`utils/domain_validation.validate_and_normalize_domain`); this "
            "alert remains as defense-in-depth for any future bypass."
        ),
    )

    @field_validator("retention_days")
    @classmethod
    def _warn_retention_days(cls, v: int) -> int:
        """Warn if retention is very short (< 7 days)."""
        return warn_below(7, "safe_default.short_consider_using_data")(v)


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_dlq_settings() -> "DLQSettings":
    from baldur.settings.root import get_config

    return get_config().services_group.dlq


def reset_dlq_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["dlq"]
    except KeyError:
        pass
