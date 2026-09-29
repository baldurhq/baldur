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
**healing ledger** charts the dead letter queue over time (what failed into it, what healed, and
how much is still unhealed), so a failure that never reached the queue is not on it. Below that the
checks split in two. **Needs attention** holds the ones that are degraded or broken; **System**
holds the rest as compact rows you can skim, including any check that has not answered, which it
counts as "no signal" rather than as healthy.

Each row is one subsystem. It states its condition in a sentence rather than a field dump ("circuit
closed · 2 failures recorded"), expands in place for the detail, and carries the buttons for the
actions that make sense there. Rows move between the two sections as their state changes, so the
top of the page is always the short list. A row backed by a PRO service is labelled **PRO**;
everything unlabelled is OSS, apart from the Dead Letter Queue row's batch actions, which appear
only with PRO.

Circuit breakers expand one level further: each breaker gets its own row, so you read "payments —
circuit open" rather than "Circuit Breakers — degraded", and the reset, block and allow buttons on
that row already target that service.

| Subsystem | Tier | What it shows | What you can do |
|-----------|------|---------------|-----------------|
| Dashboard | OSS | The rolled-up self-healing summary — status counts, recent activity, an overall health verdict. The counts need a statistics source such as a SQL database; without one they read zero, and the row flags that once the ledger shows entries | — (read-only) |
| Circuit Breakers | OSS | The state of each service's breaker, one row per service | Reset a breaker, or pin it open (Block) or closed (Allow) until the override lapses |
| System Control | OSS | Whether automation is enabled, and the kill-switch state | Enable or disable (kill-switch) automation, with a dry-run mode |
| Emergency | PRO | The current emergency level | Trigger or release emergency mode, or step it down gradually and stop that recovery |
| Dead Letter Queue | OSS | The backlog of failed operations, browsable entry by entry | Retry or resolve a single entry; batch replay, archive, and purge with PRO |
| Bulkheads | OSS | Per-compartment concurrency usage | — (read-only) |
| Canary Rollouts | PRO | Rollouts in progress, finished ones in a history list, and each rollout's metrics | Create a rollout; start, promote, pause, resume, roll back or cancel one; panic-roll back every active rollout at once |
| Throttle | PRO | The adaptive throttle's current limit and state | — (read-only) |
| Governance | PRO | The pending-approval queue | — (read-only) |
| Meta-Watchdog | PRO | The self-monitor's health | Force a check, or send a test escalation |
| Runtime Config | PRO | The runtime-editable settings — retry attempts, circuit-breaker thresholds, and the like — grouped by area, each with its current value | Change a value and apply it now or schedule it (each area says whether running processes pick the change up or may keep the old value until they restart); cancel a pending change; reset to defaults |

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
TLS proxy and setting an admin key, which you paste into the header bar once per browser tab; the
console then sends it with each request. On a localhost bind the server checks each request's
origin to shut out DNS rebinding (on a wider bind it does so once you name the allowed origins), and
every load carries a fresh content-security-policy nonce. All data is rendered as plain text, never
as HTML, so a hostile value in your own data cannot script the page.

**Built for incidents.** You reach for this console precisely when things are wedged, so it is built
to stay usable under stress: each row loads independently (one failing shows an inline error and
leaves the rest working) and every request gives up after five seconds, so an unresponsive backend
cannot hang the browser. Giving up does not recall an action already sent: one that timed out may
still have run on the server, so refresh its row before sending it again. An optional auto-refresh
toggle (off by default) keeps the rows current during an active incident.

**It says what it does not know.** A check that never answered is never counted as healthy: the
verdict adds how many checks are reporting, so a page with one silent check reads "all clear · 9 of
10 reporting" rather than a bare all-clear. Because auto-refresh ships off, the verdict and the
ledger each state how old their data is, so a console left open overnight tells you it is stale
instead of quietly repeating last night's verdict. The ledger names the window it drew from, and
says so when it is showing a sample of a longer backlog rather than the whole of it. Where a number
is not reported, the console shows a dash or leaves the clause out rather than printing a zero you
cannot distinguish from a real one.

## Configuration

The Web Console needs no configuration to use. Once Baldur is initialized the built-in admin server
starts automatically and serves the console at `http://localhost:9090/`, provided that port is free.

Three admin-server settings are on the stable operator allowlist:

| Variable | Default | What it does |
|----------|---------|--------------|
| `BALDUR_ADMIN_UNLOCK` | `false` | Set to `1` to allow the destructive actions; until then the server refuses them with a `403` |
| `BALDUR_ADMIN_PORT` | `9090` | The port the console and the rest of the admin server listen on |
| `BALDUR_ADMIN_ENABLED` | `true` | Set to `false` to run with no admin server and no console |

Check the port before you deploy: `9090` is also Prometheus's default. Baldur does not take a port
another process already serves; it logs a warning and the app keeps running without the console, so
on a host running both, move one of them. The same holds inside one application server that runs
several worker processes. Only the first to start holds the port, and with the default in-process
storage the console shows that process's own breakers and queue, not the others'.

The admin server's other settings are advanced and may change before they are promoted to the
stable operator contract. Reaching the console from beyond localhost (a different bind address
behind your own proxy), setting an access key, naming additional allowed origins, or turning off
just the console page are all done through them; see the [API Reference](../../reference/index.md)
for the current names and values.

## Tier behavior

The Web Console is one console for both tiers; what scopes by tier is *which rows appear*. The
triage verdict, the healing ledger and the two-section layout are the same on both.

- **In OSS**: the console is a complete operate-and-recover surface for the core resilience layer.
  You get the OSS rows: the Dashboard summary (the at-a-glance self-healing picture), Circuit
  Breakers (reset, block or allow a breaker, with a row per service), System Control (the
  kill-switch, including a dry-run mode), the Dead Letter Queue (browse the backlog; retry or resolve
  an entry), and Bulkheads (per-compartment concurrency at a glance, read-only). None of it depends
  on PRO.

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
