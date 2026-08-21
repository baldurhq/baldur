# Audit Trail Activation Runbook

> **Purpose**: Get Baldur's Audit Trail recording — **on by default under an active PRO entitlement, off everywhere else** — and verify the hash-chained record is actually being written. Covers who flips the master switch, the signing key the chain depends on, backend selection (file / Redis / SQL), single-host vs multi-host, and the verify go/no-go. A missing signing key aborts a production boot; a multi-host deployment without the distributed hash chain boots but cannot prove integrity.
> **Audience**: Operator / SRE enabling audit for compliance (regulated data, B2B audit requirements), OR an auditor confirming an existing deployment actually records.
> **Cadence**: One-time per deployment + revisit when changing backend (file → Redis → SQL) or scaling from single-host to multi-host.

---

## TL;DR

Baldur's Audit Trail is gated behind a single **master switch** — and which tier you run decides who flips it:

| Deployment | `BALDUR_AUDIT_ENABLED` left unset | Your job |
|---|---|---|
| **PRO entitlement ACTIVE** | **On.** `init()`'s PRO hook flips the switch and promotes the `file_hashchain` backend — a chained, tamper-evident trail with no configuration. | Provision the signing key, point the trail at durable storage, verify (Phases 1 → 4). |
| **OSS / no entitlement** | **Off.** No WAL, no files, no sync worker, no directories created. | Set the switch, select a backend, verify (Phases 2 → 4). |

An explicit `BALDUR_AUDIT_ENABLED` value is **sovereign in both directions**: `false` keeps a PRO deployment silent, `true` turns an OSS deployment's switch on. The PRO hook acts only when the variable was left unset.

| Setting | Default | What it does |
|---|---|---|
| `BALDUR_AUDIT_ENABLED` | `false` — **raised to `true` by the PRO hook when unset** | Master switch. When off, `bootstrap.init()` wires the `null` audit provider — no WAL, no files, no sync worker, no directories created. When on, it starts `AuditSyncWorker`; under PRO the hook has already promoted `file_hashchain`. |
| `BALDUR_SECRETS_AUDIT_SIGNING_KEY` | _(unset)_ | Keys the HMAC-SHA256 hash chain. A **CRITICAL** secret — production boot aborts if it is missing. |
| `BALDUR_AUDIT_LOG_DIR` | `logs/audit` | Where `audit_{date}.jsonl` + `.hash_chain_state.json` are written. |
| `BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN` | `false` | Redis-backed hash chain. **Multi-host (K8s ≥2 pods) MUST set `true`** — file locks do not span hosts. |

Audit does real I/O (WAL writes, hash-chain files, a background sync worker), so Baldur never creates audit artifacts an operator did not ask for — but under PRO the **entitlement is the request**, which is why the trail is on with nothing set. Either way it is **startup-wired, not a runtime toggle**: changing the switch takes a restart. Single-host file mode needs no external infrastructure.

**The single most important rule**: **provision the signing key first.** In production a missing CRITICAL secret aborts the boot, and that gate runs at the top of `init()` — before the audit switch is read — so it fires whether or not audit is on. Under PRO, where the trail needs no flag, the key is the one thing you must actively sequence.

---

## Background — Who Flips the Switch

The `enabled` field is the audit subsystem's **master switch** — it dominates every other audit toggle. When it is `false`:

- `bootstrap.init()` sets the audit provider to `"null"`, so both the resilience-event pipeline (WAL / `AuditSyncWorker`) and the unified audit logger silently drop every write.
- No `audit_{date}.jsonl`, no `.hash_chain_state.json`, no `logs/audit/` directory is created. No background worker thread starts.

The `false` state is a genuine I/O fail-safe: nothing is written, and nothing is created to write into. What differs by tier is only **who asks** for the trail — the two sections below.

### An active PRO entitlement turns it on

PRO registration runs as a `baldur.bootstrap_hooks` entry point during `init()` — before the audit provider is applied and the pipeline starts. With an ACTIVE entitlement it:

