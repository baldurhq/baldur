"""Atheris harness for ``baldur.utils.duration``.

Fuzzes the ISO-8601 timestamp parser and the incident-duration calculator.
Both consume externally-sourced timeline data (event dicts persisted by the
audit / forensics paths), so they must tolerate arbitrary shapes without
raising. Any escaping exception is a finding.
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from baldur.utils import duration


def _fuzz_timeline(fdp: atheris.FuzzedDataProvider) -> list:
    timeline: list = []
    for _ in range(fdp.ConsumeIntInRange(0, 8)):
        timeline.append(
            {
                "event_type": fdp.ConsumeUnicodeNoSurrogates(16),
                "timestamp": fdp.ConsumeUnicodeNoSurrogates(32),
            }
        )
    return timeline


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    duration.parse_iso_timestamp(fdp.ConsumeUnicodeNoSurrogates(64))

    timeline = _fuzz_timeline(fdp)
    current = fdp.ConsumeUnicodeNoSurrogates(32) or None
    duration.calculate_incident_duration(timeline, current_time_iso=current)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
