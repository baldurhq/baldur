---
title: Idempotency keys for Python services
description: >-
  How an idempotency key keeps a charge, an email, or a shipment from running
  twice in a Python service when retries, double-clicks, or duplicate webhooks
  replay the same request.
---

# Idempotency for Python services

> Makes "this must never happen twice" operations safe (a card charge, an email, a shipment) by remembering which requests have already run and blocking the repeats, even when a client retry, a double-click, or a duplicate webhook fires the same request again.

## What is it?

An operation is *idempotent* when running it twice has the same effect as running it once. An
elevator call button is idempotent: pressing it five times still summons one elevator. Most
real-world side effects are not naturally like that — charge a card twice and the customer pays
twice.

The problem is that distributed systems *will* deliver the same request more than once. A user
double-clicks "Pay". A webhook provider redelivers an event it isn't sure you received. A client
resends a request whose response was lost in transit even though the work itself succeeded. In Baldur,
**Idempotency** is key-based deduplication: you give each logical operation a key (such as the
order ID), Baldur remembers which keys it has already seen, and a second arrival of the same key is
blocked instead of executed again.

## Why it matters

- **At-least-once delivery becomes safe to receive.** Queues redeliver, webhook providers resend,
  and clients retry requests whose answer they never got. With a key on the operation, the repeat
  is recognized and the side effect does not run again. What the key does *not* cover is Baldur's
  own `retry=` on the same call: the key is checked once, when the call starts, so every attempt
  retry makes inside that call runs your function again. Turn `retry=` on only for work that is
  safe to repeat by itself (see the [Retry](retry.md) guide).
- Double-submits and duplicate webhooks are blocked, even concurrent ones. The key is claimed
  atomically, so two requests racing in at the same instant can't both win. There is no
  check-then-act window for a duplicate to slip through.
- **No hand-rolled dedup.** The homegrown "look it up, then insert" check is exactly the racy
  pattern that fails under concurrency. Baldur replaces it with an atomic claim plus an explicit,
  catchable duplicate error.
- A failure doesn't poison the key. If your function raises, its key is released so a later call
  can run the operation (once any work a timeout cut off has ended, below), and if several race
  for it, exactly one wins. The flip side: a function that raised *after* its side effect took
  hold (the charge went through, then the response timed out) releases the key too, so the repeat
  runs the charge again. The payment provider's own key is what covers that case (below). A call
  ended from outside rather than by an error (an async cancellation, or a sync `BaseException`
  such as a gevent timeout) settles once nothing it started is still running: its key is
  released, or completed if your function was running under the facade's own `timeout=` and went
  on to return (below).

## How it works in Baldur

You attach a key to the operation on whichever surface fits:

