"""
Cold Storage for Anchor Archival.

Contains:
- AnchorColdStorage: Archives expired anchors before deletion
- Supports local filesystem and pluggable backends

Purpose:
    90-day retention in Redis is sufficient for operational audits.
    However, legal/compliance audits may require 5-7 years of history.

    This module archives anchors to cold storage before Redis TTL expiration,
    ensuring long-term immutable records are preserved.

Industry Standards:
    - SOC2: 7 years data retention
    - HIPAA: 6 years minimum
    - PCI-DSS: 1 year + 3 months readily accessible, then 7 years archived
    - GDPR: Depends on purpose, but audit trails often 5+ years

Reference:
    92_CONFIG_IMPLEMENTATION_GUIDE.md Week 4 [25] AuditIntegritySettings 참조.
"""

from __future__ import annotations

import gzip
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import structlog

from baldur.settings.audit_integrity import get_audit_integrity_settings
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_archive_threshold_days() -> int:
    """Get archive threshold from settings."""
    return get_audit_integrity_settings().archive_threshold_days


def _get_cold_retention_years() -> int:
    """Get cold retention years from settings."""
    return get_audit_integrity_settings().cold_retention_years


class ColdStorageBackend(Protocol):
    """Protocol for cold storage backends."""

    def write(self, key: str, data: bytes) -> bool:
        """Write data to cold storage."""
        ...

    def read(self, key: str) -> bytes | None:
        """Read data from cold storage."""
        ...

    def exists(self, key: str) -> bool:
        """Check if key exists in cold storage."""
        ...

    def list_keys(self, prefix: str = "") -> list[str]:
        """List keys with optional prefix filter."""
        ...


class LocalFileColdStorage:
    """
    Local filesystem cold storage backend.

    Stores compressed anchor data in JSONL format.

    Directory Structure:
        {base_dir}/
            anchors/
                2026/
                    01/
                        anchors_2026-01.jsonl.gz
                        anchors_2026-01.jsonl.gz.sha256
    """

    def __init__(
        self,
        base_dir: Path,
        create_dirs: bool = True,
    ):
        """
        Initialize LocalFileColdStorage.

        Args:
            base_dir: Base directory for cold storage
            create_dirs: Create directories if they don't exist
        """
        self._base_dir = Path(base_dir) / "cold_storage" / "anchors"
        if create_dirs and not self._base_dir.exists():
            self._base_dir.mkdir(parents=True, exist_ok=True)

    def _get_archive_path(self, year: int, month: int) -> Path:
        """Get path for monthly archive file."""
        year_dir = self._base_dir / str(year)
        month_dir = year_dir / f"{month:02d}"
        month_dir.mkdir(parents=True, exist_ok=True)
        return month_dir / f"anchors_{year}-{month:02d}.jsonl.gz"

    def write(self, key: str, data: bytes) -> bool:
        """
        Write data to cold storage.

        Args:
            key: Storage key (format: "anchors/{year}/{month}/{date}")
            data: Compressed data to store

        Returns:
            True if write succeeded
        """
        try:
            parts = key.split("/")
            year = int(parts[1])
            month = int(parts[2])

            archive_path = self._get_archive_path(year, month)

            # Append to existing archive (decompressing and recompressing)
            # This is inefficient but ensures atomicity
            existing_data = b""
            if archive_path.exists():
                with gzip.open(archive_path, "rb") as f:
                    existing_data = f.read()

            # Append new data
            combined = existing_data + data + b"\n"

            with gzip.open(archive_path, "wb") as f:
                f.write(combined)

            # Write checksum
            import hashlib

            checksum = hashlib.sha256(combined).hexdigest()
            checksum_path = Path(str(archive_path) + ".sha256")
            with open(checksum_path, "w") as f:
                f.write(checksum)

            logger.info(
                "cold_storage.archived",
                archive_path=archive_path,
            )
            return True

        except Exception as e:
            logger.exception(
                "cold_storage.write_failed",
                storage_key=key,
                error=e,
            )
            return False

    def read(self, key: str) -> bytes | None:
        """Read data from cold storage."""
        try:
            parts = key.split("/")
            year = int(parts[1])
            month = int(parts[2])

            archive_path = self._get_archive_path(year, month)

            if not archive_path.exists():
                return None

            with gzip.open(archive_path, "rb") as f:
                return f.read()

        except Exception as e:
            logger.exception(
                "cold_storage.read_failed",
                storage_key=key,
                error=e,
            )
            return None

    def exists(self, key: str) -> bool:
        """Check if key exists in cold storage."""
        try:
            parts = key.split("/")
            year = int(parts[1])
            month = int(parts[2])
            archive_path = self._get_archive_path(year, month)
            return archive_path.exists()
        except Exception:
            return False

    def list_keys(self, prefix: str = "") -> list[str]:
        """List archive files."""
        keys = []
        for year_dir in self._base_dir.glob("*"):
            if year_dir.is_dir():
                for month_dir in year_dir.glob("*"):
                    if month_dir.is_dir():
                        for archive in month_dir.glob("*.jsonl.gz"):
                            keys.append(str(archive.relative_to(self._base_dir)))
        return keys

    def verify_integrity(self, year: int, month: int) -> bool:
        """
        Verify archive integrity using stored checksum.

        Returns:
            True if checksum matches
        """
        import hashlib

        archive_path = self._get_archive_path(year, month)
        checksum_path = Path(str(archive_path) + ".sha256")

        if not archive_path.exists() or not checksum_path.exists():
            return False

        with gzip.open(archive_path, "rb") as f:
            data = f.read()

        with open(checksum_path) as f:
            stored_checksum = f.read().strip()

        computed_checksum = hashlib.sha256(data).hexdigest()
        return computed_checksum == stored_checksum


