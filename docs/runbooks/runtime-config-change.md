# Runtime Config Change Runbook

> **TL;DR**: The PRO admin console **Panel: Runtime Config** lets you edit ~11 config
> domains at runtime. A safe change is: read the current value, apply through the
> console/REST (not the raw store), confirm the apply response, then confirm the
> *live* effect — because "the console shows my new value" and "the running code
> uses my new value" are **two different things** in v1.0.
> **Audience**: A PRO operator/on-call editing runtime config through the admin
> console or the `/config/*` REST API, who needs to know a change's blast radius and
> when it actually takes effect before touching a production setting.
> **Cadence**: Task-time. Reach for this before every non-trivial config edit, and
> when an edit "didn't do anything" or "broke something else".

---

## TL;DR

- **Two clocks, not one.** *Applying* a change (persisting it, possibly after a
  delay) is separate from a running consumer *observing* it. Each domain below has a
  `**Takes effect**` line telling you which.
- **Reach classes:**
  - `on next read` — the next request/operation picks it up, no restart (`retry`,
    `rate_limit`, `replay_automation`, `sla`, `security`, `notification`, `forensic`,
    `metrics`).
  - `within 30s (read cache TTL)` — the value is observed within a per-process
    read-cache TTL (~30s) on the pod that applied it, no restart (`idempotency`; also
    `metrics.enabled`).
  - `after worker restart` — the value persists, but the live component keeps the old
    value until you restart workers (`circuit_breaker`, `dlq`; plus a few
    constructor-captured per-field exceptions called out in each domain entry —
    `notification` channel targets, `idempotency` TTL fields, `metrics` install gates).
  - `not currently reached` — the value persists and the console shows it, but no
    running consumer reads it. No console-visible domain is in this class today; it
    remains defined for fields deliberately curated out of the editable projection.
- **Per-pod convergence.** Every reach class above describes the pod that *applied* the
  change (and any pod after restart). In a multi-pod deployment, non-applier pods do not
  converge on that timeline — the config cache is per-process and reloads only at process
  start or on a console-display read, and the in-process config-updated signal is not a
  cross-pod one — so other pods converge on restart (or via the deferred cross-pod
  push-reload). This is the same cross-worker boundary the `circuit_breaker` / `dlq`
  domains already carry, not a new limitation. See
  [multi-worker-coherence.md](multi-worker-coherence.md).
- **Always change through the console or `/config/*` REST.** Never mutate the backing
  store directly — that bypasses history + audit and leaves you no rollback.
- **Every change is recoverable** via config history rollback, or global reset as a
  last resort. See `## Rollback`.

---

## Before you change anything

Work through this once per change. All endpoints are relative to the admin server
base (the console/control plane, e.g. `http://localhost:9090`).

1. **Know the reach class.** Find the domain in the `## Blast-radius map` below and
   read its `**Takes effect**` line — it tells you *when* a running consumer observes
   the edit (`on next read`, `within 30s (read cache TTL)`, or `after worker restart`),
   and flags any per-field exceptions.
2. **Read the current value first.** `GET /config/<section>` (viewer). Record it — it
   is your manual rollback target if history is unavailable.
   - Section slugs are **hyphenated**: `/config/circuit-breaker`, `/config/rate-limit`,
     `/config/replay-automation`, `/config/dlq`, `/config/retry`, `/config/sla`,
     `/config/security`, `/config/idempotency`, `/config/notification`,
     `/config/forensic`, `/config/metrics`.
3. **Confirm nobody else is mid-edit.** `GET /config/pending` lists in-flight
   DELAYED/GRACEFUL changes. A pending change on your section means another operator's
   edit is queued — coordinate before stacking a change.
4. **Know the risk tier.** Safety-critical domains (below) can block traffic or lose
   data if mistuned. Change one field at a time; never batch a safety-critical edit
   with an unrelated one.