1. Sets `enabled=True`, **but only when `BALDUR_AUDIT_ENABLED` was left unset**. An explicit value of either polarity is sovereign, which is what makes `false` a true rollback switch.
2. Re-reads the switch and stops when it is off — an opt-out install gets no promotion, no directory creation, no singleton reset.
3. Yields to a backend the host application already selected (`entitlement.pro_audit_backend_respected`).
4. Promotes `ProviderRegistry.audit` to `file_hashchain` only after constructing that adapter once, so an unwritable log directory fails here — with the previous backend restored (`entitlement.pro_audit_backend_unavailable`) — instead of parking a dangling default that makes every later resolution raise.

Success is one INFO line: `entitlement.pro_audit_activated`, carrying `enabled_source=auto` (nothing set) or `enabled_source=env` (you set it) plus the chosen provider. The hook is fail-soft throughout — any exception is a WARNING and `init()` continues as OSS.

### Without an entitlement, the switch alone is not enough

On an install with no PRO entitlement, `BALDUR_AUDIT_ENABLED=true` turns the subsystem on but selects no backend: the registry default stays `null`, so `init()` logs `audit.backend_unwired` at WARNING and publishes `audit_backend_wired=0` — the series to alert on. In that state every record is accepted and discarded. Selecting a backend is a programmatic step (Phase 3): the same `ProviderRegistry.audit` promotion the PRO hook performs.

**Relationship to the Meta-Watchdog.** A disabled audit subsystem is *intentionally skipped* by the Meta-Watchdog's `audit_system` probe — it is not reported as UNHEALTHY, because "this deployment has not opted in" is not a fault. Once audit is on, that probe activates and monitors WAL / sync-worker health for real. This makes the watchdog the fastest activation signal (see Phase 4).

---

## Phase 1 — Provision the signing key (do this first)

`BALDUR_SECRETS_AUDIT_SIGNING_KEY` is the HMAC-SHA256 key for the audit hash chain: each entry's `current_hash` is keyed by this secret, so an actor who cannot read the key cannot forge a chain that still verifies. It is classified **CRITICAL** — in production (`BALDUR_ENVIRONMENT=production`) a missing CRITICAL secret raises `RuntimeError` at boot.

### Step 1.1 — Generate the key

```bash
# High-entropy opaque string for BALDUR_SECRETS_AUDIT_SIGNING_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Store it in your secret manager and inject it as an env var — never commit it. See `docs/runbooks/secure-deployment.md` (the CRITICAL-secrets phase) for the full secrets workflow, including the recoverable-PII `encryption_key` you will likely set at the same time.

**Next step go/no-go**: `BALDUR_SECRETS_AUDIT_SIGNING_KEY` is set in the deployment environment → proceed to Phase 2.

---

## Phase 2 — Confirm the master switch

### Step 2.1 — Set the flag, or confirm you do not need to

**PRO, entitlement ACTIVE** — nothing to set. Leave `BALDUR_AUDIT_ENABLED` unset and the hook turns audit on during `init()`; go to Step 2.2.

**No entitlement** — set the switch, then select a backend in Phase 3. The flag alone leaves the trail unwired.

```bash
BALDUR_AUDIT_ENABLED=true
```

**Opting out on PRO** — an explicit `false` wins over the hook and stops it at its first step.

```bash
BALDUR_AUDIT_ENABLED=false
```

### Step 2.2 — (Optional) choose the log location

```bash
BALDUR_AUDIT_LOG_DIR=/var/lib/baldur/audit   # default: logs/audit (RELATIVE — resolves under the process CWD)
```

`BALDUR_AUDIT_LOG_DIR` holds the durable, hash-chained trail (`audit_{date}.jsonl` + `.hash_chain_state.json`) — this is the artifact you verify in Phase 4. Its default `logs/audit` is **relative**, so in a container it lands inside the writable layer and is wiped on restart; point it at **persistent storage** (see Common Mistakes). The write-ahead log is a separate, PRO-internal buffer that defaults to `/var/log/audit/wal` (override with `AUDIT_WAL_DIR`, not a `BALDUR_`-prefixed name) — put it on the same persistent volume.

### Step 2.3 — Restart the process

Audit is startup-wired: `bootstrap.init()` reads `get_audit_settings().enabled` **once** and, if true, starts the WAL + `AuditSyncWorker`. The PRO hook runs inside that same `init()`, so it too is a per-process decision. A live process does not pick up the change.

```bash
# Docker compose
docker compose restart app

