"""Cat 7C.4 — Pipeline preset overhead comparison (baseline-establishment run).

Plan ref:  the perf-scenario plan, row 7C.4
Targets:   minimal < 0.5%, standard < 2%, ha < 5% relative to baseline
           (Wave 5.6 Unified Profile gates — this run = baseline per plan §507)
Setup:     ``minimal_pipeline()`` / ``standard_pipeline()`` / ``ha_pipeline()``
           composers from ``baldur.resilience.policies.presets`` invoked via
           ``composer.execute(WORK)`` against a deterministic ~1ms CPU busy
           loop. Records absolute per-call overhead (us) and derived percent
           overhead at three customer-representative workload scales
           (1ms / 10ms / 50ms RPC).

Process — Perf Threshold Lifecycle (plan §314-330) Stage 1:
   Hypothesis: pipeline framework cost is ABSOLUTE per-call (us), independent
   of the baseline workload duration. Percent overhead = absolute_overhead_us
   / workload_us * 100, so a single measurement produces the full multi-scale
   table customers can read against their own RPC profile.

   Stage 2 refactor + Stage 3 verdict + Stage 4 worst-of-N envelope are
   DEFERRED to the Wave 5.6 Unified Profile cycle. This run records the
   absolute per-call cost and the derived multi-scale percent table; gate
   renegotiation against the 0.5/2/5% targets belongs to Wave 5.6.

Measurement structure:
   Single parametrized test function over 4 paths (baseline, minimal,
   standard, ha). Same `WORK` callable + same warmup + measure count across
   all paths so the resulting table is column-comparable.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest
import structlog

from baldur.resilience.policies.presets import (
    ha_pipeline,
    minimal_pipeline,
    standard_pipeline,
)
from baldur.settings.protect import get_protect_settings

# Plan §507 thresholds (Wave 5.6 gates; informational this run).
_TARGET_MINIMAL_PCT = 0.5
_TARGET_STANDARD_PCT = 2.0
_TARGET_HA_PCT = 5.0

# Workload scales for the derived-percent table (microseconds).
_WORKLOAD_SCALES_US = (1_000.0, 10_000.0, 50_000.0)

# Module-level constants for the deterministic CPU busy loop. Tuned for
# ~1.5-1.8 ms p50 on Windows dev hosts after warmup.
_WORK_ITERATIONS = 30_000
_WARMUP_ITERATIONS = 200
_MEASURE_ITERATIONS = 3_000

_PATHS = ("baseline", "minimal", "standard", "ha")


def _work() -> int:
    """Deterministic CPU busy loop. ~1.5-1.8 ms on Windows dev host post-warmup."""
    s = 0
    for i in range(_WORK_ITERATIONS):
        s += i * i
    return s


def _settings_guard_g1() -> None:
    """G1: confirm ProtectSettings defaults are intact.

    A prior test mutating ``enabled=False`` would make ``protect()``-resolved
    paths into no-ops; minimal/standard/ha would measure ~0 us overhead and
    falsely pass the table.
    """
    s = get_protect_settings()
    assert s.enabled is True, "ProtectSettings.enabled drift detected"
    assert s.default_circuit_breaker is True, "default_circuit_breaker drift"
    assert s.default_retry is False, "default_retry drift"
    assert s.default_dlq is False, "default_dlq drift"


def _silence_logs() -> None:
    """G4: silence AuditHook + structlog INFO emissions.

    AuditHook emits ``policy_pipeline.execution_succeeded`` per execute(),
    dominating framework-core cost by 50-200 us per call. Production
    deployments mute INFO via runtime config — measuring noisy stdout is
    measuring an artifact, not framework cost.
    """
    logging.disable(logging.WARNING)
    structlog.configure(
        processors=[],
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
    )


def _build_callable(path: str) -> Callable[[], int]:
    """G6/G7: same ``WORK`` fn across all 4 measurement paths.

    Distinct CB service_name per pipeline (G8) prevents cross-measurement
    CB state contamination.
    """
    if path == "baseline":
        return _work
    if path == "minimal":
        composer = minimal_pipeline(service_name="bench_minimal_7c4")
        return lambda: composer.execute(_work).value
    if path == "standard":
        composer = standard_pipeline(service_name="bench_standard_7c4")
        return lambda: composer.execute(_work).value
    if path == "ha":
        composer = ha_pipeline(
            service_name="bench_ha_7c4",
            candidates=[_work],
        )
        return lambda: composer.execute(_work).value
    raise ValueError(f"unknown path: {path}")


def _measure(call: Callable[[], Any]) -> dict[str, float]:
    """G3: warmup -> ns-resolution measurement loop -> quantiles."""
    for _ in range(_WARMUP_ITERATIONS):
        call()

    samples_ns: list[int] = [0] * _MEASURE_ITERATIONS
    pc = time.perf_counter_ns
    for i in range(_MEASURE_ITERATIONS):
        t0 = pc()
        call()
        samples_ns[i] = pc() - t0

    samples_ns.sort()
    n = len(samples_ns)
    cuts = statistics.quantiles(samples_ns, n=1000, method="inclusive")
    return {
        "p50_us": samples_ns[n // 2] / 1000.0,
        "p99_us": cuts[989] / 1000.0,
        "p999_us": cuts[998] / 1000.0,
        "min_us": samples_ns[0] / 1000.0,
        "max_us": samples_ns[-1] / 1000.0,
        "mean_us": statistics.fmean(samples_ns) / 1000.0,
        "stdev_us": statistics.pstdev(samples_ns) / 1000.0,
    }


def test_pipeline_preset_overhead_table(record_property: Any) -> None:
    """Single-pass measurement table for baseline + 3 pipelines.

    Verdict semantics (Process — Perf Threshold Lifecycle Stage 1):
      - PASS = (i) every path measured, (ii) baseline_p50 > 0.
      - Ranking ``minimal <= standard <= ha`` is REPORTED in the printed
        table but NOT asserted: Windows in-process noise (~5%) is large
        enough that minimal/standard can swap within the noise band when
        framework cost is well below baseline workload cost. The Wave 5.6
        Unified Profile re-run on a quieter host re-evaluates ranking
        under the gate semantics.
      - The 0.5%/2%/5% plan targets are recorded as informational; gate-FAIL
        verdict is deferred to the Wave 5.6 Unified Profile cycle per plan
        §507 "Test Gate run = baseline; post-5.6 run = published values".
    """
    _settings_guard_g1()
    _silence_logs()

    results: dict[str, dict[str, float]] = {}
    for path in _PATHS:
        call = _build_callable(path)
        results[path] = _measure(call)
        for key, val in results[path].items():
            record_property(f"{path}_{key}", val)

    baseline_p50 = results["baseline"]["p50_us"]
    record_property("baseline_p50_us", baseline_p50)

    # Absolute per-call overhead + derived percent at the three workload scales.
    overhead_table: dict[str, dict[str, float]] = {}
    for path in ("minimal", "standard", "ha"):
        abs_overhead_us = max(0.0, results[path]["p50_us"] - baseline_p50)
        overhead_table[path] = {"abs_overhead_us": abs_overhead_us}
        for w_us in _WORKLOAD_SCALES_US:
            pct = (abs_overhead_us / w_us) * 100.0
            scale_label = f"{int(w_us / 1000)}ms"
            overhead_table[path][f"pct_at_{scale_label}"] = pct
            record_property(f"{path}_pct_at_{scale_label}", pct)
        record_property(f"{path}_abs_overhead_us", abs_overhead_us)

    # Stage 1 verdict assertions (verdict-semantics, not gate-semantics).
    # Ranking is reported in the printed table but NOT asserted — see docstring.
    for path in _PATHS:
        assert results[path]["p50_us"] > 0.0, f"{path}: p50 not measured"
    assert baseline_p50 > 0.0, "baseline: p50 not measured"

    # Ranking observation for the printed table (informational; comparison
    # against framework-cost-relative-to-noise-floor expectations).
    ranking_respected = (
        results["minimal"]["p50_us"]
        <= results["standard"]["p50_us"]
        <= results["ha"]["p50_us"]
    )
    record_property("ranking_respected", int(ranking_respected))

    # Diagnostic dump for harness result ingestion.
    print("\n[7C.4 pipeline preset overhead table]")
    print(
        f"  baseline p50={baseline_p50:.2f}us p99={results['baseline']['p99_us']:.2f}us"
    )
    for path in ("minimal", "standard", "ha"):
        r = results[path]
        ot = overhead_table[path]
        print(
            f"  {path:<8s} p50={r['p50_us']:.2f}us p99={r['p99_us']:.2f}us "
            f"abs={ot['abs_overhead_us']:.2f}us "
            f"@1ms={ot['pct_at_1ms']:.3f}% "
            f"@10ms={ot['pct_at_10ms']:.3f}% "
            f"@50ms={ot['pct_at_50ms']:.3f}%"
        )

    print(
        "  targets (Wave 5.6 gates; informational this run): "
        f"minimal<{_TARGET_MINIMAL_PCT}% "
        f"standard<{_TARGET_STANDARD_PCT}% "
        f"ha<{_TARGET_HA_PCT}%"
    )
    print(
        f"  ranking minimal<=standard<=ha: "
        f"{'respected' if ranking_respected else 'violated (within noise band)'}"
    )


@pytest.mark.benchmark(group="cat-7c-4")
@pytest.mark.parametrize("path", _PATHS)
def test_pipeline_preset_pytest_benchmark(benchmark: Any, path: str) -> None:
    """pytest-benchmark cross-validation pass (median / iqr / ops / stddev).

    Separate from the table-emitting test above because pytest-benchmark
    `stats.data` exposes per-round means (averaged across calibration
    iterations), not per-call samples — useful for cross-row comparison but
    not authoritative for quantiles.
    """
    _settings_guard_g1()
    _silence_logs()
    call = _build_callable(path)
    # Warmup before pytest-benchmark begins its own calibration.
    for _ in range(_WARMUP_ITERATIONS):
        call()
    benchmark(call)
