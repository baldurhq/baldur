---
title: Celery task failed after max_retries — where it went and how to get it back
description: >-
  After max_retries Celery re-raises the exception, marks the task FAILURE and
  acks the message; the arguments are gone unless you logged them. How to
  capture every task that fails for good, and replay it once the dependency
  is back.
---

# Your Celery task failed after max_retries. Where did it go?

A task calls something outside your process: a payment provider, a model API,
an email service. Tonight that dependency is down for forty minutes. Every task
that runs in the window fails, retries, fails again, and after the third retry
Celery gives up — `max_retries` defaults to `3`, and once it is exceeded the
current exception is re-raised (or `MaxRetriesExceededError`, when `retry()`
was called without one). The task's state becomes `FAILURE`. The worker
acknowledges the message. The next task starts.

Here is what Celery keeps of that task:

- **With a result backend** (and `ignore_result` off): the `FAILURE` state and
  the traceback, filed under the task id. Not the arguments — and you need the
  id to look it up.
- **Without a result backend**, or with `ignore_result=True`: nothing.
- **In the broker**: nothing either. The message that carried the arguments
  was acknowledged, and an acknowledged message is gone.

So the list of what you lost this morning is your log file, if you logged the
arguments. `grep MaxRetriesExceededError` gives you a count, not a list.

## Why retry did not cover it

Retry is for blips. `retry_backoff` is off by default, so the three attempts
land within seconds of each other; turn it on and `retry_backoff_max` caps the
wait at 600 seconds, so the whole budget still fits inside half an hour. A
forty-minute outage outlasts every task's retries — and each task spends them
alone. Two hundred queued tasks make six hundred calls into an API that is
down, and then two hundred tasks are gone. Retry answers "was that a blip?" It
has no answer to "the dependency is down; hold this and run it later."

## What Celery gives you for this, honestly

- **`Reject` and a dead-letter exchange.** Raise `Reject(requeue=False)` and a
  RabbitMQ broker with a dead-letter exchange configured will file the message
  there. That is broker-level, RabbitMQ-only (a Redis broker has no
  equivalent), and it takes you as far as "the message is somewhere" — replay
  is still re-publishing by hand.
- **`acks_late` and `task_reject_on_worker_lost`.** These protect a task whose
  *worker* died mid-run. A task that raised is acknowledged either way.
- **The result backend.** A record that it failed, not a copy you can re-run.
- **Building it yourself.** An `on_failure` hook that writes the task name,
  args and kwargs to a table; a management command that re-enqueues them; and
  some way of deciding that the provider is back, so the replay does not run
  into the same outage. It is a weekend of work, and it is the part of the
  codebase that never gets a test.

## Capturing every task that fails for good

One call in the module that builds your Celery app:

```python
from celery import Celery
from baldur.adapters.celery import setup_baldur_signals

app = Celery("myproject")

setup_baldur_signals(
    app=app,
    task_domain_mapping={
        "myproject.tasks.send_invoice": "billing",
        "myproject.tasks.summarize_document": "llm",
    },
)
```

That connects Baldur to Celery's task signals. From then on, a failure that
arrives after the retries are spent (`retries >= max_retries`, or from a task
with no retry budget at all) is captured with the task name and id, its
`args` and `kwargs`, the exception type and message, the traceback, a
failure-type classification read off the exception (`NETWORK_ERROR`,
`TIMEOUT`, `RATE_LIMITED`, `EXTERNAL_SERVICE_ERROR`, `VALIDATION_ERROR`,
`AUTH_ERROR`, …), and a recommended action. Intermediate retry failures are
not captured — only the attempt Celery gave up on. Nothing changes per task;
`@app.task` stays as it is.

The same signals feed a circuit breaker per domain, so `billing` has a live
state you can read. The signal path records; it does not stop tasks from
running while the breaker is open. For the calls you want held back as well,
put `@baldur.protected("billing")` under `@app.task` — the
[Celery quickstart](getting-started/celery.md) shows the two together.

## Seeing what you lost

```bash
baldur dlq list --pending
baldur dlq list --domain billing --json
```

Or open the built-in web console's dead-letter panel. Either way, each entry
is the task, the arguments it was called with, why it failed, and what to do
about it — the list the log file could not give you.

## Getting it back

Baldur captured the work; only your code knows how to re-run a task. A replay
handler for a Celery domain re-enqueues the task from the captured request:

```python
from baldur.services.replay_service import (
    ReplayHandler,
    ReplayResult,
    register_replay_handler,
)
from myproject.tasks import send_invoice


class BillingReplayHandler(ReplayHandler):
    @property
    def domain(self) -> str:
        return "billing"

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        request = failed_op.request_data or {}
        send_invoice.apply_async(
            args=request.get("args", []),
            kwargs=request.get("kwargs", {}),
        )
        return ReplayResult.succeeded(failed_op.id, "re-enqueued")


register_replay_handler(BillingReplayHandler())
```

Register it in a module the worker imports — registration is per process, and
the worker is where replay runs. "Succeeded" here means re-enqueued; from
there the task's outcome is Celery's again, retries included.

With the handler in place, two ways to drain the queue:

- **By hand**, once you know the provider is back:

    ```bash
    baldur dlq replay --domain billing
    ```

    or the per-entry Retry action in the console.

- **Automatically, when the dependency recovers.** Tell Baldur which failure
  types the outage produced for the domain, and run a worker on the
  `dlq_processing` queue the replay is dispatched to:

    ```bash
    export BALDUR_REPLAY_AUTOMATION_SERVICE_FAILURE_TYPE_MAP='{"billing": ["NETWORK_ERROR", "TIMEOUT", "EXTERNAL_SERVICE_ERROR"]}'
    celery -A myproject worker -Q celery,dlq_processing
    ```

    When the `billing` breaker closes again, the captured entries with those
    failure types go back through your handler. The console's dead-letter
    panel reports whether the loop is armed and, if it is not, names the
    prerequisite that is missing — a handler, the map, or a worker on the
    queue.

## What this is not

**It is not "replay everything."** A task that failed because its input was
bad will fail the same way tomorrow. That is what `can_replay` is for, and why
the map lists the failure types an outage produces rather than every type
there is; a `VALIDATION_ERROR` entry waits for a person.

**Replay is a bet that the first attempt did nothing.** For a task that
charged a card and then timed out, that bet is not free — the charge may have
gone through and the confirmation never came back. That is a different
mechanism: an idempotency key on the protected call, so the second run returns
the first run's result instead of charging again. It has its own page:
[Idempotency keys for Python services](concepts/oss/idempotency.md).

**Shared means shared storage.** Out of the box the queue lives in process
memory, and Celery almost always runs more than one process. Point Baldur at
Redis before you start the second worker — `BALDUR_REDIS_URL` is the one
variable — or the entries one worker captured are invisible to the others. The
[Celery quickstart](getting-started/celery.md#going-to-production) has the
production checklist.

## Where to go next

- [Celery quickstart](getting-started/celery.md) — `pip install
  baldur-framework[celery]`, the signal setup, and Baldur's maintenance on your
  beat
- [DLQ + replay](concepts/foundations/dlq-replay.md) — how capture, the
  outbox, and the on-recovery sweep work end to end
- [Idempotency keys for Python services](concepts/oss/idempotency.md) — for
  the tasks a replay must not run twice
- [Circuit breaker for Python](concepts/oss/circuit-breaker.md) — the state
  behind "the dependency is back"