@dataclass
class ArchiveResult:
    """Result of anchor archival operation."""

    archived_count: int = 0
    failed_count: int = 0
    archived_dates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    archived_at: str = field(default_factory=lambda: utc_now().isoformat())


class AnchorColdStorage:
    """
    Manages archival of expired anchors to cold storage.

    Problem:
        Redis anchors expire after 90 days (configurable).
        For legal/compliance audits, we may need 5-7 years of history.

    Solution:
        Before Redis TTL expires, archive anchors to cold storage:
        1. Daily Celery task checks for soon-to-expire anchors
        2. Archive to compressed JSONL files with checksums
        3. After successful archive, allow Redis TTL expiration

    Archive Strategy:
        - Archive anchors when TTL < 7 days remaining
        - Store in monthly archives (reduced file count)
        - Include cryptographic checksums for integrity
        - Support retrieval for historical audits
    """

    # Legacy constants for backward compatibility
    ARCHIVE_THRESHOLD_DAYS = 7  # Archive when TTL < 7 days
    DEFAULT_COLD_RETENTION_YEARS = 7  # Keep cold archives for 7 years

    def __init__(
        self,
        redis_client: Any,
        cold_backend: ColdStorageBackend | None = None,
        base_dir: Path | None = None,
        key_prefix: str = "baldur:",
        archive_threshold_days: int | None = None,
        cold_retention_years: int | None = None,
    ):
        """
        Initialize AnchorColdStorage.

        Args:
            redis_client: Redis client instance
            cold_backend: Cold storage backend (default: LocalFileColdStorage)
            base_dir: Base directory for local cold storage
            key_prefix: Redis key prefix
            archive_threshold_days: Archive when TTL < this many days (default from AuditIntegritySettings)
            cold_retention_years: Years to retain cold archives (default from AuditIntegritySettings)
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._archive_threshold = (
            archive_threshold_days
            if archive_threshold_days is not None
            else _get_archive_threshold_days()
        )
        self._cold_retention_years = (
            cold_retention_years
            if cold_retention_years is not None
            else _get_cold_retention_years()
        )

        # Default to local filesystem backend
        self._cold_backend: ColdStorageBackend
        if cold_backend is None:
            base = base_dir or Path(os.getenv("BALDUR_DATA_DIR", "/var/lib/baldur"))
            self._cold_backend = LocalFileColdStorage(base)
        else:
            self._cold_backend = cold_backend

    def _get_anchor_pattern(self) -> str:
        """Get Redis key pattern for anchors."""
        return f"{self._key_prefix}audit:hash_chain:anchor:*"

    def _parse_anchor_date(self, key: str) -> str | None:
        """Extract date from anchor key."""
        try:
            # Key format: baldur:audit:hash_chain:anchor:2026-01-15
            return key.split(":")[-1]
        except Exception:
            return None

    def find_expiring_anchors(self) -> list[dict[str, Any]]:
        """
        Find anchors that are close to expiration.

        Returns:
            List of anchor data dictionaries with TTL info
        """
        expiring = []
        threshold_seconds = self._archive_threshold * 86400

        try:
            pattern = self._get_anchor_pattern()
            for key in self._redis.scan_iter(match=pattern):
                if isinstance(key, bytes):
                    key = key.decode("utf-8")

                ttl = self._redis.ttl(key)

                # TTL <= threshold, needs archival
                if 0 < ttl <= threshold_seconds:
                    anchor_date = self._parse_anchor_date(key)
                    if anchor_date:
                        data = self._redis.hgetall(key)
                        # Decode bytes
                        decoded = {}
                        for k, v in data.items():
                            k_str = k.decode("utf-8") if isinstance(k, bytes) else k
                            v_str = v.decode("utf-8") if isinstance(v, bytes) else v
                            decoded[k_str] = v_str

                        decoded["_redis_key"] = key
                        decoded["_ttl_seconds"] = ttl
                        decoded["_ttl_days"] = ttl / 86400
                        expiring.append(decoded)

            logger.info(
                "cold_storage.found_expiring_anchors",
                expiring_count=len(expiring),
            )
            return expiring

        except Exception as e:
            logger.exception(
                "cold_storage.error_finding_expiring_anchors",
                error=e,
            )
            return []

    def archive_anchor(self, anchor_data: dict[str, Any]) -> bool:
        """
        Archive a single anchor to cold storage.

        Args:
            anchor_data: Anchor data dictionary

        Returns:
            True if archive succeeded
        """
        try:
            date_str = anchor_data.get("date", "")
            if not date_str:
                return False

            # Parse date for storage path
            date = datetime.strptime(date_str, "%Y-%m-%d")
            year = date.year
            month = date.month

            # Prepare archive record
            archive_record = {
                "date": date_str,
                "sequence": anchor_data.get("sequence"),
                "hash": anchor_data.get("hash"),
                "created_at": anchor_data.get("created_at"),
                "archived_at": utc_now().isoformat(),
            }

            # Serialize as JSONL line
            json_line = fast_dumps_str(archive_record)
            data = json_line.encode("utf-8")

            # Write to cold storage
            key = f"anchors/{year}/{month}/{date_str}"
            success = self._cold_backend.write(key, data)

            if success:
                logger.info(
                    "cold_storage.archived_anchor",
                    date_str=date_str,
                )

            return success

        except Exception as e:
            logger.exception(
                "cold_storage.archive_failed",
                error=e,
            )
            return False

    def archive_expiring_anchors(self) -> ArchiveResult:
        """
        Archive all expiring anchors.

        Returns:
            ArchiveResult with statistics
        """
        result = ArchiveResult()

        expiring = self.find_expiring_anchors()

        for anchor in expiring:
            date_str = anchor.get("date", "unknown")
            try:
                if self.archive_anchor(anchor):
                    result.archived_count += 1
                    result.archived_dates.append(date_str)
                else:
                    result.failed_count += 1
                    result.errors.append(f"Archive failed for {date_str}")
            except Exception as e:
                result.failed_count += 1
                result.errors.append(f"Error archiving {date_str}: {e}")

        # Log to self-audit if available
        self._log_archive_event(result)

        return result

    def _log_archive_event(self, result: ArchiveResult) -> None:
        """Log archive event to self-audit trail."""
        try:
            from baldur.audit.self_audit import SelfAuditEvent, self_audit

            # Use RECOVERY_COMPLETED as closest match
            self_audit().log(
                SelfAuditEvent.RECOVERY_COMPLETED,
                f"Cold storage archive: {result.archived_count} anchors",
                {
                    "action": "cold_storage_archive",
                    "archived_count": result.archived_count,
                    "failed_count": result.failed_count,
                    "archived_dates": result.archived_dates,
                    "errors": result.errors,
                },
            )
        except (ImportError, AttributeError):
            logger.info(
                "cold_storage.archive_result",
                archive_result=result,
            )

    def retrieve_archived_anchor(
        self,
        date_str: str,
    ) -> dict[str, Any] | None:
        """
        Retrieve an archived anchor from cold storage.

        Args:
            date_str: Date string (YYYY-MM-DD)

        Returns:
            Anchor data dictionary, or None if not found
        """
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
            year = date.year
            month = date.month

            key = f"anchors/{year}/{month}/{date_str}"
            data = self._cold_backend.read(key)

            if not data:
                return None

            # Parse JSONL and find matching date
            lines = data.decode("utf-8").strip().split("\n")
            for line in lines:
                if line.strip():
                    record = fast_loads(line)
                    if record.get("date") == date_str:
                        return record

            return None

        except Exception as e:
            logger.exception(
                "cold_storage.retrieval_failed",
                date_str=date_str,
                error=e,
            )
            return None

    def get_archived_months(self) -> list[str]:
        """
        List all archived months.

        Returns:
            List of month strings (YYYY-MM format)
        """
        keys = self._cold_backend.list_keys()
        months = set()

        for key in keys:
            # Extract YYYY-MM from path
            parts = key.split("/")
            if len(parts) >= 2:
                try:
                    year = parts[0]
                    month = parts[1]
                    months.add(f"{year}-{month}")
                except Exception:
                    continue

        return sorted(months)


__all__ = [
    "AnchorColdStorage",
    "LocalFileColdStorage",
    "ColdStorageBackend",
    "ArchiveResult",
]
