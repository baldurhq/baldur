"""Self-healing demo: a dependency dies mid-traffic, nothing is lost.

Runs entirely in this process — no Redis, no database, no message broker::

    pip install "baldur-framework[celery]"
    python -m baldur.scripts.demo_self_healing

What it shows:

1. ``charge()`` is protected by ``@dlq_protect``: circuit breaker, retry,
   and DLQ capture composed by one decorator.
2. The fake payment gateway goes down. Every failed charge is captured with
   its arguments; after enough failures the circuit breaker opens and starts
   rejecting instantly instead of piling onto the dying dependency.
3. The gateway comes back. The breaker probes, closes, and the CLOSED event
   automatically replays every captured charge through the registered replay
   handler. The summary at the end is computed from what actually happened.

The replay wiring this demo performs — an eager Celery app, a replay handler
for its domain, and the failure-type routing map — is the same wiring a real
deployment does; only the eager Celery app stands in for a real worker.

Set ``BALDUR_DEMO_VERBOSE=1`` to also see the framework's own structured log
events instead of the quiet demo narrative alone.
"""

from __future__ import annotations

import logging
import os
import sys
import time

# Demo-scale tuning so the whole story fits in ~30 seconds. Every knob is a
# documented BALDUR_* setting; applied with setdefault so explicit env wins.
_DEMO_ENV = {
    "BALDUR_ENVIRONMENT": "development",
    "BALDUR_OBSERVABILITY_PROFILE": "local",
    "BALDUR_CB_FAILURE_THRESHOLD": "5",
    "BALDUR_CB_RECOVERY_TIMEOUT": "3",
    "BALDUR_RETRY_MAX_ATTEMPTS": "2",
    "BALDUR_RETRY_BASE_DELAY": "0.2",
    # Keep retry/backoff ceilings inside the shortened breaker window — the
    # settings conflict detector flags the defaults against a 3s recovery
    # timeout (see the backoff-cb-timeout and retry-cb-timeout runbooks).
    "BALDUR_RETRY_MAX_DELAY": "10",
    "BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY": "10",
    # Opt-in routing: which captured failure types auto-replay for the demo
    # domain when its circuit closes. Empty by default in the framework —
    # auto-re-running business operations is always an explicit decision.
    "BALDUR_REPLAY_AUTOMATION_SERVICE_FAILURE_TYPE_MAP": (
        '{"demo.charge": ["MAX_RETRIES_GATEWAYDOWNERROR"]}'
    ),
}

_DOMAIN = "demo.charge"
_OUTBOX_FLUSH_WAIT_S = 8.0  # capture is async-durable; store visibility follows
_REPLAY_WAIT_S = 10.0

_USE_COLOR = sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))


def _c(code: str) -> str:
    return f"\x1b[{code}m" if _USE_COLOR else ""


R, DIM, BOLD = _c("0"), _c("2"), _c("1")
GREEN, RED, YELLOW, MAGENTA = _c("1;32"), _c("1;31"), _c("33"), _c("35")

_CB_COLOR = {"closed": GREEN, "open": RED, "half_open": YELLOW}


class GatewayDownError(Exception):
    """Raised by the fake payment gateway while it is down."""


