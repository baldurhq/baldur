"""Cat 7A.1 — `protect()` success-path overhead micro-benchmark.

Plan ref: `memory/scenario-test-plan-2026-04-12.md` §431 row 7A.1
Targets:  p50 < 0.1 ms, p99 < 0.5 ms, p999 < 1 ms
Setup:    `protect(name, fn)` with default kwargs (CB on, retry off, dlq off,
          timeout 30s) on a healthy lambda. Warmup loop hydrates the in-memory
          CB state and primes the ProviderRegistry layered-failure path before
          measurement.

Two complementary measurement paths:

1. **Manual ns-resolution loop** (`test_protect_success_path_quantiles`) —
   `time.perf_counter_ns()` around 10000 inner iterations after a 1000-call
   warmup. Computes p50 / p99 / p999 from the raw sample list via
   `statistics.quantiles(..., n=1000)`. This is the AUTHORITATIVE quantile
   source because pytest-benchmark `stats.data` exposes per-round means
   (averaged across calibration iterations), not per-call samples.

2. **pytest-benchmark cross-validation** (`test_protect_success_path_pytest_benchmark`) —
   standard `benchmark(callable)` invocation with `min_rounds=2000`. Provides
   median / mean / stddev / iqr / ops via the pytest-benchmark plugin so the
   numbers can be compared against future regression runs.

The first run of either test establishes BASELINE per plan §484-488; PASS/FAIL
verdict is recorded by the /scenario harness, not by these tests.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

from baldur.protect_facade import protect
from baldur.settings.protect import get_protect_settings

# Plan §431 thresholds (microseconds for ns-precision arithmetic).
_TARGET_P50_NS = 100_000  # 0.1 ms
_TARGET_P99_NS = 500_000  # 0.5 ms
_TARGET_P999_NS = 1_000_000  # 1.0 ms

_BENCH_NAME = "bench_success_7a1"
_WARMUP_ITERATIONS = 1000
_MEASURE_ITERATIONS = 10_000


def _settings_guard_g1() -> None:
    """G1: confirm ProtectSettings defaults match the documented hot path.

    If a prior test mutated `BALDUR_PROTECT_ENABLED=false`, `protect()` would
    short-circuit to bare `fn()` (`protect.py:394-395`) and report ~0 ns —
    falsely passing the threshold while measuring nothing.
    """
    s = get_protect_settings()
    assert s.enabled is True, "ProtectSettings.enabled drift detected"
    assert s.default_circuit_breaker is True, "default_circuit_breaker drift"
    assert s.default_retry is False, "default_retry drift"
    assert s.default_dlq is False, "default_dlq drift"
    assert s.default_timeout_seconds is None, "default_timeout_seconds drift"


def _warmup() -> None:
    """G2: hydrate CB state + prime registry layered-failure path."""
    for _ in range(_WARMUP_ITERATIONS):
        protect(_BENCH_NAME, lambda: 1)


def test_protect_success_path_quantiles(record_property: Any) -> None:
    """Manual ns-resolution quantile capture (authoritative for p50/p99/p999)."""
    _settings_guard_g1()
    _warmup()

    samples_ns: list[int] = [0] * _MEASURE_ITERATIONS
    pc = time.perf_counter_ns
    fn = lambda: 1  # noqa: E731 — pinned name; cannot rebind per loop

    # Tight loop. Avoid per-iteration attribute lookups.
    for i in range(_MEASURE_ITERATIONS):
        t0 = pc()
        result = protect(_BENCH_NAME, fn)
        t1 = pc()
        samples_ns[i] = t1 - t0
        # G3: assert return value plumbs through correctly. Doing this inside
        # the loop is intentional — a regression that returns None would
        # otherwise sail past the latency threshold.
        assert result == 1

    samples_ns.sort()
    n = len(samples_ns)
    p50_ns = samples_ns[n // 2]
    # statistics.quantiles(n=1000) returns 999 cut points — index 989 = p99,
    # index 998 = p999.
    cuts = statistics.quantiles(samples_ns, n=1000, method="inclusive")
    p99_ns = cuts[989]
    p999_ns = cuts[998]
    p_min_ns = samples_ns[0]
    p_max_ns = samples_ns[-1]
    mean_ns = statistics.fmean(samples_ns)
    stdev_ns = statistics.pstdev(samples_ns)

    # Record raw stats for /scenario Stage 7 ingestion. record_property writes
    # to the JUnit XML report and prints under -v.
    record_property("p50_us", p50_ns / 1000)
    record_property("p99_us", p99_ns / 1000)
    record_property("p999_us", p999_ns / 1000)
    record_property("min_us", p_min_ns / 1000)
    record_property("max_us", p_max_ns / 1000)
    record_property("mean_us", mean_ns / 1000)
    record_property("stdev_us", stdev_ns / 1000)
    record_property("samples", n)

    print(
        "\n[7A.1 quantiles] "
        f"n={n} "
        f"p50={p50_ns / 1000:.3f}us (target<{_TARGET_P50_NS / 1000:.1f}us) "
        f"p99={p99_ns / 1000:.3f}us (target<{_TARGET_P99_NS / 1000:.1f}us) "
        f"p999={p999_ns / 1000:.3f}us (target<{_TARGET_P999_NS / 1000:.1f}us) "
        f"mean={mean_ns / 1000:.3f}us "
        f"stdev={stdev_ns / 1000:.3f}us "
        f"min={p_min_ns / 1000:.3f}us "
        f"max={p_max_ns / 1000:.3f}us"
    )


@pytest.mark.benchmark(group="cat-7a-1")
def test_protect_success_path_pytest_benchmark(benchmark: Any) -> None:
    """pytest-benchmark cross-validation report (median / iqr / ops)."""
    _settings_guard_g1()
    _warmup()

    fn = lambda: 1  # noqa: E731
    result = benchmark(protect, _BENCH_NAME, fn)
    # G3: post-bench return-value sanity check (single observation is enough —
    # the in-loop assert in the quantile test covers per-call attribution).
    assert result == 1
