"""Browser-driven console lane — boot, tier and skip policy.

Why this tree exists at all: every other assertion about `console.html` in this
suite is a case-insensitive whole-file substring scan. That catches a renamed
string; it cannot catch client logic whose failure is ordering-dependent. A
fetch chain that resolves in the wrong order, an arrival handler that never
repaints, a secondary payload that blanks a primary one — all of those spell
correctly and are invisible to a static anchor.

**This lane is additive, not a replacement.** The static anchors stay, and are
load-bearing beyond tests: two architectural fitness gates regex-parse the
`PANELS` block, and the console's MUST-KEEP anchor inventory is an adversarially
extracted list. Deleting an anchor because "the browser lane covers it now"
breaks a gate that has nothing to do with this file.

Skip policy mirrors the infra lanes (`requires_redis` and friends): the marker
carries the dependency, and collection auto-skips when the browser is not
installed, so a contributor without Chromium gets a green run and a clear skip
reason rather than a failure. Install with::

    pip install -e ".[test-e2e]" && playwright install --with-deps chromium

Isolation: the fixtures start the real admin server on an ephemeral port and
pin the memory repositories, then restore every global they touched. The
metrics registry is process-global and its counters are monotone, so the
"nothing replayed yet" state exists exactly once per session — the test that
needs it says so in its own docstring.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import pytest

_BROWSER_PROBE: bool | None = None


def _browser_available() -> bool:
    """Whether Playwright AND a Chromium build are actually usable here.

    Importable-but-no-binary is the common half-installed state (``pip install
    playwright`` without ``playwright install``), and it fails at launch rather
    than at import — so the probe launches once and caches the verdict.
    """
    global _BROWSER_PROBE
    if _BROWSER_PROBE is not None:
        return _BROWSER_PROBE

    override = os.environ.get("TEST_BROWSER_AVAILABLE", "").strip().lower()
    if override in {"true", "false"}:
        _BROWSER_PROBE = override == "true"
        return _BROWSER_PROBE

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        _BROWSER_PROBE = True
    except Exception:
        _BROWSER_PROBE = False
    return _BROWSER_PROBE


def pytest_collection_modifyitems(config, items):
    """Auto-skip `requires_browser` tests when no Chromium is installed."""
    if _browser_available():
        return
    skip = pytest.mark.skip(
        reason="Chromium not available — "
        'pip install -e ".[test-e2e]" && playwright install chromium '
        "(or set TEST_BROWSER_AVAILABLE=true)"
    )
    for item in items:
        if "requires_browser" in [m.name for m in item.iter_markers()]:
            item.add_marker(skip)


@dataclass
class ConsoleWorld:
    """The live console under test, plus the handles a case needs to drive it."""

    base_url: str
    repo: object
    retryable_ids: list[str] = field(default_factory=list)

    def request(self, path: str, method: str = "GET", body: dict | None = None):
        import json
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw) if raw else {}
                except ValueError:
                    # The console root answers HTML — a liveness probe must not
                    # read that as "the server did not answer".
                    return resp.status, raw[:400]
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()[:400]

    def healing_summary(self) -> dict:
        _status, body = self.request("/healing/summary")
        return body if isinstance(body, dict) else {}

    def replayed_count(self) -> int:
        return int(self.healing_summary().get("counters", {}).get("replayed", 0))

    def retry_one(self) -> str:
        """Replay one seeded entry through the real operator route."""
        entry_id = self.retryable_ids.pop()
        status, body = self.request(
            f"/dlq/{entry_id}/retry", "POST", {"reason": "console e2e"}
        )
        assert status == 200, f"retry failed: {status} {body}"
        return entry_id


@pytest.fixture
def console_world():
    """Boot the admin server on an ephemeral port over a seeded memory DLQ.

    Function-scoped on purpose, against the instinct to boot once per session.
    The suite's shared conftest resets singletons between files and re-pins
    provider registrations between functions, so a session-scoped world is torn
    out from under its own tests: the server singleton is stopped by the
    autodiscovered ``reset_*`` sweep and the repository reverts to the default
    backing. Building the world after those resets have run is what makes it
    hold for the length of a case.

    `AdminServer` is constructed directly rather than through
    `start_admin_server`, which is both the documented test entry point (the
    suite disables `BALDUR_ADMIN_AUTOSTART` and expects explicit ephemeral-port
    starts) and what keeps the singleton sweep from stopping it mid-case.
    `baldur.init()` is never called: the console needs the route registry and a
    repository, nothing else, and a full init would leave background workers
    behind.
    """
    from datetime import timedelta

    from baldur.api.admin.server import AdminServer, get_admin_server_settings
    from baldur.factory.registry import ProviderRegistry
    from baldur.interfaces.repositories import FailedOperationData
    from baldur.services.dlq_read import get_dlq_read_service
    from baldur.services.replay_service import ReplayHandler, register_replay_handler
    from baldur.services.replay_service import handlers as replay_handlers
    from baldur.utils.time import utc_now

    handler_snapshot = dict(replay_handlers._replay_handlers)

    class _SucceedingHandler(ReplayHandler):
        @property
        def domain(self) -> str:
            return "payment"

        def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
            return True, ""

        def replay(self, failed_op: FailedOperationData):
            from baldur.services.replay_service.models import ReplayResult

            return ReplayResult.succeeded(failed_op.id, "console e2e replay")

    register_replay_handler(_SucceedingHandler())

    ProviderRegistry.failed_op_repo.set_default("memory")
    service = ProviderRegistry.dlq_service.safe_get() or get_dlq_read_service()
    service._repository = None
    repo = service.repository

    # A window with both resolved and still-open entries, so the ledger has a
    # real series to draw and the footer's three window counters are non-trivial.
    now = utc_now()
    seed = [(38, 37), (34, 33), (28, 27), (22, 20), (14, 13), (9, 8)]
    open_ago = [6, 4, 3, 1]
    retryable: list[str] = []

    for index, (made_ago, healed_ago) in enumerate(seed):
        entry = repo.create(
            domain="payment",
            failure_type="external_call_failed",
            error_message="gateway timeout after 3 retries",
            error_code="UPSTREAM_TIMEOUT",
            entity_type="order",
            entity_id=f"ord-{index}",
        )
        row = repo._storage[entry.id]
        row.created_at = now - timedelta(minutes=made_ago)
        row.status = "resolved"
        row.resolved_at = now - timedelta(minutes=healed_ago)
        row.updated_at = row.resolved_at
        repo._index_by_status.setdefault("pending", set()).discard(entry.id)
        repo._index_by_status.setdefault("resolved", set()).add(entry.id)

    for index, made_ago in enumerate(open_ago):
        entry = repo.create(
            domain="payment",
            failure_type="external_call_failed",
            error_message="gateway timeout after 3 retries",
            error_code="UPSTREAM_TIMEOUT",
            entity_type="order",
            entity_id=f"ord-open-{index}",
        )
        row = repo._storage[entry.id]
        row.created_at = now - timedelta(minutes=made_ago)
        row.updated_at = row.created_at
        retryable.append(entry.id)

    # port=0 -> the OS picks a free port and `bound_port` reads it back, which
    # is the shared conftest's stated pattern for tests that need a real socket.
    settings = get_admin_server_settings().model_copy(
        update={"port": 0, "bind": "127.0.0.1"}
    )
    server = AdminServer(settings=settings)
    server.start()
    world = ConsoleWorld(
        base_url=f"http://127.0.0.1:{server.bound_port}",
        repo=repo,
        retryable_ids=retryable,
    )

    waiter = threading.Event()
    for _ in range(40):
        try:
            if world.request("/healing/summary")[0] is not None:
                break
        except Exception:  # noqa: BLE001, PERF203 - liveness poll, not a check
            pass
        waiter.wait(0.25)
    else:  # pragma: no cover - a dead server fails every case anyway
        pytest.fail(f"admin server never answered on {world.base_url}")

    # A cold DLQ-panel route runs an arming probe that outlives the console's
    # own 5 s fetch timeout, which would show as "server slow/unresponsive"
    # rows rather than as anything this lane is about.
    for path in ("/dashboard/summary", "/dlq/cleanup/stats", "/healing/summary"):
        world.request(path)

    yield world

    server.stop(timeout=2.0)
    replay_handlers._replay_handlers.clear()
    replay_handlers._replay_handlers.update(handler_snapshot)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        launched = p.chromium.launch()
        yield launched
        launched.close()


@pytest.fixture
def page(browser):
    new_page = browser.new_page()
    yield new_page
    new_page.close()


@pytest.fixture
def a_replay_has_happened(console_world):
    """Guarantee the process has replayed at least once.

    The counter is process-local and monotone while the DLQ world is rebuilt per
    case, so this is idempotent: once any case has replayed, every later one
    finds the counter already above zero and performs no extra retry.
    """
    if console_world.replayed_count() < 1:
        console_world.retry_one()
    assert console_world.replayed_count() >= 1
    return console_world
