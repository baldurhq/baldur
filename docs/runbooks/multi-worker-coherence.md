# Multi-Worker Coherence Runbook

> **Purpose**: Explain how Baldur's scheduled jobs and runtime-config changes stay coherent as you scale from a single host to multiple workers/pods, and which settings each topology requires.
> **Audience**: Operator running Baldur on more than one process (gunicorn `-w N`, multiple pods) who edits runtime config via the console/API or relies on scheduled maintenance jobs.
> **Cadence**: One-time when you move past a single process; refer back when adding pods or switching to Celery beat.

---

## TL;DR

- **Single host (default)** — works out-of-box, no external infra. One process per host elects itself via a local file lock and runs the scheduled jobs (including applying DELAYED/GRACEFUL config changes). Nothing to configure.
- **Multiple pods/hosts** — set all three:

  ```bash
  export BALDUR_SYSTEM_CONTROL_BACKEND=redis     # shared state backend
  export BALDUR_EVENT_BUS_BACKEND=redis          # cross-pod CONFIG_UPDATED
  export BALDUR_LEADER_ELECTION_ENABLED=1        # one scheduler cluster-wide
  export BALDUR_LEADER_ELECTION_BACKEND=redis
  export BALDUR_REDIS_URL=redis://your-redis:6379/0
  ```

  (Requires `baldur-pro` for the Redis leader elector.) **Or** run Celery beat instead of the inline scheduler and set `BALDUR_SCHEDULER_AUTOSTART=0`.

---

## Why topology matters

Three independent mechanisms must line up for a runtime-config change made on one process to be coherent on the others, and for a scheduled job to run exactly once:

| Mechanism | Default | What it governs |
|-----------|---------|-----------------|
| State backend | `file` (single host) | Where pending changes + applied config live. Shared only when `redis`. |
| EventBus backend | `memory` (in-process) | `CONFIG_UPDATED` propagation to live resilience consumers. Cross-pod only when `redis`. |
| Leader election | disabled | Which process runs the scheduler. Single-host uses a local file lock; cross-host needs Redis/K8s. |

When these are at their defaults, everything is correct **on one host** and silently degrades across hosts (a change applied on pod A is invisible to pod B; a leader-only scheduler may run per-host).

---

## Single host (OSS / small-team baseline)

No configuration needed. With leader election disabled (the default), `baldur.init()` constructs the inline scheduler with a `LocalFileLeaderElector`: exactly one process per host acquires an OS file lock (`<temp>/baldur-scheduler-<service>.lock`) and runs the scheduled jobs; the others stay followers and take over automatically if the leader dies. This covers `gunicorn -w N` on a single host — the N workers contend for one lock, so only one runs the scheduler.

The scheduled jobs include applying due **DELAYED/GRACEFUL** runtime-config changes (every 30s), so editing a delayed-default section in the console and clicking **Apply** now actually takes effect after the delay.

To opt out entirely (e.g. you schedule jobs another way), set `BALDUR_SCHEDULER_AUTOSTART=0`.

---

## Multiple workers / pods (PRO growth-stage baseline)

Cross-pod coherence and true single-execution require all of:

### 1. Shared state backend

```bash
export BALDUR_SYSTEM_CONTROL_BACKEND=redis
export BALDUR_REDIS_URL=redis://your-redis:6379/0
```

Without this each pod keeps its own file/memory state, so a pending change created on pod A is never visible to the applier on pod B.

### 2. Cross-pod event propagation

```bash
export BALDUR_EVENT_BUS_BACKEND=redis
# optional dedicated bus URL; falls back to BALDUR_REDIS_URL:
# export BALDUR_EVENT_BUS_REDIS_URL=redis://your-redis:6379/1
```

`CONFIG_UPDATED` is how live resilience consumers (hedging, adaptive throttle, etc.) pick up an applied change without a restart. With `EVENT_BUS_BACKEND=memory` (the default) those events stay in-process, so other pods' consumers keep the old value until they restart.

### 3. Distributed leader election (or Celery beat)

```bash
export BALDUR_LEADER_ELECTION_ENABLED=1
export BALDUR_LEADER_ELECTION_BACKEND=redis   # or kubernetes
```

Requires `baldur-pro` (Redis elector) or `baldur-pro[kubernetes]`. This elects one scheduler **cluster-wide**. With leader election disabled across multiple hosts, the local file-lock elector elects one scheduler **per host** — the maintenance jobs are idempotent so this is bounded (duplicate, not wrong), but you get one scheduler per host rather than one per cluster.

**Alternative — Celery beat.** If you already run Celery beat, call `configure_baldur_celery(app)` (it schedules `apply-pending-config-changes` every 30s on the `maintenance` queue) and disable the inline scheduler so it does not also run the jobs:

```bash
export BALDUR_SCHEDULER_AUTOSTART=0
```

The config-apply task is idempotent (diff-aware + status-guarded), so leaving the inline scheduler on alongside beat is wasteful, not wrong — but `AUTOSTART=0` is the clean choice.

---

## Residual: concurrent same-section writes

Cross-pod **read/apply** coherence is what this runbook covers (the applier sees cross-pod pending changes; console reads reflect changes applied elsewhere). It does **not** add cross-process optimistic concurrency for two operators editing the **same** config section in the same read→write window — that last-writer-wins residual is rollback-recoverable via ConfigHistory and is a known limitation tracked for a future release.

---

## Verification

- **Single host**: edit a DELAYED-default section (e.g. `circuit_breaker`) in the console, click **Apply**, wait past the delay, reload the panel — the value should reflect the change.
- **Scheduler running**: look for `scheduler.default_jobs_started` and `local_file_elector.acquired` in the logs of exactly one process.
- **Multi-pod**: apply a change on one pod, then `GET /config/editable` against another pod — it should show the new value (cold console read reloads from the shared backend).