# Or directly
sudo systemctl restart gunicorn
```

### Step 2.4 — Verify startup

Check the process logs immediately after restart — `audit.startup_completed` is logged at **INFO**, so it appears at the default log level:

```
[info] audit.startup_completed
```

Under PRO you also get `entitlement.pro_audit_activated` (INFO) earlier in the same startup, carrying `enabled_source=auto|env` and `provider=file_hashchain`. That pair — hook activated, pipeline started — is the clearest confirmation the trail is real.

If `audit.startup_completed` is **absent**, the flag did not take — confirm `BALDUR_AUDIT_ENABLED` is exactly that name and is `true` in the environment the process actually loaded. The corresponding `audit.startup_skipped reason=disabled` line is logged at **DEBUG**, so it only shows with `BALDUR_LOG_LEVEL=DEBUG`; at the default level, absence of `audit.startup_completed` is the signal.

**Next step go/no-go**: `audit.startup_completed` present → proceed to Phase 3.

---

## Phase 3 — Persistence backend & multi-host

Audit records persist through a **pluggable backend** (`ProviderRegistry.audit`). The file hash-chain is the default — it is what the PRO bootstrap hook promotes (`ProviderRegistry.audit` → `file_hashchain`) when the switch is on and no backend was already selected — and it needs no external infrastructure. Without an entitlement nothing promotes it for you: the adapter is registered but the default stays `null` until you promote it, which is the `audit.backend_unwired` state described above. The heavier backends are real, but a bare connection string does **not** select them: each needs the explicit activation in the third column.

| Backend | How to activate | What it does |
|---|---|---|
| **File hash-chain** (default) | _(none under PRO — the entitlement hook promotes it; without one, promote `ProviderRegistry.audit` yourself. `BALDUR_AUDIT_LOG_DIR` only sets the path)_ | Writes hash-chained `audit_{date}.jsonl`. Tamper-evident, zero external deps. |
| **Redis flush buffer** | `BALDUR_AUDIT_BUFFER_REDIS_ENABLED=true` (+ `BALDUR_REDIS_URL` for the connection) | Stages records in Redis and drains them to the terminal store via the audit-flush Celery beat tasks — a buffering tier in front of the file/SQL adapter, **not** a replacement for it. Requires Celery + Redis. Effective gate is `enabled AND buffer_redis_enabled`. |
| **SQL / Postgres archival** | Programmatic — register `DjangoAuditLogAdapter(model_class=<your audit model>)` as the audit provider | Durable, queryable Postgres rows with `ON CONFLICT (audit_event_id) DO NOTHING` dedup. Wired in code against your Django model — **not** auto-selected by `BALDUR_SQL_DSN`. |

> Setting `BALDUR_REDIS_URL` or `BALDUR_SQL_DSN` **alone** does not switch the audit backend — those are shared connection inputs (the Redis flush still needs `BALDUR_AUDIT_BUFFER_REDIS_ENABLED`; the SQL backend still needs the Django adapter wired programmatically). With no extra activation, records persist to the file hash-chain.

### Multi-host (K8s ≥2 pods) — required

```bash
BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=true   # requires BALDUR_REDIS_URL
```

File locks (`BALDUR_AUDIT_USE_FILE_LOCK`, default `true`) protect a single host's chain state but **do not span hosts**. With ≥2 pods writing local file chains, the chains fork and cross-pod integrity verification fails. The Redis-backed distributed hash chain is mandatory above one writer.

---

## Phase 4 — Verify the trail is actually recording

### Step 4.1 — Meta-Watchdog probe (fastest signal)

```bash
curl -s http://127.0.0.1:9090/meta-watchdog/status | python -m json.tool
```

`components.audit_system` should be present and `healthy`. When audit is off it is *absent* (the probe is skipped); its presence + `healthy` confirms the WAL initialized.

### Step 4.2 — Files on disk

```bash
ls -la "${BALDUR_AUDIT_LOG_DIR:-logs/audit}"
# Expect: audit_<date>.jsonl  and  .hash_chain_state.json
```

These appear after the first audited event (a config change or an automated healing decision).

### Step 4.3 — Drive an audited event

Make one config change or trigger one healing action, then confirm a new line landed:

```bash
tail -n 1 "${BALDUR_AUDIT_LOG_DIR:-logs/audit}/audit_$(date +%Y-%m-%d).jsonl"   # filenames use YYYY-MM-DD (UTC)
```

### Step 4.4 — Integrity check (admin API)

```bash
curl -s http://127.0.0.1:9090/audit/integrity/verify | python -m json.tool
# Reports whether the hash chain is intact; pinpoints the first broken link if not.