- On the `@baldur.protected` facade (or its call forms `protect` / `aprotect`), pass
  `idempotency_key=` to compose the key with the rest of the pipeline. A string names a field on
  the call's context (e.g. `"order_id"`); a callable builds a composite key. The key is checked once when the call
  starts, before the circuit breaker and retry run, and marked once the call hands you its result
  or its error, so the retry attempts in between are not deduplicated. A call the `fallback=`
  answered after a failure or a circuit-breaker refusal releases its key like a call that raised,
  so a genuine repeat runs the work. A sync call that `timeout=` cut off (with or without a
  fallback answer) hands you its error (or the fallback's answer) before your function has
  stopped, because the sync path cannot kill the function's thread; its key stays held while that
  work runs, so a repeat in that time is blocked with `"ABORT"` instead of running alongside it.
  When the work ends, the key follows how it ended: completed (a repeat gets `"SKIP"`) if it
  returned, released if it raised. The same holds when something else cuts that wait short while
  your function runs (a Celery soft time limit, a gevent timeout, Ctrl-C): your wait ends at
  once, the key stays held while the function runs, and then it follows how the function ended. Work the timeout cut off before it started, and an async
  call's timed-out work (the async path cancels it), leave nothing running, so the key is
  released at once and an immediate retry runs. A timeout inside your function (a nested
  `protect(timeout=...)`, whether its timeout fired or an interruption cut its wait short) holds
  the key the same way when the keyed call then ends in an error:
  held while its work runs, then released, as long as that work runs in the function's own
  thread or task, in `asyncio.to_thread`, in a Baldur timeout worker, or (with PRO) in a
  thread-pool compartment's worker. Work started
  with `loop.run_in_executor`, `threading.Thread` or a plain executor `submit`, and threads an
  async function starts itself, are not tracked: the key is released while they may still run. A
  hold lasts at most the execution window (`idempotency_execution_ttl`, 30 minutes by default);
  work still running after that no longer holds the key, and a repeat can start the operation
  again beside it. Set the window above your operation's worst-case run, and give the
  operation's own outbound calls a timeout so a stuck run ends. Give each keyed call its own context
  object, and don't pass it to the protected calls inside it: calls that share one can mark each
  other's keys, and a timeout inside reads as the keyed call's own.
- The standalone decorator `@idempotent` wraps any sync or `async` function. Name the parameters
  that identify the request (`key_args=["order_id"]`) or supply a `key_fn=` for a custom key, and
  pick a domain to namespace it. To combine it with the facade, stack it beneath
  `@baldur.protected`, or use the facade's own `idempotency_key=`. Stacked above a facade that has
  a `fallback=`, it sees the fallback's answer as your function's return and marks the key
  completed. A function that raised while work a timeout cut off inside it is still running keeps
  its key held until that work ends, then releases it.
- For programmatic use, `IdempotencyService` with `IdempotencyKey` gives you explicit
  check-then-mark control when a decorator doesn't fit (batch jobs, event consumers). Its
  contract is looser than the two surfaces above: a duplicate is reported in the returned
  result rather than raised, and because checking and marking are two separate steps, two
  callers racing on a not-yet-marked key can both pass the check. It also fails open where they
  fail closed. When the store can't be reached, the check does not raise; it carries on as if
  the store held no record of the key. In production with no shared cache, the service falls
  back to per-process state (logging a warning) instead of refusing. Reach for the facade or the
  decorator (or the service's distributed-lock helpers) when concurrent duplicates matter.

On the facade and decorator surfaces, the key's life is the same: the first call **claims** the
key atomically and runs. Success marks the key **completed**, and it is remembered for a memory
window (a TTL). A failure marks it **failed**, which releases it so a later call can claim it again.
Only a raised error (an `Exception`) counts as a failure, plus a returned value that the retry's
`retry_on_result` predicate still rejects when the retries run out: a call that *returns* an error
response (a 503 object, say) that nothing rejects counts as a success, so raise on error statuses.
A call ended from outside Baldur by a `BaseException` (an async call's
`asyncio.CancelledError` when an enclosing `asyncio.wait_for` gives up on it, say, or a gevent
timeout interrupting a sync call) settles by the same rule as a failure: its key is
released once nothing it started is still running, so a retry can run right away. While work the
call stopped waiting for still runs, the key stays held, then settles as above: by how that work
ended when the interrupted wait was the facade call's own `timeout=`, released otherwise. Like a
call that raised, a call cancelled
after its side effect took hold (the request reached the provider, or your function had just
returned) is released too, so a retry runs the side effect again; the provider's own key covers
that case (below). Two narrow exceptions leave the claim held until the execution window runs
out: a sync interruption that lands while the dedup store is still answering the claim or the
mark, and a coroutine closed without finishing (its event loop shut down with the call still
pending).

```mermaid
stateDiagram-v2
    [*] --> UNCLAIMED
    UNCLAIMED --> RUNNING: first call claims the key
    RUNNING --> COMPLETED: your function returns
    RUNNING --> FAILED: your function raises
    FAILED --> RUNNING: a later call claims the key again
    COMPLETED --> UNCLAIMED: the memory window (TTL) expires
```

| What you observe | When it happens |
|------------------|-----------------|
| The call runs normally | the key's first arrival, or a later arrival after your function raised or the circuit breaker refused the call (even when the facade's fallback answered), after timed-out work that raised or never started, after an async call's timeout, or after a call ended from outside with nothing it started still running |
| The duplicate is blocked: `IdempotencyDuplicateError` with `decision` `"SKIP"` | the same key arrives again after a successful run (or after work a `timeout=` cut off, or whose wait a soft time limit or another interruption cut short, went on to return), within the memory window |
| The duplicate is blocked: `IdempotencyDuplicateError` with `decision` `"ABORT"` | the same key arrives while the first call is still running (including work a `timeout=` cut off, or work whose wait a soft time limit or another interruption cut short, that is still running, up to the execution window) or, in the two narrow cases above, after a call ended from outside, until the execution window runs out |
| The call is blocked: `IdempotencyUnavailableError` | the dedup store could not be reached, under the default fail-closed posture |

Where the guarantee holds, and where it stops:

- **Blocked means a clear error, not a silent skip.** A duplicate raises
  `IdempotencyDuplicateError` (the same error type on the facade and the decorator alike), and
  its `decision` tells you whether the original already completed (`"SKIP"`) or is still in
  flight (`"ABORT"`). Baldur does *not* replay the original call's response — catch the error and
  treat it as "this work already happened."
- **Fail-closed by default.** On the facade and the decorator, supplying a key is a "must not
  duplicate" signal, so if the dedup store can't be checked (say, a momentary network blip), the
  call is blocked with `IdempotencyUnavailableError` rather than risking a duplicate side effect.
  If availability matters more than the guarantee, you can opt a facade call into fail-open with
  `idempotency_fail_open=True`, letting the unverifiable call proceed.
- The seen-keys ledger lives in the cache `baldur.init()` wires, so
  with `BALDUR_REDIS_URL` set the same key is blocked across every worker and host. In production,
  `init()` refuses to start without it rather than let dedup shrink to per-worker memory: a dedup
  that only works within one process is a false promise. A production process with no shared
  cache (it skipped `init()`, or `init()` failed before wiring one) refuses every guarded call
  with `ConfigurationError`, unless `BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK=true` accepts a
  per-process ledger. The async facade shares the ledger only through Redis, so in production its
  keyed calls refuse the same way when the registered cache is another backend, while sync calls
  use that backend. Outside production, or with that setting on, call `init()` before the first
  guarded call: a surface first used before `init()` keeps its in-process ledger for the life of
  the process.
- **The honest boundary.** Dedup is exactly-once for the duplicate and concurrent cases. If a
  process crashes *after* the side effect but *before* the completion mark, the key's claim
  eventually goes stale and a later call may run the operation again, an essential
  at-least-once limit of any external dedup ledger. True end-to-end exactly-once requires a
  transactional outbox in the same datastore as your own side effect.
- Pair it with your payment provider's own key. When the side effect is a call to an
  external payment API, the crash window above, a function that raised after the charge went
  through, and the attempts of Baldur's own `retry=` all have one practical fix: every major
  provider (Stripe, Adyen, PayPal, Toss Payments) accepts an idempotency key of its own and
  deduplicates on *its* side — and, unlike Baldur, replays the original response to a repeat.
  Derive that key deterministically from the business identifier (the order ID, never a
  random value generated per attempt), so every repeat sends the *same* key and the provider
  recognizes it. Baldur's dedup then covers your process (double-clicks, concurrent workers,
  duplicate webhooks), while the provider's key covers the in-doubt window Baldur cannot see.
