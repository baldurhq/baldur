"""Cross-platform file locking utilities for checkpoint storage.

Provides lock_file() and unlock_file() for cross-platform (Windows/Linux)
file locking with configurable timeout and retry interval.

Version: 1.0.0
"""

from __future__ import annotations

import sys
import time
from typing import BinaryIO

__all__ = [
    "FILE_LOCK_TIMEOUT_SECONDS",
    "FILE_LOCK_RETRY_INTERVAL",
    "lock_file",
    "unlock_file",
]

FILE_LOCK_TIMEOUT_SECONDS = 5.0
FILE_LOCK_RETRY_INTERVAL = 0.05


def lock_file(f: BinaryIO, *, blocking: bool = True) -> None:
    """Cross-platform file lock acquisition.

    Args:
        f: File handle to lock
        blocking: If True, retry until timeout; if False, fail immediately
    """
    if sys.platform == "win32":
        import msvcrt

        if not blocking:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return

        deadline = time.monotonic() + FILE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(FILE_LOCK_RETRY_INTERVAL)
    else:
        import fcntl

        if not blocking:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return

        deadline = time.monotonic() + FILE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(FILE_LOCK_RETRY_INTERVAL)


def unlock_file(f: BinaryIO) -> None:
    """Cross-platform file lock release."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
