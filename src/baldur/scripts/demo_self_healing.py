"""Self-healing demo: a dependency dies mid-traffic, nothing is lost.

Runs entirely in this process — no Redis, no database, no message broker::

    pip install "baldur-framework[celery]"
    python -m baldur.scripts.demo_self_healing

What it shows:

1. ``charge()`` is protected by ``@dlq_protect``: circuit breaker, retry,
   and DLQ capture composed by one decorator.
2. The fake payment gateway goes down. Every failed charge is captured with
   its arguments; after enough failures the circuit breaker opens and starts
   rejecting instantly instead of piling onto the dying dependency. A charge
   the open breaker rejects never ran, and it is captured too.
3. The gateway comes back. The breaker probes, closes, and the CLOSED event
   automatically replays every captured charge through the registered replay
   handler. The summary at the end is computed from what actually happened.

Make the outage bigger to watch a backlog drain. A recovery replays in passes
of 100 entries by default, and the tally waits for every pass::

    python -m baldur.scripts.demo_self_healing --outage-charges 500

The replay wiring this demo performs — an eager Celery app, a replay handler
for its domain, and the failure-type routing map — is the same wiring a real
deployment does; only the eager Celery app stands in for a real worker.

Set ``BALDUR_DEMO_VERBOSE=1`` to also see the framework's own structured log
events instead of the quiet demo narrative alone.
"""

from __future__ import annotations

import argparse
import codecs
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.interfaces.repositories import FailedOperationRepository

__all__ = ["main"]

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
# Both waits end early once their work is done, and otherwise end only after
# this long without progress, so a large outage never outruns them.
_OUTBOX_FLUSH_WAIT_S = 8.0  # capture is async-durable; store visibility follows
_REPLAY_WAIT_S = 10.0

# The outage: this many charges hit the dead gateway. The first few are
# narrated one line each at a readable pace; the rest of a larger outage run
# back to back and are summed in one line. The ceiling stays well inside the
# 10,000 entries one recovery drains on the default replay settings, so a
# charge still parked at the end means lost, not "past the drain budget".
_DEFAULT_OUTAGE_CHARGES = 7
_NARRATED_OUTAGE_CHARGES = 7
_MAX_OUTAGE_CHARGES = 5000
_OUTAGE_PACE_S = 0.4

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