curl -s http://127.0.0.1:9090/audit/integrity/state    # current chain head
```

**Final go/no-go**: `audit_system: healthy` + `audit_<date>.jsonl` growing + `/audit/integrity/verify` reports intact → audit is live and tamper-evident.

---

## Common Mistakes

### Mistake 1 — Deploy without the signing key

In production, a missing `BALDUR_SECRETS_AUDIT_SIGNING_KEY` is a CRITICAL-secret boot abort — and that gate is **unconditional**: it runs before the audit switch is read, so it hits a PRO deployment that never touched the flag just as hard as one that set it. Provision the key (Phase 1) *before* the deploy.

### Mistake 2 — Log directory on ephemeral container storage

If `BALDUR_AUDIT_LOG_DIR` points inside the container's writable layer, every restart wipes the trail — the opposite of an audit guarantee. Mount a persistent volume.

### Mistake 3 — Multi-host without the distributed hash chain

Two or more pods each writing a local file chain produces forked chains that fail cross-pod integrity verification. Above one writer, set `BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=true` with `BALDUR_REDIS_URL`.

### Mistake 4 — Expecting a live toggle from the admin console

There is no runtime-enable path. Audit is startup-wired; the admin console is an operational-action surface, not a settings editor. Set the env var and restart.

### Mistake 5 — Assuming an unset flag means audit is off

Under an active PRO entitlement it means the opposite: the bootstrap hook turns audit on when `BALDUR_AUDIT_ENABLED` is unset, so a PRO deployment records — and writes files — from first boot. If you need it silent (a staging clone, a data-residency hold), set `BALDUR_AUDIT_ENABLED=false` explicitly; an explicit value always wins over the hook.

The mirror-image trap belongs to deployments without an entitlement: setting `true` is not enough on its own. The switch is on, no backend is selected, and every record is accepted and discarded — `init()` says so with `audit.backend_unwired` and `audit_backend_wired=0`.

---

## Cross-References

- `src/baldur/settings/audit.py` — master switch `enabled` + `partition`, `use_file_lock`, `distributed_hash_chain`
- `src/baldur/bootstrap.py` — startup steps: applies the audit default provider per `enabled` (reporting `audit.backend_unwired` + the `audit_backend_wired` gauge), then starts WAL + `AuditSyncWorker` when true
- the PRO package's `baldur.bootstrap_hooks` entry point — the entitlement hook that raises the master switch when unset and promotes `file_hashchain`
- `src/baldur/adapters/audit/hashchain_adapter.py` — `DEFAULT_LOG_DIR = "logs/audit"`, `audit_{date}.jsonl` + `.hash_chain_state.json` naming
- `src/baldur/api/admin/routes/continuous_audit.py` — `/audit/integrity/verify`, `/audit/integrity/state`, `/audit/logs`, `/audit/export/jsonl`
- `docs/runbooks/secure-deployment.md` — the audit signing key (Phase 1) and the audit / DLQ data-masking boundary (read this before routing regulated data — PAN, SSN — through a Baldur-protected path)
- `docs/concepts/pro/audit.md` — conceptual overview of the Audit Trail and its configuration knobs
- `docs/runbooks/meta-watchdog-escalation-response.md` — sibling runbook; the `audit_system` probe referenced in Phase 4 is part of the watchdog surface

---

## Rollback

Set `BALDUR_AUDIT_ENABLED=false` and restart. This is a real rollback switch on PRO too: the entitlement hook acts only when the variable is unset, so an explicit `false` stops it at its first step — no promotion, no directory creation, no singleton reset. The subsystem is back to silent when `audit.startup_completed` no longer appears at startup; with `BALDUR_LOG_LEVEL=DEBUG` you also see `audit.startup_skipped reason=disabled` (that line is DEBUG-level, so it is hidden at the default INFO level). Existing `audit_{date}.jsonl` files and `.hash_chain_state.json` are **left on disk** — there is no auto-deletion; archive or remove them per your retention policy. No state migration is required, and the rest of Baldur is unaffected (the master switch is local to the audit subsystem).
