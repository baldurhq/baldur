# Secure Deployment Runbook

> **Purpose**: Configure the two production-critical security surfaces — Baldur secrets (CRITICAL/IMPORTANT/OPTIONAL classification + Fernet encryption + audit signing) and TLS settings (cipher / version / verification mode for Redis, Kafka, and outbound HTTP). Walking this runbook end-to-end leaves a Baldur deployment with every CRITICAL secret populated and TLS hardened against downgrade attacks.
> **Audience**: Operator / SRE preparing a Baldur-protected service for its first production deploy, OR auditor reviewing existing prod env for missing secrets.
> **Cadence**: One-time per environment + revisit on secret rotation, TLS certificate renewal, or after upgrading past a Baldur major version that changes the secret set.

---

## TL;DR

1. Copy `.env.template` from the repo root to `.env` (gitignored) and fill in the placeholders.
2. Populate the two CRITICAL secrets — `BALDUR_SECRETS_ENCRYPTION_KEY` and `BALDUR_SECRETS_AUDIT_SIGNING_KEY`. Boot aborts in production if either is missing.
3. Enable TLS by setting `BALDUR_TLS_ENABLED=true` and the certificate paths.
4. Run `python -c "from baldur.settings.secrets import validate_required_secrets; print(validate_required_secrets())"` and confirm the `critical` list is empty.
5. Review **Phase 5** to understand the audit / DLQ data-masking boundary before routing regulated data (PAN, SSN, etc.) through a Baldur-protected path.
6. If you enable OpenTelemetry (`OTEL_ENABLED=true`) behind an untrusted public ingress, review **Phase 6** — strip the inbound W3C `baggage` header at the gateway.

Out of scope here: reverse-proxy / WAF templates, secret-rotation procedures, RBAC group provisioning, log-redaction tuning. Those land in the broader Wave 7.5 hardening cookbook.

---

## Phase 1 — Critical Secrets

### Step 1.1 — Classification

Baldur classifies secrets into three tiers (`src/baldur/settings/secrets.py`):

| Tier | Secrets | Boot behavior when missing |
|------|---------|---------------------------|
| **CRITICAL** | `encryption_key`, `audit_signing_key` | `RuntimeError` raised in production (`BALDUR_ENVIRONMENT=production`); ERROR-level log in non-production |
| **IMPORTANT** | `database_password`, `redis_password` | WARNING log; boot continues |
| **OPTIONAL** | `toss_secret_key`, `slack_webhook_token`, `slack_bot_token`, `pagerduty_api_key`, `aws_access_key_id`, `aws_secret_access_key` | INFO log; boot continues |

Production deployments MUST provide the two CRITICAL secrets. Other tiers depend on which integrations the deployment enables.

### Step 1.2 — Generate the CRITICAL secrets

`encryption_key` is a Fernet-compatible URL-safe base64 key used for recoverable PII encryption. `audit_signing_key` is an opaque high-entropy string used to sign audit hash-chain blocks — each chain entry's `current_hash` is an HMAC-SHA256 keyed by this secret, so an actor who cannot read the key cannot forge a chain that still verifies.

<!-- verified-by: tests/unit/audit/integrity — keyed signing + forge rejection (test_forge_without_key_fails) -->

