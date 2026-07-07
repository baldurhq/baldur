# Celery Quickstart

Protect Celery tasks with Baldur, and let Baldur run its own scheduled
maintenance on the beat you already have.

> Supports Python 3.11–3.13 and Celery 5.3+ (5.4 in CI). Assumes you have a
> working Celery app.

Baldur meets Celery in two places: the same `@baldur.protected` facade wraps a
task body, and an app-wide signal integration records every task's health for
you without touching each task.

## 1. Install

```bash
pip install baldur-framework[celery]
```

## 2. Protect a task body

`@baldur.protected` composes into a Celery task exactly as into any function.
Declare it just **below** `@app.task` so Celery registers the wrapped function:

```python
import baldur
from celery import Celery

app = Celery("myproject")

@app.task
@baldur.protected("charge-customer", retry=True)
def charge_customer(order_id):
    return payment_gateway.charge(order_id)
```

The call now travels through a circuit breaker and retry. Add `fallback=` for a
safe default, `idempotency_key="order_id"` to dedup a re-delivered task, or
`dlq=True` to set a final failure aside for replay (dead-letter storage is
**PRO**). See [Composing with @baldur.protected](../concepts/foundations/composition.md).

## 3. Observe every task automatically

Rather than decorate each task, connect Baldur to Celery's task signals once —
in the module that builds your Celery app. Task failures, retries, and successes
then feed Baldur's circuit breaker and metrics on their own, and trace/actor
context is carried across the enqueue → execute hop:

```python
from celery import Celery
from baldur.adapters.celery import setup_baldur_signals

app = Celery("myproject")

setup_baldur_signals(
    app=app,
    task_domain_mapping={
        "myproject.tasks.charge_customer": "payment",
        "myproject.tasks.sync_inventory": "inventory",
    },
)
```

`task_domain_mapping` groups tasks under a shared circuit-breaker / metric domain
(every payment task trips one breaker); an unmapped task uses its own task name.
The circuit-breaker, dead-letter, metrics, and forensic-capture hooks each toggle
independently (`cb_enabled` / `dlq_enabled` / `metrics_enabled` /
`forensics_enabled`), all on by default. Durable failure capture through the
dead-letter queue is a **PRO** feature.

## 4. Run Baldur's scheduled maintenance on your beat

Baldur relies on a handful of background jobs to heal itself — circuit-breaker
recovery probes, dead-letter archival and cleanup, expired-override cleanup,
metric collection. On a single host it elects itself with a local lock and runs
them out of the box. Across multiple hosts, hand them to your existing Celery
beat instead — one call injects Baldur's schedule (and its queues and routes)
into your app:

```python
from baldur.adapters.celery import configure_baldur_celery

configure_baldur_celery(app)
```

Then run beat and a worker as usual:

```bash
celery -A myproject beat -l info
celery -A myproject worker -l info
```

Each job lane is opt-out through an `include_*` flag, and multi-service
deployments can isolate queues with `queue_prefix=`. The
[multi-worker coherence runbook](https://github.com/baldurhq/baldur/blob/main/docs/runbooks/multi-worker-coherence.md)
walks through the single-host-lock vs. distributed-beat decision.

## See Baldur's events

Baldur logs to stdout automatically. Raise the log level to watch circuit
breaker and retry events as your tasks run:

```bash
export BALDUR_LOG_LEVEL=INFO   # circuit opened/closed, retries, ...
```

## Going to production

!!! danger "The in-memory fallback is single-worker only"

    Celery almost always runs more than one worker, so this matters from the
    first deploy. The zero-config path keeps circuit breaker state, idempotency
    keys, and counters in a per-process store — across workers they diverge
    silently, which breaks **correctness**, not just scale. This is a hazard,
    not a tuning knob: give Baldur a shared backend before you run a second
    worker.

Point Baldur at Redis so every worker shares state. No code changes needed — set
one environment variable before starting the workers:

```bash
pip install baldur-framework[celery,redis]
export BALDUR_REDIS_URL=redis://localhost:6379/0
```

## See also

- [Composing with @baldur.protected](../concepts/foundations/composition.md) — how the facade layers retry, fallback, and idempotency
- [Circuit Breaker](../concepts/oss/circuit-breaker.md) — the pattern your tasks travel through
- [DLQ + Replay](../concepts/pro/dlq-replay.md) — durable failure capture and replay (PRO)
