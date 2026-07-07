"""Atheris harness for ``baldur.utils.serialization.fast_loads``.

Targets the canonical JSON deserialization entry point shared by the WAL,
audit, Redis, and API hot paths. ``fast_loads`` is documented to raise
``ValueError``/``TypeError`` on malformed input; any other escaping
exception (or a hang / crash) is a finding.
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from baldur.utils import serialization


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # Exercise both the bytes and the str decode paths.
    if fdp.ConsumeBool():
        payload: bytes | str = fdp.ConsumeBytes(fdp.remaining_bytes())
    else:
        payload = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    try:
        serialization.fast_loads(payload)
    except (ValueError, TypeError):
        # Documented failure mode for malformed input.
        pass


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