```bash
# Generate a Fernet key for BALDUR_SECRETS_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate a random signing key for BALDUR_SECRETS_AUDIT_SIGNING_KEY
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Store both values in a secrets manager (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, Kubernetes Secret) and inject them as environment variables at process start.

### Step 1.3 — Audit hash-chain signing: verifier key & re-anchor

`audit_signing_key` keys the audit integrity hash chain (HMAC-SHA256). Two operational consequences follow:

- **Verifiers need the same key.** HMAC is symmetric, so any process that *verifies* the chain — the offline `python -m baldur.audit.verify_audit_integrity` tool, the scheduled daily integrity check, and the PRO integrity sealer — must run with the **same `audit_signing_key`** as the writers. A verifier run without the key (or with the wrong key) against a keyed chain fails **safely** (hash mismatch, never a false "valid"), but it cannot validate the chain. Provision the key in every environment that verifies, not only where audit entries are written.
- **Enabling or rotating the key requires a one-time re-anchor.** Turning the key on, or changing it, is a clean break: blocks written under the old mode (keyless, or the previous key) no longer recompute to their stored `current_hash`, so the historical chain fails verification under the new key. Re-anchor the chain once, immediately after the key change, so a fresh chain starts from the new key. There is no `baldur audit` CLI verb for this yet — re-anchor from a Python shell on the deployment:

```python
# File-based deployments (the default audit hash chain): reset the local
# chain singleton. Clears the sequence counter, the previous-hash pointer, and
# the on-disk state file, so the next entry starts a fresh chain.
from baldur.audit.integrity.local_manager import get_hash_chain_manager

get_hash_chain_manager().reset()
```

```python
# Redis-backed (multi-pod) deployments: reset the distributed chain. Pass the
# same Redis client your deployment configures for Baldur.
from baldur.audit.integrity.factory import create_hash_chain_manager

manager = create_hash_chain_manager(distributed=True, redis_client=redis_client)
manager.reset()
```

Re-anchoring discards tamper-evidence for entries written before the key change. Do it only as part of a deliberate enable/rotate, and archive the pre-rotation log segment first if you need to retain its (old-key) verifiability.

---

## Phase 2 — TLS Hardening

### Step 2.1 — TLS option reference

All TLS settings live in `src/baldur/core/tls.py` (`TLSConfig`). Env-var prefix is `BALDUR_TLS_`.

| Env var | Default | Description |
|---------|---------|-------------|
| `BALDUR_TLS_ENABLED` | `false` | Master switch. Set `true` to enable TLS for outbound Redis/Kafka/HTTP. |
| `BALDUR_TLS_CERTIFICATE_PATH` | `None` | Path to TLS certificate file (`.crt` / `.pem`). Required when serving TLS. |
| `BALDUR_TLS_KEY_PATH` | `None` | Path to TLS private key file (`.key` / `.pem`). Required when serving TLS. |
| `BALDUR_TLS_CA_BUNDLE_PATH` | `None` | Path to CA bundle for peer-certificate verification. Required for self-signed CA / private PKI. |
| `BALDUR_TLS_VERIFY_SSL` | `true` | Verify peer certificates. Keep `true` in production. |
| `BALDUR_TLS_MIN_VERSION` | `TLSv1.2` | Minimum protocol version (`TLSv1.2`, `TLSv1.3`). Set `TLSv1.3` if every peer supports it. |
| `BALDUR_TLS_VERIFY_MODE` | `CERT_REQUIRED` | SSL verification mode (`CERT_NONE`, `CERT_OPTIONAL`, `CERT_REQUIRED`). |

### Step 2.2 — Production posture

Production deployments SHOULD enable TLS for every outbound connection that crosses a security boundary (cross-pod Redis, cross-region Kafka, third-party HTTP). The framework defaults (`min_version=TLSv1.2`, `verify_mode=CERT_REQUIRED`, `verify_ssl=true`) match OWASP TLS guidance; do not loosen them unless a specific compatibility constraint forces it, and document the deviation if you do.

---

## Phase 3 — Worked Example

Once the secrets are generated and TLS cert paths are known, the typical production env block is:

```bash
# CRITICAL secrets — missing values abort startup in production
export BALDUR_SECRETS_ENCRYPTION_KEY="<fernet-key-from-step-1.2>"
export BALDUR_SECRETS_AUDIT_SIGNING_KEY="<token-urlsafe-key-from-step-1.2>"

# TLS hardening
export BALDUR_TLS_ENABLED=true
export BALDUR_TLS_MIN_VERSION=TLSv1.2

