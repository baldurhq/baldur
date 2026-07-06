"""Cat 7A.3 — `protect()` CB-reject-path (fast-fail) overhead micro-benchmark.

Plan ref: `memory/scenario-test-plan-2026-04-12.md` §433 row 7A.3
Targets:  p50 < 0.05 ms, p99 < 0.2 ms
Setup:    `protect(name, fn)` with default kwargs after the named CB has been
          forced to OPEN via the documented operator API
          (`ManualControlMixin.force_open`). The protect call goes through
          `CircuitBreakerPolicy.execute` → `should_allow` returns False (OPEN
          with elapsed < recovery_timeout) → the policy builds a
          `PolicyResult(REJECTED, error=CircuitBreakerOpenError)` and runs
          `on_reject` hooks; `_finalize_value` re-raises the
          ``CircuitBreakerOpenError`` to the caller. The wrapped function
          MUST NOT be invoked — that is the whole point of "fast-fail" and
          the value proposition this row exists to measure.

Two complementary measurement paths (matching 7A.1/7A.2 layout):

1. **Manual ns-resolution loop** (`test_protect_cb_reject_path_quantiles`) —
   `time.perf_counter_ns()` around 10000 inner iterations after a 1000-call
   warmup. Computes p50 / p99 / p999 from the raw sample list via
   `statistics.quantiles(..., n=1000)`.

2. **pytest-benchmark cross-validation** (`test_protect_cb_reject_path_pytest_benchmark`) —
   standard `benchmark(callable)` invocation. Provides median / iqr / ops.

The first run of either test establishes BASELINE per plan §484-488; PASS/FAIL
verdict is recorded by the /scenario harness, not by these tests.

Why force_open (and not "trigger N failures to auto-trip"): the plan setup
says "CB already OPEN" — auto-trip would mix `record_failure` cost into setup
and is not what this row measures. force_open is the documented operator API
(`manual_control.py:58`) and lands the same OPEN+opened_at state-machine
posture that auto-trip would.

Why InMemoryCircuitBreakerStateRepository is pinned (NOT the default
``LayeredRepository`` resolution): the default resolution would route through
the layered Redis adapter even when no Redis is wired in the benchmark env
(`adapters/redis/circuit_breaker.py:try_acquire_half_open_slot` fails with
``AttributeError("'NoneType' object has no attribute '_redis'")``), and
async EventBus dispatches to the same Redis backend block warmup with
~4-second kombu OperationalError retries that push test wall-clock past the
default ``recovery_timeout=60s`` mid-warmup, allowing OPEN→HALF_OPEN
auto-transition that contaminates the measurement. Pinning InMemory for the
benchmark posture is the simplest production-realistic guard: a real
deployment with InMemory CB is a valid configuration (OSS-tier default per
plan §492), so this is not a synthetic shortcut. We additionally bump
``recovery_timeout=86400`` so the OPEN window dominates the test wall-clock
by 4-5 orders of magnitude — G6 confirms post-loop.

Why ``hooks=[]`` on the pinned policy: post-#494 the production default for
``CircuitBreakerPolicy(...).hooks`` is also ``[]`` (transition-only — per-reject
hook bodies were deleted along with ``AuditPolicyHook``/``EventBusPolicyHook``),
so the benchmark posture matches production exactly. The kombu-retry rationale
that previously motivated ``hooks=[]`` survives only as a note for **external**
callers that still inject custom EventBus-emitting hooks: in a no-Redis
benchmark env those custom hooks would push warmup wall-clock past
``recovery_timeout`` via ~4 s kombu OperationalError retries per iteration. The
default-kwargs ``protect()`` path no longer has that exposure. 7A.3's scope, per
plan §433 rationale "OPEN CB must be CHEAPER than executing the function", is
the **reject decision path** itself: ``should_allow`` returning False +
``PolicyResult(REJECTED, …)`` build + ``_finalize_value`` re-raise. F1 (T3)
candidate: re-evaluate threshold once 7A.5 (EventBus emit throughput) baseline
lands so a future "full reject chain incl. custom hooks" row can either
renegotiate against measured EventBus cost or live alongside as a separate
benchmark.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
from baldur.protect_facade import _cb_policy_cache, _cb_policy_lock, protect
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.settings.protect import get_protect_settings

# Plan §433 thresholds (microseconds for ns-precision arithmetic).
_TARGET_P50_NS = 50_000  # 0.05 ms
_TARGET_P99_NS = 200_000  # 0.2 ms

_BENCH_NAME = "bench_reject_7a3"
_WARMUP_ITERATIONS = 1000
_MEASURE_ITERATIONS = 10_000

# Module-level call counter for the wrapped fn. G7 asserts this stays 0
# across warmup + measurement. A nonlocal counter would be lost between the
# two test functions in the same module; module-level is intentional so a
# regression that flips `should_allow` to True in EITHER test would be caught
# by the next test's G7 assertion.
_fn_call_count: int = 0


def _never_called() -> None:
    """Wrapped fn that MUST NOT execute when CB is OPEN.

    G7's positive-attribution guard: this counter must remain 0 across all
    11000 (warmup + measurement) iterations. If it ever increments, the
    library's "OPEN CB rejects without running fn" contract is broken.
    """
    global _fn_call_count
    _fn_call_count += 1
    raise AssertionError(
        "CB OPEN reject path invoked the wrapped fn — value proposition violated"
    )


def _settings_guard_g1() -> None:
    """G1: confirm ProtectSettings defaults match the documented hot path.

    If a prior test mutated `BALDUR_PROTECT_ENABLED=false`, `protect()` would
    short-circuit to bare `fn()` (`protect.py:553-554`); `_never_called`
    would raise AssertionError and we would falsely report a "fast" path
    while not actually measuring CB reject.
    """
    s = get_protect_settings()
    assert s.enabled is True, "ProtectSettings.enabled drift detected"
    assert s.default_circuit_breaker is True, "default_circuit_breaker drift"
    assert s.default_retry is False, "default_retry drift"
    assert s.default_dlq is False, "default_dlq drift"
    assert s.default_timeout_seconds is None, "default_timeout_seconds drift"


def _install_pinned_cb_policy() -> CircuitBreakerPolicy:
    """Pre-install a CircuitBreakerPolicy with InMemory repo + bumped
    recovery_timeout into ``_cb_policy_cache``, BEFORE the first ``protect()``
    call that would otherwise build the default (LayeredRepository-backed)
    policy via ``_get_or_build_cb_policy``.

    Two material deviations from the default cached policy, both required to
    measure the CB reject path cleanly in a no-Redis benchmark env:

    1. ``InMemoryCircuitBreakerStateRepository`` instead of LayeredRepository.
       The default ``LayeredRepository`` resolves through Redis on
       ``try_acquire_half_open_slot``; without a wired Redis backend, the
       call fails with ``AttributeError("'NoneType' object has no attribute
       '_redis'")`` and async EventBus dispatches incur ~4-second kombu
       retries that bloat warmup wall-clock. InMemory is a documented
       OSS-tier valid configuration (plan §492), not a synthetic shortcut.
    2. ``recovery_timeout=86400`` (1 day) instead of the default 60s. The
       OPEN→HALF_OPEN auto-transition fires when ``elapsed >=
       recovery_timeout`` (`service.py:271`); 1 day is far longer than any
       conceivable test wall-clock. G6 confirms post-loop.

    The cache pre-population must occur INSIDE ``_cb_policy_lock`` so it does
    not race with a concurrent ``_get_or_build_cb_policy`` call from another
    test in the same session.
    """
    repo = InMemoryCircuitBreakerStateRepository()
    config = CircuitBreakerConfig(enabled=True, recovery_timeout=86_400)
    cb_service = CircuitBreakerService(config=config, repository=repo)
    policy = CircuitBreakerPolicy(
        service_name=_BENCH_NAME,
        cb_service=cb_service,
        hooks=[],  # See module docstring — hook chain explicitly out of scope.
    )
    with _cb_policy_lock:
        _cb_policy_cache[_BENCH_NAME] = policy
    return policy


def _force_cb_open() -> CircuitBreakerPolicy:
    """Pin the cached policy + drive its CB service to OPEN state.

    The pinned policy uses InMemory + recovery_timeout=86400 (see
    ``_install_pinned_cb_policy``). ``force_open`` writes state=OPEN +
    opened_at=now via ``repository.atomic_force_open``; the bumped recovery
    timeout means ``should_allow`` will return False on every measurement
    iteration without flipping to HALF_OPEN. G6 confirms post-loop.
    """
    policy = _install_pinned_cb_policy()
    policy.cb_service.force_open(_BENCH_NAME, reason="bench_7a3_setup")
    return policy


def _warmup() -> None:
    """G2: drive enough reject iterations to hydrate cached
    CircuitBreakerPolicy + cached default-composer + Prometheus blocked-metric
    deferred imports + on_reject hook chain before measurement."""
    for _ in range(_WARMUP_ITERATIONS):
        try:
            protect(_BENCH_NAME, _never_called)
        except CircuitBreakerOpenError:
            pass


def _setup_open_state() -> None:
    """Combined fixture: settings guard + force OPEN + warmup. Resets the
    module-level call counter so each test's G7 assertion measures only its
    own iterations."""
    global _fn_call_count
    _settings_guard_g1()
    _force_cb_open()
    _fn_call_count = 0
    _warmup()
    # Reset again post-warmup so measurement-loop assertion is scoped to the
    # measurement loop only.
    _fn_call_count = 0


def test_protect_cb_reject_path_quantiles(record_property: Any) -> None:
    """Manual ns-resolution quantile capture (authoritative for p50/p99/p999)."""
    _setup_open_state()

    samples_ns: list[int] = [0] * _MEASURE_ITERATIONS
    pc = time.perf_counter_ns

    # Tight loop. Avoid per-iteration attribute lookups.
    for i in range(_MEASURE_ITERATIONS):
        rejected_with: type[BaseException] | None = None
        t0 = pc()
        try:
            protect(_BENCH_NAME, _never_called)
        except CircuitBreakerOpenError:
            rejected_with = CircuitBreakerOpenError
        except BaseException as e:  # pragma: no cover — guard, never expected
            rejected_with = type(e)
            t1 = pc()
            samples_ns[i] = t1 - t0
            raise AssertionError(
                f"Iteration {i}: expected CircuitBreakerOpenError, got "
                f"{rejected_with.__name__}: {e!r}"
            ) from e
        t1 = pc()
        samples_ns[i] = t1 - t0
        # G3: rejection MUST be CircuitBreakerOpenError specifically. A
        # regression that returns None silently or raises a different
        # exception would still complete fast and pass latency thresholds.
        assert rejected_with is CircuitBreakerOpenError

    # G6: OPEN state did NOT flip to HALF_OPEN mid-test. Without this, an
    # opened_at-drift bug or a recovery_timeout-config drift would route some
    # iterations through the HALF_OPEN-acquire branch (Lua/RLock cost) and
    # contaminate the reject-path measurement.
    policy = _cb_policy_cache[_BENCH_NAME]
    final_state = policy.cb_service.get_state(_BENCH_NAME)
    assert final_state == "open", (
        f"CB state drifted to {final_state!r} during measurement — "
        "reject path was not exercised consistently across iterations"
    )

    # G7: fn was NEVER called across the entire measurement loop. THE positive
    # attribution proof — "OPEN CB rejects without running fn" is the row's
    # value proposition (plan §433 rationale).
    assert _fn_call_count == 0, (
        f"_never_called was invoked {_fn_call_count} times during "
        "measurement — CB OPEN failed to short-circuit fn execution"
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
    record_property("fn_call_count", _fn_call_count)
    record_property("final_cb_state", final_state)

    print(
        "\n[7A.3 quantiles] "
        f"n={n} "
        f"p50={p50_ns / 1000:.3f}us (target<{_TARGET_P50_NS / 1000:.2f}us) "
        f"p99={p99_ns / 1000:.3f}us (target<{_TARGET_P99_NS / 1000:.2f}us) "
        f"p999={p999_ns / 1000:.3f}us "
        f"mean={mean_ns / 1000:.3f}us "
        f"stdev={stdev_ns / 1000:.3f}us "
        f"min={p_min_ns / 1000:.3f}us "
        f"max={p_max_ns / 1000:.3f}us "
        f"fn_calls={_fn_call_count} state={final_state}"
    )


@pytest.mark.benchmark(group="cat-7a-3")
def test_protect_cb_reject_path_pytest_benchmark(benchmark: Any) -> None:
    """pytest-benchmark cross-validation report (median / iqr / ops)."""
    _setup_open_state()

    pre_count = _fn_call_count

    def _measured() -> None:
        try:
            protect(_BENCH_NAME, _never_called)
        except CircuitBreakerOpenError:
            return

    benchmark(_measured)

    # G7 over the pytest-benchmark run (calibration + measurement rounds).
    # pytest-benchmark invokes the callable an opaque number of times so we
    # cannot assert exact count, but the wrapped fn must remain unexecuted.
    assert _fn_call_count == pre_count, (
        f"_never_called was invoked {_fn_call_count - pre_count} times "
        "during pytest-benchmark run — CB OPEN failed to short-circuit"
    )

    # G6: state stayed OPEN throughout.
    policy = _cb_policy_cache[_BENCH_NAME]
    assert policy.cb_service.get_state(_BENCH_NAME) == "open"
