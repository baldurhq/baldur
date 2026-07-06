# DLQ + Replay

> Catches every operation that fails because a dependency was down — with the context needed to run it again — so nothing is silently lost, and you can replay the backlog once the dependency recovers.

!!! info "PRO feature"
    DLQ + Replay is a PRO-tier feature. It answers the production question every team eventually
    hits: *"what happened to the work that failed while our database / payment provider / webhook
    target was down?"* Without it, that work is simply gone. With it, the work is preserved and
    replayable.

## What is it?

When an operation in your app fails because something it relies on is unavailable (a database
timeout, a payment provider returning 503, a webhook endpoint that won't answer), that unit of work
usually just disappears. The request errors out, maybe a line lands in a log file, and the actual
work (the payment, the job, the message) is lost.

A **dead letter queue** (DLQ) is the standard name for a holding area where failed work is parked
instead of thrown away. Think of the "undeliverable mail" bin at a post office: a letter that
couldn't be delivered isn't shredded; it's set aside so it can be looked at and sent again later.
**Replay** is the act of taking that parked work and running it again. In Baldur these two halves
are a single feature: **DLQ + Replay**.

## Why it matters

Without a dead letter queue, an outage doesn't only cause errors *while* it is happening — it causes
**permanent data loss**. Every payment, job, or message that failed during the outage is gone, and
recovering it means digging through logs and reconstructing the work by hand, if it can be recovered
at all.

DLQ + Replay turns that permanent loss into a recoverable backlog:

- **No silent loss.** Every failed operation is captured together with the forensic context (what
  was being done, the request data, the failure reason, the stack trace) needed to understand and
  re-run it.
- **Recover on your schedule.** When the dependency comes back, replay the backlog instead of
  rebuilding lost work from log files.
- **Catch-up can be automatic, and self-paced.** Baldur can replay queued work the moment a tripped
  circuit breaker for that dependency recovers, so the backlog drains itself without an operator
  watching the clock. The catch-up can adapt to how the recovery is going, shrinking each replay
  batch while replays are still failing and growing it once they succeed, so draining a backlog never
  re-hammers a dependency that has only just come back up.
- **Even repeat failures aren't a dead end.** A failure that keeps failing isn't replayed forever or
  silently dropped. Once it exhausts its replay budget it is parked for review instead of looping.
  After you fix the underlying cause you can deliberately re-drive it, so even a "poison-pill" failure
  is recoverable rather than lost.
- **The queue protects itself.** Size limits (overall and per-domain) plus an overflow strategy
  keep a failure storm from filling your storage and dragging the rest of the system down with it.

## How it works in Baldur

When an operation Baldur is protecting fails, it is captured as an **entry** in the dead letter
queue, recording the context needed to replay it later. Capturing a failure is designed to stay off
the request's critical path, so recording a failure doesn't add latency to the call that already
failed. Each entry then moves through a lifecycle you can watch in the Web Console DLQ panel or query
over the REST API:

```mermaid
stateDiagram-v2
    [*] --> PENDING: operation fails, captured
    PENDING --> REPLAYING: replay triggered
    REPLAYING --> RESOLVED: replay succeeds
    REPLAYING --> PENDING: replay fails, attempts remain
    REPLAYING --> REQUIRES_REVIEW: replay attempts exhausted
    REQUIRES_REVIEW --> REPLAYING: operator force-redrives after a fix
    PENDING --> EXPIRED: retention window passes
    RESOLVED --> ARCHIVED: aged out / cleaned up
```

You have three ways to replay the queued work:

- **Targeted replay.** Pick a single entry (or one domain) and replay it from the Web Console or the
  REST API. Useful when you want to confirm a fix before draining everything.
- **Batch replay by type.** Replay everything of a given domain or failure type at once, for
  example every database-timeout failure after the database recovers.
- **Automatic on recovery.** When a dependency's circuit breaker closes again after an outage, Baldur
  can sweep that dependency's queued failures and replay them, so recovery and catch-up happen
  together. You can enable **adaptive batch sizing** for these automatic sweeps: instead of a fixed
  batch size, Baldur watches the success rate of each batch and adjusts the next one, shrinking the
  batch when too many replays are still failing and growing it again after several clean batches,
  staying between a floor and a ceiling you set. The backlog drains quickly while the dependency is
  healthy and backs off automatically if it starts struggling again.

When a failure can't be replayed successfully (the dependency is still down, or the work itself is
broken), Baldur retries it up to a configurable budget. An entry that exhausts that budget is neither
retried forever nor discarded: it converges to a terminal **needs-review** state, where it stays
queryable so an operator can investigate. Once the root cause is fixed, an operator can deliberately
**force-redrive** the parked entry, an audited, admin-level action that grants it a fresh replay
budget and sends it back through replay. If the underlying problem still isn't fixed, the entry simply
returns to the needs-review state, so a force-redrive can never turn a poison-pill into an endless loop.

