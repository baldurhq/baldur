"""Cat 7A.2 — `protect()` failure-path overhead micro-benchmark.

Plan ref: `memory/scenario-test-plan-2026-04-12.md` §432 row 7A.2
Targets:  p50 < 0.3 ms, p99 < 1 ms
Setup:    `protect(name, fn, dlq=True, retry=RetryPolicyConfig(max_attempts=1,
          enable_dlq=True), circuit_breaker=False)` on a function that always
          raises. RetryPolicy with max_attempts=1 invokes ``fn`` once, marks
          ``should_dlq=True`` in PolicyResult.metadata, emits RETRY_EXHAUSTED
          to EventBus, and DLQSink stores a FailedOperationData entry into
          the InMemoryFailedOperationRepository (resolved via
          ``baldur.core.di_fallback.resolve_with_fallback`` because no Redis
          is wired in the benchmark env). This isolates DLQ serialization +
          EventBus dispatch cost without network, per plan §432 rationale.

Why circuit_breaker=False: the plan rationale bounds this measurement at
"DLQ + EventBus" — including CB.record_failure double-counts what 7A.1
(success path with CB on) and 7A.3 (CB OPEN reject path) already isolate.
Single-variable progression across 7A rows.

Why max_attempts=1: default RetryPolicyConfig has max_attempts=3 +
backoff_base=4 (seconds!) which would 12+ second per call. With max=1 the
RetryPolicy skips the backoff sleep entirely (delay only happens between
attempts) but still emits RETRY_EXHAUSTED + sets should_dlq=True on the
exhausted PolicyResult — exactly the failure-path-with-DLQ semantics this
row is supposed to measure.

Two complementary measurement paths (matching 7A.1 layout):

1. **Manual ns-resolution loop** (`test_protect_failure_path_quantiles`) —
   `time.perf_counter_ns()` around 10000 inner iterations after a 1000-call
   warmup. Computes p50 / p99 / p999 from the raw sample list via
   `statistics.quantiles(..., n=1000)`.

2. **pytest-benchmark cross-validation** (`test_protect_failure_path_pytest_benchmark`) —
   standard `benchmark(callable)` invocation. Provides median / iqr / ops.

The first run of either test establishes BASELINE per plan §484-488; PASS/FAIL
verdict is recorded by the /scenario harness, not by these tests.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

# This benchmark measures the DLQ failure path, which is a PRO-tier feature
# (DLQService lives in baldur_pro); both tests resolve it, so skip the module
# wholesale when the PRO tier is absent (the public mirror).
pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro

from baldur.protect_facade import protect  # noqa: E402
from baldur.services.event_bus.bus import get_event_bus  # noqa: E402
from baldur.services.event_bus.bus.event_types import EventType  # noqa: E402
from baldur.services.retry_handler.models import RetryPolicyConfig  # noqa: E402
from baldur.settings.protect import get_protect_settings  # noqa: E402

# Plan §432 thresholds (microseconds for ns-precision arithmetic).
_TARGET_P50_NS = 300_000  # 0.3 ms
_TARGET_P99_NS = 1_000_000  # 1.0 ms

_BENCH_NAME = "bench_failure_7a2"
_WARMUP_ITERATIONS = 1000
_MEASURE_ITERATIONS = 10_000


def _always_fail() -> None:
    """Raise a stable RuntimeError every call — the failure path under test."""
    raise RuntimeError("bench_failure_7a2")


def _retry_config_max_attempts_1() -> RetryPolicyConfig:
    """RetryPolicyConfig with max_attempts=1 — exhausts on first call without
    a between-attempt sleep. enable_dlq=True so DLQSink fires."""
    return RetryPolicyConfig(
        max_attempts=1,
        enable_dlq=True,
        domain=_BENCH_NAME,
    )


def _settings_guard_g1() -> None:
    """G1: confirm ProtectSettings defaults match the documented hot path.

    If a prior test mutated `BALDUR_PROTECT_ENABLED=false`, `protect()` would
    short-circuit to bare `fn()` (`protect.py:553-554`) and the failing fn
    would re-raise WITHOUT going through DLQSink — falsely reporting low
    latency while measuring nothing.
    """
    s = get_protect_settings()
    assert s.enabled is True, "ProtectSettings.enabled drift detected"
    assert s.default_circuit_breaker is True, "default_circuit_breaker drift"
    assert s.default_retry is False, "default_retry drift"
    assert s.default_dlq is False, "default_dlq drift"
    assert s.default_timeout_seconds is None, "default_timeout_seconds drift"


def _call_protect_failing() -> None:
    """One protected call to the always-failing fn. Re-raises RuntimeError
    after RetryPolicy sets should_dlq=True, EventBus emits RETRY_EXHAUSTED,
    and DLQSink stores into InMemoryFailedOperationRepository."""
    protect(
        _BENCH_NAME,
        _always_fail,
        dlq=True,
        retry=_retry_config_max_attempts_1(),
        circuit_breaker=False,
    )


def _warmup() -> None:
    """G2: hydrate DLQ ProviderRegistry layered-failure path + InMemory dict +
    RetryPolicy module imports + DLQSink module imports + ``baldur_pro.dlq``
    convenience-singleton lazy init before measurement."""
    for _ in range(_WARMUP_ITERATIONS):
        try:
            _call_protect_failing()
        except RuntimeError:
            pass


def _dlq_count() -> int:
    """G6 helper: read the live InMemory DLQ count by calling the same
    convenience function the production sink path uses, then querying the
    repository it landed in. ``baldur_pro.services.dlq.get_dlq_service``
    returns the singleton DLQService whose ``repository`` property resolves
    via ``resolve_with_fallback`` to ``InMemoryFailedOperationRepository``
    when no Redis is wired (the benchmark env)."""
    from baldur_pro.services.dlq import get_dlq_service

    return get_dlq_service().repository.count_all()


def test_protect_failure_path_quantiles(record_property: Any) -> None:
    """Manual ns-resolution quantile capture (authoritative for p50/p99/p999)."""
    _settings_guard_g1()
    _warmup()

    bus = get_event_bus()
    pre_dlq = _dlq_count()
    pre_history_count = len(
        bus.get_history(event_type=EventType.RETRY_EXHAUSTED, limit=10)
    )

    samples_ns: list[int] = [0] * _MEASURE_ITERATIONS
    pc = time.perf_counter_ns

    # Tight loop. Avoid per-iteration attribute lookups.
    for i in range(_MEASURE_ITERATIONS):
        raised = False
        t0 = pc()
        try:
            _call_protect_failing()
        except RuntimeError:
            raised = True
        t1 = pc()
        samples_ns[i] = t1 - t0
        # G3: assert the call actually raised. A regression that swallowed the
        # error inside _finalize_value would still complete fast and pass the
        # latency threshold despite being functionally broken.
        assert raised

    # G6: DLQSink fired on every iteration — InMemory DLQ count grew by
    # exactly _MEASURE_ITERATIONS. Without this, a regression that flipped
    # should_dlq to False would silently measure a "no-op DLQ" path.
    post_dlq = _dlq_count()
    dlq_delta = post_dlq - pre_dlq
    assert dlq_delta == _MEASURE_ITERATIONS, (
        f"DLQ entry delta {dlq_delta} != expected {_MEASURE_ITERATIONS} "
        "— DLQSink did not fire on every failure-path iteration"
    )

    # G7: EventBus emitted RETRY_EXHAUSTED at least once. Since
    # _event_history is capped at audit_settings.event_history_max (1000 by
    # default) we cannot assert exact count == 10000 — but post-loop the
    # capped buffer must be saturated with RETRY_EXHAUSTED entries.
    post_history_count = len(
        bus.get_history(event_type=EventType.RETRY_EXHAUSTED, limit=10)
    )
    assert post_history_count >= 1, (
        "EventBus history has no RETRY_EXHAUSTED entries — "
        "RetryPolicy._emit_exhausted_event silently failed"
    )
    # When pre-loop history was empty, expect the cap to fill (≥10 within the
    # limit=10 query). Skip when warmup already populated it.
    if pre_history_count == 0:
        assert post_history_count >= 10, (
            f"Only {post_history_count} RETRY_EXHAUSTED entries after 10000 "
            "iterations — emission rate is below 1 per call"
        )

    samples_ns.sort()
    n = len(samples_ns)
    p50_ns = samples_ns[n // 2]
    cuts = statistics.quantiles(samples_ns, n=1000, method="inclusive")
    p99_ns = cuts[989]
    p999_ns = cuts[998]
    p_min_ns = samples_ns[0]
    p_max_ns = samples_ns[-1]
    mean_ns = statistics.fmean(samples_ns)
    stdev_ns = statistics.pstdev(samples_ns)

    record_property("p50_us", p50_ns / 1000)
    record_property("p99_us", p99_ns / 1000)
    record_property("p999_us", p999_ns / 1000)
    record_property("min_us", p_min_ns / 1000)
    record_property("max_us", p_max_ns / 1000)
    record_property("mean_us", mean_ns / 1000)
    record_property("stdev_us", stdev_ns / 1000)
    record_property("samples", n)
    record_property("dlq_delta", dlq_delta)

    print(
        "\n[7A.2 quantiles] "
        f"n={n} "
        f"p50={p50_ns / 1000:.3f}us (target<{_TARGET_P50_NS / 1000:.1f}us) "
        f"p99={p99_ns / 1000:.3f}us (target<{_TARGET_P99_NS / 1000:.1f}us) "
        f"p999={p999_ns / 1000:.3f}us "
        f"mean={mean_ns / 1000:.3f}us "
        f"stdev={stdev_ns / 1000:.3f}us "
        f"min={p_min_ns / 1000:.3f}us "
        f"max={p_max_ns / 1000:.3f}us "
        f"dlq_delta={dlq_delta}"
    )


@pytest.mark.benchmark(group="cat-7a-2")
def test_protect_failure_path_pytest_benchmark(benchmark: Any) -> None:
    """pytest-benchmark cross-validation report (median / iqr / ops)."""
    _settings_guard_g1()
    _warmup()

    pre_dlq = _dlq_count()

    def _measured() -> None:
        try:
            _call_protect_failing()
        except RuntimeError:
            return

    benchmark(_measured)

    # G6: at least one DLQ entry landed during pytest-benchmark calibration +
    # measurement rounds. Cannot assert exact count because pytest-benchmark
    # invokes the callable an opaque number of times.
    post_dlq = _dlq_count()
    assert post_dlq > pre_dlq, (
        "pytest-benchmark run produced no DLQ entries — DLQSink did not fire"
    )
