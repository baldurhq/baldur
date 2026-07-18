# Meta-Watchdog Escalation Response Runbook

> **Purpose**: When Baldur's own Meta-Watchdog pages you — a `Baldur <component> Failure` notification on PagerDuty/Slack — this runbook tells you, **without reading the source**, how to diagnose and manually remediate each failure mode. At v1.0 the watchdog runs in **detect-and-escalate mode** (`recovery_enabled=False`): it autonomously notices when Baldur's self-healing has stalled and pages a human, but takes **no automatic recovery action**. You are that human.
> **Audience**: On-call operator / SRE who received a `Baldur <component> Failure` page.
> **Cadence**: On every page. Also review the per-component **Graduation Note** sections when deciding whether to promote a component to auto-recovery (Slice B/C).

---

## TL;DR

1. The page always has this shape (title `Baldur <component> Failure`, level CRITICAL):

   ```
   Component '<component>' is unhealthy; automatic recovery is disabled
   (detect-and-escalate mode), so none was attempted.
   Error: <error or 'Unknown'>
   Manual intervention required.
   ```

2. A component pages only after **~5 consecutive unhealthy probe cycles** (`self_cb_failure_threshold`, default 5), and **once per unhealthy episode** — so a page means a sustained problem, not a blip.
3. **Triage first** on the admin server: `GET /meta-watchdog/status` returns `overall_status` + per-component health. Read the `Error:` line in the page, then jump to the matching section below.
4. **After you fix it**, run `POST /meta-watchdog/force-check` to confirm the component returns healthy immediately (don't wait for the next probe cycle).
5. If **pages stop arriving** at all, the channel itself may be down — see [Escalation Pipeline Health](#escalation-pipeline-health).
6. Each component section ends with a **Graduation Note**: what auto-recovery would replace the manual step, which flag flips it on, which graduation slice it belongs to, and the risk. This runbook is the first rung of the `detect → manual runbook → automation` maturity ladder — see [Graduation Ladder](#graduation-ladder-slice-a--b--c).

---

## How the watchdog pages you (mechanics)

The PRO `SelfHealerWatchdog` runs a probe loop on a background daemon thread (started by `baldur.init()`, fork-safe — see `gunicorn-graceful-shutdown.md`). Each cycle probes a fixed set of components; a component must be UNHEALTHY for `self_cb_failure_threshold` consecutive cycles before it pages. Anti-flap is built in: a single healthy cycle resets the counter, so a flapping component (alternating healthy/unhealthy) never reaches the threshold and never pages.

Monitored components, in recovery-priority order (lower = more foundational; budget goes to infrastructure first):

| Component | Priority | Class | Auto-recovery at v1.0 |
|---|---|---|---|
| `redis` | 0 | Infrastructure (everything depends on it) | Off (Slice C target) |
| `dlq` | 1 | Core data pipeline | Off (Slice C target) |
| `circuit_breaker` | 1 | Core resilience | Off (Slice B target) |
| `recovery_pipeline` | 2 | Baldur internal | **None** (no impl — see §) |
| `audit_system` | 2 | Compliance-critical | **Escalation-only by design** |
| `daemon_workers` | 2 | Baldur internal — catch-all liveness for all 31 background worker threads; always active | **Probe-side respawn gate** (separate flag, off by default — see §) |
| `chaos_scheduler` | 3 | Application-level (chaos experiments are off by default at v1.0) | Off (Slice B target) |
| `notification_channels` | 3 | Application-level (meta-critical) | Off (degraded fallback only) |
| `precomputed_cache` | 3 | Application-level | Off (Slice B target) |
| `error_budget_gate` | 3 | Application-level | **Escalation-only by design** |
| `canary_rollout` | 3 | Application-level (semantic-stuck) — active only when the PRO canary rollout service is registered | **Escalation-only by design** |
| `emergency_mode` | 3 | Application-level (semantic-stuck) — active only when the PRO emergency manager is registered | **Escalation-only by design** |
| `adaptive_throttle` | 3 | Application-level (semantic-stuck) — active only when the PRO adaptive throttle is registered | **Escalation-only by design** |

### Admin-server surfaces you will use

All under the admin server (mount prefix is deployment-specific):

| Endpoint | Use |
|---|---|
| `GET /meta-watchdog/status` | Overall + per-component health — **start here** |
| `GET /meta-watchdog/liveness` | Is the watchdog itself alive (K8s liveness) |
| `POST /meta-watchdog/force-check` | Trigger an immediate probe cycle (verify a fix) |
| `POST /meta-watchdog/escalation-test` | Send a test page (verify the delivery channel) |
| `GET circuit_breaker_status` | Per-CB state (for the `circuit_breaker` section) |
| `GET /health/pool` | Connection-pool health (for the `redis` section) |
| `GET /health/gate` / `POST /gate/reset` | Error-budget gate state / reset |

---

## Step 0 — Triage (every page)

1. Open `GET /meta-watchdog/status`. Confirm `overall_status` and note **which** component(s) are unhealthy (the page names one, but check for correlated failures — e.g. `redis` down will cascade to `dlq` and `circuit_breaker`).
2. Read the `Error:` line in the page body — it is `result.error` from the probe and usually names the root cause.
3. If multiple components are unhealthy, **fix the lowest-priority-number first** (`redis` before `dlq` before app-level) — the dependency graph is `redis → dlq/cb → recovery_pipeline → chaos_scheduler`, so the foundational fix often clears the rest.
4. Jump to the matching section.

> **Verify before proceeding**: you know which component paged and have its `Error:` text. If `overall_status` is healthy and no component is unhealthy, the problem already self-cleared (the episode ended) — confirm with the EventJournal record (`meta_watchdog.escalated`) and close the page.

---

## redis — infrastructure (priority 0)

**Symptom**: `Baldur redis Failure`. Redis is the foundation; a redis page usually cascades. `Error:` is typically a `ConnectionError`, `TimeoutError`, or `BusyLoadingError`.

**Diagnose**:
- `GET /health/pool` — pool health and whether the adapter can reach Redis.
- From a shell: `redis-cli -h <host> ping` (PRO Sentinel topology: check the Sentinel master via `redis-cli -p 26379 sentinel get-master-addr-by-name <master>`).

**Manual remediation**:
1. **Confirm Redis itself is up.** If the server/Sentinel master is down, that is the fix — restore Redis infrastructure first. Baldur's cache adapter auto-reconnects on the next operation once Redis is reachable.
2. **If Redis is up but Baldur cannot connect**, the connection pool is stale. The adapter's `reconnect()` runs on the next op; force it by restarting the app workers if needed.
3. **If `Error:` is `AuthenticationError` / `AuthorizationError`**, this is **not** a transient failure — fix the credentials (`BALDUR_REDIS_*`). Baldur deliberately treats auth errors as non-recoverable.

> **Verify before proceeding**: `POST /meta-watchdog/force-check`; `redis` returns healthy and `/health/pool` is green.

**Graduation Note** — auto-recovery target exists (`_recover_redis_impl`, Slice C): a 2-stage strategy — Stage 1 resets the connection pool via the `ProviderRegistry` cache singleton (`reconnect()`), Stage 2 restarts the Redis workload via `RecoveryInfrastructureAdapter.restart_worker(redis_workload_name)`. Auth errors stay non-recoverable by design. **Flag**: `recovery_enabled=True`. **Risk**: high — Stage 2 restarts shared infrastructure; a false-positive restart during a real traffic spike amplifies the incident. Do not graduate until the EventJournal shows redis pages reliably resolve via the pool reset (Stage 1), not by chance.

---

## dlq — core data pipeline (priority 1)

**Symptom**: `Baldur dlq Failure`. The DLQ consumer/worker is stuck. While stuck, failed operations are **not being drained or replayed** — backlog grows.

**Diagnose**:
- `GET /meta-watchdog/status` → `dlq` component detail.
- Check the DLQ worker process liveness and the dead-letter queue depth (the worker is the `celery-dlq-worker` workload — `dlq_worker_workload_name`).

**Manual remediation**:
1. Restart the DLQ worker:
   - K8s: `kubectl rollout restart deployment/celery-dlq-worker`
   - Process/Compose: restart the Celery worker serving the DLQ queue.
2. Confirm the queue depth starts dropping (the worker is draining).

> **Verify before proceeding**: `POST /meta-watchdog/force-check`; `dlq` healthy and queue depth decreasing.

**Graduation Note** — auto-recovery target exists (`_recover_dlq_impl`, Slice C): `RecoveryInfrastructureAdapter.restart_worker(dlq_worker_workload_name)`. **Flag**: `recovery_enabled=True`. **Risk**: medium — a worker restart is idempotent and bounded, but a restart loop on a worker that crashes on a poison message would mask the real defect. Graduate once the EventJournal shows dlq pages are "worker died / wedged" (restart-fixable), not "worker crashing on bad payload" (needs a code fix).

---

## circuit_breaker — core resilience (priority 1)

**Symptom**: `Baldur circuit_breaker Failure`. A circuit breaker is **stuck OPEN**. Normally a CB auto-transitions OPEN → HALF_OPEN after `recovery_timeout` (default 60s) and auto-closes on a successful probe. A stuck-OPEN page means it is **not** recovering — almost always because the protected dependency is still failing.

**Diagnose**:
- `GET circuit_breaker_status` — which CB(s), current state, last error, time in state.
- Identify the **dependency** the CB protects, and check whether that dependency is actually healthy now.

**Manual remediation**:
1. **PRIMARY — fix the root-cause dependency.** Once the dependency is healthy, the CB probes it via HALF_OPEN and closes itself. **Do not force-close a CB whose dependency is still down** — you would immediately re-open it and add a thundering-herd of retries on top of an already-broken dependency.
2. **ONLY if** the dependency is confirmed healthy but the CB is genuinely wedged (clock skew, a stuck half-open gate), force-close it via the circuit-breaker service's `force_close(name)` path.

> **Verify before proceeding**: `GET circuit_breaker_status` shows the CB CLOSED or HALF_OPEN; `POST /meta-watchdog/force-check`; `circuit_breaker` healthy.

**Graduation Note** — auto-recovery target exists (`_recover_circuit_breaker_impl`, Slice B): `force_close()` of stuck-OPEN CBs, bounded by `max_items_per_recovery`. **Flag**: `recovery_enabled=True`. **Risk**: highest of all components — auto force-closing a CB whose dependency is still down causes exactly the thundering-herd failure mode the CB exists to prevent. Graduate **only** with strong EventJournal evidence that the stuck-OPEN pages correlate with *recovered* dependencies (false-open), and only with the per-cycle cap (`max_items_per_recovery`) in place.

---

## recovery_pipeline — Baldur internal (priority 2)

**Symptom**: `Baldur recovery_pipeline Failure`. Baldur's own recovery orchestration (recovery coordinator sessions / replay pipeline) is unhealthy.

**Diagnose**:
- `GET /meta-watchdog/status` → `recovery_pipeline` detail.
- Inspect in-flight recovery-coordinator sessions and the replay backlog.

**Manual remediation**: inspect and restart the recovery pipeline components manually. There is no one-button fix.

**Graduation Note** — **no auto-recovery target exists.** `_recover_recovery_pipeline_impl` is currently a no-op placeholder, so even Slice C has nothing to enable; this component is effectively **permanent escalation-only** until that impl is written.

> ✅ **Resolved**: the recovery-coordinator canary pause/resume steps now call the real canary rollout service and its real resume/pause methods, and report real per-call counts — a **nonzero** `resumed_count` can be trusted. ⚠️ Post-landing verification found the step still typically selects **zero** rollouts in canonical conditions: the service's namespace filter never matches (`CanaryRollout` carries no namespace field), production pause paths tag `triggered_by="manual"` which the recovery whitelists exclude, and the resume governance gate blocks while emergency is still elevated (CANARY_RESUME runs before GOVERNANCE_NORMAL). Expect `resumed_count: 0` until the resume-efficacy work lands (tracked on the maintainer backlog). The phantom-API readiness blocker itself is removed; graduating the pipeline still requires writing the `_recover_recovery_pipeline_impl` (see the Graduation Note above) **and** the resume-efficacy work.

---

## audit_system — compliance-critical (priority 2)

**Symptom**: `Baldur audit_system Failure`. The audit backend (WAL / DB / hash-chain) is unhealthy. This is **escalation-only by design** — Baldur will never auto-remediate its own audit trail.

**Diagnose**:
- `GET audit_health` (admin server).
- Check the audit backend: DB connectivity, WAL disk space, hash-chain integrity.

**Manual remediation**: restore the audit backend (DB connection, free disk for the WAL). **Compliance action**: investigate and document any gap in the audit trail during the outage window — a missing audit record is a compliance event, not just an availability one.

**Graduation Note** — **never graduates.** Auto-remediating an audit subsystem risks silently masking a compliance gap, which defeats the purpose of the audit trail. This is a deliberate escalation-only component.

---

## daemon_workers — Baldur internal (priority 2)

**Symptom**: `Baldur daemon_workers Failure`. One or more of Baldur's 31 background daemon worker threads is DEAD or STALE. This is the **widest** watchdog component — a single bad worker flips the whole component UNHEALTHY (worst-status aggregation) — so the page names the *component*; the `Error:`/reason line names the *worker(s)*: `N unhealthy daemon worker(s): <names>`.

**Diagnose**:
- `GET /meta-watchdog/status` → `component_details.daemon_workers.details.workers` is the per-worker drill-down map: each entry is `HEALTHY` / `STALE` / `DEAD` / `STOPPING` with `heartbeat_age_seconds` (the Web Console renders the same map). Find the worker(s) named in the page reason.
- **State meanings**:
  - **STOPPING** — graceful stop in progress; skipped by the probe, never counts as unhealthy. Ignore.
  - **DEAD** — the worker thread has exited (crashed). The only state the auto-respawn path ever acts on.
  - **STALE** — the thread is alive but its heartbeat is older than its staleness threshold (default: 2× the worker's tick interval via `BALDUR_DAEMON_WORKER_DEFAULT_STALENESS_MULTIPLIER`) — a wedge/livelock, usually the worker loop blocked on I/O. Respawn **never** fires on STALE (restarting a live thread would double-run it).
- Prometheus per-worker signals (all labeled by worker `name`): `baldur_daemon_worker_alive`, `baldur_daemon_worker_last_heartbeat_age_seconds`, `baldur_daemon_worker_restarts_total`.

**Manual remediation**:
1. Locate the named worker in the inventory table below; follow its family/bespoke row if present.
2. **DEAD worker**: check whether the auto-respawn gate (below) already restarted it — `restart_count` in the drill-down entry and `baldur_daemon_worker_restarts_total` rise on each respawn. If respawn is off, ineligible, or exhausted (`respawn_max_attempts`), restart the owning process — every worker is an in-process daemon thread and re-registers on process start.
3. **STALE worker**: check the worker's blocking dependency first — a STALE worker usually points at a stuck I/O dependency, so correlate with `redis` / `dlq` / `audit_system` pages and fix the foundational component first. If the worker alone is wedged, restart the owning process (respawn cannot help — the thread is still alive).

**Worker inventory** (31 production registrations at authoring time; `-*` names are dynamic per-resource/per-experiment):

| Family | Workers |
|---|---|
| Audit pipeline (8) | `AuditSyncWorker`, `AuditFlushWorker`, `AsyncAuditWriter`, `AuditWatchdog`, `PendingSequenceWatchdog`, `WAL-RetentionCleanupScheduler`, `AsyncLoggerAdapter`, `AsyncHealingLogger` |
| DLQ / outbox (2) | `DLQOutboxWorker`, `DLQConsumer-*` (one per resource) |
| Coordination / election / scheduling (2) | `Scheduler-*` (one per resource), `LeaderElector-*` (one per resource — bespoke row below) |
| Event bus / config / IPC / pool (4) | `RedisEventBusListener`, `GlobalConfigPropagatorListener`, `CBStateSnapshotWriter`, `PoolCB-Refresh` |
| Scaling / capacity (3) | `RateController`, `HPAMetricsExporter`, `capacity-reservation-scheduler` |
| Meta probes / guards / feedback (4) | `HealthProbeManager`, `AutoRollbackGuard`, `RuntimeFeedback`, `SelfHealerWatchdog` (PRO) |
| Regional / cell (2) | `PartitionHeartbeat`, `cell-topology-anti-entropy` |
| PRO services (6) | `ThrottleAuditWorker`, `CanaryStateRefresher`, `bulkhead_metrics_updater`, `ChaosWorkerHeartbeat`, `EmergencyGradualRecovery` (bespoke row below), `synthetic-load-*` (one per experiment) |

Bespoke rows (the 2 respawn-ineligible workers) and the catch-all:

| Worker | Why no auto-respawn | Remediation |
|---|---|---|
| `LeaderElector-*` | Split-brain risk — blindly respawning an elector thread could contend a held lease | Restart the owning process; the election protocol handles handover/re-election safely |
| `EmergencyGradualRecovery` | Episodic state machine — registered per recovery episode, self-unregistering; a blind respawn would restart recovery mid-state | Check emergency state (see the `emergency_mode` section): `stop_gradual_recovery()` the wedged episode, then re-trigger recovery or `deactivate()` |
| **Any worker not listed** | (added after this table was authored) | Default path: check the respawn gate below; if it did not or cannot fire, restart the owning process |

> **Episodic workers**: `EmergencyGradualRecovery` and `synthetic-load-*` register per operation and self-unregister — their *absence* from the drill-down map is normal.

**Auto-respawn gate (probe-side — NOT the watchdog's `recovery_enabled`)**: dead-worker respawn is a separate path inside the probe itself. It fires only on **DEAD** (never STALE), and only when **all** of these hold: `BALDUR_DAEMON_WORKER_RESPAWN_ENABLED=true` (default **false**) AND the worker registered a restart callback (29 of 31 are eligible; the 2 bespoke rows above are not). Attempts are capped (`BALDUR_DAEMON_WORKER_RESPAWN_MAX_ATTEMPTS`, default 3) with exponential backoff, and the attempt counter resets after sustained health (`BALDUR_DAEMON_WORKER_RESPAWN_COUNT_RESET_SECONDS`, default 3600).

> **Verify before proceeding**: `POST /meta-watchdog/force-check`; `daemon_workers` healthy — every entry in the drill-down map is `HEALTHY` or `STOPPING`.

**Graduation Note** — `daemon_workers` does **not** graduate via `recovery_enabled`: the watchdog has no `_recover_*` impl for it (an unhealthy `daemon_workers` always escalates). The auto path is the **probe-side respawn gate** above — flag `BALDUR_DAEMON_WORKER_RESPAWN_ENABLED` + per-worker restart-callback eligibility (29 of 31), DEAD-only. **Risk**: low — respawn is attempt-capped with backoff, never touches the 2 ineligible workers, and never acts on STALE wedges (those stay manual). Graduate it by flipping the probe-side flag once the restart counter dashboards show DEAD events are transient crashes, not crash loops.

---

## notification_channels — meta-critical (priority 3)

**Symptom**: `Baldur notification_channels Failure`. The delivery channel (Slack/PagerDuty) is itself unhealthy.

> ⚠️ **Meta-critical**: if the channel that delivers pages is down, *other* escalations may not reach you. Baldur falls back to a disk JSONL record on genuine delivery failure (`_record_fallback_escalation`) — **check that file for pages you never received** while the channel was down.

**Diagnose**:
- `POST /meta-watchdog/escalation-test` — sends a test page; tells you whether delivery works now.
- Verify the channel config: Slack webhook URL, PagerDuty routing key.
- Inspect the fallback JSONL on disk for queued/missed escalations.

**Manual remediation**:
1. Fix the channel config (rotate the webhook, correct the routing key).
2. `POST /meta-watchdog/escalation-test` until it succeeds.
3. **Replay missed pages**: read the fallback JSONL and action any escalation that fired while the channel was down.

> **Verify before proceeding**: `POST /meta-watchdog/escalation-test` succeeds; no new fallback JSONL entries are being written.

**Graduation Note** — auto-recovery target exists (`_recover_notification_channels_impl`): registers Stdout + Logging fallback adapters so pages are at least captured in local logs. **Flag**: `recovery_enabled=True`. **Risk**: low, but note this is a **degraded fallback, not a real fix** — the operator must still restore the real channel; auto-registering local-log adapters only prevents total page loss.

---

## precomputed_cache — application-level (priority 3)

**Symptom**: `Baldur precomputed_cache Failure`. The precomputed-cache proactive-refresh worker has stopped.

**Diagnose**: `GET /meta-watchdog/status` → `precomputed_cache` detail.

**Manual remediation**: restart the precomputed-cache worker (`get_precomputed_cache_worker().start()` via the app, or restart the app workers).

> **Verify before proceeding**: `POST /meta-watchdog/force-check`; `precomputed_cache` healthy.

**Graduation Note** — auto-recovery target exists (`_recover_precomputed_cache_impl`, Slice B): restarts the stopped worker (`worker.start()`). **Flag**: `recovery_enabled=True`. **Risk**: low — restarting a stateless refresh worker is safe; this is a good early graduation candidate.

---

## error_budget_gate — application-level (priority 3)

**Symptom**: `Baldur error_budget_gate Failure`. Escalation-only.

**Diagnose**: `GET /health/gate` — gate state and config.

**Manual remediation**: inspect the error-budget state; if the gate is wedged (not the budget genuinely exhausted), `POST /gate/reset`.

**Graduation Note** — escalation-only. The gate's job is to *block* automation when the budget is exhausted; auto-resetting it would defeat its purpose. Manual reset only.

---

## chaos_scheduler — application-level (priority 3)

**Symptom**: `Baldur chaos_scheduler Failure`. Relevant only when chaos experiments are scheduled (a PRO feature, off by default at v1.0). The probe's details include `zombie_experiments` — experiments whose lease/lifecycle has lapsed without cleanup.

**Manual remediation**: inspect the chaos scheduler; pause scheduled experiments if they are the source of instability; clean up any zombie experiments the probe details name.

**Graduation Note** — auto-recovery target exists (`_recover_chaos_scheduler_impl`, Slice B): item-capped zombie-experiment cleanup via the in-process `scheduler.cleanup_zombie_experiment()`, fed live by the probe's `zombie_experiments` details. **Flag**: `recovery_enabled=True` — the same single flip as every other impl, with **no per-component opt-out**: when Slice B is promoted, zombie cleanup **will** auto-run. **Risk**: low — cleanup of already-zombie experiments, bounded by `max_items_per_recovery`.

---

## canary_rollout — application-level (priority 3)

**Symptom**: `Baldur canary_rollout Failure`. A **live** canary rollout is semantically stuck — wedged at a stage, not crashed (worker liveness is `daemon_workers`' job). Stuck means one of: in CANARY beyond 2× the stage duration, PAUSED past the zombie threshold, or PROMOTING past the transition window — the same stall definitions as the Celery canary watchdog (both read `RolloutWatchdog.detect_stalled_rollouts()`, a read-only shared singleton, so the two cannot drift). Active only when the PRO canary rollout service is registered; escalation-only.

**Diagnose**:
- The page reason lists up to 5 stalled rollouts with per-rollout stall reasons: `N canary rollout(s) stalled: <reasons>`.
- `GET /meta-watchdog/status` → `component_details.canary_rollout.details` carries `stalled_count` + `rollout_ids`.
- Inspect each rollout in the Web Console canary panel, or via the canary service: `get_rollout(rollout_id)` / `get_active_rollouts()` / `get_paused_rollouts()`.

**Manual remediation** (per stalled state, via the canary service or its console actions):
1. **PAUSED zombie**: decide the rollout's fate — `resume(rollout_id)` if the pause reason is resolved, `rollback(rollout_id, reason=...)` or `cancel(rollout_id, reason=...)` if abandoned.
2. **CANARY stage overstay**: the stage health evaluation is not concluding — check the promotion gates (is a metrics provider connected? is governance blocking?), then `promote(rollout_id)` or `rollback(rollout_id, reason=...)` manually.
3. **PROMOTING overstay**: the config transition is wedged mid-apply — check the runtime-config apply path (section lock, pending changes), then `rollback(rollout_id, reason=...)` if the target state cannot be reached.

> **Verify before proceeding**: `POST /meta-watchdog/force-check`; `canary_rollout` healthy (`stalled_count` 0).

**Graduation Note** — **escalation-only by design** (same class as `audit_system` / `error_budget_gate`): a stalled rollout needs a promote-vs-rollback judgment call; auto-resolving would either promote an unvalidated config or revert a healthy one. Never graduates.

---

## emergency_mode — application-level (priority 3)

**Symptom**: `Baldur emergency_mode Failure`. The emergency *level* is failing to release — frozen, not crashed (the gradual-recovery worker's liveness is `daemon_workers`' job). Two clauses, named in the page reason:
- **Recovery wedged** (primary): gradual recovery has been running (`is_recovering`) past `emergency_stuck_threshold_seconds` (default 1800) — the recovery worker is alive but retrying a perpetually-failing gate forever.
- **Auto-triggered overstay** (backstop): an auto-triggered, non-recovering level held past the threshold with its expiry lapsed — auto-deactivation itself failed.

Operator-held levels (manually activated, not recovering) are deliberately excluded — an intentional incident response never pages. Active only when the PRO emergency manager is registered; escalation-only.

**Diagnose**:
- `GET /meta-watchdog/status` → `component_details.emergency_mode.details`: `level`, `is_recovering`, `is_auto_triggered`, `wedged_since`, `clause`.
- Check emergency state via the Web Console emergency panel or the emergency manager's `get_state()` — for a wedged recovery, identify which recovery gate keeps failing (system still genuinely unstable vs. a stuck gate input).

**Manual remediation** (via the emergency manager or its console actions):
1. **Recovery wedged**: if the system is still genuinely under stress, the hold is correct — fix the underlying stress first. If the gate input is stuck/false, `stop_gradual_recovery()` the wedged episode, then either re-run `start_gradual_recovery()` or `deactivate()` directly once stability is verified.
2. **Auto-triggered overstay**: auto-deactivation failed — verify stability, then `deactivate()` manually (or start a gradual recovery if a stepped release is safer).

> **Verify before proceeding**: `POST /meta-watchdog/force-check`; `emergency_mode` healthy (level NORMAL, or a recovery in progress with a fresh `recovery_started_at`).

**Graduation Note** — **escalation-only by design**: auto-releasing a held emergency level would re-admit shed traffic on a system the healer itself could not verify as stable. Never graduates.

---

## adaptive_throttle — application-level (priority 3)

**Symptom**: `Baldur adaptive_throttle Failure`. The throttle's `current_limit` is **frozen while constrained** — near-zero variance across the sample window while in full-stop, in emergency, or pinned at its floor with rejections still rising. The floor case is demand-gated: an idle throttle resting at its floor with no denied traffic never pages. Reason: `Throttle limit frozen at N while constrained (variance V, error_rate E%)`. Active only when the PRO adaptive throttle is registered; escalation-only.

**Diagnose**:
- `GET /meta-watchdog/status` → `component_details.adaptive_throttle.details`: `current_limit`, `min_limit`, `variance`, `constrained`, `full_stop_active`, `emergency_active`, `dampening_active`.
- Inspect the throttle state: `get_stats()` on the adaptive throttle (Web Console throttle panel shows the same) — is `full_stop_active` / `emergency_active` still on? Are `rejected_requests` still rising?

**Manual remediation**:
1. **Full-stop / emergency still active**: the throttle is held down by an emergency signal that never clears — this is usually a correlated `emergency_mode` problem; fix that first (see its section). Once the emergency clears, the throttle's recovery ramp resumes on its own.
2. **Frozen at floor under real demand** (no emergency active): the adaptation loop is not moving the limit — check `dampening_active` (a recovery-dampening hold) and whether the protected dependency is genuinely still slow (a correctly-pinned limit is protection, not a wedge). If the limit is verifiably wedged, restart the owning process — the throttle re-initializes and re-adapts from its initial limit.

> **Verify before proceeding**: `POST /meta-watchdog/force-check`; `adaptive_throttle` healthy (limit moving again, or no longer constrained).

**Graduation Note** — **escalation-only by design**: force-raising a frozen limit on a still-distressed service re-admits exactly the load the throttle is shedding. Never graduates.

---

## Graduation Ladder (Slice A → B → C)

The watchdog ships as risk-graded slices, promoted by **data, not by date** (the `observe before you remediate` SRE maturity ladder):

- **Slice A (v1.0, now)** — autonomous DETECTION + ESCALATION. No recovery actions. This is `recovery_enabled=False`.
- **Slice B (deferred)** — in-process auto-recovery: CB `force_close`, precomputed-cache worker restart, chaos-scheduler zombie cleanup. Low-blast-radius, same-process actions.
- **Slice C (deferred)** — infrastructure-level recovery: Redis / DLQ-worker restart via `RecoveryInfrastructureAdapter`. Highest blast radius.

> Daemon-worker auto-respawn is **not** a `recovery_enabled` slice item — it is a separate **probe-side** gate (`BALDUR_DAEMON_WORKER_RESPAWN_ENABLED` + per-worker restart-callback eligibility, DEAD-only) and graduates independently; see the `daemon_workers` section.

`recovery_enabled` is a single bool, tracked as a tier contract (`Deferred/false`) in `baldur/_data/V1_LAUNCH_MANIFEST.yaml` and enforced by the v1.0-default-enable fitness function — so a slice promotion is **one manifest flip**, which is the data-driven gate.

### The data source

Every escalation writes a durable, queryable EventJournal record. Query the failure-mode history that feeds the promotion decision:

```python
query(JournalQueryFilter(event_types=["meta_watchdog.escalated"], start_time=...))
```

> **Durability caveat**: the EventJournal backend defaults to `"memory"` (per-process, lost on restart). For cross-worker, restart-durable gate data, set `BALDUR_EVENT_JOURNAL_BACKEND=redis` (PRO's Sentinel topology supports it). This does not affect detect+escalate — the journal is forward-looking telemetry, not load-bearing for paging.

### Gate criteria (skeleton — thresholds calibrate after production data)

A component graduates from Slice A → auto-recovery only when **all** of these hold. The numeric thresholds are intentionally left to calibrate against real Slice-A escalation data (DP / Founding-50 cohort) — setting them pre-production would be guesswork.

1. **A proven manual remediation exists** — the matching section in this runbook, executed successfully in production at least once.
2. **The failure mode recurs** often enough to be worth automating (frequency threshold — *TBD from EventJournal*).
3. **The remediation is deterministic** — the same action resolves it every time (automatable, not judgment-dependent).
4. **Low false-positive rate** — the auto-action would not fire on a transient/false page (*TBD from EventJournal*).
5. **Dry-run validated** — the recovery impl does the right thing under `dry_run_mode` against a real instance of the failure.
6. **Readiness blockers closed** — e.g. the `recovery_pipeline` / canary resume-readiness blocker (now closed; see that section's note).

### Per-component graduation map

| Component | Manual step (this runbook) | Auto-recovery impl | Slice | Risk | Blocker |
|---|---|---|---|---|---|
| `precomputed_cache` | restart worker | `_recover_precomputed_cache_impl` (`worker.start()`) | B | Low | — |
| `notification_channels` | fix channel + replay JSONL | `_recover_notification_channels_impl` (fallback adapters) | B | Low (degraded) | — |
| `circuit_breaker` | fix dependency / force_close | `_recover_circuit_breaker_impl` (`force_close`) | B | **Highest** | — |
| `chaos_scheduler` | pause experiments / clean up zombies | `_recover_chaos_scheduler_impl` (item-capped zombie cleanup) | B | Low | — |
| `dlq` | restart DLQ worker | `_recover_dlq_impl` (`restart_worker`) | C | Medium | — |
| `redis` | restore Redis / reconnect | `_recover_redis_impl` (pool reset → restart) | C | High | — |
| `daemon_workers` | per-worker triage / restart owning process | **probe-side respawn** — `BALDUR_DAEMON_WORKER_RESPAWN_ENABLED` + per-worker eligibility (29 of 31), DEAD-only; independent of `recovery_enabled` | — (separate flag) | Low | — |
| `recovery_pipeline` | manual inspect/restart | **none (placeholder)** | — | — | impl placeholder + resume-efficacy stack (maintainer backlog) |
| `audit_system` | restore audit backend | — | never | — | by design |
| `error_budget_gate` | `/gate/reset` | — | never | — | by design |
| `canary_rollout` | resume / rollback / cancel stalled rollout | — | never | — | by design |
| `emergency_mode` | stop wedged recovery / deactivate | — | never | — | by design |
| `adaptive_throttle` | clear emergency hold / restart owning process | — | never | — | by design |

---

## Escalation Pipeline Health

If you suspect pages are **not arriving** (silence is not the same as healthy):

1. `GET /meta-watchdog/liveness` — confirm the watchdog daemon is alive at all. Under Gunicorn `--preload`, threads do not survive `fork()`; the watchdog deliberately skips the master and runs in workers (see `gunicorn-graceful-shutdown.md`).
2. `POST /meta-watchdog/escalation-test` — sends a synthetic page through the real channel.
3. Check the fallback JSONL on disk — genuine delivery failures are recorded there even when no channel works. Any entries are pages you did not receive; action them.
4. If the watchdog is alive but not paging, confirm `meta_watchdog.enabled` and `meta_watchdog.escalation_enabled` are both `True` (v1.0 defaults) and that the component is genuinely staying unhealthy for ≥ `self_cb_failure_threshold` cycles.

---

## See also

- `docs/runbooks/gunicorn-graceful-shutdown.md` — why the watchdog runs in workers, not the Gunicorn master.