| What you observe | When it happens |
|------------------|-----------------|
| A failed operation appears in the queue with its failure reason and request data | a protected operation fails |
| You replay one entry, a whole domain, or a failure type | a targeted or batch replay from the Web Console or REST API |
| Queued work drains on its own | a dependency's circuit breaker recovers and an automatic replay sweep runs |
| The automatic sweep speeds up or eases off batch by batch | adaptive batch sizing is enabled and the recent replay success rate changes |
| An entry stops being retried and is parked in a needs-review state | its replay attempts are exhausted |
| You re-drive a parked entry after fixing its root cause | a deliberate, admin-level force-redrive from the Web Console or REST API |
| Old entries age out — expiring, then archiving | the retention window passes |
| New failures are dropped, rejected, or summarized instead of growing the queue without bound | the queue hits its size limit and the overflow strategy applies |

When the queue reaches its size limit, the **overflow strategy** decides what gives:

- `drop_oldest`: evict the oldest entries to make room for new failures (the default).
- `reject`: refuse new entries (surfaced as a 503) so nothing already queued is displaced.
- `compress_oldest`: summarize the oldest entries into a compact record before evicting them, so an
  aggregate trace of what failed survives even after the raw entries are gone. These summaries are
  grouped by failure type and stay queryable over the REST API, and they age through their own
  lifecycle — `ACTIVE`, then `STALE`, then `ARCHIVED` — so old aggregates clean themselves up over
  time instead of accumulating forever.

By default the queue lives in your configured storage backend; for deployments that cannot tolerate
losing even queued-but-not-yet-replayed work across a process crash, a durable storage mode keeps the
queue crash-safe without slowing down the failure path that feeds it.

Because capture runs off the request's critical path, the outbox that buffers failures is itself
monitored so it can never silently lose work. It exposes leading-indicator signals (how deep the
buffer is and how long entries wait before being written) and raises an alert if its drop rate
crosses a threshold, while its background drain worker's liveness is watched separately. You see the
buffer filling up *before* it starts shedding, rather than discovering after the fact that captured
failures were dropped.

## Trace continuity — from the original failure to its replay

When you run distributed tracing (OpenTelemetry into Jaeger, Datadog, Tempo, and the like),
the DLQ sits across a deliberate gap in time: an operation fails *now*, is parked, and is replayed
*later* — sometimes seconds later, sometimes weeks. Baldur links the two ends so that "original
failure → DLQ capture → replay" reads as one connected story instead of two unrelated traces.

**How the link is made.** At capture time Baldur records the failing request's trace on the entry
(its `origin_trace_id`, plus the full W3C trace/span ids when an OpenTelemetry span is active). When
that entry is later replayed — by any path: a targeted retry, a batch, an automatic on-recovery
sweep, or a force-redrive — Baldur:

- adds an `origin_trace_id` field to the replay's log line,
- records the origin trace in the replay's audit entry, and
- when OpenTelemetry is active, wraps the replay in a `dlq.replay` span carrying a **span link** back
  to the original failure's span, plus a searchable `baldur.dlq.origin_trace_id` attribute.

The link is **additive**: the replay keeps its own trace (the operator request or circuit-breaker
recovery that triggered it) as its primary trace, and the origin is attached alongside. The two
answer different questions — *"under what request did this replay run?"* versus *"what failure is this
replay recovering?"* — so both are kept rather than one overwriting the other.

**Finding the connection in your tools.**

- *In an OpenTelemetry backend (Jaeger/Datadog/Tempo):* open the `dlq.replay` span and follow its
  span link to jump to the original failure's trace. You can also search spans by the
  `baldur.dlq.origin_trace_id` attribute.
- *Without OpenTelemetry (plain structured logs):* the replay log line and the capture log line both
  carry the origin id, so a `origin_trace_id=<id>` query across your logs returns both ends. Note
  that a bare `trace_id=<origin>` search will **not** match the replay lines — because the replay
  keeps its own trigger trace, the origin rides the separate `origin_trace_id` field. Search that
  field, not `trace_id`.

**Two limits worth knowing about — both expected, neither a defect:**

