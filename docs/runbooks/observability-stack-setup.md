# Observability Stack Setup

**TL;DR**: Baldur emits metrics, traces, and logs, but it does NOT run the storage/visualization
backends — you do, alongside your app. One knob (`BALDUR_OBSERVABILITY_PROFILE`) picks the export
mode. This runbook takes you from "nothing wired" to a full Grafana stack in four phases. Stop at the
phase that matches your tier — **metrics-only (Phase 1–2) is enough for most OSS deployments.**

**Intended audience**: an operator wiring Baldur into a monitoring stack. No source-code reading
required. Assumes you can set environment variables and run containers.

---

## Mental model — two planes (read this first)

Baldur observability splits into two planes that people routinely conflate:

| Plane | What lives here | Where it runs |
|-------|-----------------|---------------|
| **In-process (data plane)** | Baldur core + the **admin console** (control panel) | Inside your Python process |
| **External (observation plane)** | Prometheus/Mimir, Grafana, OTel Collector, Tempo, Loki | Separate containers you run |

Key consequences:

- The **admin console** (port `9090`) is Baldur's own control panel — it is **not** Grafana. Use it to
  *act* (trip/reset breakers, redrive DLQ, emergency stop). Use Grafana to *observe* (time-series history).
  They are complementary, not alternatives.
- **Metrics always leave via Prometheus scrape** — even with OTel enabled. OTel adds **traces and logs**
  (OTLP push), it does not change how metrics travel. There is no duplicate metric path.
- Nothing in the observation plane is required to *run* Baldur. It is required only to *see* it.

---

## Phase 0 — Choose your profile

`BALDUR_OBSERVABILITY_PROFILE` is the single selector. The boot log line
`observability.profile_resolved` always records which backend was chosen — it is never silent.

| Profile | Metrics | Traces | Logs | Pick when |
|---------|---------|--------|------|-----------|
| `local` | Prometheus scrape | — | — | You only need metrics + dashboards |
| `otel_collector` | Prometheus scrape (still) | OTLP → Tempo | OTLP → Loki | You want distributed tracing + log correlation |
| `auto` (default) | resolves at boot | resolves at boot | resolves at boot | Let SDK availability decide |

`auto` resolves to `otel_collector` only when **both** the OTel trace SDK and the Prometheus metric
bridge are importable (i.e. the `opentelemetry` extra is installed); otherwise it falls back to `local`.

**Before moving to the next step**: decide local vs otel_collector. If unsure, start `local` (Phase 1–2)
and upgrade later — the upgrade is additive and non-breaking.

---

## Phase 1 — Minimum: admin console + `/prometheus` (no external infra)

This phase gives you live metrics and a control panel with zero external dependencies.

1. Install with the Prometheus extra:

   ```bash
   pip install "baldur-framework[prometheus]"
   ```

2. Set environment:

   ```bash
   BALDUR_OBSERVABILITY_PROFILE=local
   BALDUR_ADMIN_ENABLED=true
   BALDUR_ADMIN_PORT=9090          # default
   BALDUR_ADMIN_BIND=127.0.0.1     # default; TLS is delegated to a reverse proxy
   ```

