"""LLM outage demo: a provider rate-limits and then goes down, no job is lost.

Runs entirely in this process — no Redis, no message broker, no API key::

    pip install "baldur-framework[celery]" openai
    python -m baldur.scripts.demo_llm_outage

A local HTTP server speaks the OpenAI chat-completions API and the real
``openai`` SDK talks to it. Eight worker threads play the fleet.

1. **Rate limit.** The server refuses with ``429`` and ``retry-after: 5``.
   First the workers call the SDK directly, with its own retries; then the same
   workers call it through ``baldur.llm.wrap``. The server counts the requests
   it receives inside the five seconds it asked for.
2. **Outage.** The server answers ``503`` for a while. The jobs are decorated
   ``@baldur.protected(..., replay=True)``: each one no endpoint could answer
   is parked with its arguments, the breaker opens and parks the rest without
   calling the provider, and when the provider is back and the breaker closes,
   every parked job is re-run automatically. The tally at the end is computed
   from what actually happened.

The threads share one in-process store; worker processes share waits and
parked jobs only through Redis. The eager Celery app stands in for the worker
that runs the recovery sweep in a real deployment.

Set ``BALDUR_DEMO_VERBOSE=1`` to also see the framework's own structured log
events instead of the quiet demo narrative alone.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from baldur.core.process_utils import fork_safe_lock
from baldur.scripts.demo_self_healing import (
    BOLD,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    YELLOW,
    R,
    _protect_console_encoding,
    _say,
    _start_demo_process,
)

__all__ = ["main"]

# Demo-scale tuning so both scenes fit in about half a minute. Every knob is a
# documented BALDUR_* setting; applied with setdefault so explicit env wins.
_DEMO_ENV = {
    "BALDUR_ENVIRONMENT": "development",
    "BALDUR_OBSERVABILITY_PROFILE": "local",
    "BALDUR_CB_FAILURE_THRESHOLD": "5",
    "BALDUR_CB_RECOVERY_TIMEOUT": "3",
    "BALDUR_RETRY_MAX_ATTEMPTS": "2",
    "BALDUR_RETRY_BASE_DELAY": "0.2",
    "BALDUR_RETRY_MAX_DELAY": "10",
    "BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY": "10",
    # The shared wait Baldur installs on an overload that names no wait of its
    # own is held at half a second instead of escalating; a wait the provider
    # names (the rate-limit scene's five seconds) is still served in full.
    "BALDUR_RATE_LIMIT_BACKOFF_DEFAULT_RETRY_AFTER": "0.5",
    "BALDUR_RATE_LIMIT_BACKOFF_BACKOFF_MULTIPLIER": "1",
    "BALDUR_RATE_LIMIT_BACKOFF_MAX_DELAY": "5.5",
}

_WORKERS = 8
_RATE_LIMIT_WAIT_S = 5
# The workers of scene 1 reach the API this far apart, as jobs off a queue do.
_ARRIVAL_SPACING_S = 0.15
# Scene 2: one job per worker while the provider is down, then a few more once
# the breaker has opened on them.
_OUTAGE_LATE_JOBS = 4
# Live jobs keep arriving after the provider is back until the breaker has
# probed and closed, or this long has passed.
_RECOVERY_WINDOW_S = 15.0
_PROBE_SPACING_S = 0.5
# Capture is async-durable and the recovery replays in passes; each wait ends
# as soon as its work is done, otherwise after this long without progress.
_CAPTURE_WAIT_S = 8.0
_REPLAY_WAIT_S = 15.0

_SCENE1_MODEL = "gpt-4o-mini"
_SCENE2_MODEL = "gpt-4o"
_JOB_NAME = "demo.summarize"


# =============================================================================
# The fake provider
# =============================================================================


@dataclass
class _ProviderState:
    """What the local provider answers, and what it saw."""

    mode: str = "ok"  # "ok" | "rate_limit" | "outage"
    window_started: float | None = None
    requests_in_window: int = 0
    answered: list[str] = field(default_factory=list)
    lock: Any = field(default_factory=fork_safe_lock)

    def reset(self, mode: str) -> None:
        with self.lock:
            self.mode = mode
            self.window_started = None
            self.requests_in_window = 0

    def decide(self, doc_id: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        """``(status, body, headers)`` for one request."""
        with self.lock:
            if self.mode == "outage":
                return 503, _error("The server is overloaded.", "server_error"), {}
            if self.mode == "rate_limit":
                now = time.monotonic()
                if self.window_started is None:
                    self.window_started = now
                if now - self.window_started < _RATE_LIMIT_WAIT_S:
                    self.requests_in_window += 1
                    return (
                        429,
                        _error("Rate limit reached for requests.", "requests"),
                        {"retry-after": str(_RATE_LIMIT_WAIT_S)},
                    )
            self.answered.append(doc_id)
        return 200, _completion(doc_id), {}


def _error(message: str, error_type: str) -> dict[str, Any]:
    return {
        "error": {"message": message, "type": error_type, "param": None, "code": None}
    }


def _completion(doc_id: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{doc_id}",
        "object": "chat.completion",
        "created": 0,
        "model": "demo",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"summary of {doc_id}"},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _start_provider(state: _ProviderState) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 — the stdlib's handler name
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                doc_id = str(payload["messages"][-1]["content"])
            except (ValueError, KeyError, IndexError, TypeError):
                doc_id = "?"
            status, body, headers = state.decide(doc_id)
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# =============================================================================
# Scenes
# =============================================================================


def _run_workers(job: Callable[[int], None], count: int, spacing: float) -> None:
    """Run ``count`` jobs on the worker pool, the k-th starting ``k * spacing`` in."""
    start = time.monotonic()

    def run(index: int) -> None:
        delay = start + index * spacing - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        job(index)

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        list(pool.map(run, range(count)))


class _Demo:
    def __init__(self, openai_module: Any) -> None:
        self.openai = openai_module
        self.state = _ProviderState()
        self.server = _start_provider(self.state)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def client(self) -> Any:
        return self.openai.OpenAI(api_key="demo", base_url=self.base_url)

    def banner(self) -> None:
        _say(
            f"{BOLD}  ⚡ Baldur LLM demo — a provider limits, then dies; no job is lost{R}"
        )
        _say(f"  {DIM}{'─' * 64}{R}")
        _say(
            f"{DIM}  The real openai SDK talks to a local fake provider; eight threads{R}"
        )
        _say(f"{DIM}  play the workers. They share one in-process store — separate{R}")
        _say(f"{DIM}  worker processes share waits and parked jobs through Redis.{R}")
        _say(f'{DIM}      pip install "baldur-framework[celery]" openai{R}')
        _say(f"{DIM}      python -m baldur.scripts.demo_llm_outage{R}")
        _say()

    # -- scene 1 --------------------------------------------------------------

    def rate_limit_arm(self, label: str, client: Any) -> tuple[int, int]:
        self.state.reset("rate_limit")
        failed = 0
        lock = fork_safe_lock()

        def job(index: int) -> None:
            nonlocal failed
            try:
                client.chat.completions.create(
                    model=_SCENE1_MODEL,
                    messages=[{"role": "user", "content": f"doc-{index}"}],
                )
            except Exception:
                with lock:
                    failed += 1

        t0 = time.monotonic()
        _run_workers(job, _WORKERS, _ARRIVAL_SPACING_S)
        inside = self.state.requests_in_window
        colour = GREEN if inside <= 1 else RED
        _say(
            f"  {label:<26} requests inside the {_RATE_LIMIT_WAIT_S}s wait: "
            f"{colour}{BOLD}{inside}{R}   failed jobs: {BOLD}{failed}{R}"
            f"  {DIM}({time.monotonic() - t0:.1f}s){R}"
        )
        return inside, failed

    def scene_rate_limit(self) -> None:
        _say(
            f"{BOLD}  1. Rate limit{R} {DIM}— 429 with retry-after: "
            f"{_RATE_LIMIT_WAIT_S}, {_WORKERS} workers{R}"
        )
        import baldur

        self.rate_limit_arm("SDK retries, per worker", self.client())
        self.rate_limit_arm("baldur.llm.wrap", baldur.llm.wrap(self.client()))
        _say(
            f"  {DIM}The SDK honors the wait — each worker learns it alone. The wrap{R}"
        )
        _say(f"  {DIM}shares it: one worker is refused, the rest wait for it.{R}")
        _say()

    # -- scene 2 --------------------------------------------------------------

    def scene_outage(self) -> int:
        return _OutageScene(self).run()

    def close(self) -> None:
        self.server.shutdown()


def _wait_for(read: Callable[[], int], expected: int, quiet_s: float) -> int:
    """Poll ``read`` until it reaches ``expected`` or stops moving for ``quiet_s``."""
    value = read()
    last_move = time.monotonic()
    while value < expected and time.monotonic() - last_move < quiet_s:
        time.sleep(0.2)
        current = read()
        if current != value:
            value = current
            last_move = time.monotonic()
    return value


class _OutageScene:
    """Scene 2: the provider goes down, jobs are parked, then replayed."""

    def __init__(self, demo: _Demo) -> None:
        import baldur
        from baldur.services.circuit_breaker import get_circuit_breaker_service
        from baldur.services.dlq_capture import resolve_dlq_backing
        from baldur.services.event_bus import EventType, get_event_bus

        self.demo = demo
        self.stopped = (baldur.LLMUnavailableError, baldur.CircuitBreakerError)
        self.breaker_open = baldur.CircuitBreakerError
        self.breakers = get_circuit_breaker_service()
        self.repository = resolve_dlq_backing().repository
        self.lock = fork_safe_lock()
        self.done: list[str] = []
        self.parked: set[str] = set()
        self.rejected = 0
        self.batches: list[dict[str, Any]] = []
        get_event_bus().subscribe(
            EventType.DLQ_REPLAY_BATCH_COMPLETED,
            lambda event: self.batches.append(dict(event.data)),
        )
        llm = baldur.llm.wrap(demo.client())
        done, lock = self.done, self.lock

        @baldur.protected(_JOB_NAME, replay=True)
        def summarize(doc_id: str) -> str:
            response = llm.chat.completions.create(
                model=_SCENE2_MODEL,
                messages=[{"role": "user", "content": doc_id}],
            )
            with lock:
                done.append(doc_id)
            return str(response.choices[0].message.content)

        self.summarize = summarize

    def run(self) -> int:
        _say(
            f"{BOLD}  2. Outage{R} {DIM}— the provider answers 503; jobs are "
            f"@protected(..., replay=True){R}"
        )
        self.outage()
        self.recover()
        return self.tally()

    def job(self, doc_id: str) -> None:
        """One job; a stopped one is noted as parked."""
        try:
            self.summarize(doc_id)
        except self.stopped as error:
            with self.lock:
                self.parked.add(doc_id)
                if isinstance(error, self.breaker_open):
                    self.rejected += 1

    def outage(self) -> None:
        self.demo.state.reset("outage")
        t0 = time.monotonic()
        _run_workers(lambda index: self.job(f"doc-{100 + index}"), _WORKERS, 0.0)
        _run_workers(
            lambda index: self.job(f"doc-{100 + _WORKERS + index}"),
            _OUTAGE_LATE_JOBS,
            0.0,
        )
        parked = len(self.parked)
        _say(
            f"  {RED}✖ {parked} jobs failed{R} {DIM}in "
            f"{time.monotonic() - t0:.1f}s —{R} {parked - self.rejected} after "
            f"every endpoint was tried, {YELLOW}{self.rejected} rejected by the "
            f"open breaker{R} {DIM}without calling the provider{R}"
        )
        captured = _wait_for(
            lambda: self.repository.get_pending_count_by_domain(_JOB_NAME),
            parked,
            _CAPTURE_WAIT_S,
        )
        _say(f"  {MAGENTA}◆ {captured} parked with their arguments{R}")

    def recover(self) -> None:
        """The provider is back: live jobs arrive until the breaker has closed."""
        self.demo.state.reset("ok")
        _say(
            f"  {GREEN}✔ the provider is back{R} {DIM}— the breaker waits, "
            f"probes, closes:{R}"
        )
        time.sleep(float(os.environ["BALDUR_CB_RECOVERY_TIMEOUT"]) + 0.5)
        recovery_ends = time.monotonic() + _RECOVERY_WINDOW_S
        probe = 0
        while time.monotonic() < recovery_ends:
            # A live job the half-open breaker turned away is parked like any
            # other, and comes back with them.
            self.job(f"doc-live-{probe}")
            probe += 1
            if self.breakers.get_state(_JOB_NAME) == "closed":
                return
            time.sleep(_PROBE_SPACING_S)

    def tally(self) -> int:
        replayed_ok = _wait_for(
            lambda: sum(int(b.get("success_count", 0)) for b in self.batches),
            len(self.parked),
            _REPLAY_WAIT_S,
        )
        with self.lock:
            came_back = set(self.done) & self.parked
        lost = len(self.parked) - len(came_back)
        pending = self.repository.get_pending_count_by_domain(_JOB_NAME)
        _say(
            f"  {MAGENTA}⟳ breaker closed → {replayed_ok} parked jobs re-run "
            f"automatically{R} {DIM}(dlq pending {pending}){R}"
        )
        _say()
        _say(f"  {DIM}{'─' * 64}{R}")
        _say(
            f"  parked {BOLD}{len(self.parked)}{R}  ·  replayed "
            f"{BOLD}{len(came_back)}{R}  ·  lost {BOLD}{lost}{R}"
        )
        if lost == 0 and self.parked and pending == 0:
            _say(f"  {BOLD}Every job the outage stopped came back on its own.{R}")
        _say()
        return lost


def main(argv: list[str] | None = None) -> int:
    _protect_console_encoding()
    for key, value in _DEMO_ENV.items():
        os.environ.setdefault(key, value)

    try:
        import openai
    except ImportError:
        _say("This demo talks to its fake provider through the openai SDK:")
        _say('    pip install "baldur-framework[celery]" openai')
        return 1

    if not _start_demo_process():
        return 1

    import baldur
    import baldur.celery_tasks.dlq_tasks  # noqa: F401  # bind tasks to the eager app

    baldur.init()

    demo = _Demo(openai)
    try:
        demo.banner()
        demo.scene_rate_limit()
        lost = demo.scene_outage()
    finally:
        demo.close()
    return 0 if lost == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
