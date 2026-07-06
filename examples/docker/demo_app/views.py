"""Demo views for the Baldur Grafana sample stack.

These endpoints feed the Baldur Overview (OSS) dashboard end to end:

- ``/demo/``  — always succeeds. Exercises the circuit breaker (stays CLOSED)
  and the HTTP latency histogram.
- ``/flaky/`` — fails intermittently. ``retry=True`` exercises the retry stage;
  every terminal retry outcome records to ``baldur_retry_outcomes_total`` (the
  Retry Outcomes panel), and sustained failures trip the circuit breaker to OPEN
  and emit error spans that surface in the Recent Traces panel.
- ``/idempotent/`` — drives the IdempotencyGate over a small rotating key set so
  the Idempotency Gate Decisions panel shows continue / skip / abort.
- ``/system-control/`` — cycles the global kill switch (disable -> enable within
  one request, ending enabled) so the System Control panel populates.

Health Check status populates from ``/api/baldur/health/?nocache=true`` (driven
by the traffic script). Graceful Shutdown is intentionally NOT demo-driven — a
SIGTERM mid-scrape is impractical to script; that panel renders on a real
shutdown and is verified by the framework shutdown test.
"""

from __future__ import annotations

import os
import random
import threading
import time

from django.http import HttpRequest, JsonResponse

import baldur
from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.core.idempotency_gate import IdempotencyDecision, IdempotencyGate
from baldur.metrics import get_metrics
from baldur.services.system_control import get_system_control

# Fraction of /flaky/ calls that fail before retries. Tuned high enough that a
# short traffic loop reliably drives retries and trips the circuit breaker.
_FLAKY_FAILURE_RATE = float(os.environ.get("DEMO_FLAKY_FAILURE_RATE", "0.6"))

# A single in-memory gate for the demo. The in-memory adapter provides atomic
# setnx / cas_dict_field, so a real (metered) gate runs — unlike the cache=None
# no-op path, which is deliberately un-metered.
_IDEMPOTENCY_GATE = IdempotencyGate(
    cache=InMemoryCacheAdapter(key_prefix="demo-idem:", cache_name="demo")
)


class FlakyDependencyError(RuntimeError):
    """Simulated downstream fault for the /flaky/ demo endpoint."""


@baldur.protected("demo")
def demo(request: HttpRequest) -> JsonResponse:
    """Happy-path endpoint — always succeeds, keeping the CB CLOSED."""
    return JsonResponse({"status": "ok", "service": "demo"})


@baldur.protected("flaky", retry=True)
def flaky(request: HttpRequest) -> JsonResponse:
    """Intermittently-failing endpoint — drives retries and trips the CB."""
    if random.random() < _FLAKY_FAILURE_RATE:
        raise FlakyDependencyError("simulated downstream failure")
    return JsonResponse({"status": "ok", "service": "flaky"})


@baldur.protected("idempotent")
def idempotent(request: HttpRequest) -> JsonResponse:
    """Drive the IdempotencyGate so its decision counter populates.

    Rotating over a few keys with a mix of complete / leave-executing makes
    repeated traffic surface all three decisions (continue / skip / abort).
    """
    key = f"demo-key-{random.randint(0, 3)}"
    result = _IDEMPOTENCY_GATE.check_and_acquire(key)
    decision = result.decision
    if decision is IdempotencyDecision.CONTINUE:
        # Complete most acquisitions (-> SKIP next time); leave a few executing
        # so a later hit on the same key surfaces ABORT.
        if random.random() < 0.7:
            _IDEMPOTENCY_GATE.mark_completed(key, {"ok": True})
    elif decision is IdempotencyDecision.SKIP:
        # Re-arm the key so it can CONTINUE again on a future hit.
        _IDEMPOTENCY_GATE.release(key)
    return JsonResponse({"decision": decision.value, "key": key})


@baldur.protected("system_control")
def system_control_cycle(request: HttpRequest) -> JsonResponse:
    """Cycle the kill switch so the System Control panel populates.

    Disables then re-enables within the one request — the gauge ends ENABLED
    and the state-change counter advances, while the transient disable does not
    outlast the request (healing stays on for the /demo and /flaky panels).
    """
    ctrl = get_system_control()
    ctrl.disable(actor="demo", reason="demo cycle")
    state = ctrl.enable(actor="demo")
    return JsonResponse({"enabled": state.enabled})


