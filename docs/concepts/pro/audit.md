# Audit Trail

> A tamper-evident record of every configuration change and healing decision in your system — who changed what, when, and why — built for compliance and incident forensics.

!!! info "PRO feature"
    Audit Trail is a PRO-tier feature. It answers the question every regulated or production-critical team eventually has to answer for an auditor or an incident review: *"can you prove who changed this, and when?"*

## What is it?

When you run a system in production, things change constantly. Someone raises a retry limit, an operator forces a recovery, the framework itself trips a circuit breaker to protect a failing dependency. Most of those changes leave no trace — until something breaks and you need to know what happened.

An **audit trail** (also called an audit log) is a permanent, append-only history of those changes: a chronological account of *who* did *what*, *when*, and *why*. It is the same idea as a bank statement or a patient chart — a trustworthy record you can go back and read, and that you can rely on not to have been quietly rewritten. In Baldur, the Audit Trail records every configuration change and automated healing decision as a structured, privacy-safe, tamper-evident entry.

## Why it matters

Without an audit trail, the history of your system lives in memory and guesswork. That is fine until the day it isn't: a compliance review, a security incident, or a 2 a.m. outage where the first question is "what changed?"

The Audit Trail turns that history into something you can actually rely on:

- **Prove what changed, for compliance.** Regulations such as GDPR and CCPA require demonstrable control over configuration and data-handling changes. An audit trail is the evidence — a defensible record that change tracking is in place and that personal data was handled carefully.
- **Reconstruct an incident.** After an outage, the audit trail lets you answer "what changed right before this broke?" and "did the system heal itself, or did someone intervene?" without reverse-engineering the answer from logs that were never meant to tell that story.
- **Attribute every change.** Each entry records the actor, the action, the before-and-after values, and a reason, so accountability is built in rather than reconstructed after the fact.
- **Capture identity without hoarding PII.** Attribution needs to know *who*, but storing a raw client IP address (or the secrets inside a config change) is itself a compliance liability. Baldur masks client IP addresses and redacts sensitive values, so you keep accountability without retaining the raw personal data.
- **Trust the record itself.** An audit log that can be silently edited is worthless. Baldur's entries are tamper-evident, so a removed or altered record is detectable rather than invisible.

## How it works in Baldur

When the Audit Trail is enabled, every configuration change and automated healing decision produces a structured record. You do not call it explicitly for each change — the framework records the events it manages, and the same trail captures changes an operator makes through the admin surface.

Each entry captures the full story of a single change:

- **Who:** the user or system actor responsible.
- **What:** the action taken (create, update, delete, apply, roll back, and so on) and the configuration it touched, with the value before and after.
- **When:** the timestamp.
- **Why:** the reason supplied with the change, and where it came from (an API call, the CLI, the scheduler, or the framework itself).

On top of that record, three properties make the trail safe to depend on:

- **Privacy-safe identity.** Client IP addresses are masked: the host portion is redacted (for example `192.168.***.***`) rather than stored in full, and sensitive values inside a change (passwords, tokens, keys) are redacted. You keep the record of *who* acted without retaining the raw personal data.
- **Trace correlation.** Each entry carries a trace ID, so an audit record can be lined up with the distributed trace of the request that caused it — connecting "this config changed" to the exact call that changed it.
- **Tamper-evidence through a hash chain.** Each entry carries a cryptographic fingerprint: a SHA-256 hash computed over the entry's own contents *together with* the fingerprint of the entry immediately before it. The records are therefore linked into a chain, every entry bound to its predecessor back to the first. Editing or deleting any past entry changes its fingerprint and breaks the link to every entry that follows, something a value quietly rewritten in place cannot avoid. An integrity check walks the chain and reports whether it is intact, and where the first break is, so tampering is not just detectable but locatable rather than silent.

Records are kept for a configurable retention period and persist to your configured storage backend, so the trail survives restarts and is available long after the change it describes.

| What you observe | When it happens |
|------------------|-----------------|
| A structured entry recording who, what, when, and why | a configuration change or an automated healing decision occurs |
| Client IP addresses appear masked (host portion redacted), not as raw values | any entry that captures a client IP |
| An entry can be matched to a request's distributed trace | the change happened in the context of a traced request |
| An integrity check reports whether the hash chain is intact, and pinpoints the first broken link | you verify the trail |
| Older entries age out | the retention period passes |

## Configuration

The knobs an operator sets most often. The full list lives in the API reference.

| Env Var | Default | What it controls |
|---------|---------|------------------|
| `BALDUR_AUDIT_ENABLED` | `false` | Master switch for the audit subsystem — when off, no audit records are written |
| `BALDUR_LICENSE_KEY` |  | PRO entitlement (unset in OSS mode); the Audit Trail ships with the PRO tier |
| `BALDUR_SECRETS_AUDIT_SIGNING_KEY` |  | Keys the HMAC-SHA256 hash chain; a CRITICAL secret — in production, boot aborts if it is missing |
| `BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN` | `false` | Redis-backed hash chain — required for multi-host deployments (≥2 pods) |

The file hash-chain is the default, zero-config backend. Heavier backends are pluggable but need explicit activation, not just a connection string: a **Redis flush buffer** (turned on with `BALDUR_AUDIT_BUFFER_REDIS_ENABLED`, which stages records in Redis and drains them to the terminal store) and a **Postgres archival adapter** (wired in code against your Django audit model). Setting `BALDUR_REDIS_URL` or `BALDUR_SQL_DSN` alone does not switch the backend — those are shared connection inputs.

## See also

- [Getting Started](../../getting-started/index.md) — set Baldur up
- [Eventing, notification & audit interfaces](../../reference/interfaces/eventing_and_notification.md) — the audit adapter contract
- [Environment Variables](../../reference/env-vars.md) — the complete operator-tunable list
