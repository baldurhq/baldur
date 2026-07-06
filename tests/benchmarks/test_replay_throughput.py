"""Cat 7C.2 — DLQ replay throughput macro-benchmark.

Plan ref: `memory/scenario-test-plan-2026-04-12.md` §505 row 7C.2
Target:   Replay rate >= 1000 ops/sec (framework-rate ceiling).
Setup:    Pre-load N=100_000 PENDING entries into a fresh
          `InMemoryFailedOperationRepository`; register a no-op success
          handler for the bench domain; iterate `replay_batch(max_items=500)`
          until the queue drains; measure `ops_per_sec = N / total_wall`.

Why in-process and not Locust HTTP (deviates from default 7C lane)
==================================================================
Cat 7C.1 measured ~151 ops/sec via the production `xtest/replay/batch/`
HTTP endpoint (see `scenario-results/7C/7C.1-zero-loss-guarantee.md`
verdict) and explicitly recommended 7C.2 "measure rate in-process to
avoid the HTTP-orchestrator artifact." Plan §505's "1000 ops/sec"
target is a FRAMEWORK-rate ceiling claim — HTTP RTT (~50 ms/batch)
would false-fail the gate by ~7x without measuring framework cost.
The 7A.x suite (7A.1-7A.7) already establishes the in-process
`time.perf_counter_ns() + samples_ns list + statistics.quantiles +
ops_per_sec` scaffold; 7C.2 plugs into that convention at macro scale.
Decision recorded in /scenario Stage 3 Section 2 Gap #1 sync.

Why InMemoryFailedOperationRepository (not Redis-backed)
========================================================
Redis-backed throughput adds network RTT (~500-1000 us/op) that
dominates over framework cost; the published claim "rate >= 1000
ops/sec" only makes sense as a framework ceiling. Redis-backed
throughput is deferred to a production-cluster benchmark per 7C.1
Gap #2 OOS precedent. Decision recorded in Stage 3 Section 2 Gap #2.

Why a no-op success handler (not domain-realistic)
==================================================
A DB-bound handler would measure customer-application cost (ORM,
network), not framework. Plan §505's "rate" claim implies the
framework-ceiling for the SUCCESS path. Local `NoOpSuccessReplayHandler`
mirrors the precedent at `tests/unit/services/test_replay_service_unit.py:52`
(`FakeReplayHandler(success=True)`) — re-defined locally rather than
cross-test-dir imported. Decision recorded in Stage 3 Section 2 Gap #3.

Why N=100_000 and not the plan-cited 1_000_000
==============================================
Rate is steady-state; 100K is sufficient sample size and fits ~200 MB
heap. At 1000 ops/sec floor the drain takes ~100 s — fast iteration.
Plan §505's absolute "1M < 17 min" assertion is the rate floor's
linear extrapolation: `1_000_000 / 1000 ops/s = 1000 s = 16.67 min`.
1M-direct measurement deferred to production benchmark per 7C.1 Gap #2
precedent. Decision recorded in Stage 3 Section 2 Gap #4.

Structural guards (G1-G9) — see Section 4 of the result file
============================================================
G1/G2 (handler registration), G3 (explicit DI of in-memory repo),
G4/G5 (pre/post count assertions), G6 (single-threaded drain),
G7 (no idempotency-gate patching — fail-open path exercised),
G8 (EventBus subscribers NOT registered for DLQ_REPLAY_COMPLETED),
G9 (warmup absorbed in measurement window — no separate exclusion).
"""

from __future__ import annotations

import time
from typing import Any

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationData
from baldur.services.event_bus.bus.convenience import reset_event_bus
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.replay_service.service import ReplayService

# Plan §505 framework-rate ceiling.
_TARGET_OPS_PER_SEC = 1000.0

_BENCH_DOMAIN = "bench.replay_throughput"
_BENCH_FAILURE_TYPE = "bench"
_PRELOAD_COUNT = 100_000
_BATCH_MAX_ITEMS = 500
_MAX_BATCH_ITERATIONS = 500  # safety cap; 100_000 / 500 = 200 expected


class NoOpSuccessReplayHandler(ReplayHandler):
    """Trivial success handler for framework-rate measurement.

    Mirrors the shape of `FakeReplayHandler(success=True)` at
    `tests/unit/services/test_replay_service_unit.py:52`; reproduced
    locally to avoid cross-test-dir imports.
    """

    def __init__(self, domain_name: str):
        self._domain = domain_name

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "bench")


