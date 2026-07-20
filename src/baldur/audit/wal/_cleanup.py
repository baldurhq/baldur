"""
JSONL WAL cleanup utilities.

Unifies the cleanup logic of the four WAL implementations.
Every cleanup utility applies the atomic-replace pattern
(.tmp + os.replace + directory fsync).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.utils.serialization import fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def atomic_rewrite(target: Path, lines: list[str]) -> None:
    """Write to a temp file, then replace atomically (prevents data loss)."""
    tmp = target.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    try:
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def cleanup_by_sequence(file_path: Path, keep_after_seq: int) -> int:
    """Sequence-based compaction.

    Delegation target of ``HashChainWAL.compact()``.
    """
    if not file_path.exists():
        return 0

    kept_lines: list[str] = []
    removed_count = 0

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            try:
                data = fast_loads(line.strip())
                seq = data.get("seq", 0)
                if seq > keep_after_seq:
                    kept_lines.append(line)
                else:
                    removed_count += 1
            except ValueError:
                kept_lines.append(line)

    if removed_count > 0:
        atomic_rewrite(file_path, kept_lines)

    return removed_count


def cleanup_by_age(directory: Path, pattern: str, max_age_days: int) -> int:
    """Date-based file deletion.

    Delegation target of ``HashChainWALRecovery.cleanup_old_wal_files()``.
    """
    cutoff = utc_now() - timedelta(days=max_age_days)
    removed = 0

    for wal_file in directory.glob(pattern):
        try:
            date_str = wal_file.stem.split("_")[-1]
            file_date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=UTC)
            if file_date < cutoff:
                if safe_unlink(wal_file):
                    removed += 1
                logger.debug(
                    "wal_cleanup.removed_old_file",
                    wal_file=wal_file.name,
                )
        except Exception:
            continue

    return removed


def cleanup_by_namespace(file_path: Path, namespace: str) -> int:
    """Namespace-based filtered rewrite.

    Delegation target of ``WALRecoveryMixin._remove_namespace_from_wal()``.
    """
    if not file_path.exists():
        return 0

    remaining: list[str] = []
    removed_count = 0

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = fast_loads(line.strip())
                if entry.get("namespace") == namespace:
                    removed_count += 1
                else:
                    remaining.append(line)
            except ValueError:
                remaining.append(line)

    if removed_count > 0:
        if remaining:
            atomic_rewrite(file_path, remaining)
        else:
            file_path.unlink(missing_ok=True)

    return removed_count


__all__ = [
    "atomic_rewrite",
    "cleanup_by_age",
    "cleanup_by_namespace",
    "cleanup_by_sequence",
]