# Domains used to give the DLQ panels a realistic per-domain spread.
_PRO_DLQ_DOMAINS = ("payment", "async_task", "notification", "data_sync")
_pro_round = {"n": 0}


def _drive_emergency(m, n: int) -> None:
    """Emergency Mode — level/active gauges + activation counter."""
    level = ("normal", "warning", "critical")[n % 3]
    m.emergency_mode.set_level(level)
    m.emergency_mode.set_active(level != "normal")
    if level != "normal":
        m.emergency_mode.record_activation(level=level, trigger_type="error_rate")


def _drive_dlq(m, n: int) -> None:
    """DLQ — pending gauge per domain + per-domain items counter."""
    for i, dom in enumerate(_PRO_DLQ_DOMAINS):
        m.dlq.set_pending_count(dom, (i + 1) * 3)
        if random.random() < 0.6:
            m.dlq.record_item_created(domain=dom, failure_type="timeout")


def _drive_throttle(m, n: int) -> None:
    """Adaptive Throttle — limit/denied/saturation + adjustment counters."""
    for svc in ("checkout", "search"):
        m.throttle.record_throttle_metrics(
            service=svc,
            limit=80 + (n % 20),
            rtt_ms=45.0,
            gradient=0.9,
            request_result="denied" if random.random() < 0.3 else "allowed",
            denied_reason="over_limit",
            emergency_level=1,
            cb_state="closed",
            limit_change_direction="down",
            limit_change_trigger="cb",
            limit_change_percent=10.0,
            max_limit=120,
            error_budget_status="warning",
            error_budget_multiplier=0.8,
            error_budget_reduction_active=True,
        )
        m.throttle.record_saturation_metrics(service=svc, limit=80, max_limit=120)


def _drive_notification(m, n: int) -> None:
    """Unified Notification — sent by channel/result + suppressed."""
    for ch in ("slack", "email"):
        m.notification.record_sent(
            channel=ch,
            priority="high",
            result="success" if random.random() < 0.85 else "failure",
        )
    if random.random() < 0.4:
        m.notification.record_suppressed(reason="cooldown")


def _drive_watchdog(m, n: int) -> None:
    """Meta-Watchdog — probe by component/status + recovery + governance-blocked."""
    for comp in ("circuit_breaker", "dlq_consumer", "emergency_mode"):
        m.watchdog.record_probe(
            component=comp, status="ok" if random.random() < 0.85 else "stuck"
        )
    if random.random() < 0.3:
        m.watchdog.record_recovery(
            component="dlq_consumer", action="restart", result="success"
        )
    if random.random() < 0.15:
        m.watchdog.record_governance_blocked(component="emergency_mode")


def _drive_bulkhead(m, n: int) -> None:
    """Bulkhead — utilization gauge + rejection counter."""
    for bh in ("db_pool", "http_pool"):
        mx = 10
        active = random.randint(2, mx)
        m.bulkhead.update_metrics(
            bulkhead_name=bh,
            bulkhead_type="semaphore",
            active_count=active,
            max_concurrent=mx,
            waiting_count=random.randint(0, 3),
        )
        if active >= mx and random.random() < 0.6:
            m.bulkhead.increment_rejected(bulkhead_name=bh)


def _drive_canary(m, n: int) -> None:
    """Canary — rollout lifecycle counters."""
    if n % 4 == 1:
        m.canary.record_rollout_started()
    m.canary.record_stage_advanced(stage_name=("10pct", "50pct", "100pct")[n % 3])
    if n % 4 == 0:
        m.canary.record_rollout_completed()
    if n % 7 == 0:
        m.canary.record_rollback(stage_name="50pct")


_PRO_DRIVERS = (
    ("emergency_mode", _drive_emergency),
    ("dlq", _drive_dlq),
    ("throttle", _drive_throttle),
    ("notification", _drive_notification),
    ("watchdog", _drive_watchdog),
    ("bulkhead", _drive_bulkhead),
    ("canary", _drive_canary),
)