def _g1_handler_registered(repo_domain: str) -> None:
    """G1+G2: confirm a non-default success handler is registered for the
    bench domain BEFORE the drain begins.

    If `get_replay_handler(bench_domain)` returns `DefaultReplayHandler`
    (the always-failing fallback), each `_execute_replay` exercises the
    failure path — additional logging + status-revert + different metric
    branch — and the ops/sec number measures a code path no successful
    customer drain takes.
    """
    handler = _replay_handlers.get(repo_domain)
    assert handler is not None, (
        f"NoOpSuccessReplayHandler not registered for domain={repo_domain!r}"
    )
    assert isinstance(handler, NoOpSuccessReplayHandler), (
        f"Registered handler for {repo_domain!r} is "
        f"{type(handler).__name__}, expected NoOpSuccessReplayHandler"
    )


def _g3_explicit_di(
    service: ReplayService, repo: InMemoryFailedOperationRepository
) -> None:
    """G3: confirm the service is using the explicit in-memory repo, not
    a ProviderRegistry-resolved Redis/Django backend.

    `ReplayService.repository` returns the injected `_repository` when
    present; if it were `None`, `resolve_with_fallback` could return a
    different backend depending on what other tests have configured. The
    benchmark's ops/sec claim is only meaningful against the in-memory
    backend per Stage 3 Section 2 Gap #2.
    """
    assert service._repository is repo, (
        "ReplayService._repository != injected in-memory repo — "
        "ProviderRegistry fallback would change the measurement backend"
    )
    assert isinstance(service.repository, InMemoryFailedOperationRepository), (
        f"service.repository type = {type(service.repository).__name__}, "
        "expected InMemoryFailedOperationRepository"
    )


def _preload_dlq_entries(
    repo: InMemoryFailedOperationRepository, n: int, domain: str
) -> None:
    """Insert n PENDING entries into the repository. Uses the public
    `create()` API directly — no chained builder overhead, since the
    pre-load latency is NOT part of the measurement window."""
    for _ in range(n):
        repo.create(
            domain=domain,
            failure_type=_BENCH_FAILURE_TYPE,
            error_message="bench",
            metadata={"bench": True},
        )


def _g4_preload_count(
    repo: InMemoryFailedOperationRepository, n: int, domain: str
) -> None:
    """G4: confirm exactly n PENDING entries exist for the bench domain.

    If `create()` short-stopped due to an exception, the rate would be
    computed against the wrong denominator (N total wall but N - k
    actually drained).
    """
    pending = repo.get_pending_count_by_domain(domain)
    assert pending == n, (
        f"Pre-load count = {pending} != expected {n} — denominator drift"
    )


def _drain(service: ReplayService, domain: str) -> tuple[int, int, int, int]:
    """Iterate `replay_batch(max_items=_BATCH_MAX_ITEMS)` until the queue
    drains. Returns (sum_total, sum_success, sum_failed, batch_count).

    Hard cap at _MAX_BATCH_ITERATIONS prevents an infinite loop if the
    drain stalls (e.g., entries flip back to PENDING after failure path).
    """
    sum_total = 0
    sum_success = 0
    sum_failed = 0
    batch_count = 0
    for _ in range(_MAX_BATCH_ITERATIONS):
        result = service.replay_batch(domain=domain, max_items=_BATCH_MAX_ITEMS)
        batch_count += 1
        if result.total == 0:
            break
        sum_total += result.total
        sum_success += result.success_count
        sum_failed += result.failed_count
    return sum_total, sum_success, sum_failed, batch_count


def _g5_post_drain_zero(repo: InMemoryFailedOperationRepository, domain: str) -> None:
    """G5: confirm 0 PENDING entries remain for the bench domain.

    If residual PENDING entries remain (e.g., max_replay_attempts cap
    hit, or handler returned failure flipping status back), the drain is
    partial and `ops_per_sec` over the full N would be inflated.
    """
    remaining = repo.get_pending_count_by_domain(domain)
    assert remaining == 0, (
        f"Post-drain pending count = {remaining}, expected 0 — partial drain"
    )


