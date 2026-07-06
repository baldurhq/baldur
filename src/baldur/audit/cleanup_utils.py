import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from baldur.core.file_utils import safe_unlink
from baldur.utils.time import utc_now

logger = logging.getLogger(__name__)


def iter_files_by_age(
    directory: Path, pattern: str, max_age_days: int
) -> Iterator[Path]:
    """Yield files exceeding retention period, oldest first."""
    cutoff = utc_now() - timedelta(days=max_age_days)
    files_with_mtime = []
    for f in directory.glob(pattern):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
            if mtime < cutoff:
                files_with_mtime.append((mtime, f))
        except OSError:
            continue
    files_with_mtime.sort(key=lambda x: x[0])
    for _, f in files_with_mtime:
        yield f


def delete_files_by_age(directory: Path, pattern: str, max_age_days: int) -> int:
    """Delete files exceeding retention period using safe_unlink. Returns deleted count."""
    deleted = 0
    for f in iter_files_by_age(directory, pattern, max_age_days):
        if safe_unlink(f):
            deleted += 1
    return deleted


def delete_files_by_priority(
    directory: Path,
    pattern: str,
    priority_fn: Callable[[Path], int],
    target_free_bytes: int = 0,
) -> int:
    """Priority-based file deletion (disk full response). Uses safe_unlink. Returns deleted count.

    Note: ``freed`` is approximate — file size is read before deletion
    and may differ if another worker modifies the file concurrently.
    """
    candidates = []
    for f in directory.glob(pattern):
        try:
            candidates.append((priority_fn(f), f.stat().st_size, f))
        except OSError:
            continue
    candidates.sort(key=lambda x: x[0])

    deleted = 0
    freed = 0
    for _, size, f in candidates:
        if target_free_bytes > 0 and freed >= target_free_bytes:
            break
        if safe_unlink(f):
            deleted += 1
            freed += size
    return deleted