# Admin server auth — operator (full) vs read-only (VIEWER) credentials
export BALDUR_ADMIN_KEY="<operator-secret>"           # OPERATOR; required for a non-localhost bind
export BALDUR_ADMIN_UNLOCK=0                           # 1 = permit ADMIN-level destructive ops
export BALDUR_ADMIN_READONLY_KEY="<readonly-secret>"  # VIEWER — read-only admin/observability (optional, must differ from BALDUR_ADMIN_KEY)

# Production environment signal — required for CRITICAL-secret enforcement
export BALDUR_ENVIRONMENT=production
```

`BALDUR_ENVIRONMENT=production` is the single canonical production signal (`src/baldur/runtime.py`). Without it, missing CRITICAL secrets only emit ERROR logs and do not abort startup — fine for local development, but every prod / staging-as-prod deployment MUST set it explicitly.

Both admin keys ride the same `X-Baldur-Admin-Key` header: `BALDUR_ADMIN_KEY` resolves to **OPERATOR** (and to **ADMIN** when `BALDUR_ADMIN_UNLOCK=1`) — it can trip breakers and purge the DLQ — while `BALDUR_ADMIN_READONLY_KEY` resolves to **VIEWER** (read-only). Give read-only integrations (AI operators, Grafana, status pages) the read-only key, never the operator key. A non-localhost bind still requires `BALDUR_ADMIN_KEY`; the read-only key is additive and never substitutes for it as the bind-safety gate.

A copy-pasteable starting point with every relevant variable is committed at the repo root as **`.env.template`**. The recommended local workflow:

```bash
cp .env.template .env  # .env is gitignored — never commit it
# edit .env, fill in real values, source it in your process manager
```

---

## Phase 4 — Validation

### Step 4.1 — Pre-flight check

Before starting the application, validate that every CRITICAL secret is populated:

```bash
python -c "from baldur.settings.secrets import validate_required_secrets; print(validate_required_secrets())"
```

Expected output:

```python
{'critical': [], 'warning': [...], 'info': [...]}
```

If `critical` is non-empty, the listed secrets MUST be populated before boot. If `BALDUR_ENVIRONMENT=production`, the same check also raises `RuntimeError` and aborts startup — running it manually first gives a friendly preview of the same gate.

`warning` and `info` lists are informational only; populate them only when the corresponding integration is enabled.

### Step 4.2 — TLS smoke test

After boot, verify TLS is actually in effect by checking that Redis/Kafka connections negotiated at the configured minimum version. The exact verification step depends on the backend; for Redis with `redis-py`, the `connection_pool.connection_kwargs` dict will carry `ssl=True` when `BALDUR_TLS_ENABLED=true` is honored.

---

## Phase 5 — Audit & DLQ Data Masking Boundary

Baldur persists failure context to the audit trail and the Dead Letter Queue (DLQ) so operators can diagnose and replay failed operations. That persisted context can carry user data, so understand exactly what the framework masks — and what it does not — before routing regulated data (PAN, SSN, etc.) through a Baldur-protected path.

### Step 5.1 — What Baldur masks automatically

`mask_sensitive_fields` (`src/baldur/audit/masking.py`) redacts values whose **key name** matches the sensitive-key set (`DEFAULT_SENSITIVE_KEYS` in the same module — `password`, `token`, `api_key`, `card_number`, `cvv`, `iban`, `account_number`, `tax_id`, `access_key`, and similar). Matching is structural and key-based:

- It walks `dict` and `list` structures and replaces matching values with `***REDACTED***`.
- A field named `card_number` inside an audit payload is redacted regardless of its value.

This covers the common case: structured request/response payloads keyed by a known-sensitive name.

### Step 5.2 — What Baldur does NOT mask (operator responsibility)

Masking is **key-based, not content-based**. It does not scan free-text strings for embedded sensitive patterns. In particular:

- **Exception messages** — the `error_message` stored on DLQ entries, third-party payment-gateway / driver error strings, and Django `ValidationError` text — are persisted verbatim. If a gateway echoes a card number inside its error string, that string is stored unredacted.
- A sensitive value placed under a **non-sensitive key** (e.g. `{"note": "card 4111111111111111 declined"}`) is not detected — only the key is inspected, never the value content.

Baldur deliberately does not attempt free-text PII scrubbing: regex-based scrubbing of arbitrary error text is incomplete (it misses partial numbers, non-Western identifiers, and names) and gives false assurance. Treat masking as a structural safeguard, not a compliance guarantee.

### Step 5.3 — Recommended operator controls

1. **Stop sensitive data at the source.** Configure payment-gateway, database, and third-party clients so they do not echo PANs, full account numbers, or other regulated data inside exception messages. This is the only reliable control for free text.
2. **Use sensitive key names.** When attaching diagnostic context to a protected call, name regulated fields with the conventional keys (`card_number`, `cvv`, `tax_id`, …) so structural masking applies. Extend `DEFAULT_SENSITIVE_KEYS`, or pass an explicit `sensitive_keys` list, for domain-specific identifiers.
3. **Encrypt the recoverable PII path.** Populate `BALDUR_SECRETS_ENCRYPTION_KEY` (Phase 1) so the recoverable-PII and on-disk DLQ fallback paths are encrypted at rest.
4. **Restrict DLQ / audit read access.** The DLQ admin console and audit store expose persisted failure context; gate them behind operator RBAC and network policy.

---

## Phase 6 — Inbound OTel Baggage Trust Boundary

When OpenTelemetry is enabled (`OTEL_ENABLED=true`), Baldur installs a W3C TraceContext+Baggage propagator and instruments inbound Django requests. At request start, the propagator extracts the incoming W3C `baggage` header into the OTel context, and Baldur restores its own `baldur.cell_id` / `baldur.domain` keys from that baggage into the local execution context (`restore_contextvars_from_baggage`). On egress, the same propagator re-propagates all in-context baggage onto outgoing requests.

This is the intended cross-service flow **inside a trusted boundary** (in-org mesh, private network). The values arrive from a sibling Baldur service that legitimately set them.

### Step 6.1 — The trust boundary

Behind an **untrusted public ingress** (internet-facing edge, third-party clients), the inbound `baggage` header is attacker-controlled. A client could:

- **Spoof** `baldur.cell_id` / `baldur.domain` — values Baldur would then trust for cell/domain tagging instead of computing them locally.
- **Inject foreign baggage** that Baldur's egress propagator re-propagates to downstream internal services.

### Step 6.2 — Recommended control: strip baggage at the gateway

The correct control is a **deployment practice**, not a framework setting: terminate and strip the inbound W3C `baggage` header at your public gateway / reverse proxy / WAF before traffic reaches the application. For example, drop the `baggage` request header (and, if you do not want clients influencing trace sampling, `traceparent` / `tracestate` as well) at the edge for any route exposed to untrusted callers.

- **Untrusted public ingress** → strip inbound `baggage` at the gateway. Baldur then computes `cell_id` / `domain` locally, as it does when OTel is off.
- **Trusted in-org / service-mesh ingress** → no action needed; the baggage flow is the intended cross-service propagation path.

### Step 6.3 — Why no framework-level egress allowlist

Baldur does **not** ship a code-level baggage allowlist, and none is needed at the OSS/PRO single-org target tiers:

- The W3C propagator already bounds inbound baggage size (8192 bytes total / 180 pairs / 4096 bytes per pair), so an attacker cannot use it to exhaust memory.
- `restore_contextvars_from_baggage` reads only the two `baldur.*` keys, ignoring any other inbound baggage for its own context restoration.

A trust boundary belongs at the deployment edge where the operator knows which ingress is trusted — the framework cannot infer that. This phase requires **no Baldur configuration change**; it is purely a gateway/proxy deployment note.

---

## Pointer back to SECURITY.md

Vulnerability reporting (in-scope / out-of-scope categories, Safe Harbor clause, OSS vs PRO channel split) lives in [`/SECURITY.md`](../../SECURITY.md). This runbook is for operators configuring secure deployment; SECURITY.md is for researchers reporting a vulnerability.
