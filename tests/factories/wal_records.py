"""Raw on-disk WAL file authoring for tests.

``WriteAheadLog`` names its own files ``{prefix}_{timestamp}_{pid}.wal`` and
always writes a correct CRC32 checksum, so two things a reader test needs are
unreachable through the public write path: a filename whose timestamp does not
match the sequences inside it, and a record whose checksum field is wrong (or
not even ASCII). These helpers write the on-disk record format directly —
``AWAL`` header, then ``length(4) + checksum(8) + json`` per record — which is
exactly what ``WALReaderMixin._read_wal_file_impl`` parses.

Sequences are the reader's only ordering key, so a record is fully described by
its sequence plus an optional checksum override.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "RawRecord",
    "own_pid_wal_name",
    "write_raw_wal_file",
]

_MAGIC = b"AWAL"
_VERSION = 1


@dataclass(frozen=True)
class RawRecord:
    """One WAL record to stamp on disk.

    Args:
        sequence: the record's ``seq`` field — the reader's ordering key.
        payload: the record's ``data`` field.
        checksum: raw 8-byte checksum field. ``None`` writes the correct
            CRC32, which is what an intact record carries. Anything else is
            written verbatim, so a test can produce a mismatch
            (``b"deadbeef"``) or a field the strict reader cannot even decode
            (a byte >= 0x80).
    """

    sequence: int
    payload: dict | None = None
    checksum: bytes | None = None

    def as_entry(self, timestamp: float | None = None) -> dict:
        return {
            "seq": self.sequence,
            "ts": timestamp if timestamp is not None else float(self.sequence),
            "data": self.payload if self.payload is not None else {"n": self.sequence},
        }


def write_raw_wal_file(
    filepath: Path,
    records: list[RawRecord],
    magic: bytes = _MAGIC,
) -> Path:
    """Write ``records`` to ``filepath`` in the WAL's on-disk format."""
    with open(filepath, "wb") as f:
        f.write(magic)
        f.write(struct.pack(">I", _VERSION))

        for record in records:
            data = json.dumps(record.as_entry()).encode("utf-8")
            checksum = record.checksum
            if checksum is None:
                checksum = format(zlib.crc32(data) & 0xFFFFFFFF, "08x").encode("ascii")
            if len(checksum) != 8:
                raise ValueError("a WAL checksum field is exactly 8 bytes")
            f.write(struct.pack(">I", len(data)))
            f.write(checksum)
            f.write(data)

    return filepath


def own_pid_wal_name(prefix: str, stamp: str) -> str:
    """A filename this process owns — matched by the runtime glob."""
    return f"{prefix}_{stamp}_{os.getpid()}.wal"
