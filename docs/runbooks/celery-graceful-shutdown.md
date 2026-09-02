# Celery Graceful Shutdown Runbook

> **Purpose**: Understand what baldur runs when a celery worker stops, and size the platform's stop timeout so it runs to the end instead of being killed halfway.
> **Audience**: Operator running baldur under celery at OSS or PRO tier.
> **Cadence**: One-time read at deployment + revisit when changing `terminationGracePeriodSeconds` / `TimeoutStopSec`, `--max-tasks-per-child`, or any recovery-shutdown setting.

---

## TL;DR

Nothing to wire. The same `connect_celery_bootstrap_receivers()` that starts baldur in a celery worker also connects its stop side, so if your worker boots with baldur it also stops with baldur.

**What you do have to do is give the worker enough time to stop.** The window baldur needs at defaults is about **42 seconds**; Kubernetes grants **30** unless you say otherwise. Set `terminationGracePeriodSeconds` to the real number and tell baldur what it is:

```yaml
spec:
  terminationGracePeriodSeconds: 45     # >= child teardown + drain + main teardown
  containers:
    - name: worker
      env:
        - name: BALDUR_RECOVERY_SHUTDOWN_MAX_SHUTDOWN_WAIT_SECONDS
          value: "45"                   # your ACTUAL grace period
```

The second variable is what arms baldur's own cross-validator: it warns at startup when the drain timeout does not fit inside the grace period. Its default is 600 s, so it stays silent for anyone who never set it — which means a misconfigured deployment gets no warning until a stop is cut short.

---

## What Baldur Runs at Worker Stop

Two processes, two different pipelines. Both are connected by the same function that connects the start-side receivers.

### The pool child (`worker_process_shutdown`)

Runs in every process that executes tasks — every prefork child, including the replacements a `--max-tasks-per-child` recycle forks in. It is the child's last executable frame before the process exits, and it is reached on every child-exit lane: the recycle return, the pool-shutdown sentinel, an uncaught task-loop exception, and a terminating signal.

1. **DLQ outbox teardown** — flush the buffered DLQ entries through the real store, join the writer thread, spill whatever is left to the local fallback tier.
2. **Audit flush** — close the WAL, save the checkpoint, drain the disk buffer.
3. **`shutdown.worker_exit_completed`** (INFO) with `process_role="celery_pool_child"`.

It deliberately does **not** run the shutdown coordinator's drain. A pool child inherited the parent's handler list rather than registering its own, so firing it would run leader-election release and exporter teardown against state the child does not own — and a recycle is routine operation that must not pay a full drain every time.

### The worker main process (`worker_shutdown`)

Runs when the worker itself stops, after the pool has stopped and the task blueprint has joined — so no task is running. This is the celery equivalent of the gunicorn `worker_exit` pipeline:

1. **Initiate the coordinator drain** and wait for it, minus the reserve below.
2. **`shutdown.worker_drained`** (INFO) or **`shutdown.worker_drain_incomplete`** (WARNING) — or neither, when nothing was ever initiated.
3. **DLQ outbox teardown**, unconditionally.
4. **Audit flush**, unconditionally.
5. **`shutdown.worker_exit_completed`** (INFO) with `process_role="celery_worker_main"`.

Steps 3 and 4 are unconditional on purpose: when the drain converged, the coordinator's own handlers already ran both and their once-guards make these no-ops; when it did not, they are the only teardown this process gets. The main process matters even on a forking pool — the scheduler, the admin server and leader election live there, and they capture DLQ entries of their own.

**Step 1 reserves step 3's budget.** The drain waits on *other* subsystems while baldur's own teardown is queued behind it, so the wait is shortened by the outbox teardown's worst case (`BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS` + 1 s). Without that reserve a slow drain would consume the whole window and the platform's SIGKILL would land before the teardown ever ran. A drain that needed those seconds says so in step 2.

---

## Sizing the Stop Window

**There is no in-process watcher.** Unlike gunicorn — whose arbiter enforces `--timeout` on every worker — celery joins its pool children with no timeout at all. The only thing bounding a celery worker's stop is the platform: Kubernetes `terminationGracePeriodSeconds` (default **30 s**), systemd `TimeoutStopSec`, or your process supervisor's equivalent.

