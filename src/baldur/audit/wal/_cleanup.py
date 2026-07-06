"""
JSONL WAL 정리 유틸리티.

4중 WAL 구현의 정리 로직을 통합합니다.
모든 정리 유틸리티는 Atomic Replace 패턴(.tmp + os.replace + directory fsync)을 적용합니다.
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
    """임시 파일에 쓴 후 원자적으로 교체 (데이터 유실 방지)."""
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
    """시퀀스 기반 compact (HashChainWAL.compact() 위임 대상)."""
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
    """날짜 기반 파일 삭제 (HashChainWALRecovery.cleanup_old_wal_files() 위임 대상)."""
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
    """네임스페이스 기반 필터링 재작성 (WALRecoveryMixin._remove_namespace_from_wal() 위임 대상)."""
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
