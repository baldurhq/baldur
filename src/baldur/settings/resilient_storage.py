"""
Resilient Storage Settings - Pydantic v2.

Single Source of Truth for ``ResilientStorageBackend`` configuration.

Replaces:
- adapters/resilient/backend.py:ResilientStorageConfig (dataclass)

Environment Variables:
    BALDUR_RESILIENT_STORAGE_REDIS_URL=redis://localhost:6379/0
    BALDUR_RESILIENT_STORAGE_WAL_DIR=/var/log/baldur/wal
    BALDUR_RESILIENT_STORAGE_RECOVERY_JITTER_MAX=5.0
    BALDUR_RESILIENT_STORAGE_RECOVERY_PROBE_INTERVAL=5.0
    BALDUR_RESILIENT_STORAGE_AUTO_RECOVERY=true
    BALDUR_RESILIENT_STORAGE_KEY_PREFIX=baldur:
    BALDUR_RESILIENT_STORAGE_ALLOW_MEMORY_ONLY=false
    BALDUR_RESILIENT_STORAGE_USE_DYNAMIC_PREFIX=true
    BALDUR_RESILIENT_STORAGE_DEGRADED_BLOB_MEMORY_MAX_BYTES=134217728

Reference:
- 470 (D8)
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ResilientStorageSettings(BaseSettings):
    """
    Resilient Storage Backend configuration with validation.

    Redis-First + Graceful Degradation + WAL backend settings.

    Replaces the legacy ``ResilientStorageConfig`` dataclass with a
    Pydantic settings class so that ``BALDUR_RESILIENT_STORAGE_*`` env
    vars flow through automatically and operators can tune the recovery
    loop without code changes.
    """

    model_config = make_settings_config("BALDUR_RESILIENT_STORAGE_")

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        min_length=1,
        description=(
            "Redis connection URL. When unset, falls back to "
            "BALDUR_REDIS_URL (RedisSettings.url); a per-class override "
            "(BALDUR_RESILIENT_STORAGE_REDIS_URL or explicit kwarg) wins."
        ),
    )
    wal_dir: str = Field(
        default="/var/log/baldur/wal",
        min_length=1,
        description="WAL directory for degraded-mode write-ahead logging.",
    )
    recovery_jitter_max: float = Field(
        default=5.0,
        ge=0.0,
        le=30.0,
        description=(
            "Max jitter (seconds) before each recovery payload runs. "
            "Disperses thundering herd when N workers' cooldowns expire "
            "near-simultaneously after a shared Redis recovery edge."
        ),
    )
    recovery_probe_interval: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description=(
            "Cooldown (seconds) between consecutive degraded-mode "
            "recovery dispatches. Distinct from the 30s first-init "
            "cooldown — degraded recovery uses this shorter interval "
            "to cap the diverged-write window."
        ),
    )
    auto_recovery: bool = Field(
        default=True,
        description=(
            "Enable lazy auto-recovery from degraded mode. When False, "
            "the backend stays stuck in DEGRADED forever until "
            "``check_and_recover()`` is invoked manually — emergency "
            "kill switch for operators that need legacy behavior."
        ),
    )
    key_prefix: str = Field(
        default="baldur:",
        min_length=1,
        description="Static key prefix when ``use_dynamic_prefix=False``.",
    )
    allow_memory_only: bool = Field(
        default=False,
        description=(
            "Allow the backend to operate without ever connecting to "
            "Redis. Used by tests and dev environments — production "
            "should always have Redis available."
        ),
    )
    use_dynamic_prefix: bool = Field(
        default=True,
        description=(
            "Apply the dynamic ``xtest:`` prefix when "
            "``TestModeContext`` is active. Keeps test data segregated "
            "from production data in shared Redis instances."
        ),
    )
    degraded_blob_memory_max_bytes: int = Field(
        default=134217728,  # 128 MiB
        ge=1048576,  # 1 MiB floor
        le=2147483648,  # 2 GiB ceiling
        description=(
            "Byte budget for the degraded-mode in-memory blob store "
            "(``set_blob`` payloads). Once the accumulated blob bytes "
            "exceed this cap, least-recently-written blobs are evicted "
            "(degraded-read-invisible until recovery, never lost — they "
            "stay durably in WAL and are reconstructed on recovery). The "
            "OOM threat is measured in bytes, not entry count, so the cap "
            "is bytes-based. Set it with headroom below the worker's hard "
            "memory limit: ``len(blob)`` is a payload proxy that excludes "
            "Python per-object / OrderedDict-node / key-string overhead. "
            "Fail-safe: bounded by construction, no unbounded option."
        ),
    )

    @model_validator(mode="after")
    def _fallback_redis_url(self) -> ResilientStorageSettings:
        """Resolve redis_url to BALDUR_REDIS_URL when not explicitly set.

        Keeps the localhost default and ``min_length=1``: the
        ``model_fields_set`` convention needs no empty-string sentinel.
        Compatible with bootstrap's ``ResilientStorageSettings(redis_url=...)``
        injection — an explicit kwarg sets ``model_fields_set`` so the
        helper no-ops and the injected value is honored.
        """
        from baldur.settings.redis import apply_redis_url_fallback

        apply_redis_url_fallback(self, "redis_url")
        return self

    @model_validator(mode="after")
    def _validate_jitter_within_probe_interval(
        self,
    ) -> ResilientStorageSettings:
        """Ensure ``recovery_jitter_max <= recovery_probe_interval``.

        Per-field ranges (probe: 1.0~60.0, jitter: 0.0~30.0) overlap.
        Without this constraint, configurations like ``probe=2s`` +
        ``jitter=5s`` allow jitter sleep to extend past the next probe
        window — a logical inconsistency. Not a lock-contention defense
        (the lock try-acquire already discards racing dispatchers); a
        configuration sanity check.
        """
        if self.recovery_jitter_max > self.recovery_probe_interval:
            raise ValueError(
                f"recovery_jitter_max ({self.recovery_jitter_max}) "
                f"must be <= recovery_probe_interval "
                f"({self.recovery_probe_interval}); jitter sleep would "
                f"otherwise extend past the next probe window."
            )
        return self


def get_resilient_storage_settings() -> ResilientStorageSettings:
    from baldur.settings.root import get_config

    return get_config().resilience.resilient_storage


def reset_resilient_storage_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().resilience.__dict__["resilient_storage"]
    except KeyError:
        pass