def _protect_console_encoding() -> None:
    """Switch a stream that is not UTF-8 to UTF-8 so the narrative's symbols print.

    A Windows console window takes Unicode, but a pipe, a redirect, or a terminal
    such as Git Bash's mintty gets the legacy code page (cp949, cp1252, cp932),
    which has no check mark or lightning bolt: the banner's first line raised
    ``UnicodeEncodeError``. Those destinations read UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            is_utf8 = codecs.lookup(stream.encoding or "ascii").name == "utf-8"
        except LookupError:
            is_utf8 = False
        if is_utf8:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


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


@dataclass
class _Tally:
    """What the demo counts, kept apart from how it observes the framework.

    A charge is *parked* when it failed on the way out — either it exhausted
    its retries against the dead gateway, or the OPEN breaker rejected it
    before it ran. Both are captured to the DLQ and both must come back for
    "zero lost" to hold, so ``lost`` is measured against every parked order,
    not against the retry-exhausted ones alone.
    """

    ok: int = 0
    failed: int = 0
    rejected: int = 0
    parked_orders: set[int] = field(default_factory=set)

    def record_ok(self) -> None:
        self.ok += 1

    def record_failed(self, order: int) -> None:
        self.failed += 1
        self.parked_orders.add(order)

    def record_rejected(self, order: int) -> None:
        self.rejected += 1
        self.parked_orders.add(order)

    @property
    def parked(self) -> int:
        return len(self.parked_orders)

    def span(self) -> str:
        if not self.parked_orders:
            return "none"
        return f"#{min(self.parked_orders)}-{max(self.parked_orders)}"

    def replayed(self, charged_orders: list[int]) -> list[int]:
        """Parked orders that were charged again, in charge order."""
        return sorted(o for o in charged_orders if o in self.parked_orders)

    def lost(self, replayed_ok: int) -> int:
        return self.parked - replayed_ok


def _batch_totals(batches: list[dict]) -> tuple[int, int]:
    """``(re-executed ok, attempted)`` summed over every replay pass seen."""
    ok = sum(int(b.get("success_count", 0)) for b in batches)
    total = sum(int(b.get("total", 0)) for b in batches)
    return ok, total


def _replay_drained(batches: list[dict], expected: int) -> bool:
    """Have the replay passes seen so far attempted every parked charge?

    A recovery replays its backlog in passes, one batch event each, and the
    passes keep arriving after the first one lands. Summing the first batch
    alone reported a 500-charge outage as ``400/400 ... lost 100`` while the
    last pass was still running.
    """
    return bool(batches) and _batch_totals(batches)[1] >= expected


def _outage_charges(value: str) -> int:
    """argparse type for ``--outage-charges``: 1 to the demo's ceiling."""
    try:
        charges = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from e
    if not 1 <= charges <= _MAX_OUTAGE_CHARGES:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {_MAX_OUTAGE_CHARGES}, got {charges}"
        )
    return charges


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m baldur.scripts.demo_self_healing",
        description=(
            "Kill a fake payment gateway mid-traffic and watch every failed "
            "charge come back on its own."
        ),
    )
    parser.add_argument(
        "--outage-charges",
        type=_outage_charges,
        default=_DEFAULT_OUTAGE_CHARGES,
        metavar="N",
        help=(
            "charges attempted while the gateway is down, 1 to "
            f"{_MAX_OUTAGE_CHARGES} (default: %(default)s); a few hundred "
            "shows the backlog replaying in passes"
        ),
    )
    return parser.parse_args(argv)


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
        self.tally = _Tally()
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
        self._dlq_repo: FailedOperationRepository = resolve_dlq_backing().repository
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
            f"{DIM}{_now()}{R}  charge(#{self.order})  {verdict}"
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
            self.tally.record_ok()
            self.charge_line(f"{GREEN}✔ charged{R}")
            time.sleep(0.4)

    def outage(self, charges: int) -> None:
        _say(f"\n  {RED}✖ payment gateway goes DOWN{R}")
        self.gateway_up = False
        narrated = min(charges, _NARRATED_OUTAGE_CHARGES)
        for _ in range(narrated):
            self.order += 1
            self._one_outage_charge()
            time.sleep(_OUTAGE_PACE_S)
        if charges > narrated:
            self._outage_burst(charges - narrated)

    def _outage_burst(self, charges: int) -> None:
        """The rest of a large outage: back-to-back charges, summed in one line."""
        failed, rejected = self.tally.failed, self.tally.rejected
        t0 = time.perf_counter()
        for _ in range(charges):
            self.order += 1
            self._one_outage_charge(narrate=False)
        elapsed = time.perf_counter() - t0
        burst_rejected = self.tally.rejected - rejected
        burst_failed = self.tally.failed - failed
        line = (
            f"  {DIM}… {charges} more charges in {elapsed:.1f}s:{R}"
            f" {YELLOW}⚡ {burst_rejected} rejected{R}"
        )
        if burst_failed:
            line += f"{DIM},{R} {RED}✖ {burst_failed} failed{R} {DIM}(retried){R}"
        _say(line)

    def _one_outage_charge(self, *, narrate: bool = True) -> None:
        t0 = time.perf_counter()
        try:
            self.charge(order_id=self.order)
            self.tally.record_ok()
            if narrate:
                self.charge_line(f"{GREEN}✔ charged{R}")
        except GatewayDownError:
            self.tally.record_failed(self.order)
            if not narrate:
                return
            if self.tally.failed == 1:
                note = "← capturing"
            elif self.cb_state() == "open":
                note = "← breaker OPEN"
            else:
                note = ""
            self.charge_line(
                f"{RED}✖ GatewayDownError{R} {DIM}(retried){R}", extra=note
            )
        except Exception:  # CircuitBreakerOpenError — fail fast, captured too
            self.tally.record_rejected(self.order)
            if not narrate:
                return
            ms = (time.perf_counter() - t0) * 1000
            self.charge_line(
                f"{YELLOW}⚡ rejected in {ms:.1f}ms{R}",
                extra="← shields the gateway" if self.tally.rejected == 1 else "",
            )

    def capture_tally(self) -> None:
        # Capture is async: entries leave the request path through the outbox
        # and become store-visible when it flushes. Say exactly what the store
        # shows; the replay tally at the end is the authoritative proof of
        # what was captured.
        expected = self.tally.parked
        self.captured = self.dlq_pending()
        last_move = time.monotonic()
        while (
            self.captured < expected
            and time.monotonic() - last_move < _OUTBOX_FLUSH_WAIT_S
        ):
            time.sleep(0.5)
            visible = self.dlq_pending()
            if visible != self.captured:
                self.captured = visible
                last_move = time.monotonic()
        if self.captured == expected:
            _say(
                f"  {MAGENTA}◆ {self.captured} charges captured with their arguments{R}"
                f" {DIM}({self.tally.span()}: {self.tally.failed} failed,"
                f" {self.tally.rejected} rejected){R}"
            )
        else:
            _say(
                f"  {MAGENTA}◆ DLQ capture: {self.captured}/{expected}"
                f" store-visible so far{R} {DIM}(async — see replay tally){R}"
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
                self.tally.record_ok()
                state = self.cb_state()
                self.charge_line(
                    f"{GREEN}✔ charged{R}",
                    extra="← breaker CLOSED" if state == "closed" else "",
                )
                if state == "closed":
                    return
            except Exception as exc:
                # A HALF_OPEN breaker admits one probe; the rest are rejected
                # and captured, so they are parked work like any other.
                self.tally.record_rejected(self.order)
                self.charge_line(
                    f"{YELLOW}⚡ rejected{R} {DIM}({type(exc).__name__}){R}"
                )
            time.sleep(0.7)

    def replay_tally(self) -> None:
        # The replay runs off the CLOSED event, one pass per batch event, and
        # later passes land after the first. Wait until the passes have
        # attempted every parked charge, or until they stop arriving.
        expected = self.tally.parked
        seen = 0
        last_move = time.monotonic()
        while time.monotonic() - last_move < _REPLAY_WAIT_S:
            batches = list(self.replay_batches)
            if len(batches) != seen:
                seen = len(batches)
                last_move = time.monotonic()
            if _replay_drained(batches, expected):
                break
            time.sleep(0.3)
        batches = list(self.replay_batches)
        if not batches:
            _say(f"  {RED}⟳ replay batch not observed within {_REPLAY_WAIT_S:.0f}s{R}")
            return
        self.replayed_ok, self.replayed_total = _batch_totals(batches)
        replayed_orders = self.tally.replayed(self.charged_orders)
        span = (
            f"#{replayed_orders[0]}-{replayed_orders[-1]}"
            if replayed_orders
            else "none"
        )
        passes = f", {len(batches)} passes" if len(batches) > 1 else ""
        _say(
            f"  {MAGENTA}⟳ auto-replay on circuit close: {BOLD}{self.replayed_ok}/"
            f"{self.replayed_total}{R}{MAGENTA} charges re-executed{R}"
            f" {DIM}({span}{passes}, dlq {self.dlq_pending()}){R}"
        )

    def summary(self) -> int:
        lost = self.tally.lost(self.replayed_ok)
        # The replay batch read its entries from the store, so its total is
        # first-hand evidence of capture even when the earlier count lagged.
        captured = max(self.captured, self.replayed_total)
        _say()
        _say(f"  {DIM}{'─' * 58}{R}")
        _say(
            f"  OK {BOLD}{self.tally.ok}{R}  ·  failed {BOLD}{self.tally.failed}{R}"
            f"  ·  rejected {BOLD}{self.tally.rejected}{R}"
            f"  ·  captured {BOLD}{captured}{R}"
            f"  ·  replayed {BOLD}{self.replayed_ok}/{self.replayed_total}{R}"
            f"  ·  lost {BOLD}{lost}{R}"
        )
        if lost == 0 and self.tally.parked > 0 and self.dlq_pending() == 0:
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


def main(argv: list[str] | None = None) -> int:
    _protect_console_encoding()
    args = _parse_args(argv)
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
    demo.outage(args.outage_charges)
    demo.capture_tally()
    demo.recovery()
    demo.replay_tally()
    return demo.summary()


if __name__ == "__main__":
    sys.exit(main())