- *Mixed-mode capture.* If a failure was captured while OpenTelemetry was **not** producing a span,
  only the display `origin_trace_id` is stored — there is no full span id to build an OTEL span link
  from at replay time. Nothing is lost: an operation with no capture-time span had no origin span to
  link to in the first place. The `baldur.dlq.origin_trace_id` attribute is still searchable on the
  `dlq.replay` span.
- *APM retention mismatch.* Because the DLQ can outlive any trace-retention window by design, an entry
  replayed weeks later may link to an origin trace your APM store has already aged out (Jaeger/Datadog
  typically keep ~14–30 days). The span link then resolves to an expired trace — an orphan span or a
  "Trace Not Found" in the UI. This is inherent to store-and-forward across a retention boundary, not
  a bug; the `origin_trace_id` remains recorded on the entry and in the logs/audit regardless.

Entries captured **before** this linkage existed, and captures that had no active trace, simply
replay without an origin link — the absence is the normal state, not an error.

## Configuration

The knobs an operator sets most often. The full list lives in the API reference.

| Env Var | Default | What it controls |
|---------|---------|------------------|
| `BALDUR_DLQ_ENABLED` | `true` | Whether failed operations are captured into the dead letter queue at all |
| `BALDUR_DLQ_MAX_SIZE` | `100000` | Maximum total entries the queue holds before the overflow strategy applies |
| `BALDUR_DLQ_OUTBOX_ENABLED` | `true` | Capture failures through a non-blocking outbox so recording a failure stays off the request hot path |
| `BALDUR_REDIS_URL` | `redis://localhost:6379/0` | The Redis backend the queue is stored in (shared by Baldur's Redis consumers) |
| `BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL` | `INFO` | Log level for DLQ capture and replay events |

### Closing the loop — making automatic replay actually drain

Automatic replay on circuit-breaker recovery is on by default, but it only *drains*
your backlog once four prerequisites are in place. Until they are, a recovery leaves the entries
parked — captured and safe, but not replayed. The Web Console DLQ panel and the
`GET /dlq/cleanup/stats/` payload report an **armed / disarmed** state and name the first missing
prerequisite, so you can tell at a glance whether the loop is live:

1. **Register a replay handler per domain.** Baldur captures the failed work, but only *your* code
   knows how to re-run it. Register a handler for each domain you want replayed:

    ```python
    from baldur.services.replay_service import register_replay_handler, ReplayHandler, ReplayResult

    class PaymentReplayHandler(ReplayHandler):
        @property
        def domain(self) -> str:
            return "payment"

        def can_replay(self, failed_op) -> tuple[bool, str]:
            return True, ""

        def replay(self, failed_op) -> ReplayResult:
            # re-run the captured work; return succeeded/failed
            return ReplayResult.succeeded(failed_op.id, "reprocessed")

    register_replay_handler(PaymentReplayHandler())
    ```

    Without a registered handler, every replay for that domain fails per-entry and the entry is
    parked for review.

2. **Map recovered services to their failure types.** When a circuit breaker closes, Baldur needs to
   know *which* captured entries the recovered dependency is responsible for. Configure that mapping
   with `BALDUR_REPLAY_AUTOMATION_SERVICE_FAILURE_TYPE_MAP` (see
   [Environment Variables](../../reference/env-vars.md)). An empty mapping is surfaced as a
   blocked-with-signal event on recovery, not a silent no-op.

3. **Run a Celery worker on the `dlq_processing` queue.** On-recovery replay execution is dispatched to Celery.
   A worker must be consuming the `dlq_processing` queue for the dispatched replay to run:

    ```bash
    celery -A your_app worker -Q dlq_processing
    ```

    If Baldur is running without Celery available, the recovery logs a WARNING naming this
    remediation, and the arming surface reports `celery_missing`. You can still drain the backlog
    manually with the console **Replay** action.

4. **Keep on-recovery replay enabled.** `BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_ENABLED` is `true` by default; the
   arming surface reports `disabled` when it is turned off.

**Recommended alert:** because a dispatched replay only runs when a worker is actually consuming
`dlq_processing`, alert on the depth of that queue (broker-side) in addition to the
`baldur_dlq_auto_replay_armed` gauge. A queue that grows without draining means the dispatch is
succeeding but no worker is consuming it.

## See also

- [Getting Started](../../getting-started/index.md) — set it up
- [DLQ API Reference](../../reference/pro/dlq.md) — full options and signatures
- [Replay API Reference](../../reference/pro/replay.md) — replay service options
- [Environment Variables](../../reference/env-vars.md) — the complete operator-tunable list
