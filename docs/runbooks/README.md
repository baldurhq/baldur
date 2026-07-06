# Runbooks

Step-by-step playbooks for operational procedures.
Each document is written to stand alone — **follow it top to bottom and you are done**.

These runbooks are operator-facing: deployment, incident diagnosis, and periodic
audits that must be executable without reading the source code.

> **New here, or arriving with a symptom? Start with the [Runbook Router](symptom-router.md).**
> It maps what you are seeing (something is wrong) or what you are turning on (setup)
> to the right runbook in the table below.

## When to write an operator runbook

- The procedure spans multiple layers (code + GitHub settings + secrets + CI) and order matters
- A multi-step task that does not finish in one shot — intermediate-step failure needs an explicit resume point
- A beginner or operator must be able to execute it without reading internal code

## Structure rules

- State the **TL;DR** and the **intended audience** right below the title
- End each phase with a **"Before moving to the next step"** check
- Include rollback points and recovery procedures inline
- Prefer in-repo paths over external links
- When you **add, rename, or remove** a runbook, update both this table below and the [Runbook Router](symptom-router.md) so the "read this first" index stays complete

## Operator Runbooks

| File | Purpose | Last verified |
|------|---------|---------------|
| [data-consistency-boundaries.md](data-consistency-boundaries.md) | DEGRADED-mode data trade-offs and per-data-kind placement decisions (which data belongs in Baldur vs an ACID DB) | 2026-05-01 |
| [gunicorn-graceful-shutdown.md](gunicorn-graceful-shutdown.md) | Wire `GracefulShutdownCoordinator` into a gunicorn deployment — guarantees SIGTERM drain, WAL flush, lease release, and LB removal (pick one of two patterns) | 2026-05-03 |
| [protect-hang-troubleshooting.md](protect-hang-troubleshooting.md) | Diagnose and resolve a suspected `protect()` zone hang (operator guide after the default timeout became None) | 2026-05-07 |
| [dlq-two-layer-activation.md](dlq-two-layer-activation.md) | Two-layer activation to satisfy the "DLQ absorbs ALL failures" contract (view-level `@dlq_protect` + middleware-level `BALDUR_DLQ_ELIGIBLE_PATHS`). Enabling only one layer leaves a 10–22% gap under a failure storm | 2026-05-12 |
| [secure-deployment.md](secure-deployment.md) | Production-critical secrets (`BALDUR_SECRETS_ENCRYPTION_KEY` / `BALDUR_SECRETS_AUDIT_SIGNING_KEY` CRITICAL tier + IMPORTANT/OPTIONAL classification) + TLS hardening (`BALDUR_TLS_*` 7-option reference) + the `validate_required_secrets()` pre-flight gate. `.env.template`-based workflow | 2026-05-24 |
| [meta-watchdog-escalation-response.md](meta-watchdog-escalation-response.md) | On-call diagnosis and manual-recovery procedure after a `Baldur <component> Failure` page (per-component symptom→diagnosis→recovery→graduation note). The v1.0 watchdog is detect+escalate only (`recovery_enabled=False`) — this manual runbook is the first rung of the auto-recovery ladder | 2026-06-11 |
| [audit-trail-activation.md](audit-trail-activation.md) | Activate the default-OFF Audit Trail — master switch `BALDUR_AUDIT_ENABLED` + signing-key (`BALDUR_SECRETS_AUDIT_SIGNING_KEY`, CRITICAL) pre-provisioning + pluggable backends (file hash-chain default / Redis flush buffer `BALDUR_AUDIT_BUFFER_REDIS_ENABLED` / SQL Django adapter via programmatic wiring) + multi-host distributed hash chain (`BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN`) + integrity verification (`/audit/integrity/verify`) go/no-go. Includes why it is OFF by default (opt-in, I/O fail-safe) | 2026-06-16 |
| [api-discoverability.md](api-discoverability.md) | Stand up the OpenAPI 3.0 schema, Swagger UI, ReDoc, and the consolidated `/features/` inventory endpoint on a Baldur-instrumented Django project | 2026-06-18 |
| [observability-stack-setup.md](observability-stack-setup.md) | Four-phase path from "nothing wired" to a full Grafana stack — the `BALDUR_OBSERVABILITY_PROFILE` selector (`local` / `otel_collector` / `auto`), admin console (`9090`, control plane) vs Grafana (observation plane), `/prometheus` scrape + dashboards, the additive OTel upgrade (traces→Tempo, logs→Loki, metrics stay scrape-only), the reference `examples/docker` stack, and the metrics-no-OTLP-push scale boundary. Clarifies that OTel does not duplicate the metric path | 2026-06-21 |
| [slack-alert-action-buttons.md](slack-alert-action-buttons.md) | Turn on the opt-in 📊 Dashboard / ⚙️ Admin Panel / 📖 Runbook buttons under Circuit Breaker (`CB_DASHBOARD_URL` / `CB_ADMIN_BASE_URL` / `CB_RUNBOOK_URL`) and Adaptive Throttle SLA (`THROTTLE_SLA_*`) Slack alerts — absolute-vs-relative URL rule, the `SITE_URL` dependency for relative links, unset→no-button (body still delivers) behavior, and the per-button verify go/no-go | 2026-06-24 |
| [multi-worker-coherence.md](multi-worker-coherence.md) | How Baldur's scheduled jobs and runtime-config changes stay coherent as you scale from a single host to multiple workers/pods, and which settings each topology requires (state backend / EventBus / leader election, or Celery beat) | 2026-06-29 |
| [runtime-config-change.md](runtime-config-change.md) | Safe procedure for editing runtime config via the console/`/config/*` REST — per-domain blast-radius map, the apply-strategy-vs-"takes effect" reach model (on next read / after worker restart / not currently reached), reading the apply response (Safe-Default clamps, 409), the inline rollback ladder (cancel → history rollback → global reset), and the v1.0 limitations (6 domains persisted-but-not-consumed, canary/governance advisory) | 2026-07-04 |
