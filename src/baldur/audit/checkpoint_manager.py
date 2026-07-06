"""
CheckpointManager - WAL 처리 시퀀스 영속화.

마지막 처리된 WAL 시퀀스를 디스크에 저장하여 프로세스 재시작 시 복구 지원.
WriteAheadLog의 recover_unprocessed()와 함께 사용하여 데이터 유실 0% 달성.

주요 기능:
- 환경변수 기반 경로 설정 (BALDUR_AUDIT_PATH)
- 멀티 프로세스 파일 락 지원
- 쓰기 권한 검증 및 자동 폴백

Usage:
    from baldur.audit.checkpoint_manager import CheckpointManager

    checkpoint = CheckpointManager("/var/log/audit/checkpoint")

    # 처리 완료 후 체크포인트 저장
    checkpoint.save(last_seq=1234)

    # 재시작 시 체크포인트 로드
    last_seq = checkpoint.load()
    entries = wal.recover_unprocessed(last_seq)

Version: 1.1.0
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import structlog

from baldur.core.serializable import SerializableMixin

logger = structlog.get_logger()


# =============================================================================
# Cross-Platform File Locking
# =============================================================================


def lock_file(f: BinaryIO) -> None:
    """파일 락 획득 (크로스 플랫폼)."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(f: BinaryIO) -> None:
    """파일 락 해제 (크로스 플랫폼)."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@dataclass(kw_only=True)
class CheckpointData(SerializableMixin):
    """체크포인트 데이터."""

    last_sequence: int = 0
    timestamp: float = 0.0
    version: int = 1


# CheckpointError: 단일 소스는 checkpoint_strategy.py (Item 1 중복 제거)
from baldur.audit.checkpoint import CheckpointError  # noqa: F401


class CheckpointManager:
    """
    WAL 처리 시퀀스 관리자.

    .. deprecated::
        Use ``CheckpointStorageStrategy`` from ``checkpoint_strategy.py`` instead.
        This class will be removed in the next major version.

    마지막 처리된 WAL 시퀀스를 디스크에 영속화하여
    프로세스 재시작 시 정확한 복구 지점 제공.
    """

    DEFAULT_CHECKPOINT_DIR = "/var/log/audit"
    DEFAULT_CHECKPOINT_FILENAME = "checkpoint.json"

    @staticmethod
    def _get_default_path() -> Path:
        """환경변수 기반 기본 경로 결정."""
        env_path = os.environ.get("BALDUR_AUDIT_PATH")
        if env_path:
            return Path(env_path) / "checkpoint.json"

        # OS별 기본 경로
        if os.name == "nt":  # Windows
            return Path(tempfile.gettempdir()) / "baldur" / "checkpoint.json"
        # Unix/Linux
        return Path("/var/log/audit") / "checkpoint.json"

    def __init__(
        self,
        checkpoint_path: str | Path | None = None,
        sync_on_write: bool = True,
    ):
        warnings.warn(
            "CheckpointManager is deprecated, use CheckpointStorageStrategy from "
            "baldur.audit.checkpoint_strategy instead",
            DeprecationWarning,
            stacklevel=2,
        )
        if checkpoint_path is None:
            checkpoint_path = self._get_default_path()

        self._path = Path(checkpoint_path)
        self._sync_on_write = sync_on_write
        self._lock = threading.RLock()

        # 권한 체크 및 폴백
        if not self._verify_write_permission():
            fallback_path = Path(tempfile.gettempdir()) / "baldur" / "checkpoint.json"
            logger.warning(
                "checkpoint_manager.no_write_permission_falling",
                path=self._path,
                fallback_path=fallback_path,
            )
            self._path = fallback_path

        # 디렉토리 생성
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _verify_write_permission(self) -> bool:
        """쓰기 권한 검증."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            test_file = self._path.parent / ".write_test"
            test_file.touch()
            test_file.unlink()
            return True
        except (PermissionError, OSError):
            return False

    @property
    def path(self) -> Path:
        """체크포인트 파일 경로."""
        return self._path

    def save(self, last_sequence: int) -> None:
        """
        체크포인트 저장 (멀티 프로세스 파일 락 지원).

        원자적 쓰기를 위해 임시 파일에 먼저 쓰고 rename.

        Args:
            last_sequence: 마지막 처리된 시퀀스 번호

        Raises:
            CheckpointError: 저장 실패 시
        """
        with self._lock:
            checkpoint_data = CheckpointData(
                last_sequence=last_sequence,
                timestamp=time.time(),
            )

            temp_path = self._path.with_suffix(".tmp")
            lock_file_path = self._path.with_suffix(".lock")

            try:
                # 파일 락 획득
                with open(lock_file_path, "wb") as lock_f:
                    try:
                        lock_file(lock_f)

                        # 임시 파일에 쓰기
                        with open(temp_path, "w", encoding="utf-8") as f:
                            json.dump(checkpoint_data.to_dict(), f, indent=2)

                            if self._sync_on_write:
                                f.flush()
                                os.fsync(f.fileno())

                        # 원자적 rename
                        temp_path.replace(self._path)

                        # Directory fsync (optional, recommended on Linux).
                        # os.O_DIRECTORY is POSIX-only — Windows mypy and
                        # runtime both lack it; the AttributeError catch
                        # below preserves the cross-platform fail-safe.
                        if self._sync_on_write:
                            try:
                                dir_fd = os.open(
                                    str(self._path.parent),
                                    os.O_RDONLY | os.O_DIRECTORY,  # type: ignore[attr-defined]
                                )
                                try:
                                    os.fsync(dir_fd)
                                finally:
                                    os.close(dir_fd)
                            except (OSError, AttributeError):
                                pass

                    finally:
                        try:
                            unlock_file(lock_f)
                        except Exception:
                            pass

                logger.debug(
                    "checkpoint.saved",
                    last_sequence=last_sequence,
                )

            except (BlockingIOError, OSError) as e:
                # 다른 프로세스가 락 보유 중 - 스킵
                logger.warning(
                    "checkpoint_manager.lock_contention_skipping_save",
                    error=e,
                )

            except Exception as e:
                # 임시 파일 정리
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

                raise CheckpointError(f"Failed to save checkpoint: {e}") from e

    def load(self) -> int:
        """
        체크포인트 로드.

        파일이 없거나 읽기 실패 시 0 반환.

        Returns:
            마지막 처리된 시퀀스 번호 (없으면 0)
        """
        with self._lock:
            if not self._path.exists():
                logger.debug("audit_checkpoint.cache_hit")
                return 0

            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)

                checkpoint_data = CheckpointData.from_dict(data)
                logger.debug(
                    "checkpoint.loaded",
                    checkpoint_data=checkpoint_data.last_sequence,
                )
                return checkpoint_data.last_sequence

            except Exception as e:
                logger.warning(
                    "audit_checkpoint.load_checkpoint_failed",
                    error=e,
                )
                return 0

    def load_full(self) -> CheckpointData | None:
        """
        체크포인트 전체 데이터 로드.

        Returns:
            CheckpointData 또는 None
        """
        with self._lock:
            if not self._path.exists():
                return None

            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)

                return CheckpointData.from_dict(data)

            except Exception:
                return None

    def exists(self) -> bool:
        """체크포인트 파일 존재 여부."""
        return self._path.exists()

    def delete(self) -> bool:
        """
        체크포인트 파일 삭제.

        Returns:
            삭제 성공 여부
        """
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
                return True
            except Exception:
                return False

    def get_age_seconds(self) -> float | None:
        """
        체크포인트 경과 시간 (초).

        Returns:
            마지막 저장 후 경과 시간 또는 None
        """
        checkpoint_data = self.load_full()
        if checkpoint_data is None:
            return None

        return time.time() - checkpoint_data.timestamp


# =============================================================================
# Singleton Pattern
# =============================================================================

from baldur.utils.singleton import make_singleton_factory  # noqa: E402

_get_checkpoint_manager, configure_checkpoint_manager, reset_checkpoint_manager = (
    make_singleton_factory("checkpoint_manager", CheckpointManager)
)


def get_checkpoint_manager(
    checkpoint_path: str | Path | None = None,
) -> CheckpointManager:
    """
    Return default CheckpointManager instance.

    .. deprecated::
        Use ``get_default_checkpoint_strategy()`` from ``checkpoint_strategy.py`` instead.
    """
    warnings.warn(
        "get_checkpoint_manager() is deprecated, use "
        "get_default_checkpoint_strategy() from baldur.audit.checkpoint_strategy instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return _get_checkpoint_manager()
