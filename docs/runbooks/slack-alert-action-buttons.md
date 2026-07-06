# Slack Alert Action Buttons Setup Runbook

> **Purpose**: Turn on the one-click **Dashboard / Admin Panel / Runbook** buttons that appear under Circuit Breaker and Adaptive Throttle SLA alerts in Slack. These links are **off by default** — each is built from an operator-supplied base URL, and when that URL is unset the button is silently omitted (the alert body still delivers in full). Covers which env var feeds which button, the absolute-vs-relative URL rule, the `SITE_URL` dependency for relative links, and the go/no-go check.
> **Audience**: Operator / SRE who wants the Slack alert to link straight to their Grafana board, the Baldur admin panel, and their incident runbook instead of forcing a manual context switch.
> **Cadence**: One-time per deployment + revisit when a dashboard/admin/runbook URL changes or you point alerts at a new Grafana/host.

---

## TL;DR

The action buttons are **opt-in**. Baldur ships the button *mechanism*; the *target URLs* are yours. Set the per-feature base URLs as env vars and the buttons appear; leave them unset and the alert renders cleanly **without** buttons — never broken.

| Button | Slack label | Circuit Breaker env var | Adaptive Throttle SLA env var |
|---|---|---|---|
| Dashboard | 📊 Dashboard | `CB_DASHBOARD_URL` | `THROTTLE_SLA_DASHBOARD_URL` |
| Admin Panel | ⚙️ Admin Panel | `CB_ADMIN_BASE_URL` | `THROTTLE_SLA_ADMIN_BASE_URL` |
| Runbook | 📖 Runbook | `CB_RUNBOOK_URL` | `THROTTLE_SLA_RUNBOOK_URL` |

Plus one cross-cutting setting:

| Setting | Default | What it does |
|---|---|---|
| `SITE_URL` | `http://localhost:8000` (treated as **unset** until you set it explicitly) | Base URL that **relative** button paths (e.g. `/admin/baldur/circuitbreaker/`) are absolutized against. Slack rejects a message with a relative button URL, so a relative link with `SITE_URL` unset degrades to a plain (non-clickable) text field instead of a button. |

**The single most important rule**: prefer **absolute `http(s)://` URLs**. An absolute base URL renders as a real one-click button with no further setup. Only reach for relative paths + `SITE_URL` if you have a reason to.

---

## Background — How the buttons are built

Each alert family has a small per-feature URL builder that reads its base URLs from env vars at process start (`ActionableAlertUrlBuilder` for Circuit Breaker — `src/baldur/services/circuit_breaker/actionable_alert_urls.py`; a sibling builder for Adaptive Throttle SLA). The builder appends per-event context (service name, action, trigger time) as query parameters, then the Slack handler maps three fixed metadata keys to buttons:

- `dashboard_url` → **📊 Dashboard**
- `admin_url` → **⚙️ Admin Panel** (primary / highlighted)
- `runbook_url` → **📖 Runbook**

Three behaviors follow directly from the builder:

1. **Unset base URL → no button.** If the env var is empty, the builder returns `None` for that URL and the Slack handler skips the button. The alert's header, fields, and description are unaffected — you simply get an alert with fewer (or no) buttons. This is the safe default.
2. **Absolute URL → button as-is.** A base URL starting with `http://` or `https://` is used directly. This is the recommended form.
3. **Relative URL → needs `SITE_URL`.** A relative base path (e.g. `/admin/baldur/circuitbreaker/`) is absolutized against `SITE_URL` **only when `SITE_URL` is explicitly set**. If it is left at its default, the relative link is preserved as a plain text field (not a button), because Slack rejects a message carrying a relative button URL as `invalid_blocks`, and joining against a guessed host would emit misleading links on a multi-host deployment.

The buttons navigate the operator to the real surface (Grafana, the admin panel) rather than firing a one-click action — every state change still goes through the admin panel so it stays authenticated and audit-logged.

---

## Phase 1 — Decide your URL strategy

Pick **one** of the two forms for each base URL:

- **Absolute (recommended)** — `https://grafana.example.com/d/circuit-breaker`, `https://baldur.example.com/admin/baldur/circuitbreaker/`, `https://runbooks.example.com/circuit-breaker`. No `SITE_URL` needed; the buttons just work.
- **Relative + `SITE_URL`** — base paths like `/admin/baldur/circuitbreaker/` plus a single `SITE_URL=https://baldur.example.com`. Use this only if you want one host setting to drive every relative link.

```bash
# Only needed for the relative-path strategy:
SITE_URL=https://baldur.example.com
```

**Go/no-go**: you have decided absolute-vs-relative and, if relative, `SITE_URL` is set in the deployment environment → proceed to Phase 2.

---

## Phase 2 — Circuit Breaker buttons (OSS)

The Circuit Breaker OPEN / recovered / governance-blocked alerts carry the three buttons. Set whichever you want; omit the rest.

