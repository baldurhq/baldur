"""Cat 7A.5 — EventBus emit throughput micro-benchmark.

Plan ref: the perf-scenario plan, row 7A.5
Setup:    Emit ``EventType.EMERGENCY_LEVEL_CHANGED`` events with exactly 5
          registered handlers on a fresh ``BaldurEventBus()`` instance,
          measuring per-emit synchronous-dispatch overhead. Plan target
          translated to absolute thresholds (a parameter-sync decision,
          2026-05-07): "10,000 events/sec sustained" =>
          per-emit p50 <= 100 us / p99 <= 200 us / p999 <= 500 us.

Plan rationale ("Verify synchronous dispatch doesn't create backlog")
maps to two correctness gates measured alongside latency:

  - G6: ``bus.get_stats()['handler_timeouts'] == 0`` over the entire
    measurement loop. A non-zero count proves a handler exceeded the
    ``handler_timeout_seconds`` budget and was silently dropped, which is
    the exact failure mode plan asks to falsify.
  - G7: ``sum(_handler_counters) == _MEASURE_ITERATIONS * 5``. Every one
    of the 5 handlers must have fired on every iteration. If a regression
    drops handler invocations (e.g. duplicate-name dedup at subscribe time,
    Thread.start failure, or join-timeout silent skip), G7 catches it
    while wall-clock numbers would still look "fast".

Why fresh ``BaldurEventBus()`` (NOT singleton ``get_event_bus()``):
``register_default_handlers()`` (called only by ``bootstrap.py:380`` in
production startup) wires ~30+ handlers across many EventTypes. The
singleton in a multi-test session would have whatever default handlers
the bootstrap path wired — diverging from plan §435's "5 registered
handlers" specification. Fresh instance guarantees exactly 5 handlers
(verified at G1 via ``len(bus.get_subscriptions(...))==5``) while the
underlying ``BaldurEventBus`` class is identical to what the singleton
returns; the dispatch hot path measured here is byte-identical to
production. Pattern precedent: 12 baldur test files instantiate
``BaldurEventBus()`` directly (e.g. ``tests/unit/services/test_event_bus_unit.py``).

Why default ``handler_timeout_seconds = 5.0`` (production posture, NOT
override to 0): the production hot path goes through
``_execute_handler_with_timeout`` (`event_bus.py:75-113`) which spawns a
``threading.Thread(daemon=True)`` per handler when timeout > 0. The plan
rationale "synchronous dispatch doesn't create backlog" *targets exactly
this code path* — Thread.start()/join() per handler is the structural
source of any potential backlog under sustained 10K/s emit pressure.
Override to 0 would skip the Thread branch entirely (`event_bus.py:82-84`
inline call) and report numbers from a code path no production caller
ever takes. Posture decision recorded as a parameter-sync decision in the
the parameter-sync step.

Two complementary measurement paths (matching 7A.1/7A.2/7A.3/7A.4 layout):

1. **Manual ns-resolution loop** (``test_eventbus_emit_quantiles``) —
   ``time.perf_counter_ns()`` around 10000 inner iterations after a 1000-
   call warmup. Computes p50 / p99 / p999 from the raw sample list via
   ``statistics.quantiles(..., n=1000)``. Records OPS = N / total_wall.

2. **pytest-benchmark cross-validation** (``test_eventbus_emit_pytest_benchmark``)
   — standard ``benchmark(callable)`` invocation. Provides median / iqr / ops.

The first run of either test establishes BASELINE per plan §484-488;
PASS/FAIL verdict is recorded by the scenario harness, not by these
tests.

Why no ``clear_history()`` between warmup and measurement: ``_record_event``
appends to ``_event_history`` and rotates via list slicing once
``len > _max_history`` (default 1000). The rotation cost (O(N) slice on
every emit at steady state) is part of what production callers pay; not
clearing matches the steady-state posture this row is meant to baseline.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.settings.event_bus import get_event_bus_settings, reset_event_bus_settings

# Plan §435 thresholds (microseconds for ns-precision arithmetic).
# Derived from "Emit 10,000 events/sec" via the parameter-sync step
# (Round 2 reuse audit exhausted: no existing throughput baseline in
# services/, core/, adapters/, resilience/, coordination/, audit/, scaling/,
# settings/, tests/, or docs/).
_TARGET_P50_NS = 100_000  # 0.1 ms (10K emits/sec sustainable)
_TARGET_P99_NS = 200_000  # 0.2 ms
_TARGET_P999_NS = 500_000  # 0.5 ms

_HANDLER_COUNT = 5  # plan §435 "5 registered handlers"
_WARMUP_ITERATIONS = 1000
_MEASURE_ITERATIONS = 10_000

# Module-level counters (one slot per handler). Module scope so the two test
# functions can share the helper-defined handlers; reset to 0 at the top of
# each test's setup phase. Distinct __name__ per handler is required —
# ``BaldurEventBus.subscribe`` dedup check at event_bus.py:149-154 keys on
# ``handler.__name__``, so 5 anonymous lambdas would collapse to a single
# subscription and silently violate plan §435.
_handler_counters: list[int] = [0] * _HANDLER_COUNT


def _h0(event: Any) -> None:
    _handler_counters[0] += 1


def _h1(event: Any) -> None:
    _handler_counters[1] += 1


def _h2(event: Any) -> None:
    _handler_counters[2] += 1


def _h3(event: Any) -> None:
    _handler_counters[3] += 1


def _h4(event: Any) -> None:
    _handler_counters[4] += 1


_HANDLERS = (_h0, _h1, _h2, _h3, _h4)


def _settings_guard_g1(bus: BaldurEventBus) -> None:
    """G1: confirm EventBusSettings + bus subscription count match the
    documented hot path.

    Two drift modes silently invalidate the measurement:

    1. ``BALDUR_EVENT_BUS_BACKEND=redis`` would route the singleton through
       ``RedisEventBus``; we instantiate ``BaldurEventBus`` directly so the
       singleton path is bypassed, BUT the assertion documents that this
       row's posture deliberately measures the in-process L1 backend.
    2. ``BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS=0`` from a prior test
       would silently flip the bench to inline path
       (``event_bus.py:82-84``) and falsely report ~10x faster numbers.
       The assertion catches this drift.

    Subscription count assertion guards against a duplicate-dedup bug
    (e.g. two handlers sharing ``__name__`` would collapse to 1).
    """
    s = get_event_bus_settings()
    assert s.backend == "memory", (
        f"EventBusSettings.backend={s.backend!r} drift — benchmark posture "
        "requires in-process memory backend"
    )
    assert s.handler_timeout_seconds == 5.0, (
        f"EventBusSettings.handler_timeout_seconds={s.handler_timeout_seconds} "
        "drift — production default is 5.0; override to 0 would skip the "
        "Thread.spawn branch and measure a code path no production caller takes"
    )
    subs = bus.get_subscriptions(EventType.EMERGENCY_LEVEL_CHANGED)
    assert len(subs) == _HANDLER_COUNT, (
        f"Subscription count = {len(subs)} != {_HANDLER_COUNT} — "
        "duplicate-name dedup or subscribe failure"
    )


def _build_bus() -> BaldurEventBus:
    """Construct a fresh BaldurEventBus + subscribe exactly 5 handlers.

    No ``register_default_handlers()`` invocation — the fresh instance
    has ``_handlers_registered=False`` (event_bus.py:53) and stays that
    way. Subscriber priority defaults to NORMAL for all 5 handlers
    (priority differentiation is not what plan §435 measures).
    """
    bus = BaldurEventBus()
    for handler in _HANDLERS:
        bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)
    return bus


def _warmup(bus: BaldurEventBus) -> None:
    """G2: drive enough emits to:

    1. Hydrate ``_load_max_history`` + ``_load_handler_timeout`` deferred
       imports (one-shot cost: ~200-500 us first call).
    2. Hit steady-state event_history rotation regime
       (``_event_history[-self._max_history:]`` triggers once history
       crosses ``_max_history=1000``).
    3. Pay the one-time threading machinery init cost
       (``threading.Thread`` first construction + GIL warm-up).

    1000 iterations is sufficient: per-call body is ~500 us-2 ms (Thread
    spawn + join × 5 handlers); total warmup ~ 0.5-2 sec.
    """
    for _ in range(_WARMUP_ITERATIONS):
        bus.emit(
            EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": 2, "previous_level": 1},
            source="bench_7a5_warmup",
            priority=EventPriority.HIGH,
        )


def _setup_bus() -> BaldurEventBus:
    """Combined fixture: settings reset + bus build + G1 + warmup + counter
    reset. Counter reset post-warmup ensures G7 measures only the
    measurement-loop iterations."""
    reset_event_bus_settings()
    bus = _build_bus()
    _settings_guard_g1(bus)
    # Reset counters before warmup to keep semantics clean (warmup
    # accumulates _WARMUP_ITERATIONS * 5 = 5000 in each slot, then we
    # zero them before measurement).
    for i in range(_HANDLER_COUNT):
        _handler_counters[i] = 0
    _warmup(bus)
    # Post-warmup reset: G7's positive-attribution sum is scoped to the
    # measurement loop only.
    for i in range(_HANDLER_COUNT):
        _handler_counters[i] = 0
    return bus


def test_eventbus_emit_quantiles(record_property: Any) -> None:
    """Manual ns-resolution quantile capture (authoritative for p50/p99/p999)
    + OPS derivation from total wall-clock."""
    bus = _setup_bus()
    pre_timeouts = bus.get_stats()["handler_timeouts"]

    samples_ns: list[int] = [0] * _MEASURE_ITERATIONS
    pc = time.perf_counter_ns
    emit = bus.emit
    EMERGENCY = EventType.EMERGENCY_LEVEL_CHANGED
    HIGH = EventPriority.HIGH

    wall_start = pc()
    for i in range(_MEASURE_ITERATIONS):
        t0 = pc()
        handlers_called = emit(
            EMERGENCY,
            data={"level": 2, "previous_level": 1},
            source="bench_7a5",
            priority=HIGH,
        )
        t1 = pc()
        samples_ns[i] = t1 - t0
        # G3: every emit must dispatch to all 5 handlers. A regression that
        # drops handlers (e.g. subscribe dedup bug, Thread.start silent
        # failure) would still complete fast and pass quantile thresholds
        # despite missing dispatches.
        assert handlers_called == _HANDLER_COUNT, (
            f"Iteration {i}: bus.emit returned {handlers_called} != "
            f"{_HANDLER_COUNT} — handler invocation count regression"
        )
    wall_end = pc()
    total_wall_ns = wall_end - wall_start

    # G6: handler_timeouts MUST stay 0 across the entire measurement loop.
    # A non-zero delta proves at least one handler exceeded the
    # handler_timeout_seconds budget; the timeout-promoted handler was
    # silently skipped while wall clock continued. This is the exact
    # backlog symptom plan §435 asks to falsify.
    post_timeouts = bus.get_stats()["handler_timeouts"]
    timeout_delta = post_timeouts - pre_timeouts
    assert timeout_delta == 0, (
        f"handler_timeouts increased by {timeout_delta} during measurement — "
        "synchronous dispatch failed to keep up with emit cadence"
    )

    # G7: every handler ran on every iteration. THE positive-attribution
    # proof — "all dispatched, queue depth bounded" is the row's value
    # proposition (plan §435 pass criteria).
    expected_total = _MEASURE_ITERATIONS * _HANDLER_COUNT
    actual_total = sum(_handler_counters)
    assert actual_total == expected_total, (
        f"Total handler invocations = {actual_total} != {expected_total} — "
        f"per-handler counts: {_handler_counters}"
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
    ops_per_sec = (_MEASURE_ITERATIONS * 1_000_000_000.0) / total_wall_ns

    record_property("p50_us", p50_ns / 1000)
    record_property("p99_us", p99_ns / 1000)
    record_property("p999_us", p999_ns / 1000)
    record_property("min_us", p_min_ns / 1000)
    record_property("max_us", p_max_ns / 1000)
    record_property("mean_us", mean_ns / 1000)
    record_property("stdev_us", stdev_ns / 1000)
    record_property("samples", n)
    record_property("ops_per_sec", ops_per_sec)
    record_property("total_handler_calls", actual_total)
    record_property("expected_handler_calls", expected_total)
    record_property("handler_timeouts", timeout_delta)

    print(
        "\n[7A.5 quantiles] "
        f"n={n} "
        f"p50={p50_ns / 1000:.3f}us (target<{_TARGET_P50_NS / 1000:.2f}us) "
        f"p99={p99_ns / 1000:.3f}us (target<{_TARGET_P99_NS / 1000:.2f}us) "
        f"p999={p999_ns / 1000:.3f}us (target<{_TARGET_P999_NS / 1000:.2f}us) "
        f"mean={mean_ns / 1000:.3f}us "
        f"stdev={stdev_ns / 1000:.3f}us "
        f"min={p_min_ns / 1000:.3f}us "
        f"max={p_max_ns / 1000:.3f}us "
        f"ops={ops_per_sec:.0f}/s (target>=10000/s) "
        f"handlers={actual_total}/{expected_total} timeouts={timeout_delta}"
    )


@pytest.mark.benchmark(group="cat-7a-5")
def test_eventbus_emit_pytest_benchmark(benchmark: Any) -> None:
    """pytest-benchmark cross-validation report (median / iqr / ops)."""
    bus = _setup_bus()
    pre_timeouts = bus.get_stats()["handler_timeouts"]
    pre_total = sum(_handler_counters)
    EMERGENCY = EventType.EMERGENCY_LEVEL_CHANGED
    HIGH = EventPriority.HIGH

    def _measured() -> None:
        bus.emit(
            EMERGENCY,
            data={"level": 2, "previous_level": 1},
            source="bench_7a5",
            priority=HIGH,
        )

    benchmark(_measured)

    # G6 over the pytest-benchmark run.
    post_timeouts = bus.get_stats()["handler_timeouts"]
    assert post_timeouts == pre_timeouts, (
        f"handler_timeouts increased during pytest-benchmark run "
        f"({post_timeouts - pre_timeouts}) — backlog symptom"
    )

    # G7 over the pytest-benchmark run. The plugin invokes the callable an
    # opaque number of times, so we cannot assert exact count, but every
    # invocation must dispatch all 5 handlers (post_total - pre_total must
    # be a clean multiple of 5).
    post_total = sum(_handler_counters)
    delta = post_total - pre_total
    assert delta > 0, "pytest-benchmark did not invoke the callable"
    assert delta % _HANDLER_COUNT == 0, (
        f"Total handler delta {delta} is not a multiple of {_HANDLER_COUNT} — "
        "at least one emit dispatched fewer than 5 handlers"
    )
