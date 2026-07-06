"""Cat 7A.7 — DLQ outbox producer-side latency micro-benchmark.

Plan ref: `memory/scenario-test-plan-2026-04-12.md` §436 row 7A.7
Targets:  producer p50 < 1 μs, p99 < 10 μs, drop_rate < 1%
Setup:    `outbox.put({...kwargs...})` directly — isolates the producer hot
          path (RingBuffer.put) from the surrounding `_dispatch_to_outbox`
          Python-call overhead. Verifies the #486 docstring claim of
          producer-side ~50-100 ns lock-bounded RingBuffer.put cost.

Why measure `outbox.put` directly rather than the full `protect()` chain
under `BALDUR_DLQ_OUTBOX_ENABLED=true`: the plan target (1 μs / 10 μs) is an
order of magnitude smaller than `protect()`'s composer + RetryPolicy +
PolicyResult overhead (already measured by 7A.2). Only `Outbox.put` itself
matches the structural floor the plan asserts. The wider chain is constant
and well-understood; the formal regression gate must isolate the hot path.

Two complementary measurement paths (matching 7A.1-7A.6 layout):

1. **Manual ns-resolution loop** (`test_outbox_put_producer_quantiles`) —
   `time.perf_counter_ns()` around 8000 inner iterations after a 100-call
   warmup + `flush_and_wait(2s)` drain. Computes p50 / p99 / p999 from the
   raw sample list via `statistics.quantiles(..., n=1000)`.

2. **pytest-benchmark cross-validation**
   (`test_outbox_put_producer_pytest_benchmark`) — standard
   `benchmark(callable)` invocation. Provides median / iqr / ops.

The first run of either test establishes BASELINE per plan §484-488; PASS/FAIL
verdict is recorded by the /scenario harness, not by these tests.

Capacity sizing rationale (G5): measurement count 8000 < default capacity
10000, so even if the worker drains zero entries during the producer's
~8 ms wall-clock window, the buffer never fills and DROP_OLDEST never fires —
giving a deterministic drop_rate=0%. Warmup (100) is drained via
`flush_and_wait(2s)` BEFORE measurement so warmup entries do not crowd the
capacity headroom during the measurement window.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

from baldur.services.dlq_outbox.outbox import OutboxStats, get_outbox
from baldur.settings.dlq_outbox import get_dlq_outbox_settings

# Both benchmarks land entries through DLQService.store_failure, whose
# repository is resolved from baldur_pro — skip the whole module PRO-absent.
pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro

# Plan §436 thresholds (nanoseconds for ns-precision arithmetic).
_TARGET_P50_NS = 1_000  # 1.0 μs
_TARGET_P99_NS = 10_000  # 10.0 μs
_TARGET_DROP_RATE = 0.01  # 1%

_BENCH_NAME = "bench_outbox_producer_7a7"
_WARMUP_ITERATIONS = 100
_MEASURE_ITERATIONS = 8_000


def _build_payload() -> dict[str, Any]:
    """Representative kwargs matching what `_dispatch_to_outbox` would pass.

    The dict identity is reused across all measurement iterations (G3) so
    the per-iter cost is `outbox.put(SAME_DICT)` only — no per-iter dict
    allocation contaminating the sample. Shape mirrors the kwargs that
    `DLQService.store_failure` accepts in the failing-fn case.
    """
    return {
        "domain": _BENCH_NAME,
        "failure_type": "BENCH_OUTBOX_PRODUCER",
        "entity_type": None,
        "entity_id": None,
        "user_id": None,
        "error_code": "BENCH",
        "error_message": "bench_outbox_producer_7a7",
        "snapshot_data": None,
        "request_data": None,
        "response_data": None,
        "metadata": {"bench": "7a7"},
        "next_action_hint": "",
        "recommended_action": "",
    }


def _settings_guard_g1() -> None:
    """G1: confirm DLQOutboxSettings defaults match the documented hot path.

    A prior test that mutated capacity / batch_size / drop_rate_threshold
    would silently invalidate the structural sizing assumptions of G5
    (measurement < capacity) and G7 (loss accounting against the configured
    drop threshold).
    """
    s = get_dlq_outbox_settings()
    assert s.enabled is True, "DLQOutboxSettings.enabled drift detected"
    assert s.capacity == 10_000, f"capacity drift: {s.capacity} != 10000"
    assert s.batch_size == 50, f"batch_size drift: {s.batch_size} != 50"
    assert s.flush_interval_seconds == 0.1, (
        f"flush_interval_seconds drift: {s.flush_interval_seconds} != 0.1"
    )
    assert s.drop_rate_threshold == 0.01, (
        f"drop_rate_threshold drift: {s.drop_rate_threshold} != 0.01"
    )


def _drain_repository_count() -> int:
    """G7 helper: return the live InMemoryFailedOperationRepository count.

    The DLQOutboxWorker default `sync_writer` is
    `DLQService.store_failure(mode='sync', ...)` which lands entries in the
    repository resolved via `resolve_with_fallback` — InMemory in the
    benchmark env (no Redis wired).
    """
    from baldur_pro.services.dlq import get_dlq_service

    return get_dlq_service().repository.count_all()


def test_outbox_put_producer_quantiles(record_property: Any) -> None:
    """Manual ns-resolution quantile capture (authoritative for p50/p99/p999)."""
    _settings_guard_g1()

    # Build outbox + start worker (lazy singleton path; conftest's
    # reset_protect_settings → reset_protect_caches → reset_dlq_outbox
    # already cleared any prior outbox before this test ran).
    outbox = get_outbox()

    payload = _build_payload()

    # G2: hydrate module imports + lazy outbox build cost outside the
    # measurement window, then drain so the warmup entries don't crowd the
    # capacity headroom during measurement (G5).
    for _ in range(_WARMUP_ITERATIONS):
        outbox.put(payload)
    outbox.flush_and_wait(timeout=2.0)

    # Snapshot pre-measurement state so the post-loop accounting subtracts
    # warmup contributions cleanly.
    pre_stats: OutboxStats = outbox.get_stats()
    pre_repo = _drain_repository_count()

    # G8: pre-allocate the sample list with sentinel zeros so an early-loop
    # exit leaves a detectable zero pattern.
    samples_ns: list[int] = [0] * _MEASURE_ITERATIONS
    pc = time.perf_counter_ns  # G4: alias outside the loop

    # Tight loop. Avoid per-iteration attribute lookups.
    for i in range(_MEASURE_ITERATIONS):
        t0 = pc()
        outbox.put(payload)  # G3: SAME dict identity across iterations
        t1 = pc()
        samples_ns[i] = t1 - t0

    # G8: positive attribution that every iteration produced a valid sample.
    assert all(s > 0 for s in samples_ns), (
        "sample list has zero-valued entries — measurement loop exited early"
    )

    # G6: drain the worker. `flush_and_wait` exits on `buffer.size == 0`,
    # but the worker pops entries via `get_batch` (which removes them from
    # the deque immediately) and only increments `entries_written` AFTER
    # `sync_writer(kwargs)` returns — so there is an in-flight window where
    # buffer is empty yet the last batch is still being written to the
    # repository. Poll on the worker-side accounting invariant
    # `total_enqueued == total_dropped + entries_written + entries_failed`
    # which closes that window.
    outbox.flush_and_wait(timeout=30.0)
    g6_deadline = time.monotonic() + 30.0
    while time.monotonic() < g6_deadline:
        s = outbox.get_stats()
        if (
            outbox.buffer.size == 0
            and s.total_enqueued
            == s.total_dropped + s.entries_written + s.entries_failed
        ):
            break
        time.sleep(0.02)

    post_stats: OutboxStats = outbox.get_stats()
    post_repo = _drain_repository_count()

    buffer_size_after_drain = outbox.buffer.size
    assert buffer_size_after_drain == 0, (
        f"buffer.size = {buffer_size_after_drain} after flush_and_wait(30s) — "
        "worker did not drain all enqueued entries within deadline"
    )

    # Drop-rate gate: under G5 sizing (measurement count 8000 < capacity
    # 10000), the buffer never fills during measurement so drops should be 0.
    measurement_enqueued = post_stats.total_enqueued - pre_stats.total_enqueued
    measurement_dropped = post_stats.total_dropped - pre_stats.total_dropped
    drop_rate = (
        measurement_dropped / measurement_enqueued if measurement_enqueued > 0 else 0.0
    )

    # G7: loss accounting — every enqueued entry either drained to repo
    # (entries_written → repo.create), failed in dispatch (entries_failed),
    # or was dropped at enqueue time (DROP_OLDEST). No silent loss.
    accounting_lhs = post_stats.total_enqueued
    accounting_rhs = (
        post_stats.total_dropped
        + post_stats.entries_written
        + post_stats.entries_failed
    )
    assert accounting_lhs == accounting_rhs, (
        f"loss accounting failure: total_enqueued={accounting_lhs} != "
        f"total_dropped({post_stats.total_dropped}) + "
        f"entries_written({post_stats.entries_written}) + "
        f"entries_failed({post_stats.entries_failed}) = {accounting_rhs}"
    )

    # Cross-check: the repository delta should equal entries_written (the
    # worker really wrote each successful entry to the repo).
    repo_delta = post_repo - pre_repo
    measurement_written = post_stats.entries_written - pre_stats.entries_written
    assert repo_delta == measurement_written, (
        f"repo delta {repo_delta} != entries_written delta {measurement_written} "
        "— worker reported writes but repository count disagrees"
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

    record_property("p50_ns", p50_ns)
    record_property("p99_ns", p99_ns)
    record_property("p999_ns", p999_ns)
    record_property("min_ns", p_min_ns)
    record_property("max_ns", p_max_ns)
    record_property("mean_ns", mean_ns)
    record_property("stdev_ns", stdev_ns)
    record_property("samples", n)
    record_property("measurement_enqueued", measurement_enqueued)
    record_property("measurement_dropped", measurement_dropped)
    record_property("drop_rate", drop_rate)
    record_property("repo_delta", repo_delta)

    print(
        "\n[7A.7 quantiles] "
        f"n={n} "
        f"p50={p50_ns}ns (target<{_TARGET_P50_NS}ns) "
        f"p99={p99_ns}ns (target<{_TARGET_P99_NS}ns) "
        f"p999={p999_ns}ns "
        f"mean={mean_ns:.1f}ns "
        f"stdev={stdev_ns:.1f}ns "
        f"min={p_min_ns}ns "
        f"max={p_max_ns}ns "
        f"enq={measurement_enqueued} "
        f"drop={measurement_dropped} "
        f"drop_rate={drop_rate:.4%} (target<{_TARGET_DROP_RATE:.2%}) "
        f"repo_delta={repo_delta}"
    )


@pytest.mark.benchmark(group="cat-7a-7")
def test_outbox_put_producer_pytest_benchmark(benchmark: Any) -> None:
    """pytest-benchmark cross-validation report (median / iqr / ops)."""
    _settings_guard_g1()

    outbox = get_outbox()
    payload = _build_payload()

    # G2: warmup + drain.
    for _ in range(_WARMUP_ITERATIONS):
        outbox.put(payload)
    outbox.flush_and_wait(timeout=2.0)

    pre_repo = _drain_repository_count()

    def _measured() -> None:
        outbox.put(payload)

    benchmark(_measured)

    # Drain so the post-bench accounting can settle. pytest-benchmark
    # invokes `_measured` an opaque number of times, so we cannot assert
    # exact counts — only that drain succeeded and at least one entry
    # made it through to the repository.
    outbox.flush_and_wait(timeout=30.0)
    post_repo = _drain_repository_count()
    assert post_repo > pre_repo, (
        "pytest-benchmark run produced no DLQ entries — outbox.put or worker "
        "drain silently failed"
    )
