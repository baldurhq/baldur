"""Atheris harness for the binary WAL reader (``baldur.audit.wal._reader``).

The binary WAL format is length-prefixed (``struct.unpack('>I', ...)``) with
per-record CRC32 checksums, parsed from untrusted on-disk data during crash
recovery. This is the highest-value target: an attacker-controlled length or
payload must never crash the reader or the lightweight cleanup scan.

A minimal host supplies the file-format constants and config the mixin needs.
Any escaping exception is a finding.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports():
    from baldur.audit.wal._models import WALConfig
    from baldur.audit.wal._reader import WALReaderMixin


class _FuzzHost(WALReaderMixin):
    """Minimal WAL host exposing the reader mixin's required contract."""

    HEADER_SIZE = 8
    MAGIC = b"AWAL"

    def __init__(self) -> None:
        self._config = WALConfig(best_effort_recovery=True)
        self._corrupted_entries = 0
        self._recovered_entries = 0
        self._on_corruption = None

    def _record_audit_event(self, event_type: str, details: dict) -> None:
        pass


_HOST = _FuzzHost()
_WAL_PATH = Path(tempfile.gettempdir()) / f"baldur_fuzz_wal_{os.getpid()}.wal"


def TestOneInput(data: bytes) -> None:
    _WAL_PATH.write_bytes(data)
    for best_effort in (True, False):
        _HOST._corrupted_entries = 0
        for _ in _HOST._read_wal_file_impl(_WAL_PATH, best_effort=best_effort):
            pass
    # Checksum-skipping lightweight scan used by cleanup_processed().
    _HOST._get_file_max_sequence(_WAL_PATH)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