**Before moving to the next step:** you have the current value recorded, you know the
reach class, and `GET /config/pending` shows no conflicting queued change for your
section.

---

## How a change reaches running code

Two independent things happen:

**1. Apply (persistence).** Each domain has a default apply *strategy*:

- **IMMEDIATE** — persisted at once (`rate_limit`, `sla`, `metrics`, `notification`,
  `forensic`, and any domain with no explicit default).
- **DELAYED** — queued and persisted after a delay by the leader-elected scheduler,
  so you can cancel first (`circuit_breaker`, `dlq`, `retry`, `idempotency`,
  `security`).

A DELAYED change returns `status: scheduled` with a `pending_id`; cancel it before it
lands with `POST /config/pending/{pending_id}/cancel`. A GRACEFUL change returns
`status: waiting` and lands after in-progress operations drain. An IMMEDIATE change
returns `status: applied` right away.

**2. Observation (reach).** After the value is persisted, a running consumer only acts
on it according to its reach class — `on next read`, `within 30s (read cache TTL)`, or
`after worker restart`. This is why a DELAYED `circuit_breaker` change can show
"applied" in history yet the live breaker keeps the old threshold until you restart
workers: the apply clock and the observation clock are different.

Cross-worker propagation (does pod B see a change applied on pod A) is governed by
your topology — see [multi-worker-coherence.md](multi-worker-coherence.md).

---

## Reading the apply response

Do not judge success by "the panel looks different". Read the response fields:

- **`status: applied`** (IMMEDIATE) / **`status: scheduled`** (DELAYED, carries
  `pending_id` + `scheduled_at`) / **`status: waiting`** (GRACEFUL).
- **`applied_safe_defaults`** — a non-empty list here means one of your values was
  **out of range and clamped** to a safe default (e.g. `failure_threshold: 999 → 20`).
  This is a *successful, surfaced* clamp, **not** a rejected change. If your value did
  not "stick", check this list before concluding the apply failed.
- **HTTP 409 (version conflict)** — your view was stale (someone changed the section
  since you read it). Re-read `GET /config/<section>`, re-apply. Only the IMMEDIATE
  `PUT` is version-guarded this way.
- **HTTP 409 (`ROLLOUT_CONFLICT`)** — a *different* 409: the domain is under an active
  canary rollout holding its config lock (the body carries `error_code: ROLLOUT_CONFLICT`
  and names the owning rollout id). Re-applying will not help until the rollout ends — roll
  it back / cancel it, or wait. See `## v1.0 limitations`.

**Before moving to the next step:** the response `status` is `applied`/`scheduled`/
`waiting` (not `error`), and you have read `applied_safe_defaults` — any clamp is
intentional, not a silent failure.

---

## Change procedure by risk tier

Tiers are assigned per domain in the map below. The procedure escalates with the tier.

### Low-risk (observability / limits only)

Domains whose worst case is a misrouted alert or a metrics-cardinality change:
`notification`, `metrics`, `forensic` (routing/collection), `sla`.

1. `GET /config/<section>` — record current.
2. Apply the change (console or `PUT /config/<section>`).
3. Read the response (`## Reading the apply response`).

**Before moving to the next step:** response is not `error`. (These domains are
`on next read` — the edit is observed on the next operation; `notification`
channel-target fields are the one `after worker restart` exception.)

### Behavior-changing (traffic / latency / retry shape)

Domains that reshape live traffic, latency, or recovery: `rate_limit`, `retry`,
`replay_automation`.

1. `GET /config/<section>` — record current.
2. Change **one field**. Apply.
3. Read the response; confirm no unintended clamp.
4. Observe the live effect on the next requests/operations (these are `on next read`)
   using the domain's watch panel/metrics below.

**Before moving to the next step:** the intended metric/panel moved in the expected
direction within a few minutes, and no new breaker opens or DLQ inflow appeared on
**Panel: Circuit Breakers** / **Panel: Dead Letter Queue**.

