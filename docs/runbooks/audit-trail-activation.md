# Audit Trail Activation Runbook

> **Purpose**: Turn on Baldur's Audit Trail — which ships **off by default** — and verify the hash-chained record is actually being written. Covers the master switch, the signing key the chain depends on, backend selection (file / Redis / SQL), single-host vs multi-host, and the verify go/no-go. Enabling the flag without the signing key, or on a multi-host deployment without the distributed hash chain, produces a deployment that boots but cannot prove integrity.
> **Audience**: Operator / SRE enabling audit for compliance (regulated data, B2B audit requirements), OR an auditor confirming an existing deployment actually records.
> **Cadence**: One-time per deployment + revisit when changing backend (file → Redis → SQL) or scaling from single-host to multi-host.

---

## TL;DR

Baldur's Audit Trail is gated behind a single **master switch** that defaults to **off**:

| Setting | Default | What it does |
|---|---|---|
| `BALDUR_AUDIT_ENABLED` | `false` | Master switch. When off, `bootstrap.init()` wires the `null` audit provider — no WAL, no files, no sync worker, no directories created. When on, it wires `file_hashchain` and starts `AuditSyncWorker`. |
| `BALDUR_SECRETS_AUDIT_SIGNING_KEY` | _(unset)_ | Keys the HMAC-SHA256 hash chain. A **CRITICAL** secret — production boot aborts if it is missing. |
| `BALDUR_AUDIT_LOG_DIR` | `logs/audit` | Where `audit_{date}.jsonl` + `.hash_chain_state.json` are written. |
| `BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN` | `false` | Redis-backed hash chain. **Multi-host (K8s ≥2 pods) MUST set `true`** — file locks do not span hosts. |

Audit is off by default **on purpose**: it does real I/O (WAL writes, hash-chain files, a background sync worker), and Baldur never creates audit artifacts an operator did not ask for. Enabling it is **one env var + a restart** — it is startup-wired, not a runtime toggle. Single-host file mode needs no external infrastructure.

**The single most important rule**: **provision the signing key first.** Enabling audit without `BALDUR_SECRETS_AUDIT_SIGNING_KEY` set aborts the production boot — sequence the key before the switch and you avoid a failed deploy.

---

## Background — Why Audit Is Off by Default

The `enabled` field is the audit subsystem's **master switch** — it dominates every other audit toggle. When it is `false`:

- `bootstrap.init()` sets the audit provider to `"null"`, so both the resilience-event pipeline (WAL / `AuditSyncWorker`) and the unified audit logger silently drop every write.
- No `audit_{date}.jsonl`, no `.hash_chain_state.json`, no `logs/audit/` directory is created. No background worker thread starts.

This is a deliberate **opt-in I/O fail-safe**: an operator who has not asked for an audit trail gets none — no surprise disk writes, no surprise worker.

**A PRO license does not auto-enable it.** PRO registration installs the audit *capability* (the `file_hashchain` provider via the `baldur.bootstrap_hooks` entry point), but the master switch stays operator-controlled. Even with a valid PRO entitlement, audit stays off until you set `BALDUR_AUDIT_ENABLED=true`.

**Relationship to the Meta-Watchdog.** A disabled audit subsystem is *intentionally skipped* by the Meta-Watchdog's `audit_system` probe — it is not reported as UNHEALTHY, because "the operator has not opted in" is not a fault. Once you enable audit, that probe activates and monitors WAL / sync-worker health for real. This makes the watchdog the fastest activation signal (see Phase 4).

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

## Phase 2 — Flip the master switch

### Step 2.1 — Set the flag

```bash
BALDUR_AUDIT_ENABLED=true
```

### Step 2.2 — (Optional) choose the log location

```bash
BALDUR_AUDIT_LOG_DIR=/var/lib/baldur/audit   # default: logs/audit (RELATIVE — resolves under the process CWD)
```

`BALDUR_AUDIT_LOG_DIR` holds the durable, hash-chained trail (`audit_{date}.jsonl` + `.hash_chain_state.json`) — this is the artifact you verify in Phase 4. Its default `logs/audit` is **relative**, so in a container it lands inside the writable layer and is wiped on restart; point it at **persistent storage** (see Common Mistakes). The write-ahead log is a separate, PRO-internal buffer that defaults to `/var/log/audit/wal` (override with `AUDIT_WAL_DIR`, not a `BALDUR_`-prefixed name) — put it on the same persistent volume.

