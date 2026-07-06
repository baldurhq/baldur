# Runbook Router — Start Here

> **TL;DR**: One "read this first" index for the runbook library. Find the row that
> matches **what you are seeing** (something is wrong) or **what you are turning on**
> (setup), then jump to the runbook that resolves it. Each linked runbook stands
> alone — follow it top to bottom and you are done.
> **Audience**: A new operator/SRE onboarding onto a Baldur-protected service, or an
> on-call engineer who arrives with a symptom and needs the right runbook fast.
> **Cadence**: Read once at onboarding; return whenever you are unsure which runbook applies.

---

## 1. Something is wrong right now (incident triage)

You arrived with a symptom — an alert, a page, or "it stopped working". Match the symptom, confirm the likely cause, follow the runbook.

| Symptom you are seeing | Likely cause | Go to |
|---|---|---|
| A worker is stuck inside `protect()` / `@protected` and never returns; p99 latency jumped to "infinite" (timeouts pile up at the load balancer, not inside the app) | `protect()` no longer adds a default wall-clock timeout, and the underlying I/O client (`httpx`, `psycopg`, `redis-py`, …) was constructed without its own `timeout=` | [protect-hang-troubleshooting.md](protect-hang-troubleshooting.md) |
| You got a `Baldur <component> Failure` page (CRITICAL) | The Meta-Watchdog runs detect-and-escalate only at v1.0 (no automatic recovery) — a component was unhealthy for ~5 consecutive probe cycles and paged you to intervene | [meta-watchdog-escalation-response.md](meta-watchdog-escalation-response.md) |
| Watchdog **pages stopped arriving** entirely | The escalation channel itself may be down | [meta-watchdog-escalation-response.md](meta-watchdog-escalation-response.md#escalation-pipeline-health) |
| A runtime-config edit "didn't do anything", or "broke something else" | "The console shows my new value" and "the running code uses my new value" are two different clocks — the domain you edited may be `on next read`, `after worker restart`, or `not currently reached` | [runtime-config-change.md](runtime-config-change.md) |
| Under a failure storm, the DLQ only absorbs **some** failures (measured 78–97%, run-to-run variance) instead of all | Only one of the two DLQ layers is active — view-level `@dlq_protect` without middleware-level `BALDUR_DLQ_ELIGIBLE_PATHS`, or vice versa | [dlq-two-layer-activation.md](dlq-two-layer-activation.md) |
| On deploy you see a `baldur.gunicorn_hooks_not_installed` WARNING ~2s after startup; on SIGTERM the WAL never flushes, leader leases never release, the LB keeps routing to a dying worker | The graceful-shutdown chain is not wired into gunicorn's hooks | [gunicorn-graceful-shutdown.md](gunicorn-graceful-shutdown.md) |
| Production boot **aborts** with a missing-critical-secret error | A CRITICAL secret (`BALDUR_SECRETS_ENCRYPTION_KEY` / `BALDUR_SECRETS_AUDIT_SIGNING_KEY`) is unset | [secure-deployment.md](secure-deployment.md) |
| Application state was **lost or diverged** after Redis recovered from an outage | Non-tolerable data (money, billing meters, idempotency truth-of-record) was stored inside Baldur's cache, which is an AP system and does not preserve cross-worker consistency during DEGRADED windows | [data-consistency-boundaries.md](data-consistency-boundaries.md) |
| A runtime-config edit is visible on one pod but not others, or a scheduled maintenance job fires twice | Multi-worker coherence is not configured — no shared state backend / cross-pod event bus / leader election | [multi-worker-coherence.md](multi-worker-coherence.md) |
| Slack alerts deliver, but the 📊 Dashboard / ⚙️ Admin Panel / 📖 Runbook buttons are missing | Those buttons are opt-in — their base URLs are unset, so each button is silently omitted (the alert body still delivers) | [slack-alert-action-buttons.md](slack-alert-action-buttons.md) |

---

## 2. I am setting up or turning something on (Day-1)

You are wiring Baldur into a deployment or enabling a capability. Pick the goal.

| Goal | Go to |
|---|---|
| First production deploy — populate CRITICAL secrets and harden TLS | [secure-deployment.md](secure-deployment.md) |
| Make SIGTERM drain cleanly under gunicorn (flush WAL, release leases, evict from LB) | [gunicorn-graceful-shutdown.md](gunicorn-graceful-shutdown.md) |
| Turn on the Audit Trail (off by default) for compliance / regulated data | [audit-trail-activation.md](audit-trail-activation.md) |
| Honor the "DLQ absorbs ALL failures" contract in a Django app (both layers) | [dlq-two-layer-activation.md](dlq-two-layer-activation.md) |
| Stand up metrics / Grafana / OTel (traces, logs) | [observability-stack-setup.md](observability-stack-setup.md) |
| Add one-click action buttons to Slack alerts | [slack-alert-action-buttons.md](slack-alert-action-buttons.md) |
| Expose OpenAPI 3.0 / Swagger UI / ReDoc / the `/features/` inventory | [api-discoverability.md](api-discoverability.md) |
| Scale past a single process (multiple workers / pods) and stay coherent | [multi-worker-coherence.md](multi-worker-coherence.md) |
| Edit runtime config safely via the admin console / `/config/*` REST | [runtime-config-change.md](runtime-config-change.md) |

---

## 3. Before you adopt (architecture decisions)

Read these once, at evaluation or architecture-review time — they shape what you put where, not a step you execute.

| Decision | Go to |
|---|---|
| Which data belongs inside Baldur's resilience layer vs an ACID DB (PostgreSQL/Aurora/Spanner) | [data-consistency-boundaries.md](data-consistency-boundaries.md) |
| What each single-host → multi-pod topology requires (state backend / event bus / leader election / Celery beat) | [multi-worker-coherence.md](multi-worker-coherence.md) |

---

## Maintenance

This router is an index, not a procedure — it duplicates no runbook content, only routes to it.
When you **add, rename, or remove** a runbook, add/adjust a row here **and** in
[README.md](README.md). Per-runbook `Last verified` dates live in `README.md`; this
page carries none, so it does not go stale on a verification refresh.
