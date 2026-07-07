"""Atheris harness for the JSONL WAL reader (``baldur.audit.wal._jsonl``).

Writes fuzzed bytes to a temp file and drives the corrupted-line-skipping
reader and the committed-filter parser over it. This is the recovery path
that ingests potentially-corrupt persisted WAL lines, so it must skip any
malformed line rather than raise. Any escaping exception is a finding.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports():
    from baldur.audit.wal._jsonl import JSONLReader

_WAL_PATH = Path(tempfile.gettempdir()) / f"baldur_fuzz_jsonl_{os.getpid()}.jsonl"


def TestOneInput(data: bytes) -> None:
    _WAL_PATH.write_bytes(data)
    for _ in JSONLReader.iter_entries(_WAL_PATH):
        pass
    JSONLReader.parse_with_committed_filter(_WAL_PATH)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