- A key lives under two independent clocks. The *memory window* is
  how long a completed operation is remembered — how long duplicates stay blocked after success.
  It defaults to 30 minutes, is tunable globally with
  `BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS`, and per call with `ttl=` on `@idempotent` or
  `idempotency_ttl=` on the facade. The *execution window* is how long a running claim is
  honored before a crashed attempt becomes retryable; it defaults to 30 minutes and is tuned
  per call with `execution_ttl=` / `idempotency_execution_ttl=`. Set the execution window to
  your operation's worst-case runtime, never to the dedup horizon, so a remember-for-hours key
  doesn't leave a crashed claim stuck for hours; a value *below* the true worst case risks a
  duplicate running concurrently once the claim goes stale. The programmatic check/mark API remembers for its own configurable TTL.
- **Keys are namespaced by operation.** On the decorator and the programmatic API, a domain
  (`external_service`, `event`, `async_task`, and friends) keeps the same order ID in two domains
  from colliding, and within a domain a `key_args=` key includes the decorated function's
  module-qualified name, so two different functions sharing `key_args=["order_id"]` and the same
  order ID each get their own verdict: charging order 1 never blocks shipping order 1. The facade
  has no domain; its field-name form prefixes the protected name instead (`charge-customer` plus
  the order ID), which keeps two protected operations apart the same way. A custom key gets
  neither: a `key_fn=` result is used as-is behind the domain, and a callable `idempotency_key=`
  result is used as-is, so two operations whose custom keys can come out equal block each other
  unless the key itself names the operation. On the decorator, when
  two entry points really are one logical operation (an HTTP handler and a worker guarding the
  same charge), give both the same explicit `operation=` label. One caveat follows from the
  default being derived from the function's name: renaming or moving the function resets that
  operation's dedup memory at the deploy, so set `operation=` explicitly for correctness-critical
  operations to make the identity rename-proof.

## Configuration

The most common knobs an operator sets. The full list lives in the API reference.

| Env Var | Default | What it controls |
|---------|---------|------------------|
| `BALDUR_IDEMPOTENCY_ENABLED` | `true` | Master switch — when `false`, no surface checks or records a key, so duplicates are no longer blocked anywhere |
| `BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS` | `1800` | Default memory window (in seconds) on the decorator and facade surfaces — how long a completed operation keeps blocking duplicates when no per-call `ttl` is given |
| `BALDUR_IDEMPOTENCY_DEFAULT_CACHE_TTL` | `60` | How long (in seconds) the programmatic check/mark API remembers a processed operation when neither the service nor the call is given its own TTL |
| `BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK` | `false` | Accepts a per-process ledger in production when no shared cache is wired: guarded calls run instead of raising `ConfigurationError`, and each process blocks only its own duplicates |
| `BALDUR_REDIS_URL` | `redis://localhost:6379/0` | Points the seen-keys ledger at a shared Redis, so a duplicate key is blocked across all workers and hosts; leave it unset and the ledger stays in process memory (the default address is not dialed for it) and production `init()` refuses to start |

## See also

- [Retry](retry.md) — the companion pattern: retry re-runs the work inside one call, which the key does not deduplicate
- [Composing with @baldur.protected](../foundations/composition.md) — where the key sits in the pipeline, next to retry and the fallback
- [Circuit Breaker](circuit-breaker.md) — the other resilience guard composed under `@baldur.protected`
- [Decorators API Reference](../../reference/decorators.md) — `@idempotent` and friends, full signatures
- [Environment Variables](../../reference/env-vars.md) — the complete operator-tunable list
- [Getting Started](../../getting-started/index.md) — set it up
