# PagerDuty / Slack Channel Topology Runbook

> **Purpose**: Decide and wire **where Baldur's notifications go** — which events page PagerDuty and which land in Slack. Covers the shipped severity→channel defaults, the two independent config homes (Meta-Watchdog escalation vs the PRO unified notification hub), the recommended PagerDuty-centric war-room topology for teams that run PagerDuty (critical → PagerDuty only; PagerDuty's own Slack integration echoes into the war-room channel and provides native Acknowledge/Resolve buttons), double-notification avoidance, and the trigger-only limitation (Baldur opens PagerDuty incidents; a human closes them).
> **Audience**: Operator / SRE wiring alert channels for a Baldur-protected service — both Slack-only teams and PagerDuty teams.
> **Cadence**: One-time per deployment + revisit when your on-call tooling (PagerDuty service, Slack workspace) changes.

---

## TL;DR

Severity separation is **already the shipped default** — you only choose targets:

1. **Slack is the "a human should see this" channel.** The escalation tier delivers WARNING and above to Slack; the notification hub routes every priority except `info` to Slack.
2. **PagerDuty is the "wake a human now" channel, and it is opt-in** (unset key = never pages). The escalation tier pages PagerDuty only at CRITICAL; the hub routes only `critical` priority to PagerDuty, and only when `BALDUR_CHANNEL_TARGET_PAGERDUTY_ENABLED=true`. PagerDuty delivery is a **PRO transport** — on an OSS install the PagerDuty leg degrades to a log line.
3. **Recommended for PagerDuty shops**: route `critical` to PagerDuty **only** and let PagerDuty's Slack integration echo incidents into your war-room channel — you get Acknowledge/Resolve buttons in Slack natively, with no inbound endpoint on Baldur's side (Phase 3).
4. **Baldur only opens PagerDuty incidents (`event_action: trigger`) — it never acknowledges or resolves them.** Close incidents in PagerDuty (UI, auto-resolve timeout, or the buttons from its Slack app). See [Known limitation](#known-limitation--trigger-only-outbound).

Config quick map:

| What | Env var | Default |
|---|---|---|
| Escalation + OSS Slack webhook | `BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL` | unset → no push |
| Escalation PagerDuty routing key | `BALDUR_META_WATCHDOG_PAGERDUTY_ROUTING_KEY` | unset → no page |
| Hub Slack webhook (PRO) | `BALDUR_CHANNEL_TARGET_SLACK_WEBHOOK_URL` | unset |
| Hub PagerDuty routing key (PRO) | `BALDUR_CHANNEL_TARGET_PAGERDUTY_SERVICE_KEY` | unset |
| Hub PagerDuty master switch (PRO) | `BALDUR_CHANNEL_TARGET_PAGERDUTY_ENABLED` | `false` |
| Hub priority→channel rules | `BALDUR_CHANNEL_ROUTING_PRIORITY_CHANNELS` | `critical→slack+pagerduty`, `high/medium/low→slack`, `info→(none)` |
| Daily report channels | `BALDUR_DAILY_REPORT_DEFAULT_CHANNELS` | `["slack"]` |

---

## Background — two delivery paths, one principle

The principle behind every default below: **PagerDuty receives only what justifies waking someone; Slack receives everything a human should eventually see.** Informational events never page.

### Path A — escalation tier (Meta-Watchdog pages, OSS circuit-breaker alerts)

Config home: `MetaWatchdogSettings` (`BALDUR_META_WATCHDOG_*`, `src/baldur/settings/meta_watchdog.py`).

The escalation manager (`src/baldur/meta/escalation.py`) selects channels **by escalation level, fixed in code**:

| Level | PagerDuty | Slack |
|---|---|---|
| CRITICAL | ✔ | ✔ |
| ERROR / WARNING | — | ✔ |
| INFO | — | — |

PagerDuty deduplicates repeated pages for the same component via a stable `dedup_key`; Slack has no native dedup, so escalation applies per-component cooldowns plus a cross-worker dedup lock (one page per incident cluster-wide, not one per gunicorn worker).

The same `slack_webhook_url` also feeds the OSS circuit-breaker open/close push — **when set, those POSTs are live even on a core-only install and in local development**. Leave it unset locally.

### Path B — unified notification hub (PRO)

Config homes: routing rules in `ChannelRoutingSettings` (`BALDUR_CHANNEL_ROUTING_*`, `src/baldur/settings/channel_routing.py`), delivery targets in `ChannelTargetSettings` (`BALDUR_CHANNEL_TARGET_*`, `src/baldur/settings/channel_target.py`).

The hub resolves channels per notification from a priority→channels table (with per-category overrides), then delivers to concrete targets. Two gates protect the PagerDuty leg: the routing table must list `pagerduty` for the priority (default: only `critical` does), **and** `pagerduty_enabled` must be `true`. `info` maps to an empty channel list — log-only by design.

Examples of hub `critical` events: emergency-mode escalation to its highest level, security incidents, audit integrity-gate failures.

### Daily report

The scheduled daily digest has its own channel list (`BALDUR_DAILY_REPORT_DEFAULT_CHANNELS`, default Slack only). Adding `pagerduty` there does **not** page every morning: the PagerDuty leg is skipped entirely unless the report contains actionable items (critical alerts, repeated task failures, error-budget blocks, failing chaos grades, high load-shedding), and severity is raised to critical only for the critical subset. A quiet day never pages.

---

## Phase 1 — Slack-only baseline (every tier)

Set the **escalation-home** webhook; on PRO, set the hub webhook as well. They are different settings on purpose (different delivery paths):

```bash
BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/…   # watchdog pages + OSS CB alerts
BALDUR_CHANNEL_TARGET_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/…  # PRO hub alerts (may be a different channel)
```

Pointing them at different Slack channels is normal — e.g. watchdog/CB events to `#baldur-health`, hub alerts to `#alerts`.

**Go/no-go**: `POST /meta-watchdog/escalation-test` on the admin server → the test message arrives in Slack → proceed.

---

## Phase 2 — Add PagerDuty (PRO)

Create a PagerDuty service with an **Events API v2** integration and take its routing key, then:

```bash
BALDUR_META_WATCHDOG_PAGERDUTY_ROUTING_KEY=<routing-key>   # watchdog CRITICAL pages
BALDUR_CHANNEL_TARGET_PAGERDUTY_SERVICE_KEY=<routing-key>  # hub critical alerts
BALDUR_CHANNEL_TARGET_PAGERDUTY_ENABLED=true               # hub master switch — key alone is not enough
```

What starts paging after this (and nothing else): watchdog CRITICAL component failures, hub `critical`-priority alerts, and — only if you also add `pagerduty` to `BALDUR_DAILY_REPORT_DEFAULT_CHANNELS` — daily reports that contain actionable items.

**Go/no-go**: `POST /meta-watchdog/escalation-test` → a test incident appears in PagerDuty. **The self-test opens a real incident (severity low, component `escalation_self_test`) and Baldur will not close it — resolve it in PagerDuty after verifying.**

---

## Phase 3 — Optional: PagerDuty-centric war-room topology (recommended if you run PagerDuty)

Instead of Baldur posting critical events to Slack and PagerDuty separately, make PagerDuty the hub for critical flow: **Baldur → PagerDuty → (PagerDuty's Slack integration) → war-room channel**.

1. Keep the Phase 2 keys.
2. Remove the direct Slack copy of hub critical alerts:

   ```bash
   BALDUR_CHANNEL_ROUTING_PRIORITY_CHANNELS='{"critical":["pagerduty"],"high":["slack"],"medium":["slack"],"low":["slack"],"info":[]}'
   ```

3. In PagerDuty, install the official **Slack integration** and point the service's incident notifications at your war-room channel (e.g. `#incident-war-room`). PagerDuty now posts incident opened/acknowledged/resolved messages there, **with native Acknowledge / Resolve buttons** — an engineer can ack the page from Slack and the PagerDuty incident state follows.

What you gain: one authoritative incident timeline (PagerDuty), no double-notification for critical alerts, Ack/Resolve from Slack for free.
What you accept: critical-path Slack visibility now rides on PagerDuty availability (that is PagerDuty's core competence), and non-critical alerts still flow to Slack directly from Baldur.

Notes:

- The **escalation tier keeps its direct Slack copy at CRITICAL** — its level→channel mapping is fixed in code, deliberately belt-and-suspenders when paging is at stake. This does not double-post into the war-room: point the escalation webhook at your alerts/health channel and let only the PagerDuty echo own the war-room channel.
- Do **not** remove `slack` from the `high`/`medium`/`low` rows — those never reach PagerDuty, so removing Slack leaves them with no channel at all.

**Go/no-go**: trigger (or self-test) one critical event → PagerDuty incident opens → its Slack echo lands in the war-room channel with working Ack/Resolve buttons → no duplicate direct post from Baldur in that channel.

---

## Phase 4 — Verify the full topology

1. **Channel self-test**: `POST /meta-watchdog/escalation-test` exercises the escalation transports end-to-end and reports a per-channel result. Remember to resolve the test incident in PagerDuty.
2. **Routing dry-run (hub)**: to validate routing without live sends, set `BALDUR_CHANNEL_TARGET_DRY_RUN=true` temporarily — deliveries log instead of send — then flip it back.
3. **Noise check after a week**: PagerDuty should have paged only for events you would genuinely wake someone for. If something informational paged, its priority/category routing is the knob (`BALDUR_CHANNEL_ROUTING_PRIORITY_CHANNELS` / `_CATEGORY_CHANNELS`) — not disabling PagerDuty wholesale.

**Final go/no-go**: Slack receives warning-tier traffic, PagerDuty receives only critical-tier traffic, and (if Phase 3) the war-room channel shows PagerDuty echoes with working buttons.

---

## Known limitation — trigger-only outbound

Baldur's PagerDuty integration sends `event_action: "trigger"` only — it never sends acknowledge or resolve. Even when Baldur's self-healing later recovers the condition, **the incident stays open until closed on the PagerDuty side** (an operator, the Slack-app buttons, or a PagerDuty auto-resolve timeout). Repeated triggers for the same ongoing condition collapse into the existing incident via the stable `dedup_key`, so an unresolved incident does not multiply. Treat Baldur-opened incidents as manual-close when configuring PagerDuty service settings (consider its per-service auto-resolve timeout as a backstop).

---

## Common Mistakes

### Mistake 1 — One Slack webhook, two homes

Setting only `BALDUR_CHANNEL_TARGET_SLACK_WEBHOOK_URL` and expecting watchdog pages or OSS circuit-breaker alerts (they read `BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL`) — or vice versa. The two delivery paths have separate config homes on purpose; set both on PRO.

### Mistake 2 — PagerDuty key set, hub leg silent

`BALDUR_CHANNEL_TARGET_PAGERDUTY_SERVICE_KEY` alone does nothing in the hub — `BALDUR_CHANNEL_TARGET_PAGERDUTY_ENABLED` is a separate master switch and defaults to `false`.

### Mistake 3 — Expecting PagerDuty pages on an OSS install

PagerDuty is a PRO transport. On OSS, escalation CRITICAL still *attempts* the PagerDuty channel but resolves to the logging fallback — the intent is recorded in logs, no page is sent.

### Mistake 4 — Forgetting the self-test opens a real incident

`POST /meta-watchdog/escalation-test` sends a real PagerDuty event (clearly labeled, severity low). Resolve it in PagerDuty afterward — Baldur will not.

### Mistake 5 — Live webhook in local development

`BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL` makes the OSS circuit-breaker push POST for real, including on a core-only install. Leave it unset locally unless you want live messages in a shared channel.

### Mistake 6 — Forcing PagerDuty-only across all priorities

Removing `slack` from every routing row does not "move everything to PagerDuty": only `critical` is meant for paging, and the hub's PagerDuty leg still requires the enabled flag. `high`/`medium`/`low` rows without `slack` simply deliver nowhere.

---

## Cross-References

- [meta-watchdog-escalation-response.md](meta-watchdog-escalation-response.md) — what to do *after* a page arrives; escalation pipeline health when pages stop
- [slack-alert-action-buttons.md](slack-alert-action-buttons.md) — the 📊/⚙️/📖 navigation buttons on Baldur's own Slack alerts (distinct from PagerDuty's Ack/Resolve buttons, which come from PagerDuty's Slack app)
- [observability-stack-setup.md](observability-stack-setup.md) — the dashboards your alerts should link to
- `docs/concepts/pro/unified-notification.md` — how the PRO hub routes and delivers alerts
- `src/baldur/settings/channel_routing.py` / `src/baldur/settings/channel_target.py` / `src/baldur/settings/meta_watchdog.py` / `src/baldur/settings/daily_report.py` — the four config homes
- `src/baldur/meta/escalation.py` — the fixed level→channel mapping and cross-worker dedup

---

## Rollback

- **Stop PagerDuty paging**: unset the two routing keys (or flip `BALDUR_CHANNEL_TARGET_PAGERDUTY_ENABLED=false`) and restart — PagerDuty legs skip with a "not configured" result; Slack delivery is unaffected.
- **Restore default routing**: unset `BALDUR_CHANNEL_ROUTING_PRIORITY_CHANNELS` — the shipped defaults return.
- **Undo the war-room echo**: remove the Slack integration on the PagerDuty side; nothing changes in Baldur.

No state to migrate — channel topology is pure configuration.