def test_replay_throughput_in_process(record_property: Any) -> None:
    """Framework-rate ceiling: N=100_000 drain via iterated `replay_batch`
    on InMemoryFailedOperationRepository + no-op success handler. Assert
    `ops_per_sec >= 1000` per plan §505."""
    # EventBus singleton: reset to drop any subscribers from prior tests.
    # G8: production `register_default_handlers()` is called only by
    # bootstrap; in pytest it does NOT auto-fire, so the singleton has no
    # subscribers for DLQ_REPLAY_COMPLETED. Resetting makes this
    # reproducible even if a prior test installed subscribers.
    reset_event_bus()

    repo = InMemoryFailedOperationRepository()
    service = ReplayService(repository=repo)

    _replay_handlers.clear()
    handler = NoOpSuccessReplayHandler(_BENCH_DOMAIN)
    register_replay_handler(handler)
    _g1_handler_registered(_BENCH_DOMAIN)
    _g3_explicit_di(service, repo)

    _preload_dlq_entries(repo, _PRELOAD_COUNT, _BENCH_DOMAIN)
    _g4_preload_count(repo, _PRELOAD_COUNT, _BENCH_DOMAIN)

    pc = time.perf_counter_ns
    wall_start = pc()
    sum_total, sum_success, sum_failed, batch_count = _drain(service, _BENCH_DOMAIN)
    total_wall_ns = pc() - wall_start

    _g5_post_drain_zero(repo, _BENCH_DOMAIN)

    # Headline: ops/sec over the full N.
    ops_per_sec = (_PRELOAD_COUNT * 1_000_000_000.0) / total_wall_ns
    avg_us_per_entry = total_wall_ns / _PRELOAD_COUNT / 1000.0
    total_wall_s = total_wall_ns / 1_000_000_000.0
    avg_batch_size = sum_total / batch_count if batch_count else 0.0

    record_property("ops_per_sec", ops_per_sec)
    record_property("total_wall_s", total_wall_s)
    record_property("preload_count", _PRELOAD_COUNT)
    record_property("sum_total", sum_total)
    record_property("sum_success", sum_success)
    record_property("sum_failed", sum_failed)
    record_property("batch_count", batch_count)
    record_property("avg_us_per_entry", avg_us_per_entry)
    record_property("avg_batch_size", avg_batch_size)
    record_property("target_ops_per_sec", _TARGET_OPS_PER_SEC)

    # Extrapolated 1M wall-clock (plan §505 "1M < 17 min" check).
    extrapolated_1m_s = 1_000_000.0 / ops_per_sec
    extrapolated_1m_min = extrapolated_1m_s / 60.0
    record_property("extrapolated_1m_minutes", extrapolated_1m_min)

    print(
        "\n[7C.2 replay throughput] "
        f"N={_PRELOAD_COUNT} "
        f"ops_per_sec={ops_per_sec:.1f} (target>={_TARGET_OPS_PER_SEC:.0f}) "
        f"wall={total_wall_s:.2f}s "
        f"batch_count={batch_count} avg_batch={avg_batch_size:.1f} "
        f"avg_us_per_entry={avg_us_per_entry:.1f}us "
        f"sum_total={sum_total} success={sum_success} failed={sum_failed} "
        f"extrapolated_1M={extrapolated_1m_min:.2f}min (plan<17min)"
    )

    # G4 cross-check: every pre-loaded entry was processed.
    assert sum_total == _PRELOAD_COUNT, (
        f"sum_total={sum_total} != N={_PRELOAD_COUNT} — drain accounted "
        "for the wrong number of entries"
    )
    assert sum_success == _PRELOAD_COUNT, (
        f"sum_success={sum_success} != N={_PRELOAD_COUNT} — failure path "
        "exercised; framework SUCCESS rate not isolated"
    )
    assert sum_failed == 0, (
        f"sum_failed={sum_failed} > 0 — non-success path entered, "
        "measurement no longer aligns with plan §505 success-rate claim"
    )

    # Headline gate.
    assert ops_per_sec >= _TARGET_OPS_PER_SEC, (
        f"ops_per_sec={ops_per_sec:.1f} < target {_TARGET_OPS_PER_SEC:.0f}"
    )

    # Plan §505 1M extrapolation gate (informational; the 1M direct
    # measurement is deferred to production benchmark per 7C.1 Gap #2).
    assert extrapolated_1m_min < 17.0, (
        f"Extrapolated 1M drain wall-clock = {extrapolated_1m_min:.2f} min, "
        "exceeds plan §505 'under 17 min' floor — rate dropped below "
        "1000 ops/sec headline"
    )

    # Handler registry cleanup.
    _replay_handlers.clear()