### Safety-critical (can block traffic or lose data)

Domains that can cut off traffic or drop data: `circuit_breaker`, `dlq` (and, once
wired, `security`, `idempotency`).

1. `GET /config/<section>` — record current. Announce the change window.
2. Change **one field**, well inside the advisory range shown in the editor.
3. These default to **DELAYED** — after applying you get a `pending_id`. Use the
   window to double-check the value; `POST /config/pending/{pending_id}/cancel` aborts.
4. **Restart is required to observe** `circuit_breaker` / `dlq` (they are
   `after worker restart`). Plan a rolling restart; until then the live component uses
   the old value even though history shows the change applied.
5. Watch the domain's panel closely through and after the restart.

**Before moving to the next step:** history shows the change applied, the rolling
restart completed, and the **Panel: Circuit Breakers** / **Panel: Dead Letter Queue**
show healthy state (no unexpected open breakers, no DLQ inflow spike).

---

## Blast-radius map

One section per console-visible domain. Each carries a `**Takes effect**` line (the
reach class), the blast radius, what to watch, and — for behavior-changing /
safety-critical domains — the most common misapplication and its cascade.

**Domain naming — what each console key maps to** (the keys correlate 1:1 with the
`BALDUR_*` env-var prefixes, so they are kept raw rather than relabeled):

- `notification` = the **Unified Notification** product feature (severity → channel
  routing).
- `rate_limit` = the **admin control-plane API** limiter, **not** the Throttle feature.
- `sla` = DLQ pending-item SLA-breach / expiry timing.
- `forensic` = failure-capture audit routing.
- `security` = violation-response thresholds (bans, session).

### `circuit_breaker`

**Takes effect**: after worker restart — the change persists (DELAYED by default), but
the running breaker keeps the old value until workers restart.

- **Risk tier**: safety-critical.
- **Blast radius**: this domain bundles three groups that fail differently:
  - *Core CB* (`enabled`, `failure_threshold`, `recovery_timeout`, `success_threshold`,
    `half_open_max_calls`, `half_open_stuck_timeout_seconds`) — governs when traffic is
    cut off to a failing dependency.
  - *Rate-limit cascade* (`rate_limit_cascade_*`) — trips the breaker on a 429 storm.
  - *Self-DDoS* (`self_ddos_*`) — caps per-service outbound RPS.
- **Most common misapplication → cascade**: dropping `failure_threshold` too low makes
  the breaker open on normal transient blips → traffic is force-failed → failed
  operations flow into the DLQ and retry load rises. Raising it too high delays cut-off
  and lets a failing dependency drag the caller down.
- **Watch**: **Panel: Circuit Breakers** (open/half-open state per service);
  **Panel: Dead Letter Queue** for downstream inflow driven by opens.

### `dlq`

**Takes effect**: after worker restart — the DLQ service captures its config at
construction; a change persists (DELAYED by default) but is observed only after
workers restart.

- **Risk tier**: safety-critical.
- **Blast radius**: `enabled=False` means failed operations are **no longer captured**
  — silent data loss for the failure path. `max_size` / `max_size_per_domain` /
  `overflow_strategy` govern what happens when the queue fills (`drop_oldest`,
  `reject`, `compress_oldest`). `max_replay_attempts` bounds replay retries.
- **Coupled**: `dlq.enabled` feeds retry's DLQ capture (turning DLQ off silently
  disarms retry-exhaustion capture); `max_replay_attempts` interacts with
  `replay_automation` per-domain retry caps.
- **Most common misapplication → cascade**: setting `overflow_strategy: reject` (or a
  too-small `max_size`) under a failure storm drops incoming failures at the door
  instead of the oldest → the failures you most want to replay are the ones lost.
- **Watch**: **Panel: Dead Letter Queue** (size, overflow, per-domain cardinality).

### `retry`

