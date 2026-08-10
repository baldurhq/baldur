# Web Console

> A built-in browser dashboard for operating Baldur — see what's failing and recovering, and act on it, all from one page with no extra setup.

## What is it?

Most monitoring tools are read-only. Grafana, a status page, a metrics dashboard: they draw you a
picture of what is happening, but when something is actually wrong you still have to go *somewhere
else* to fix it: open a terminal, remember the right command, and hope you got the arguments right
while the incident is live.

A **web console** closes that gap by putting the controls next to the gauges. Think of an aircraft
cockpit rather than a car's dashboard: it does not just show you the altitude, it gives you the
levers to change it. You watch the state and you act on it in the same place.

In Baldur's terms, the Web Console is a zero-configuration browser page (served by Baldur's
built-in admin server) that shows your self-healing system's current state *and* lets you take the
recovery actions a read-only dashboard cannot: reset a stuck circuit breaker, stop runaway
automation, or work through a backlog of failed operations.

## Why it matters

During an incident you need to *do* things, fast. Without a console, "reset this breaker" or "stop
the automation" means hand-crafting calls to Baldur's admin API (knowing the exact route, building
the right request body, and remembering which actions are dangerous), or having no interface at all
if you have not stood up a separate monitoring stack.

The Web Console removes that friction. A small team can open a browser to `http://localhost:9090/`
and immediately have an operate-and-recover surface: the current state is already on screen, the
safe actions are buttons, and the dangerous ones are clearly marked and gated. There are no
dashboards to build and no query language to learn. It is the incident-response surface for the
operators who have not stood up (or do not want) a full Grafana deployment, and it does the one
thing Grafana cannot: change the system's state, not just display it.

## How it works in Baldur

Once Baldur is initialized, its built-in admin server starts automatically and serves the console at
`GET /` (by default `http://localhost:9090/`). There is nothing to configure to get there.

The page is a single column ordered by what needs you, not by how Baldur is built. A one-line
verdict at the top says whether anything is wrong and names the worst offender. Under it, a
**healing ledger** charts the same story over time: what failed, what healed, and how much stayed
open. Below that the checks split in two — **Needs attention** holds the ones that are degraded or
broken, **System** holds the calm ones as compact rows you can skim.

