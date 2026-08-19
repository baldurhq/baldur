"""Measure what Baldur costs the process it runs in.

Run it against your own configuration, on your own hardware::

    python -m baldur.scripts.measure_footprint

It samples this process four times: the bare interpreter, after
``import baldur``, the moment ``baldur.init()`` returns, and the settled
steady state a few seconds later. Between each pair it prints the RSS, thread
and CPU delta, alongside an echo of the posture and host the numbers came
from.

**The settled sample is the only citable one.** ``init()`` returns while
background threads are still starting, so the reading taken at that moment is
a transient peak: RSS runs measurably higher there than a few seconds later,
against a thread inventory that is not yet complete. The peak is printed and
labelled, never presented as a resident cost.

No load is generated, and nothing is overridden except ``BALDUR_ADMIN_PORT``,
which is defaulted to an ephemeral port so the probe cannot collide with a
real admin server. Everything else is read from the environment you already
have, so the numbers describe *your* deployment's posture rather than a
synthetic one.

Two things this does not do. It does not reproduce a web worker's footprint —
a Django or FastAPI worker additionally imports its URL configuration and
whatever the application itself pulls in, which a plain process never pays.
And it attributes nothing: the thread names are reported as observed, with no
claim about which component owns which cost. Compare this process against
itself across configuration changes; do not compare it against a number
measured on a different stack.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psutil

__all__ = ["main"]

_BYTES_PER_MB = 1024 * 1024

# The settle stage is the whole point of the script. init() returns while
# background threads are still starting, so a sample taken there is a peak,
# not a resident cost. A sample counts as settled once it is at least
# _SETTLE_MIN_SECONDS past init()'s return AND its thread count is unchanged
# from the sample before it — either condition alone accepts a process that
# is still growing.
_SETTLE_MIN_SECONDS = 10.0
_SETTLE_SAMPLE_INTERVAL_SECONDS = 2.0
_SETTLE_TIMEOUT_SECONDS = 60.0

_BASELINE_LABEL = "interpreter baseline"
_IMPORT_LABEL = "import baldur"
_INIT_LABEL = "baldur.init() returned"

_COMPLETE_BOUNDARY = "--- measurement complete ---"


@dataclass(frozen=True)
class Sample:
    """One observation of this process, taken at a labelled point in time."""

    label: str
    elapsed_seconds: float
    rss_bytes: int
    num_threads: int
    thread_names: tuple[str, ...]
    cpu_seconds: float


def collect_sample(
    process: psutil.Process, label: str, elapsed_seconds: float
) -> Sample:
    """Read RSS, thread and CPU counters for ``process`` in one pass.

    ``memory_info()`` and ``cpu_times()`` are cheap self-process reads, so the
    probe's own cost stays far below what it measures.

    ``thread_names`` lists only threads created through :mod:`threading`, so it
    is a subset of ``num_threads`` (interpreter-internal and C-level threads
    are absent). Both are reported; they are never interchangeable.
    """
    memory = process.memory_info()
    cpu = process.cpu_times()
    return Sample(
        label=label,
        elapsed_seconds=elapsed_seconds,
        rss_bytes=int(memory.rss),
        num_threads=process.num_threads(),
        thread_names=tuple(sorted(t.name for t in threading.enumerate())),
        cpu_seconds=float(cpu.user) + float(cpu.system),
    )


def layer_delta(before: Sample, after: Sample) -> dict[str, Any]:
    """What ``after`` costs on top of ``before``.

    ``new_threads`` names the threads present in ``after`` and absent from
    ``before``. It is a naming aid, not an attribution: a thread appearing
    between two samples does not make it the owner of the RSS delta.
    """
    return {
        "rss_mb": (after.rss_bytes - before.rss_bytes) / _BYTES_PER_MB,
        "threads": after.num_threads - before.num_threads,
        "cpu_seconds": after.cpu_seconds - before.cpu_seconds,
        "new_threads": tuple(
            name for name in after.thread_names if name not in before.thread_names
        ),
    }


def settled_index(samples: list[Sample], min_elapsed_seconds: float) -> int | None:
    """Index of the first citable sample in ``samples``, or None if still growing.

    A sample qualifies when it was taken at or after ``min_elapsed_seconds``
    (measured on the same clock as ``Sample.elapsed_seconds``) and its thread
    count matches the sample immediately before it. The pairwise comparison is
    what rejects a process whose daemons are still arriving; the floor alone
    would accept a momentarily-flat count.
    """
    for index in range(1, len(samples)):
        current = samples[index]
        if current.elapsed_seconds < min_elapsed_seconds:
            continue
        if current.num_threads == samples[index - 1].num_threads:
            return index
    return None


def collect_posture() -> dict[str, str]:
    """Echo what this process is configured as, and what host it ran on.

    The Baldur keys say which code paths the numbers include; the host axes say
    whether the numbers are comparable to anyone else's at all, since resident
    memory does not transfer across operating system or interpreter version.
    """
    from baldur.bootstrap import get_runtime_posture

    runtime = get_runtime_posture()
    return {
        "environment": os.environ.get("BALDUR_ENVIRONMENT", "(unset)"),
        "storage backend": str(runtime.get("storage", "unknown")),
        "redis": (
            "configured" if runtime.get("storage") == "redis" else "not configured"
        ),
        "metrics backend": str(runtime.get("metrics", "unknown")),
        "baldur_pro": _pro_availability(),
        "entitlement": _entitlement_verdict(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "python": platform.python_version(),
        "cpu count": str(os.cpu_count()),
    }


def _pro_availability() -> str:
    """Report whether the PRO package is installed, without importing it.

    The canonical predicate resolves the distribution without executing it, so
    asking the question does not change the answer the rest of the report
    gives.
    """
    from baldur.utils.tier import is_pro_installed

    return "installed" if is_pro_installed() else "not installed"


def _entitlement_verdict() -> str:
    """Report the boot entitlement verdict, which gates PRO-only wiring."""
    from baldur.core.entitlement import get_entitlement_status

    return str(get_entitlement_status().status.value)


def format_posture(posture: dict[str, str]) -> list[str]:
    """Render the posture echo as aligned ``key  value`` lines."""
    width = max((len(key) for key in posture), default=0)
    return [f"  {key.ljust(width)}  {value}" for key, value in posture.items()]


def format_stage(sample: Sample, previous: Sample | None) -> list[str]:
    """Render one stage: its absolute reading, then its delta from the previous."""
    lines = [
        f"  {sample.label}  (+{sample.elapsed_seconds:.1f}s)",
        f"      RSS {sample.rss_bytes / _BYTES_PER_MB:8.1f} MB"
        f"   threads {sample.num_threads:3d}"
        f"   CPU {sample.cpu_seconds:6.2f} s",
    ]
    if previous is not None:
        delta = layer_delta(previous, sample)
        lines.append(
            f"      delta {delta['rss_mb']:+8.1f} MB"
            f"   threads {delta['threads']:+3d}"
            f"   CPU {delta['cpu_seconds']:+6.2f} s"
        )
        if delta["new_threads"]:
            lines.append(f"      new threads: {', '.join(delta['new_threads'])}")
    return lines


def _say(line: str = "") -> None:
    """Print one report line.

    Every literal passed here is ASCII. A console on a legacy code page
    (cp949, cp1252) raises UnicodeEncodeError on the first non-ASCII
    character, and a diagnostic that dies on the machine being diagnosed
    is worse than a plain-looking one.
    """
    print(line, flush=True)


def _settle(
    process: psutil.Process,
    origin: float,
    init_sample: Sample,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[Sample], int | None]:
    """Sample until the thread inventory stops moving, or the timeout expires.

    Returns every sample taken (``init_sample`` first, so the first settle
    candidate has a predecessor to compare against) and the index of the
    citable one, or None when the process never settled.
    """
    samples = [init_sample]
    floor = init_sample.elapsed_seconds + _SETTLE_MIN_SECONDS
    deadline = init_sample.elapsed_seconds + _SETTLE_TIMEOUT_SECONDS

    while True:
        sleep(_SETTLE_SAMPLE_INTERVAL_SECONDS)
        elapsed = time.monotonic() - origin
        samples.append(collect_sample(process, "settled", elapsed))
        index = settled_index(samples, floor)
        if index is not None:
            return samples, index
        if elapsed >= deadline:
            return samples, None


def main() -> int:
    # Bind the admin server to an ephemeral port so the probe cannot collide
    # with a real one. setdefault, so an explicit port still wins — this is the
    # only value the script imposes on the posture it is measuring.
    os.environ.setdefault("BALDUR_ADMIN_PORT", "0")

    import psutil

    process = psutil.Process()
    origin = time.monotonic()

    baseline = collect_sample(process, _BASELINE_LABEL, 0.0)

    import baldur

    imported = collect_sample(process, _IMPORT_LABEL, time.monotonic() - origin)

    try:
        baldur.init()
    except baldur.ConfigurationError as e:
        # The one caught probe failure. init() refuses to start in production
        # without shared storage, and that refusal is the framework's own
        # posture rather than a fault in this script — so name the cause and
        # say plainly that no measurement was taken. Every other failure stays
        # loud and unhandled.
        _say()
        _say(f"baldur.init() refused to start: {e}")
        _say("No measurement was taken. Configure storage, or run this from a")
        _say("non-production environment, and try again.")
        _end_report()
        return 1

    initialized = collect_sample(process, _INIT_LABEL, time.monotonic() - origin)
    settle_samples, citable_index = _settle(process, origin, initialized)

    _report_header()
    _report_stages(baseline, imported, initialized)

    if citable_index is None:
        _report_unsettled(settle_samples[-1])
        _end_report()
        return 1

    _report_citable(baseline, initialized, settle_samples[citable_index])
    _end_report()
    _say("Anything logged below this line is shutdown work, not measured cost.")
    return 0


def _report_header() -> None:
    """Print the title and the posture echo."""
    _say()
    _say("Baldur resident footprint: this process, this configuration")
    _say()
    _say("Posture")
    for line in format_posture(collect_posture()):
        _say(line)


def _report_stages(baseline: Sample, imported: Sample, initialized: Sample) -> None:
    """Print the three pre-settle stages, each with the caveat it needs."""
    _say()
    _say("Stages")
    for line in format_stage(baseline, None):
        _say(line)
    for line in format_stage(imported, baseline):
        _say(line)
    # The package defers almost every submodule, so this stage reads near
    # zero on purpose. The modules that make up the resident set are pulled
    # in by init() below, and by whatever the application imports itself.
    _say("      near zero by design: the package defers its submodules,")
    _say("        so the imports land in the stage below, not here.")
    for line in format_stage(initialized, imported):
        _say(line)
    _say("      ^ transient peak: background threads are still starting here,")
    _say("        so this reading is not a resident cost. Read the line below.")


def _report_unsettled(last: Sample) -> None:
    """Say that no citable figure exists, rather than printing a moving one."""
    _say()
    _say(
        f"  the thread count was still moving after "
        f"{_SETTLE_TIMEOUT_SECONDS:.0f}s "
        f"(last reading {last.num_threads} threads,"
        f" {last.rss_bytes / _BYTES_PER_MB:.1f} MB)"
    )
    _say("  No citable resident figure. Nothing here should be quoted.")


def _report_citable(baseline: Sample, initialized: Sample, citable: Sample) -> None:
    """Print the settled stage, the total against baseline, and the thread list."""
    for line in format_stage(citable, initialized):
        _say(line)

    total = layer_delta(baseline, citable)
    _say()
    _say("Baldur's resident cost in this process (settled, citable)")
    _say(f"  RSS      {total['rss_mb']:+.1f} MB")
    _say(f"  threads  {total['threads']:+d}")
    _say(f"  CPU      {total['cpu_seconds']:+.2f} s of processor time to here")
    _say()
    _say("Threads present at settle (names only; no cost is attributed to any)")
    _say(f"  {', '.join(citable.thread_names)}")


def _end_report() -> None:
    """Close the measurement with the boundary line every exit path shares."""
    _say()
    _say(_COMPLETE_BOUNDARY)


if __name__ == "__main__":
    sys.exit(main())