**Takes effect**: on next read — a change is picked up on the next `protect()`-wrapped
call. (A change to retry used through the standalone `@retry` decorator is captured at
import and needs a worker restart.)

- **Risk tier**: behavior-changing.
- **Blast radius**: `max_attempts` / `base_delay` / `max_delay` / `backoff_strategy`
  reshape retry count and backoff — directly changing downstream load and caller
  latency.
- **Coupled**: retry exhaustion arms DLQ inflow; more attempts = more delayed load on a
  struggling dependency.
- **Most common misapplication → cascade**: raising `max_attempts` or `max_delay`
  during an outage multiplies retries against an already-failing dependency →
  retry-amplified load that slows recovery (and lengthens caller latency).
- **Watch**: **Panel: Dead Letter Queue** for retry-exhaustion inflow, and downstream
  dependency latency/error rate for retry amplification.

### `rate_limit`

**Takes effect**: on next read — control-plane rate-limit values are read per request,
so new requests see the change immediately (this is the admin control-API limiter, not
your app's user-facing traffic).

- **Risk tier**: behavior-changing.
- **Blast radius**: `control_api_rate_limit` / `emergency_rate_limit` (+ their window
  fields) throttle the admin control API. Setting these too low can throttle your own
  console/automation during an incident; the `emergency_*` pair applies while the
  system is in emergency mode.
- **Most common misapplication → cascade**: dropping `emergency_rate_limit` too low
  starves the very control calls you need to drive recovery.
- **Watch**: **Panel: System Control** (control-plane state); admin API 429s.

### `replay_automation`

**Takes effect**: on next read — each replay operation reads the block fresh; the
scheduled replay lane re-reads on its next tick.

- **Risk tier**: behavior-changing.
- **Blast radius**: gates adaptive/priority replay (`adaptive_enabled`,
  `priority_enabled`) and sizes replay batches (`traffic_aware_max_items`,
  `adaptive_*_items`, `on_recovery_max_items`) — i.e. how aggressively recovery drains
  the DLQ.
- **Coupled**: batch/`domain_max_retries` interact with `dlq` drain size and
  `max_replay_attempts`; auto-replay-on-recovery is driven off this block when a
  breaker closes.
- **Most common misapplication → cascade**: a large `traffic_aware_max_items` /
  `adaptive_max_items` drains a big backlog in one burst → a thundering-herd replay
  that re-loads the just-recovered dependency.
- **Watch**: **Panel: Dead Letter Queue** (backlog drain rate) and downstream health
  during a replay window.

### `sla`

**Takes effect**: on next read — `default_hours` is read fresh (via the layered settings
overlay) on the next SLA-timing evaluation, no restart.

- **Risk tier**: behavior-changing.
- **Blast radius**: `default_hours` sets DLQ pending-item SLA-breach timing
  and expiry windows; shortening it flags more items as breached.
- **Watch**: **Panel: Dead Letter Queue** for pending/breach counts.

### `security`

**Takes effect**: on next read — the security service reads these thresholds fresh (via
the layered settings overlay) on the next violation-response evaluation, no restart.

- **Risk tier**: safety-critical.
- **Blast radius**: ban thresholds
  (`permanent_ban_threshold`, `temporary_ban_hours`, `injection_ban_hours`,
  `suspicious_ip_cache_timeout`) and `session_cookie_age` govern who is blocked/banned
  and how long — a mistuned value blocks or admits real traffic. Change with care.
- **Watch**: **Panel: System Control**; security-violation audit events.

### `idempotency`

**Takes effect**: within 30s (read cache TTL) — `enabled`, `fail_open_on_cache_error`,
and `allow_inmemory_fallback` resolve through a per-process read cache (~30s TTL) on the
hot path, so an edit is observed within that window on the applying pod, no restart.
**Per-field exception (after worker restart)**: the cache-TTL fields captured by the
service singleton at construction — `default_cache_ttl`, `extended_cache_ttl`,
`clock_skew_tolerance_seconds` — take effect only after a worker restart.

- **Risk tier**: safety-critical.
- **Blast radius**: `enabled` and `fail_open_on_cache_error` govern
  duplicate-suppression and its fail direction — flipping fail-open could let a
  possibly-duplicate side effect through on a cache blip. `allow_inmemory_fallback`
  relaxes the production fail-closed posture for a missing cache adapter.
- **Watch**: `baldur_idempotency_check_total`, `baldur_idempotency_gate_takeover_total`.

### `notification`

**Takes effect**: on next read — thresholds and formatting limits are read fresh (via
the layered settings overlay) on the next notification-routing evaluation, no restart.
**Per-field exception (after worker restart)**: the channel-target fields
(`critical_channel` / `high_channel` / `medium_channel`) are captured into the channel
resolver at construction, so a routing-target change takes effect only after a worker
restart.

- **Risk tier**: low-risk — worst case is misrouted or dropped alerts, never
  traffic-blocking or data loss.
- **Blast radius**: `critical_channel` / `high_channel` / `medium_channel` and
  thresholds reshape *where* and at *what severity* alerts land.
- **Watch**: whether alerts arrive on the expected channel (after a restart for a
  channel-target change).

### `forensic`

**Takes effect**: on next read — the forensic recorder reads its config fresh (via the
layered settings overlay) on the next capture, no restart.

- **Risk tier**: behavior-changing.
- **Blast radius**: `audit_enabled` toggles whether forensic capture records at all;
  `error_message_max_length` bounds the captured error string. (The collection/masking
  toggle widgets were removed from the editor — they had no consumer; see
  `## v1.0 limitations`.)
- **Watch**: forensic/audit capture output.

### `metrics`

**Takes effect**: on next read — the metric registry and drift detection read most
fields fresh (via the layered settings overlay) on the next operation, no restart.
**Per-field exceptions**: `enabled` gates the HTTP metric record on a per-process read
cache (`within 30s (read cache TTL)`); install-time gates that size a structure once at
startup (`endpoint_cache_size`, the `jitter_*` scheduler knobs) stay restart-bound.

- **Risk tier**: low-risk, except the cardinality/memory caps
  (`max_registered_domains`, `max_distinct_endpoints`, `endpoint_cache_size`) which are
  behavior-changing guards.
- **Blast radius**: `enabled` toggles metric collection; the cap fields bound
  registry memory/cardinality; `drift_*_threshold` set drift alert severity.
- **Watch**: `/prometheus` scrape output; drift alerts.

---

## Post-change observation

After any behavior-changing or safety-critical change:

1. Confirm the value in history: `GET /config-history/<config_type>/history` (note the
   history path uses the **underscore** config name, e.g.
   `/config-history/circuit_breaker/history`, unlike the hyphenated section slug).
2. Watch the domain's panel/metrics above for 5–15 minutes.
3. Watch the editor's own health metrics: `baldur_runtime_config_update_failed_total`
   and `baldur_runtime_config_safe_default_applied_total` (a clamp you did not expect
   is a sign the value was out of range), plus **Panel: Circuit Breakers** /
   **Panel: Dead Letter Queue** for the resilience path.

**Before moving to the next step:** the change is in history, the target signal moved
as intended, and no unexpected clamp or failure counter incremented.

---

## Rollback

Every change is recoverable. Use the ladder top-to-bottom:

1. **Cancel (before it lands).** For a DELAYED/GRACEFUL change still queued:
   `POST /config/pending/{pending_id}/cancel`.
2. **History rollback (preferred undo).** `GET /config-history/<config_type>/history`
   to find the prior version, then `POST /config-history/<config_type>/rollback` to
   restore it (full audit + history preserved). Note the rollback write itself is not
   version-guarded — it faithfully restores the chosen snapshot.
3. **Manual re-apply.** If history is unavailable, `PUT /config/<section>` with the
   value you recorded in the pre-change checklist.
4. **Global reset (last resort).** `POST /config/reset` restores **all** domains to
   defaults. Use only when a single-section rollback repeatedly fails or the store is
   in an unknown state — it is not surgical.
5. **Observe the reach class again.** For `circuit_breaker` / `dlq`, a rollback still
   needs a worker restart to take effect on the live component.

Do **not** "roll back" by editing the backing store directly — that bypasses history
and audit and can desync the editor's version counter.

**Before moving to the next step:** history shows the restored value, and (for
restart-reach domains) the workers have been restarted so the live component observes
the rollback.

---

## v1.0 limitations

Read these before trusting an edit — they are the honest gaps in the current release.

- **Some editable-looking `forensic` fields were removed because no consumer reads
  them.** The `forensic` collection/masking toggles (body/user-agent/stack capture,
  `mask_*`) were dropped from the editor projection — the backing collection/masking
  layer was never built, so those widgets would have been inert. Their `BALDUR_*`
  settings + env vars still exist; each is restorable to the editor one line at a time
  when the backing feature lands.

- **The `security` detection trio was removed outright — not deferred.**
  `rate_limit_window_seconds`, `rate_limit_max_requests`, and `failed_login_threshold`
  were deleted from `SecuritySettings` (and their `BALDUR_SECURITY_*` env vars): no
  request-counting or failed-login detection layer ever read them, so setting any of
  them never had any effect. Inbound abuse detection / rate limiting is gateway or
  host-app territory — detect the abuse there and call `handle_security_violation()`.
  The violation **response** path (temporary/permanent IP bans, suspicious-IP
  escalation) is unchanged. These fields are gone, not restorable to the editor.

- **`circuit_breaker` and `dlq` take effect only after a worker restart.** Both capture
  their config at worker construction, so an edit persists (and, for `circuit_breaker`,
  applies DELAYED by default) but the live component keeps the old value until you
  restart workers. Plan a rolling restart for a behavior-changing edit to either.

- **A domain under an active canary rollout is locked — edits return 409
  `ROLLOUT_CONFLICT`.** A canary rollout applies its config change through the same
  in-process surface as a console edit, and holds a per-`config_type` lock for the
  rollout's lifetime. While that lock is held, an editor/REST edit to the same domain is
  **rejected with HTTP 409 `error_code: ROLLOUT_CONFLICT`** (the message names the owning
  rollout id) instead of silently interleaving with the rollout — because a later rollback
  restores the full pre-rollout snapshot and would otherwise clobber your interim edit.
  The console editor also shows a `locked` badge on the domain and disables its Apply.
  **Escape hatch**: roll back or cancel the rollout from the **Panel: Canary Rollouts**
  (or wait for it to finish), then re-apply. A per-`config_type` lock freezes the *whole*
  domain (unrelated fields included) for the rollout's duration; the lock auto-expires at
  its TTL (`BALDUR_CANARY_LOCK_TIMEOUT_MINUTES`, default 30 min) if a rollout is abandoned,
  and an admin can force-release a zombie lock.

- **Governance approvals are advisory (read-only) in v1.0.** The **Panel: Governance**
  approval queue is read-only: there is no approve/reject HTTP endpoint, and the apply
  path does **not** consult approvals before applying a change. Do not rely on a
  governance approval gating a config change — it does not block the apply. Enforce
  change review out-of-band (change window + the config-history audit trail) for now.

- **Two domains are expert/REST-managed only.** `error_budget` and `drift_threshold`
  exist as config domains but are hidden from the console editor projection; manage them
  via their REST surfaces (`/config/error-budget` and the drift-threshold config
  endpoint) if needed. This runbook covers the 11 console-visible domains only.

- **Multiple workers/pods need extra wiring.** Everything above is correct on a single
  host. For cross-pod coherence of applied changes and once-only scheduled apply, follow
  [multi-worker-coherence.md](multi-worker-coherence.md).