### Step 2.3 — Restart the process

Audit is startup-wired: `bootstrap.init()` reads `get_audit_settings().enabled` **once** and, if true, starts the WAL + `AuditSyncWorker`. A live process does not pick up the change.

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

If it is **absent**, the flag did not take — confirm `BALDUR_AUDIT_ENABLED` is exactly that name and is `true` in the environment the process actually loaded. The corresponding `audit.startup_skipped reason=disabled` line is logged at **DEBUG**, so it only shows with `BALDUR_LOG_LEVEL=DEBUG`; at the default level, absence of `audit.startup_completed` is the signal.

**Next step go/no-go**: `audit.startup_completed` present → proceed to Phase 3.

---

## Phase 3 — Persistence backend & multi-host

Audit records persist through a **pluggable backend** (`ProviderRegistry.audit`). The file hash-chain is the default — it is what the PRO bootstrap hook wires (`ProviderRegistry.audit` → `file_hashchain`) when the master switch is on — and it needs no external infrastructure. The heavier backends are real, but a bare connection string does **not** select them: each needs the explicit activation in the third column.

| Backend | How to activate | What it does |
|---|---|---|
| **File hash-chain** (default) | _(none — `BALDUR_AUDIT_LOG_DIR` only sets the path)_ | Writes hash-chained `audit_{date}.jsonl`. Tamper-evident, zero external deps. |
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

### Mistake 1 — Enable audit but forget the signing key

In production, a missing `BALDUR_SECRETS_AUDIT_SIGNING_KEY` is a CRITICAL-secret boot abort. Provision the key (Phase 1) *before* flipping the switch.

### Mistake 2 — Log directory on ephemeral container storage

If `BALDUR_AUDIT_LOG_DIR` points inside the container's writable layer, every restart wipes the trail — the opposite of an audit guarantee. Mount a persistent volume.

### Mistake 3 — Multi-host without the distributed hash chain

Two or more pods each writing a local file chain produces forked chains that fail cross-pod integrity verification. Above one writer, set `BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=true` with `BALDUR_REDIS_URL`.

### Mistake 4 — Expecting a live toggle from the admin console

There is no runtime-enable path. Audit is startup-wired; the admin console is an operational-action surface, not a settings editor. Set the env var and restart.

### Mistake 5 — Assuming a PRO license auto-enables audit

It does not. PRO installs the capability; the master switch stays operator-controlled. A PRO deployment with `BALDUR_AUDIT_ENABLED` unset records nothing.

---

## Cross-References

- `src/baldur/settings/audit.py` — master switch `enabled` + `partition`, `use_file_lock`, `distributed_hash_chain`
- `src/baldur/bootstrap.py` — startup hook: reads `get_audit_settings().enabled`, starts WAL + `AuditSyncWorker` when true, wires `null` otherwise
- `src/baldur/adapters/audit/hashchain_adapter.py` — `DEFAULT_LOG_DIR = "logs/audit"`, `audit_{date}.jsonl` + `.hash_chain_state.json` naming
- `src/baldur/api/admin/routes/continuous_audit.py` — `/audit/integrity/verify`, `/audit/integrity/state`, `/audit/logs`, `/audit/export/jsonl`
- `docs/runbooks/secure-deployment.md` — the audit signing key (Phase 1) and the audit / DLQ data-masking boundary (read this before routing regulated data — PAN, SSN — through a Baldur-protected path)
- `docs/concepts/pro/audit.md` — conceptual overview of the Audit Trail and its configuration knobs
- `docs/runbooks/meta-watchdog-escalation-response.md` — sibling runbook; the `audit_system` probe referenced in Phase 4 is part of the watchdog surface

---

## Rollback

Set `BALDUR_AUDIT_ENABLED=false` and restart. The subsystem is back to silent when `audit.startup_completed` no longer appears at startup; with `BALDUR_LOG_LEVEL=DEBUG` you also see `audit.startup_skipped reason=disabled` (that line is DEBUG-level, so it is hidden at the default INFO level). Existing `audit_{date}.jsonl` files and `.hash_chain_state.json` are **left on disk** — there is no auto-deletion; archive or remove them per your retention policy. No state migration is required, and the rest of Baldur is unaffected (the master switch is local to the audit subsystem).
