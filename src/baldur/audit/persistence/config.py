"""
Disk-Persistent Buffer settings.

Controls LMDB persistent-buffer behavior via environment-variable-based settings.

Environment variable prefix: BALDUR_DISK_BUFFER_

Key settings:
- storage_type: storage type (lmdb, mmap)
- data_dir: data directory path
- lmdb_map_size_mb: LMDB maximum size
- max_entries: maximum number of entries
- group_commit_enabled: enable Group Commit
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger()


def _is_windows() -> bool:
    """Return whether the current platform is Windows."""
    return sys.platform == "win32"


def _get_default_data_dir() -> str:
    """Return the platform-specific default data directory."""
    if _is_windows():
        # Windows: use the temp directory
        return os.path.join(os.environ.get("TEMP", "C:\\Temp"), "baldur", "buffer")
    return "/var/lib/baldur/buffer"


def _get_default_lmdb_map_size_mb() -> int:
    """Platform-specific default LMDB map_size (MB).

    Linux: 10GB (reserves only virtual address space; actual disk usage scales
        with data size)
    Windows: 256MB (in writemap mode the entire map_size is allocated as a file,
        so keep it small)
    """
    if _is_windows():
        return 256
    return 10240


def _get_default_lmdb_writemap() -> bool:
    """Platform-specific default for LMDB writemap.

    Linux (ext4/XFS): True (performance improvement)
    Windows: False (with writemap=True the entire map_size is pre-allocated as a file)
    """
    return not _is_windows()


class DiskBufferSettings(BaseSettings):
    """
    Disk-Persistent Buffer settings.

    Environment variables:
    - All settings can be overridden via the BALDUR_DISK_BUFFER_* prefix

    Key feature flags:
    - group_commit_enabled: Group Commit for I/O optimization
    - fail_open_on_disk_full: keep serving when the disk is full
    - priority_based_purge: priority-based deletion when capacity is exceeded
    - quarantine_on_corruption: quarantine and create a new DB on DB corruption
    - enable_dead_letter_db: quarantine Poison Pill entries
    """

    # ─────────────────────────────────────────────────────
    # Storage settings
    # ─────────────────────────────────────────────────────

    storage_type: str = Field(
        default="lmdb",
        description="Storage type: lmdb, mmap",
    )

    data_dir: str = Field(
        default_factory=_get_default_data_dir,
        description="Data directory path",
    )

    # ─────────────────────────────────────────────────────
    # LMDB settings
    # ─────────────────────────────────────────────────────

    lmdb_map_size_mb: int = Field(
        default_factory=_get_default_lmdb_map_size_mb,
        description=(
            "LMDB maximum database size (MB). "
            "Linux: 10GB (reserves virtual address only). "
            "Windows: 256MB (set small because the file is actually allocated)."
        ),
    )

    lmdb_max_dbs: int = Field(
        default=10,
        description="LMDB maximum number of databases",
    )

    lmdb_writemap: bool = Field(
        default_factory=_get_default_lmdb_writemap,
        description=(
            "LMDB writemap mode. "
            "Linux (ext4/XFS): True (performance improvement). "
            "Windows: False (prevents the entire map_size being allocated as a file)."
        ),
    )

    lmdb_metasync: bool = Field(
        default=True,
        description="LMDB metadata sync",
    )

    # ─────────────────────────────────────────────────────
    # Multi-Instance settings
    # ─────────────────────────────────────────────────────

    include_hostname_in_db_name: bool = Field(
        default=True,
        description="Include hostname in the DB name (prevents multi-Pod collisions)",
    )

    include_pid_in_db_name: bool = Field(
        default=True,
        description=(
            "Include PID in the DB name (multi-process safe). "
            "Set to False if DBs accumulate on restart in a development environment."
        ),
    )

    instance_name: str = Field(
        default="",
        description="Instance name (for metric labels)",
    )

    # ─────────────────────────────────────────────────────
    # Buffer settings
    # ─────────────────────────────────────────────────────

    max_entries: int = Field(
        default=100000,
        description="Maximum number of entries",
    )

    flush_batch_size: int = Field(
        default=1000,
        description="Flush batch size",
    )

    # ─────────────────────────────────────────────────────
    # Cleanup settings
    # ─────────────────────────────────────────────────────

    retention_hours: int = Field(
        default=72,
        description="Data retention period (hours)",
    )

    cleanup_interval_seconds: float = Field(
        default=3600.0,
        description="Cleanup task interval (seconds)",
    )

    # ─────────────────────────────────────────────────────
    # Integrity settings
    # ─────────────────────────────────────────────────────

    enable_checksum: bool = Field(
        default=True,
        description="Enable CRC32 checksum",
    )

    sync_on_write: bool = Field(
        default=False,
        description="fsync on every write (performance impact)",
    )

    # ─────────────────────────────────────────────────────
    # Group Commit settings
    # ─────────────────────────────────────────────────────

    group_commit_enabled: bool = Field(
        default=True,
        description="Enable Group Commit (I/O optimization)",
    )

    group_commit_interval_ms: int = Field(
        default=100,
        description="Group Commit interval (ms). fsync is performed at each interval.",
    )

    group_commit_max_entries: int = Field(
        default=100,
        description="Group Commit maximum buffered entries",
    )

    # ─────────────────────────────────────────────────────
    # Disk Full handling settings
    # ─────────────────────────────────────────────────────

    fail_open_on_disk_full: bool = Field(
        default=True,
        description="Switch to Fail-Open mode when the disk is full (keep serving)",
    )

    disk_recovery_threshold: float = Field(
        default=0.1,
        description="Disk recovery threshold (return to normal mode at 10% free)",
    )

    priority_based_purge: bool = Field(
        default=True,
        description="Enable priority-based deletion",
    )

    disk_full_threshold: float = Field(
        default=0.05,
        description="Disk full threshold (Fail-Open below 5%)",
    )

    # ─────────────────────────────────────────────────────
    # Quarantine settings (Corruption handling)
    # ─────────────────────────────────────────────────────

    quarantine_on_corruption: bool = Field(
        default=True,
        description="Quarantine and create a new DB on DB corruption",
    )

    quarantine_suffix: str = Field(
        default=".corrupt",
        description="Quarantined DB file suffix",
    )

    # ─────────────────────────────────────────────────────
    # Poison Pill settings (handling flush-failed entries)
    # ─────────────────────────────────────────────────────

    max_flush_retries: int = Field(
        default=3,
        description="Maximum flush retry count",
    )

    enable_dead_letter_db: bool = Field(
        default=True,
        description="Enable Dead Letter DB (Poison Pill quarantine)",
    )

    # ─────────────────────────────────────────────────────
    # Graceful Shutdown settings
    # ─────────────────────────────────────────────────────

    enable_shutdown_handlers: bool = Field(
        default=True,
        description="Auto-register Graceful Shutdown handlers",
    )

    model_config = SettingsConfigDict(
        env_prefix="BALDUR_DISK_BUFFER_",
        env_file=".env",
        extra="ignore",
    )

    @property
    def data_path(self) -> Path:
        """Return the data path."""
        return Path(self.data_dir)

    @property
    def lmdb_map_size_bytes(self) -> int:
        """LMDB map size (bytes)."""
        return self.lmdb_map_size_mb * 1024 * 1024

    def validate_settings(self) -> list[str]:
        """
        Validate settings and return warning messages.

        Returns:
            List of warning messages
        """
        warnings: list[str] = []

        # Validate minimum map_size (100MB)
        if self.lmdb_map_size_mb < 100:
            warnings.append(
                f"lmdb_map_size_mb too small: {self.lmdb_map_size_mb}MB. "
                "Minimum recommended is 100MB."
            )

        # Validate max_entries
        if self.max_entries < 100:
            warnings.append(f"max_entries too small: {self.max_entries}")

        # Validate Group Commit
        if self.group_commit_enabled:
            if self.group_commit_max_entries < 1:
                warnings.append("group_commit_max_entries must be >= 1")
            if self.group_commit_interval_ms < 10:
                warnings.append("group_commit_interval_ms must be >= 10ms")

        # Warn on sync_on_write and group_commit conflict
        if self.sync_on_write and self.group_commit_enabled:
            warnings.append(
                "sync_on_write=True with group_commit_enabled=True "
                "reduces group commit benefits. Consider sync_on_write=False."
            )

        # Validate Poison Pill settings
        if self.max_flush_retries < 1:
            warnings.append("max_flush_retries must be >= 1")

        return warnings

    def get_lmdb_open_kwargs(self) -> dict[str, Any]:
        """
        Return LMDB environment open() arguments.

        Returns:
            lmdb.open() kwargs
        """
        return {
            "map_size": self.lmdb_map_size_bytes,
            "max_dbs": self.lmdb_max_dbs,
            "sync": self.sync_on_write,
            "writemap": self.lmdb_writemap,
            "metasync": self.lmdb_metasync,
        }


@lru_cache(maxsize=1)
def get_disk_buffer_settings() -> DiskBufferSettings:
    """Return the settings singleton."""
    settings = DiskBufferSettings()

    # Log validation warnings
    warnings = settings.validate_settings()
    if warnings:
        for warning in warnings:
            logger.warning(
                "disk_buffer_settings.event",
                warning=warning,
            )

    return settings


def reset_disk_buffer_settings() -> None:
    """Reset the settings cache (for tests)."""
    get_disk_buffer_settings.cache_clear()
