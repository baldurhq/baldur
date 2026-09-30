# Audit Trail

> A tamper-evident record of the configuration changes and healing decisions in your system — who changed what, when, and why — built for compliance and incident forensics.

!!! info "PRO feature"
    Audit Trail is a PRO-tier feature. It answers the question every regulated or production-critical team eventually has to answer for an auditor or an incident review: *"can you prove who changed this, and when?"*

## What is it?

When you run a system in production, things change constantly. Someone raises a retry limit, an operator forces a recovery, the framework itself trips a circuit breaker to protect a failing dependency. Most of those changes leave no trace — until something breaks and you need to know what happened.

An **audit trail** (also called an audit log) is a permanent, append-only history of those changes: a chronological account of *who* did *what*, *when*, and *why*. It is the same idea as a bank statement or a patient chart — a trustworthy record you can go back and read, and that you can rely on not to have been quietly rewritten. In Baldur, the Audit Trail records configuration changes and automated healing decisions as structured, privacy-safe, tamper-evident entries.

## Why it matters

Without an audit trail, the history of your system lives in memory and guesswork. That is fine until the day it isn't: a compliance review, a security incident, or a 2 a.m. outage where the first question is "what changed?"

The Audit Trail turns that history into something you can actually rely on:

- **Prove what changed, for compliance.** Regulations such as GDPR and CCPA require demonstrable control over configuration and data-handling changes. An audit trail is the evidence — a defensible record that change tracking is in place and that personal data was handled carefully.
- **Reconstruct an incident.** After an outage, the audit trail lets you answer "what changed right before this broke?" and "did the system heal itself, or did someone intervene?" without reverse-engineering the answer from logs that were never meant to tell that story.
- **Attribute changes.** Each entry records the actor and the action, and a configuration
  change also its before-and-after values and the reason given, so accountability is built in
  rather than reconstructed after the fact.
- **Capture identity without hoarding PII.** Attribution needs to know *who*, but storing a raw client IP address (or the secrets inside a config change) is itself a compliance liability. Baldur masks client IP addresses and redacts sensitive values, so you keep accountability without retaining the raw personal data.
- **Trust the record itself.** An audit log that can be silently edited is worthless. Baldur's entries are tamper-evident, so a removed or altered record is detectable rather than invisible.

## How it works in Baldur

When the Audit Trail is enabled, automated healing decisions (a breaker opening or closing, a
failed call captured to the dead letter queue, a batch replay or a forced redrive from it) and
changes made through the runtime configuration API each produce a structured record; individual
retries and fallbacks are not recorded one by one. You do not call it explicitly for each change:
the framework records the events it manages.

Operator actions are covered unevenly, which matters if you rely on the trail for attribution.
Retrying or resolving a single dead-letter entry writes no entry, and neither does changing
chaos-experiment or storage-tuning settings; resetting the configuration to its defaults is
recorded under the system actor. A breaker an operator blocks, allows or resets through the admin
server or the Web Console is recorded as an automatic change by the system actor, because that
server does not pass the operator's identity to the breaker. The same action from the `baldur`
command line is recorded as forced, under the operator's name.

Each entry captures the full story of a single change:

- **Who:** the user or system actor responsible.
- **What:** the action taken (a configuration change, a breaker opened automatically or forced
  open by an operator, a dead-letter entry stored or replayed, and so on) and what it touched; for a
  configuration change, the value before and after.
- **When:** the timestamp.
- **Why:** the reason supplied with the change, and for a healing decision the component that made
  it.

On top of that record, three properties make the trail safe to depend on:

- **Privacy-safe identity.** The IP address of the client behind a change is masked: its last two
  parts are hidden (for example `192.168.***.***`) rather than stored in full. An address that is
  itself the subject of a security event, such as the one an IP ban blocked, is recorded in full.
  Inside a changed setting, fields whose names mark a secret (such as password, secret, token, API
  key, private key) are redacted, but a setting whose own name marks a secret and whose value is a
  plain string is recorded as is. You keep the record of *who* acted without retaining their raw IP
  address.
- **Trace correlation.** Each entry carries a trace ID. For a change made inside a traced request, that ID lines the audit record up with the request's distributed trace, connecting "this config changed" to the exact call that changed it.
- **Tamper-evidence through a hash chain.** Each entry carries a cryptographic fingerprint: a SHA-256 hash computed over the entry's own contents *together with* the fingerprint of the entry immediately before it. The records are therefore linked into a chain, every entry bound to its predecessor back to the first. Editing a past entry changes its fingerprint, and deleting one leaves the entry after it pointing at a fingerprint that no longer exists; either way the chain breaks at that point, and hiding the break would mean recomputing every entry that follows. In production the fingerprints are also keyed: the signing key you configure turns each hash into an HMAC, so someone who can rewrite the log files still cannot recompute a chain that passes verification, because they do not hold the key.