Each row is one subsystem. It states its condition in a sentence rather than a field dump ("circuit
open — calls are being parked instead of sent"), expands in place for the detail, and carries the
buttons for the actions that make sense there. Rows move between the two sections as their state
changes, so the top of the page is always the short list. A row backed by a PRO service is labelled
**PRO**; everything unlabelled is OSS.

Circuit breakers expand one level further: each of *your* services gets its own row, so you read
"payments — circuit open" rather than "Circuit Breakers — degraded", and the reset button on that
row already targets that service.

| Subsystem | Tier | What it shows | What you can do |
|-----------|------|---------------|-----------------|
| Dashboard | OSS | The rolled-up self-healing summary — status counts, recent activity, an overall health verdict | — (read-only) |
| Circuit Breakers | OSS | The state of each service's breaker, one row per service | Reset a breaker |
| System Control | OSS | Whether automation is enabled, and the kill-switch state | Enable or disable (kill-switch) automation, with a dry-run mode |
| Emergency | PRO | The current emergency level | Trigger or release emergency mode |
| Dead Letter Queue | OSS | The backlog of failed operations, browsable entry by entry | Retry or resolve a single entry; batch replay, archive, and purge with PRO |
| Bulkheads | OSS | Per-compartment concurrency usage | — (read-only) |
| Canary Rollouts | PRO | In-flight canary rollouts | — (read-only) |
| Adaptive Throttle | PRO | The current auto-tuning state | — (read-only) |
| Governance | PRO | The pending-approval queue | — (read-only) |
| Meta-Watchdog | PRO | The self-monitor's health | Force a check, or send a test escalation |
| Runtime Config | PRO | The runtime-editable settings — retry attempts, circuit-breaker thresholds, and the like — grouped by area, each with its current value | Change a value and apply it now or schedule it; cancel a pending change; reset to defaults |

**Rows reflect what is actually running.** A PRO row appears only when its backing service is
genuinely active — the console keys off whether the service is registered (what is running), not off
what a license file claims. If a PRO service is not installed or not started, its row is simply
absent rather than greyed-out or broken.

**Actions are tiered by risk.** The console mirrors the server's own permission model so the
interface matches what the server will actually allow:

| What you observe | When it happens |
|------------------|-----------------|
| A simple "Proceed?" confirmation | A reversible action — for example, replaying a dead-lettered entry |
| A typed `CONFIRM` prompt, plus a note that the server must be unlocked | A destructive action — for example, resetting a breaker, purging the queue, or flipping the kill-switch. The server refuses these with a `403` until it has been explicitly unlocked, and the console names the exact switch to set (`BALDUR_ADMIN_UNLOCK=1`) |
| An extra real-world warning | An action with an external side effect — for example, the Meta-Watchdog escalation test, which warns that it will send a *real* notification to every configured channel |

That unlock requirement is a deliberate second gate: a console left open in a browser tab cannot be
used to force-open production, because the destructive actions stay locked at the server until an
operator turns the switch on intentionally.

**Safe by default, hardened for exposure.** Out of the box the console binds to localhost only, so
it is not reachable from other machines. Reaching it from elsewhere means placing it behind your own
TLS proxy and setting an admin key, which you enter once in the header bar; the console then sends it
with each request. The page is hardened against DNS-rebinding by checking the request's origin, and
every load carries a fresh content-security-policy nonce. All data is rendered as plain text, never
as HTML, so a hostile value in your own data cannot script the page.

**Built for incidents.** You reach for this console precisely when things are wedged, so it is built
to stay usable under stress: each row loads independently (one failing shows an inline
error and leaves the rest working) and every request times out quickly so an unresponsive backend
cannot hang the browser. An optional auto-refresh toggle (off by default) keeps the rows current
during an active incident.

**It says what it does not know.** A check that never answered is never counted as healthy — the
verdict says how many are reporting rather than declaring all-clear over a subsystem that returned
an error. Because auto-refresh ships off, the verdict and the ledger each state how old their data
is, so a console left open overnight tells you it is stale instead of quietly repeating last
night's verdict. The ledger names the window it drew from, and says so when it is showing a sample
of a longer backlog rather than the whole of it. Where a number is not reported, the console leaves
the space empty rather than printing a zero you cannot distinguish from a real one.

## Configuration

The Web Console needs no configuration to use. Once Baldur is initialized the built-in admin server
starts automatically and serves the console at `http://localhost:9090/`.

None of the admin server's settings are part of the stable operator allowlist yet — they are
advanced settings that may change before they are promoted to the stable operator contract.
Reaching the console from beyond localhost (a different bind address behind your own proxy), setting
an access key, naming additional allowed origins, unlocking destructive actions, or turning the
console off entirely are all done through those admin-server settings; see the
[API Reference](../../reference/index.md) for the current names and values.

## Tier behavior

The Web Console is one console for both tiers; what scopes by tier is *which rows appear*. The
triage verdict, the healing ledger and the two-section layout are the same on both.

- **In OSS**: the console is a complete operate-and-recover surface for the core resilience layer.
  You get the OSS rows — the Dashboard summary (the at-a-glance self-healing picture), Circuit
  Breakers with one-click reset and a row per service, System Control (the kill-switch, including a
  dry-run mode), the Dead Letter Queue (browse the backlog; retry or resolve an entry), and
  Bulkheads (per-compartment concurrency at a glance, read-only). None of it depends on PRO.

- **With PRO active**: additional rows appear automatically as their backing PRO services start —
  Emergency mode, Canary rollouts, Adaptive Throttle, Governance, the Meta-Watchdog
  self-monitor, and the Runtime Config editor (change a runtime-editable setting from the browser).
  They surface only when the service is actually running, so the console always reflects what is
  genuinely available. The OSS rows keep working unchanged; the Dead Letter Queue row gains its
  at-scale actions (batch replay, archive, purge), and the rest of the PRO surface is purely
  additive.

## See also

- [Dashboard Service](dashboard-service.md) — the read-model behind the console's Dashboard row
- [System Control](../oss/system-control.md) — the kill-switch the console's System Control row flips
- [Circuit Breaker](../oss/circuit-breaker.md) — what the console's "Reset breaker" action resets
- [DLQ + Replay](dlq-replay.md) — the failure backlog behind the console's Dead Letter Queue row
- [OSS vs PRO tier model](tier-model.md) — why some rows appear only when PRO is running
- [Daily Report](daily-report.md) — the once-a-day digest companion to the console's live view
- [Getting Started](../../getting-started/index.md) — set Baldur up in five minutes