def _say(line: str = "") -> None:
    print(line, flush=True)


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _quiet_logging() -> None:
    """Keep the narrative readable; ``BALDUR_DEMO_VERBOSE=1`` shows it all.

    ``logging.disable`` is a process-wide floor, so it holds regardless of
    the per-logger levels the framework wires during ``init()``. A few lanes
    go fully dark: the eager Celery app runs Django- and PRO-coupled
    housekeeping tasks inline in this plain process, and their
    expected-absence errors are unrelated to the story demonstrated.
    """
    logging.disable(logging.WARNING)
    for name in (
        "baldur.celery_tasks",
        "baldur.services.cleanup_service",
        # The OSS log-channel notifier announces CB transitions loudly
        # (by design); the demo narrative already shows them inline.
        "baldur.interfaces.notification",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def _setup_eager_celery() -> bool:
    """Install the eager Celery app the auto-replay dispatch rides on."""
    try:
        from celery import Celery
    except ImportError:
        _say("This demo needs the Celery extra for the auto-replay leg:")
        _say('    pip install "baldur-framework[celery]"')
        return False

    celery_app = Celery("baldur_demo", broker="memory://", backend="cache+memory://")
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    celery_app.conf.broker_connection_retry_on_startup = False
    celery_app.set_current()
    celery_app.set_default()
    return True


class _Demo:
    """One demo run: protected app, replay wiring, observation taps, phases."""

    def __init__(self) -> None:
        from baldur.decorators import dlq_protect
        from baldur.services.circuit_breaker import get_circuit_breaker_service
        from baldur.services.dlq_capture import resolve_dlq_backing
        from baldur.services.event_bus import EventType, get_event_bus
        from baldur.services.replay_service import register_replay_handler

        self.gateway_up = True
        self.charged_orders: list[int] = []
        self.order = 100
        self.ok = self.failed = self.rejected = 0
        self.first_order_down = 0
        self.captured = 0
        self.replayed_ok = self.replayed_total = 0

        # -- the "application" under protection ----------------------------
        @dlq_protect(_DOMAIN)
        def charge(order_id: int, amount: str = "49.99") -> dict:
            if not self.gateway_up:
                raise GatewayDownError("payment gateway unreachable")
            self.charged_orders.append(order_id)
            return {"charged": order_id, "amount": amount}

        self.charge = charge

        # -- replay wiring: how to re-execute a captured charge -------------
        register_replay_handler(_make_replay_handler(charge))

        # -- observation taps: real read APIs, no side bookkeeping ----------
        self._cb = get_circuit_breaker_service()
        self._dlq_repo = resolve_dlq_backing().repository
        self.replay_batches: list[dict] = []
        get_event_bus().subscribe(
            EventType.DLQ_REPLAY_BATCH_COMPLETED,
            lambda event: self.replay_batches.append(dict(event.data)),
        )

    def cb_state(self) -> str:
        return self._cb.get_state(_DOMAIN)

    def dlq_pending(self) -> int:
        return self._dlq_repo.get_pending_count_by_domain(_DOMAIN)

    def charge_line(self, verdict: str, extra: str = "") -> None:
        state = self.cb_state()
        scol = _CB_COLOR.get(state, "")
        line = (
            f"{DIM}{_now()}{R}  charge(order {self.order})  {verdict}"
            f"  {DIM}cb{R} {scol}{state.upper()}{R}"
        )
        if extra:
            line += f"  {BOLD}{extra}{R}"
        _say(line)

    # -- phases -------------------------------------------------------------

    def banner(self) -> None:
        _say(f"{BOLD}  ⚡ Baldur self-healing demo — kill the gateway, lose nothing{R}")
        _say(f"  {DIM}{'─' * 58}{R}")
        _say(f"{DIM}  charge() is protected by @dlq_protect('demo.charge'): circuit{R}")
        _say(
            f"{DIM}  breaker + retry + DLQ capture in one decorator. No Redis, no DB,{R}"
        )
        _say(f"{DIM}  no broker — this process is everything. Reproduce it:{R}")
        _say(f'{DIM}      pip install "baldur-framework[celery]"{R}')
        _say(f"{DIM}      python -m baldur.scripts.demo_self_healing{R}")
        _say()
        time.sleep(1.0)

    def baseline(self) -> None:
        for _ in range(3):
            self.order += 1
            self.charge(order_id=self.order)
            self.ok += 1
            self.charge_line(f"{GREEN}✔ charged{R}")
            time.sleep(0.4)

    def outage(self) -> None:
        _say(f"\n  {RED}✖ payment gateway goes DOWN{R}")
        self.gateway_up = False
        self.first_order_down = self.order + 1
        for _ in range(7):
            self.order += 1
            self._one_outage_charge()
            time.sleep(0.4)

    def _one_outage_charge(self) -> None:
        t0 = time.perf_counter()
        try:
            self.charge(order_id=self.order)
            self.ok += 1
            self.charge_line(f"{GREEN}✔ charged{R}")
        except GatewayDownError:
            self.failed += 1
            if self.failed == 1:
                note = "← failed, capturing"
            elif self.cb_state() == "open":
                note = "← breaker OPEN"
            else:
                note = ""
            self.charge_line(
                f"{RED}✖ GatewayDownError{R} {DIM}(retries exhausted){R}", extra=note
            )
        except Exception as exc:  # CircuitBreakerOpenError — fail fast
            self.rejected += 1
            ms = (time.perf_counter() - t0) * 1000
            self.charge_line(
                f"{YELLOW}⚡ rejected in {ms:.1f}ms{R} {DIM}({type(exc).__name__}){R}",
                extra="← breaker shields the gateway" if self.rejected == 1 else "",
            )

    def capture_tally(self) -> None:
        # Capture is async-durable: entries hit a local durable buffer on the
        # request path and become store-visible when the outbox flushes. Say
        # exactly what the store shows; the replay tally at the end is the
        # authoritative proof of what was captured.
        deadline = time.monotonic() + _OUTBOX_FLUSH_WAIT_S
        self.captured = self.dlq_pending()
        while self.captured < self.failed and time.monotonic() < deadline:
            time.sleep(0.5)
            self.captured = self.dlq_pending()
        span = (
            f"orders {self.first_order_down}-{self.first_order_down + self.failed - 1}"
        )
        if self.captured == self.failed:
            _say(
                f"  {MAGENTA}◆ {self.captured} failed charges captured to the DLQ{R}"
                f" {DIM}({span}, with their arguments){R}"
            )
        else:
            _say(
                f"  {MAGENTA}◆ DLQ capture: {self.captured}/{self.failed}"
                f" store-visible so far{R} {DIM}({span}; capture is async-durable"
                f" — the replay tally below is the proof){R}"
            )

    def recovery(self) -> None:
        _say(
            f"\n  {GREEN}✔ gateway is back UP{R}"
            f" {DIM}— breaker waits, probes, replays:{R}"
        )
        self.gateway_up = True
        time.sleep(float(os.environ["BALDUR_CB_RECOVERY_TIMEOUT"]) + 0.5)
        for _ in range(6):
            self.order += 1
            try:
                self.charge(order_id=self.order)
                self.ok += 1
                state = self.cb_state()
                self.charge_line(
                    f"{GREEN}✔ charged{R}",
                    extra="← breaker CLOSED" if state == "closed" else "",
                )
                if state == "closed":
                    return
            except Exception as exc:
                self.rejected += 1
                self.charge_line(
                    f"{YELLOW}⚡ rejected{R} {DIM}({type(exc).__name__}){R}"
                )
            time.sleep(0.7)

    def replay_tally(self) -> None:
        deadline = time.monotonic() + _REPLAY_WAIT_S
        while not self.replay_batches and time.monotonic() < deadline:
            time.sleep(0.3)
        if not self.replay_batches:
            _say(f"  {RED}⟳ replay batch not observed within {_REPLAY_WAIT_S:.0f}s{R}")
            return
        batches = self.replay_batches
        self.replayed_ok = sum(int(b.get("success_count", 0)) for b in batches)
        self.replayed_total = sum(int(b.get("total", 0)) for b in batches)
        lo, hi = self.first_order_down, self.first_order_down + self.failed
        replayed_orders = sorted(o for o in self.charged_orders if lo <= o < hi)
        _say(
            f"  {MAGENTA}⟳ auto-replay on circuit close: {BOLD}{self.replayed_ok}/"
            f"{self.replayed_total}{R}{MAGENTA} captured charges re-executed{R}"
            f" {DIM}(orders {replayed_orders[0]}-{replayed_orders[-1]},"
            f" dlq {self.dlq_pending()}){R}"
        )

    def summary(self) -> int:
        lost = self.failed - self.replayed_ok
        # The replay batch read its entries from the store, so its total is
        # first-hand evidence of capture even when the earlier count lagged.
        captured = max(self.captured, self.replayed_total)
        _say()
        _say(f"  {DIM}{'─' * 58}{R}")
        _say(
            f"  charges OK {BOLD}{self.ok}{R}  ·  failed {BOLD}{self.failed}{R}"
            f"  ·  captured {BOLD}{captured}{R}"
            f"  ·  auto-replayed {BOLD}{self.replayed_ok}/{self.replayed_total}{R}"
            f"  ·  lost {BOLD}{lost}{R}"
        )
        if lost == 0 and self.failed > 0 and self.dlq_pending() == 0:
            _say(f"  {BOLD}Every failed charge came back on its own. Zero lost.{R}")
        _say()
        return 0 if lost == 0 else 2


def _make_replay_handler(charge):
    """Build the demo's replay handler around the protected ``charge``."""
    from baldur.services.replay_service import ReplayHandler, ReplayResult

    class DemoChargeReplayHandler(ReplayHandler):
        """Re-executes a captured charge from its stored arguments."""

        @property
        def domain(self) -> str:
            return _DOMAIN

        def can_replay(self, failed_op) -> tuple[bool, str]:
            return True, "demo charges are always safe to re-run"

        def replay(self, failed_op) -> ReplayResult:
            request = failed_op.request_data or {}
            order_id = request.get("order_id") or failed_op.entity_id
            if order_id is None:
                return ReplayResult(
                    success=False,
                    dlq_id=str(failed_op.id),
                    error="no order_id captured",
                )
            result = charge(order_id=int(order_id))
            return ReplayResult(success=True, dlq_id=str(failed_op.id), data=result)

    return DemoChargeReplayHandler()


def main() -> int:
    for key, value in _DEMO_ENV.items():
        os.environ.setdefault(key, value)

    verbose = os.environ.get("BALDUR_DEMO_VERBOSE", "").lower() in ("1", "true")
    logging.basicConfig(
        level=logging.INFO if verbose else logging.ERROR,
        format="%(levelname).1s %(name)s %(message)s",
    )
    if not _setup_eager_celery():
        return 1
    if not verbose:
        _quiet_logging()

    import baldur
    import baldur.celery_tasks.dlq_tasks  # noqa: F401  # bind tasks to the eager app

    baldur.init()

    demo = _Demo()
    demo.banner()
    demo.baseline()
    demo.outage()
    demo.capture_tally()
    demo.recovery()
    demo.replay_tally()
    return demo.summary()


if __name__ == "__main__":
    sys.exit(main())