@baldur.protected("pro_demo")
def pro_metrics_demo(request: HttpRequest) -> JsonResponse:
    """Drive the PRO-tier metric recorders so the PRO operations board panels
    populate against the real scrape path.

    Verification-only: registered solely when ``DEMO_SIMULATE_METRICS=1`` (see
    urls.py) so the default OSS demo keeps the PRO panels honestly empty.

    The recorder layer is OSS — only the PRO *services* that normally call these
    recorders are entitlement-gated. Calling the recorders directly emits exactly
    the series + labels a live PRO deployment records, with no behavioral side
    effect on the OSS demo (it sets metric values, it does not activate emergency
    mode, run a canary, etc.). Each request advances one "round" so the counter
    panels build a rate and the gauge panels move.
    """
    m = get_metrics()
    _pro_round["n"] += 1
    n = _pro_round["n"]
    driven = []
    for name, fn in _PRO_DRIVERS:
        try:
            fn(m, n)
            driven.append(name)
        except Exception as exc:  # surface a signature mismatch without 500ing
            driven.append(f"{name}:ERR({type(exc).__name__})")
    return JsonResponse({"round": n, "driven": driven})


# Each phase is HELD longer than the 15s collector scrape interval so at least
# one scrape captures it — otherwise a real shutdown sequence flips through all
# phases in milliseconds and is never observed.
_SHUTDOWN_PHASE_HOLD_SECONDS = 20


def _run_shutdown_sequence(m) -> None:
    """Walk the shutdown recorder through RUNNING->DRAINING->TERMINATING->
    TERMINATED->RUNNING so the Graceful Shutdown panel shows a full drain.

    This drives the OSS shutdown recorder directly rather than issuing a real
    SIGTERM: a real drain stops the Django server from accepting new connections,
    so the Prometheus endpoint the collector scrapes dies mid-drain and the
    phase transition is never observable. Driving the recorder (process stays
    up) lets every phase be scraped, showing exactly what a real drain records.
    """
    from baldur.core.shutdown_coordinator import ShutdownPhase

    rec = m.shutdown
    rec.record_initiated()
    # DRAINING: phase=1, drain in-flight requests incrementally so the drained
    # counter visibly rises across the hold window.
    rec.set_phase(ShutdownPhase.DRAINING)
    drained = 0
    for _ in range(8):
        rec.record_drained(3)
        drained += 3
        time.sleep(_SHUTDOWN_PHASE_HOLD_SECONDS / 8)
    rec.record_aborted(1)  # one request exceeded the drain deadline
    # TERMINATING: phase=2.
    rec.set_phase(ShutdownPhase.TERMINATING)
    time.sleep(_SHUTDOWN_PHASE_HOLD_SECONDS)
    # TERMINATED: phase=3 + the total drain duration. Held a full scrape window
    # too so every phase (not just draining/terminating) lands in a scrape.
    rec.set_phase(ShutdownPhase.TERMINATED)
    rec.record_drain_duration(float(2 * _SHUTDOWN_PHASE_HOLD_SECONDS))
    time.sleep(_SHUTDOWN_PHASE_HOLD_SECONDS)
    # Reset to the healthy baseline so the demo keeps running normally.
    rec.set_phase(ShutdownPhase.RUNNING)


@baldur.protected("shutdown_sim")
def shutdown_sim(request: HttpRequest) -> JsonResponse:
    """Simulate a graceful-shutdown drain so the Graceful Shutdown panel moves.

    Runs the phase walk in a background thread (so the request returns at once
    and the Prometheus endpoint stays scrapeable throughout), then resets to
    RUNNING. See ``_run_shutdown_sequence`` for why this drives the recorder
    instead of a real SIGTERM. Gated identically to ``/pro/``.
    """
    threading.Thread(
        target=_run_shutdown_sequence, args=(get_metrics(),), daemon=True
    ).start()
    total = int(2.5 * _SHUTDOWN_PHASE_HOLD_SECONDS)
    return JsonResponse(
        {
            "status": "shutdown drain simulation started",
            "duration_seconds": total,
            "watch": "Grafana 'Graceful Shutdown' panel: phase 0->1->2->3->0",
        }
    )
