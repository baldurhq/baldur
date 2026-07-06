"""Cat 7A.6 — `protect()` lock contention scaling micro-benchmark.

Plan ref: `memory/scenario-test-plan-2026-04-12.md` §436 row 7A.6
Targets:  N=10 latency < 2x N=1, N=50 < 4x, N=100 < 8x (ratio gate on p50).
Setup:    4 sequential phases of N concurrent threads each calling
          ``protect(name, lambda: 1)`` with default kwargs (CB on, retry off,
          dlq off, timeout None post-#482) on a single shared CB name. The
          shared name is what makes this a lock contention benchmark — every
          call enters ``InMemoryCircuitBreakerStateRepository._lock`` (RLock,
          ``adapters/memory/circuit_breaker.py:61``) on both the read path
          (``get_or_create`` inside ``should_allow``) and the write path
          (``record_success`` after fn() returns). Distinct names per thread
          would still acquire the lock but lose the per-key state coupling
          that defines "contention".

Per-phase shape:
  1. Reset the protect caches (caches across phases would carry state for the
     prior phase's CB name, polluting sliding-window rotation cost).
  2. Allocate a fresh CB name (``bench_lock_contention_7a6_n{N}``) so phases
     are independent observations.
  3. Main-thread warmup (1000 calls) hydrates the CB policy cache, composer
     cache, and ``_storage[name]`` entry. After warmup the steady-state
     hot path is one RLock acquire per ``get_or_create_state`` and one per
     ``record_success`` — the ``_cb_policy_lock`` (Lock at protect.py:80) is
     bypassed by DCL fast-path on cache hit.
  4. Spawn N threads + ``threading.Barrier(N)`` to synchronize start. Each
     thread runs the same number of iterations so per-thread sample counts
     match.
  5. Measurement loop in each thread: ``time.perf_counter_ns()`` around
     ``protect(NAME, fn)``; sample list is pre-allocated; in-loop
     ``assert result == 1`` catches None-return regressions.
  6. After all threads join, pool samples and compute pooled quantiles.

Ratio gate is computed against phase N=1's pooled p50. Plan §484-488: first
run = baseline establishment, NOT pass/fail. The ratio thresholds are
recorded in ``record_property`` for /scenario harness verdict; they are NOT
hard ``assert`` statements.

Two complementary measurement paths (matching 7A.1/7A.5 layout):

1. **Manual ns-resolution loop across 4 phases**
   (``test_protect_lock_contention_scaling``) — pooled p50 / p99 / p999
   per phase + ratio table in ``record_property``. Authoritative for the
   plan §436 ratio verdict.

2. **pytest-benchmark cross-validation at N=1**
   (``test_protect_lock_contention_n1_pytest_benchmark``) — independent
   single-thread baseline using the pytest-benchmark plugin. Cross-checks
   the manual-loop p50 at N=1 against the same number 7A.1 publishes;
   divergence > 25% would indicate measurement-environment drift.

Why pytest-benchmark cannot wrap the multi-thread case directly: the
plugin invokes the callable many times in a tight loop and uses round
calibration. Spawning N threads inside ``benchmark()`` would charge
thread-creation cost into every round and give meaningless numbers.
The manual-loop path owns all multi-thread phases; pytest-benchmark
serves only as the N=1 cross-row consistency check.
"""

from __future__ import annotations

import statistics
import threading
import time
from typing import Any

import pytest

from baldur.protect_facade import protect, reset_protect_caches
from baldur.settings.protect import get_protect_settings

# Plan §436 ratio thresholds — applied as pooled-p50 ratios vs phase N=1.
_RATIO_TARGETS: dict[int, float] = {
    10: 2.0,
    50: 4.0,
    100: 8.0,
}

_PHASE_N_VALUES: tuple[int, ...] = (1, 10, 50, 100)
_ITERS_PER_THREAD = 1000
_WARMUP_ITERATIONS = 1000


def _settings_guard_g1() -> None:
    """G1: confirm ProtectSettings defaults match the documented hot path.

    A drift to ``BALDUR_PROTECT_ENABLED=false`` would short-circuit
    ``protect()`` to bare ``fn()`` (``protect.py`` enabled gate) and report
    ~0 ns latency for every N; ratio would always read 1.0 and falsely
    pass the gate while measuring nothing.
    """
    s = get_protect_settings()
    assert s.enabled is True, "ProtectSettings.enabled drift detected"
    assert s.default_circuit_breaker is True, "default_circuit_breaker drift"
    assert s.default_retry is False, "default_retry drift"
    assert s.default_dlq is False, "default_dlq drift"
    assert s.default_timeout_seconds is None, "default_timeout_seconds drift"