An integrity check walks the chain and reports whether it is intact, and where the first break is,
so tampering is locatable rather than silent. Run it with the same signing key set, or every keyed
entry reads as modified. The check starts each file at the chain's first entry, so it can vouch
only for the file where the chain began: a later day's file, or one host's file in a distributed
chain, reports its opening entries as missing even when nothing was touched.

Records persist to your configured storage backend, so the trail survives a restart as long as that
storage does. The default backend writes its files under the application's working directory
(`logs/audit`); if that directory cannot be created or written, startup carries on and the files go
to a per-user fallback directory instead. A replaced container keeps neither, so in a container,
mount a persistent volume at `logs/audit`.

A failed recording never stops the change it records. When the backend cannot write, a healing
decision waits in a local write-ahead log and is delivered once the backend recovers, but a
configuration change's entry is not kept, and the application log carries an error in its place.

Baldur only ever appends to the trail: no built-in job deletes or archives old entries. If your
compliance policy sets a retention limit, pruning the trail to it is a file-lifecycle task you own,
with the export tool's date-range filter to carve out what to keep.

| What you observe | When it happens |
|------------------|-----------------|
| A structured entry recording who, what, when, and why | a configuration change or an automated healing decision occurs |
| The acting client's IP address appears masked (last two parts hidden), not as a raw value | an entry records the IP of the client behind a change |
| An entry can be matched to a request's distributed trace | the change happened in the context of a traced request |
| An integrity check reports whether the hash chain is intact, and pinpoints the first broken link | you verify the file where the chain begins, with the signing key set |

## Configuration

The knobs an operator sets most often. The full list lives in the API reference.

| Env Var | Default | What it controls |
|---------|---------|------------------|
| `BALDUR_AUDIT_ENABLED` | `false` | Master switch for the audit subsystem. You rarely set it on PRO: an active entitlement switches audit on at startup while this variable is unset. Setting it yourself always wins — `false` keeps audit off on an entitled install; `true` turns the subsystem on without one, but selects no backend, so nothing reaches a trail until you select one |
| `BALDUR_LICENSE_KEY` |  | PRO entitlement (unset in OSS mode); the Audit Trail ships with the PRO tier |
| `BALDUR_SECRETS_AUDIT_SIGNING_KEY` |  | Keys the HMAC-SHA256 hash chain. Required in production while the trail is on or a PRO entitlement is active, including an entitled install that sets `BALDUR_AUDIT_ENABLED=false`: boot aborts if it is missing. Checked at startup, after the licence is validated, so restart after installing a licence |
| `BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN` | `false` | Moves hash-chain sequencing from a per-host file lock to Redis, so every host shares one ordered sequence while that Redis answers. You rarely set it on PRO: an entitled install that names a chain Redis URL turns it on at startup while this variable is unset. Set it only to override that — `false` keeps the per-host chain, and an explicit `true` also refuses the quiet fallback: when no Redis client can be built, the process still starts but writes no trail at all, with an error at startup, rather than a local chain |

Two or more hosts writing the trail with no Redis named is the one case worth stating plainly: each
host keeps its own valid, tamper-evident chain, but they are separate histories, not one ordered
ledger, so verification cannot order an entry on one host against an entry on another. Setting
`BALDUR_REDIS_URL` in the environment is what makes them one chain, and on an entitled install that
is all it takes, provided that Redis answers when each process starts: a process that starts while
it is down keeps a per-host chain until it restarts, with a warning at startup. With
`BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=true` set, such a process logs an error at startup instead and
joins the shared sequence as soon as Redis answers.

The file hash-chain is the default, zero-config backend: on a PRO-entitled install it is switched
on and selected for you, with no environment variable to set. If your application selects its own
audit backend before Baldur starts, that choice is kept, unless it is the built-in do-nothing
backend: selecting that is not a supported way to opt out, and startup will replace it. To keep
audit off on an entitled install, set `BALDUR_AUDIT_ENABLED=false`. Heavier backends are pluggable but
need explicit activation, not just a connection string: a **Redis flush buffer** (assembled in your
application code, with `BALDUR_AUDIT_BUFFER_REDIS_ENABLED` switching on the Celery tasks that drain
it to the terminal store) and a **Postgres archival adapter** (wired in code against your Django
audit model). Setting `BALDUR_REDIS_URL` or `BALDUR_SQL_DSN` alone does not switch the backend —
those are shared connection inputs.

Exporting the trail as JSONL or JSON, to a file or to stdout, needs nothing beyond the PRO package,
and the export tool's date-range filter works on it. The tool's other outputs do not serve this
trail: CSV fills only the timestamp column, and the Parquet and S3 choices stop with an error
before writing any entry, whatever is installed.

## See also

- [Getting Started](../../getting-started/index.md) — set Baldur up
- [Eventing, notification & audit interfaces](../../reference/interfaces/eventing_and_notification.md) — the audit adapter contract
- [Environment Variables](../../reference/env-vars.md) — the complete operator-tunable list