3. Start your app (the admin console auto-starts from the framework adapter's `baldur.init()` path).

4. Verify the metrics endpoint and the console:

   ```bash
   curl -s http://127.0.0.1:9090/prometheus | head        # Prometheus text exposition (public)
   curl -s http://127.0.0.1:9090/health/ping              # liveness (public)
   # open http://127.0.0.1:9090/ in a browser for the HTML console
   ```

   If you run Django, the same metrics are also exposed at `/api/baldur/prometheus/` on the app port.

**Before moving to the next step**: `GET /prometheus` returns a non-empty `# HELP ...` body and the
boot log shows `observability.profile_resolved resolved_profile=local`. If `/prometheus` 404s, the
admin server is disabled (`BALDUR_ADMIN_ENABLED`) or the Prometheus extra is missing.

---

## Phase 2 — Add an external scraper + Grafana (metrics dashboards)

Point a Prometheus-compatible scraper at the endpoint from Phase 1, then read it from Grafana.

1. Configure your Prometheus (or Mimir) server to scrape the app:

   ```yaml
   # prometheus.yml (operator-owned)
   scrape_configs:
     - job_name: baldur
       metrics_path: /prometheus            # or /api/baldur/prometheus/ for Django
       static_configs:
         - targets: ['your-app-host:9090']
   ```

   On Kubernetes, use the provided `ServiceMonitor` instead: `examples/k8s/base/servicemonitor.yaml`
   (Prometheus Operator, 15s scrape on the `http` port).

2. Add the scraper as a Grafana datasource (type `prometheus`).

3. Import the prebuilt dashboard: `examples/monitoring/baldur-overview.json` (OSS panels — CB state,
   HTTP latency, retry outcomes, idempotency, health, system control, graceful shutdown). PRO
   deployments additionally import `examples/monitoring/baldur-operations.json` (DLQ, error budget,
   throttle, saga, canary).

4. Optional alerts: `examples/monitoring/prometheus-alerts.yml` (DLQ pending, CB transitions, error
   rate/latency, error-budget exhaustion).

**Before moving to the next step**: a panel in `baldur-overview` shows live data. If panels are blank,
the scrape target is wrong or the series has not been produced yet (exercise a protected call first).

> **Stop here** if you do not need traces or logs. This is a complete metrics stack for the OSS tier.

---

## Phase 3 — Upgrade to `otel_collector` (traces + logs)

This is purely additive: metrics keep flowing exactly as in Phase 2; you gain traces (Tempo) and
log correlation (Loki) on top.

1. Install the OpenTelemetry extra (keep prometheus):

   ```bash
   pip install "baldur-framework[opentelemetry,prometheus]"
   ```

2. Set environment (OTel settings follow the standard `OTEL_*` convention):

   ```bash
   BALDUR_OBSERVABILITY_PROFILE=otel_collector   # or leave it `auto` now that the SDK is present
   OTEL_SERVICE_NAME=my-service
   OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
   OTEL_TRACES_EXPORTER=otlp                      # otlp | console | none
   OTEL_TRACES_SAMPLER=parentbased_traceidratio
   OTEL_TRACES_SAMPLER_ARG=0.1                    # 10% head sampling
   OTEL_EXPORTER_OTLP_TIMEOUT=5000                # ms
   ```

3. Run an OTel Collector. The reference config is `examples/monitoring/otel-collector.yml`:
   - **Receivers**: OTLP (gRPC `4317`, HTTP `4318`) for traces/logs **+** a Prometheus receiver that
     **scrapes the app's `/prometheus`** (this is how metrics reach the collector — still a pull).
   - **Exporters**: Tempo (traces), Mimir remote-write (metrics), Loki (logs).

4. Verify:
   - Metrics: unchanged — still visible in Grafana via Mimir/Prometheus.
   - Traces: a request produces a trace in Tempo (Grafana → Explore → Tempo).
   - Logs: app logs appear in Loki with a `trace_id` field that links to the Tempo trace.

**Before moving to the next step**: boot log shows `resolved_profile=otel_collector` and a trace is
queryable in Tempo. If you see `otel.prometheus_metric_reader_not_installed`, the metric bridge is
absent — metrics silently fell back to local Prometheus (still fine), but re-check the extra.

---

## Phase 4 — One-command reference stack (demo / staging)

`examples/docker/docker-compose.yml` wires the whole observation plane for evaluation:

- **Grafana** (`3000`) — datasources preprovisioned: Mimir (metrics, default), Tempo (traces), Loki (logs)
- **Mimir** (`9009`) — long-term metric store (Prometheus-compatible)
- **Tempo** (`3200`) — traces · **Loki** (`3100`) — logs
- **OTel Collector** (`4317`/`4318`) — ingestion with tail-sampling biased toward resilience events
  (CB/DLQ/errors/high-latency 100%, normal 1%)
- Demo Django app feeding the pipeline

Dashboards and datasource provisioning live under `examples/docker/config/`. This stack is for
demo/staging; production operators run their own managed equivalents and reuse the dashboard JSON.

---

## Known boundary — metrics are scrape-only (no OTLP push)

Even under `otel_collector`, Baldur wires only a per-process scrape-model `PrometheusMetricReader` for
metrics. There is **no** OTLP `PeriodicExportingMetricReader` (push) for metrics — traces and logs push
via OTLP, metrics do not. Consequences:

- In multi-process deployments (e.g. gunicorn prefork), each worker keeps its own metric registry; the
  collector-side aggregation benefit is not realized. Scrape each worker, or use Prometheus multiprocess
  mode, as you would for any prometheus_client app.
- This is a deliberate, documented boundary (a scale-tier limit, not a bug) — it does not affect
  correctness of the scraped series at the OSS/PRO single-region tier.

---

## Disable / rollback

| Goal | Action |
|------|--------|
| Turn OTel off, keep metrics | `BALDUR_OBSERVABILITY_PROFILE=local` (metrics stay on Prometheus scrape) |
| Hard-mute the OTel SDK (standard env) | `OTEL_SDK_DISABLED=true` → metrics fall back to Prometheus; boot logs `observability.otel_sdk_disabled_by_standard_env` |
| Turn off the admin console | `BALDUR_ADMIN_ENABLED=false` |
| Stop traces only (keep SDK/metrics bridge) | `OTEL_TRACES_EXPORTER=none` |

Disabling is always safe: metrics degrade to local Prometheus, never to silent loss, and the chosen
backend is always logged.

---

## Verification checklist

- [ ] Boot log shows `observability.profile_resolved` with the profile you intended.
- [ ] `GET /prometheus` (admin `9090`) or `/api/baldur/prometheus/` (Django) returns a non-empty body.
- [ ] Admin console `/` loads and can read live CB/DLQ state.
- [ ] (Phase 2) A `baldur-overview` Grafana panel shows live data.
- [ ] (Phase 3) A request produces a Tempo trace; its `trace_id` appears on the matching Loki log line.
- [ ] (Multi-process) You scrape every worker, or run prometheus_client multiprocess mode.