def _warmup(name: str) -> None:
    """G2: hydrate protect caches + ``_storage[name]`` BEFORE Barrier release.

    Without warmup the first thread to land pays cold-path cost
    (``_cb_policy_lock`` Lock acquire to populate the cache, plus
    ``get_or_create`` write path to populate ``_storage[name]``). That
    cold-path lock acquisition profile is qualitatively different from
    the steady-state RLock contention this row is meant to measure.
    """
    fn = lambda: 1  # noqa: E731 — pinned name; cannot rebind per loop
    for _ in range(_WARMUP_ITERATIONS):
        protect(name, fn)


def _run_phase(n_threads: int, name: str) -> dict[str, float | int]:
    """Execute one (n_threads, iters_per_thread) phase and return metrics.

    Returns a dict with pooled quantiles, sample count, and wall-clock
    duration. Per-phase fresh CB name + per-phase warmup keeps phases
    independent observations.
    """
    barrier = threading.Barrier(n_threads)
    # Per-thread sample buffers, pre-allocated. List-of-lists avoids
    # cross-thread append contention on a shared list.
    per_thread_samples: list[list[int]] = [
        [0] * _ITERS_PER_THREAD for _ in range(n_threads)
    ]

    def worker(thread_idx: int) -> None:
        pc = time.perf_counter_ns
        fn = lambda: 1  # noqa: E731
        my_samples = per_thread_samples[thread_idx]
        # G7: Barrier synchronizes start so all N threads measure under
        # peak concurrency. Without this, the first-spawned thread runs
        # ahead solo and its early samples are effectively N=1 latency.
        barrier.wait()
        for i in range(_ITERS_PER_THREAD):
            t0 = pc()
            result = protect(name, fn)
            t1 = pc()
            my_samples[i] = t1 - t0
            # G3: in-loop attribution catches None-return regressions
            # that would still pass any latency gate.
            assert result == 1

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"bench_7a6_n{n_threads}_t{i}")
        for i in range(n_threads)
    ]

    wall_start = time.perf_counter_ns()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_end = time.perf_counter_ns()

    # G8: positive-attribution — every thread completed every iteration.
    pooled: list[int] = []
    for buf in per_thread_samples:
        pooled.extend(buf)
    expected_total = n_threads * _ITERS_PER_THREAD
    assert len(pooled) == expected_total, (
        f"Pooled sample count = {len(pooled)} != {expected_total} — "
        f"a thread did not run to completion at N={n_threads}"
    )
    # Defensive: a thread that exited early via exception would leave
    # zeros in its slot. perf_counter_ns delta is never 0 on real
    # hardware; a 0 sample means uninitialized.
    assert all(s > 0 for s in pooled), (
        f"Found zero-valued sample at N={n_threads} — uncaught worker "
        "exception left an uninitialized slot"
    )

    pooled.sort()
    n = len(pooled)
    p50_ns = pooled[n // 2]
    cuts = statistics.quantiles(pooled, n=1000, method="inclusive")
    p99_ns = cuts[989]
    p999_ns = cuts[998]
    p_min_ns = pooled[0]
    p_max_ns = pooled[-1]
    mean_ns = statistics.fmean(pooled)
    stdev_ns = statistics.pstdev(pooled)

    return {
        "n_threads": n_threads,
        "samples": n,
        "p50_us": p50_ns / 1000,
        "p99_us": p99_ns / 1000,
        "p999_us": p999_ns / 1000,
        "min_us": p_min_ns / 1000,
        "max_us": p_max_ns / 1000,
        "mean_us": mean_ns / 1000,
        "stdev_us": stdev_ns / 1000,
        "wall_clock_s": (wall_end - wall_start) / 1_000_000_000.0,
    }


def test_protect_lock_contention_scaling(record_property: Any) -> None:
    """Manual ns-resolution quantile capture across 4 N values + ratio gate.

    Authoritative for plan §436 ratio verdict: pooled-p50 at N
    threads vs pooled-p50 at N=1 single-thread baseline. p99 / p999 /
    mean recorded for distribution-shape visibility but NOT used in the
    primary ratio gate (ratio gate is on p50 per Stage 3 sync Section 3).
    """
    _settings_guard_g1()

    phase_results: list[dict[str, Any]] = []
    for n in _PHASE_N_VALUES:
        # G9: per-phase fresh CB name + per-phase cache reset → phases are
        # independent observations. Without reset the prior phase's
        # success_count + sliding-window deque would carry through to the
        # next phase, polluting steady-state cost.
        reset_protect_caches()
        name = f"bench_lock_contention_7a6_n{n}"
        # G2: per-phase main-thread warmup. Reset just cleared caches, so
        # warmup re-populates them before threads enter.
        _warmup(name)

        result = _run_phase(n, name)
        phase_results.append(result)

        # Record per-phase metrics. record_property fires per-call; key
        # uniqueness is handled by suffixing with the N value.
        for k, v in result.items():
            record_property(f"n{n}_{k}", v)

    # Build ratio table relative to N=1 pooled p50.
    n1_p50_us = phase_results[0]["p50_us"]
    n1_p99_us = phase_results[0]["p99_us"]
    n1_mean_us = phase_results[0]["mean_us"]
    assert n1_p50_us > 0, f"N=1 p50_us = {n1_p50_us} — sentinel failure"

    ratios: dict[int, dict[str, float]] = {}
    for result in phase_results[1:]:
        n = int(result["n_threads"])
        ratios[n] = {
            "ratio_p50_vs_n1": result["p50_us"] / n1_p50_us,
            "ratio_p99_vs_n1": result["p99_us"] / n1_p99_us,
            "ratio_mean_vs_n1": result["mean_us"] / n1_mean_us,
            "target_p50": _RATIO_TARGETS[n],
        }
        record_property(f"n{n}_ratio_p50_vs_n1", ratios[n]["ratio_p50_vs_n1"])
        record_property(f"n{n}_ratio_p99_vs_n1", ratios[n]["ratio_p99_vs_n1"])
        record_property(f"n{n}_ratio_mean_vs_n1", ratios[n]["ratio_mean_vs_n1"])

    print("\n[7A.6 lock contention scaling]")
    print(
        f"  N=1   baseline: p50={n1_p50_us:.3f}us p99={n1_p99_us:.3f}us "
        f"p999={phase_results[0]['p999_us']:.3f}us "
        f"mean={n1_mean_us:.3f}us samples={phase_results[0]['samples']} "
        f"wall={phase_results[0]['wall_clock_s']:.3f}s"
    )
    for result, n in zip(phase_results[1:], (10, 50, 100), strict=False):
        r = ratios[n]
        target = r["target_p50"]
        verdict = "PASS" if r["ratio_p50_vs_n1"] < target else "FAIL"
        print(
            f"  N={n:<3} contended: p50={result['p50_us']:.3f}us "
            f"(ratio {r['ratio_p50_vs_n1']:.2f}x vs N=1 / target<{target}x) "
            f"[{verdict}] "
            f"p99={result['p99_us']:.3f}us "
            f"(ratio {r['ratio_p99_vs_n1']:.2f}x) "
            f"p999={result['p999_us']:.3f}us "
            f"mean={result['mean_us']:.3f}us "
            f"(ratio {r['ratio_mean_vs_n1']:.2f}x) "
            f"samples={result['samples']} wall={result['wall_clock_s']:.3f}s"
        )


@pytest.mark.benchmark(group="cat-7a-6")
def test_protect_lock_contention_n1_pytest_benchmark(benchmark: Any) -> None:
    """pytest-benchmark cross-validation at N=1 (single-thread baseline).

    Cross-row consistency check: the manual-loop N=1 p50 should agree with
    7A.1's published baseline (60-127 us post-#482) and with this
    pytest-benchmark median to within ~25%. Larger divergence would
    indicate measurement-environment drift (CPU governor, background
    load, OS scheduler) and warrants re-running rather than recording the
    7A.6 numbers as a baseline.
    """
    _settings_guard_g1()
    reset_protect_caches()
    name = "bench_lock_contention_7a6_n1_xcheck"
    _warmup(name)

    fn = lambda: 1  # noqa: E731
    result = benchmark(protect, name, fn)
    assert result == 1