The window baldur needs is:

```
child teardown  +  coordinator drain  +  main teardown
     ~6 s       +        30 s         +      ~6 s        =  ~42 s
```

| Term | Setting | Default |
|------|---------|---------|
| Child teardown | `BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS` + 1 s spill floor + audit flush | ~6 s |
| Coordinator drain | `BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` | 30.0 |
| Main teardown | same as the child teardown | ~6 s |

The children tear down in parallel, so the first term is the slowest child, not their sum. It is nonetheless serialized *ahead* of the drain: celery stops the pool before it sends the main-process stop signal.

**At the Kubernetes default of 30 s, SIGKILL lands before the main process's teardown runs.** The children spend their ~6 s first, so the drain is cut at ~24 s — before the coordinator's force branch, before the outbox teardown, and with no terminal log line to say what happened. Raise the grace period, or lower `BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` to fit inside it.

Then set `BALDUR_RECOVERY_SHUTDOWN_MAX_SHUTDOWN_WAIT_SECONDS` to the grace period you actually granted. Baldur cross-validates the drain timeout against it at startup and warns when they do not fit — but only if you have told it the truth, since the shipped default of 600 s fits everything.

---

## Cold Shutdown Is Not Covered

Celery's **cold** shutdown path — `SIGQUIT`, or a second `Ctrl-C` — terminates the worker without sending the stop signal baldur receives. Nothing above runs, and the buffered DLQ entries are lost with the process.

Stop workers with `SIGTERM` (which is what Kubernetes, systemd and `docker stop` send by default). Also check that you have **not** set `REMAP_SIGTERM=SIGQUIT`: that environment variable makes celery treat `SIGTERM` itself as a cold shutdown, so the documented graceful signal stops reaching any teardown.

---

## Verifying It Ran

One event name answers "did this process's exit pipeline run to the end", on every adapter:

```
shutdown.worker_exit_completed  process_role=celery_pool_child   worker_id=<pid>
shutdown.worker_exit_completed  process_role=celery_worker_main  worker_id=<pid>
```

Both carry the outbox teardown's terminal counts, so one line tells you how many buffered DLQ entries the process still held and where they went:

| Field | Meaning |
|-------|---------|
| `outbox_pending_at_entry` | Entries the outbox still owned when the teardown began |
| `outbox_dispatched` | Handed to the DLQ store without error |
| `outbox_soft_failed` | Store write failed, local fallback preserved the entry |
| `outbox_failed` | Reached no store at all — **lost** |
| `outbox_emergency_dumped` | Written to the local fallback by the shutdown spill |
| `outbox_residual` | Handed to the spill and not written — **lost** |
| `outbox_duplicated` | Counted in two buckets by design (the spill is at-least-once) |

A non-zero `outbox_residual` also emits its own CRITICAL line, `dlq_outbox.shutdown_dump_incomplete`. It means the spill hit its deadline: the fallback destination is too slow for the budget. Raise `BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS` (and the grace period with it), or move the fallback destination to faster storage.

**Absence of these lines after a stop** means the pipeline did not run: a cold shutdown, a SIGKILL, or a worker whose baldur receivers were never connected. Check the start side first — an unarmed stop side is exactly an unarmed start side.

---

## Settings Reference

| Environment variable | Default | What it bounds |
|----------------------|---------|----------------|
| `BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS` | 5.0 | Total DLQ outbox teardown budget (flush + join + spill), per exiting process |
| `BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` | 30.0 | Coordinator drain wait in the worker main process |
| `BALDUR_RECOVERY_SHUTDOWN_MAX_SHUTDOWN_WAIT_SECONDS` | 600.0 | The grace period you actually granted; arms the startup cross-validator |
| `BALDUR_DLQ_OUTBOX_ENABLED` | true | Set false to skip the async outbox entirely (DLQ writes become synchronous) |

---

## Related

- [Gunicorn Graceful Shutdown Runbook](gunicorn-graceful-shutdown.md) — the same pipeline for the WSGI side, including the two-timeout pre-flight check
- [DLQ Two-Layer Activation](dlq-two-layer-activation.md) — what the DLQ outbox buffers and where its local fallback writes