```bash
CB_DASHBOARD_URL=https://grafana.example.com/d/circuit-breaker   # → 📊 Dashboard (service appended as ?service=<name>)
CB_ADMIN_BASE_URL=https://baldur.example.com/admin/baldur/circuitbreaker/   # → ⚙️ Admin Panel (?service_id=<name>&action=review)
CB_RUNBOOK_URL=https://runbooks.example.com/circuit-breaker      # → 📖 Runbook
```

Notes:
- The builder appends context automatically — give it the **base** URL, not a per-service one. `CB_DASHBOARD_URL` gets `?service=<name>` (or `&service=` if it already has a query string); `CB_ADMIN_BASE_URL` gets `?service_id=<name>&action=review|history|governance_review`.
- The recovery (CLOSED) alert intentionally omits the Runbook button — recovery needs no runbook — so `CB_RUNBOOK_URL` only shows on OPEN / governance alerts.

Restart the process after setting env vars — the builder reads them once at startup.

---

## Phase 3 — Adaptive Throttle SLA buttons (PRO)

The Adaptive Throttle SLA warning / critical / recovered alerts carry the same three buttons via their own env vars:

```bash
THROTTLE_SLA_DASHBOARD_URL=https://grafana.example.com/d/throttle-sla
THROTTLE_SLA_ADMIN_BASE_URL=https://baldur.example.com/admin/baldur/throttle/
THROTTLE_SLA_RUNBOOK_URL=https://runbooks.example.com/throttle-sla
```

The same unset-→-no-button and absolute-vs-relative rules from Background apply identically. Adaptive Throttle is a PRO feature; see `docs/concepts/pro/throttle.md` for the SLA alert thresholds that trigger these notifications.

> Other notification-producing features add their own `<FEATURE>_*_URL` env vars as they ship; consult the feature's concept guide. Features that are off by default do not emit any alert (and therefore no buttons) until they are explicitly enabled.

---

## Phase 4 — Verify the buttons render

1. **Confirm the env vars are loaded.** After restart, the builder logs at DEBUG which bases it picked up:

   ```
   [debug] actionable_alert_url_builder.initialized dashboard_configured=True admin_configured=True runbook_configured=True
   ```

   Run with `BALDUR_LOG_LEVEL=DEBUG` to see it. All three `False` means the env vars did not reach the process — check the exact names and the environment the process actually loaded.

2. **Trigger one alert.** Force a Circuit Breaker to open against a test dependency (or wait for a real SLA warning) so a notification posts to Slack.

3. **Inspect the Slack message.** Below the alert fields you should see an **actions** block with the configured buttons (📊 Dashboard / ⚙️ Admin Panel / 📖 Runbook). Click one — it should land on your Grafana board / admin panel / runbook.

**Final go/no-go**: the alert shows the buttons **and** each button opens the intended page → action links are live. If the alert arrives but a button is missing, that base URL was unset or empty. If a link appears as plain non-clickable text, it was a relative path with `SITE_URL` unset (Phase 1).

---

## Common Mistakes

### Mistake 1 — Expecting buttons with no configuration

The buttons are opt-in. A fresh deployment posts alerts **without** them. There is no default Grafana/admin/runbook URL — Baldur cannot guess your infrastructure, so it omits the button rather than emit a broken link.

### Mistake 2 — Relative path with `SITE_URL` left at default

A relative base (`/admin/...`) needs an explicit `SITE_URL`. Without it the link degrades to non-clickable text. Either set `SITE_URL` or switch that base URL to an absolute `https://…` form.

### Mistake 3 — Appending the service yourself

Pass the **base** URL only. The builder adds `?service=…` / `?service_id=…&action=…`. A pre-parameterized base produces a doubled query string.

### Mistake 4 — Setting env vars on a running process

The builder reads the env vars once at startup. Set them and restart; a live process keeps the values it booted with.

### Mistake 5 — Pointing the Runbook button at Baldur

`*_RUNBOOK_URL` is **your** incident runbook, not a Baldur-hosted page. Baldur only emits the link; the content is yours to host.

---

## Cross-References

- `src/baldur/services/circuit_breaker/actionable_alert_urls.py` — the Circuit Breaker URL builder; env vars, query-parameter shapes, and the `None`-on-unset behavior
- `src/baldur/utils/url.py` — `absolutize_against_site_url`, the relative-path → `SITE_URL` join (and why an unset `SITE_URL` is left relative)
- `src/baldur/settings/root.py` — the `site_url` setting (`SITE_URL` env var) and its default
- `docs/concepts/pro/unified-notification.md` — how alerts are routed and delivered to Slack
- `docs/concepts/pro/throttle.md` — the Adaptive Throttle SLA thresholds behind the throttle alerts
- `docs/runbooks/observability-stack-setup.md` — sibling runbook; stand up the Grafana board that `*_DASHBOARD_URL` should point at

---

## Rollback

Unset the `*_URL` env vars (and `SITE_URL` if you set it only for this) and restart. Alerts revert to button-free delivery — the bodies are unchanged, so no alerting capability is lost. There is no state to migrate and nothing else in Baldur is affected; the URL builders are local to notification rendering.
