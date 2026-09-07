---
title: Retry and backoff for LLM API rate limits in Python
description: >-
  Exponential backoff lives inside one process. When several workers call the
  same rate-limited API, each one discovers the 429 alone and waits alone. What
  that costs, and how to put a single cooldown in front of all of them.
---

# Retry and backoff for LLM API rate limits in Python

The standard answer to `429 Too Many Requests` is retry with exponential
backoff, and it is a good answer. `tenacity` does it in four lines. The OpenAI
Python SDK retries some failures before you ever see them. For one process
calling one API, that is close to correct.

Most services are not one process. You run four Gunicorn workers, or eight
Celery workers, or a fan-out of agent tasks that each call a model. Backoff
state lives inside the retrying call: in memory, in that process, for the life
of that call. None of it is shared. So when the provider starts refusing you,
every worker finds out separately and waits separately, and the mechanism you
added to take pressure off the API takes it off for exactly one caller.

## What the provider sees

Eight workers are calling the same model endpoint. The provider starts
returning 429.

Worker 1 is refused, sleeps a second, tries again. While it sleeps, workers 2
through 8 keep calling — nothing told them anything happened. Each is refused
in turn, and each starts its own ladder from the bottom.

On your side this reads as *we have retries with backoff*. On the provider's
side, a refusal meant to slow you down slowed down one eighth of you. Jitter
spreads the collisions out; it does not reduce how many there are. And because
each worker's ladder starts whenever that worker happened to be refused, the
eight never line up — there is no interval where all of them are waiting at
once, which is the only kind of interval a rate limit can recover in.

Nothing here is a bug in your retry library. Per-call backoff is doing exactly
what it says. It is answering a smaller question than the one you have.

## The other half: a retry is a bet

Backoff decides *when* to try again. The question underneath is whether you
should try again at all.

A retry is a bet that the first attempt did nothing. For a read, that bet is
free. For a call that had an effect — a tool call that wrote a row, a model
call you are billed for, a webhook you delivered — it is free only if you can
tell "it failed" apart from "it succeeded and the answer never reached me." A
request that timed out looks identical from the outside.

The 429 case makes that more likely rather than less: your retries land exactly
when the provider is saturated and slowest to answer, which is when a call is
most likely to complete on their side and time out on yours. If an agent's tool
call is what got retried, the tool runs twice.

That half is a different mechanism — an idempotency key on the protected call,
so the second attempt returns the first attempt's result instead of re-running
it. It has its own page: [Idempotency keys for Python
services](concepts/oss/idempotency.md).

## One cooldown in front of all of them

The fix for the first half is to stop making each worker learn the limit for
itself. Put the cooldown somewhere all of them can see, and have them wait on
that instead.

In Baldur, naming the call is the configuration:

```python
import baldur

@baldur.protected("openai_chat", retry=True)
def ask(prompt: str) -> str:
    return client.chat.completions.create(...)
```

`"openai_chat"` is the coordination key. When any worker takes a 429 on it, the
cooldown is written to shared storage; every other worker that reaches this
call waits on that same deadline rather than discovering the limit on its own.
The retry stage resolves the shared coordinator itself, so there is nothing
else to wire. It is on by default, and
`BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED=false` turns it off for a whole
deployment if you want the per-process behavior back.

Three details worth knowing before you rely on it:

**Shared means shared storage.** Out of the box Baldur runs on an in-memory
backend, and an in-memory cooldown is shared with nobody — it is still one
process learning alone. Point it at Redis (or a database) and the same code
starts coordinating across the fleet. See [storage
backends](concepts/foundations/storage-backends.md) for the trade-offs.

**Detection reads the exception, not a status literal.** Baldur classifies a
raised error as a rate limit by checking both its message and its *type name*
for `429`, `rate limit`, `ratelimit`, `too many requests`, `throttle`, or
`quota exceeded`. A client that raises something called `RateLimitError` —
which is what the OpenAI Python SDK raises on a 429 — matches on the type name
alone, with no configuration.

**`Retry-After` is honored when the provider sends one.** Baldur reads it from
the exception's `retry_after` attribute or from its response headers, in both
forms the HTTP spec allows: a number of seconds, or a date. The provider's own
number beats a computed backoff, because the provider is the one who knows.

## The hole to know about before you rely on it

The 429 has to arrive as a **raised exception**.

If your HTTP client hands back a response object with `status_code == 429`
instead of raising — `requests` or `httpx` without `raise_for_status()` — then
as far as the retry stage is concerned the call succeeded. It does not retry,
and it does not coordinate. You get the per-process behavior you had before,
silently, which is the worst way to get it.

Two ways to close it today. Either make the client raise:

```python
@baldur.protected("openai_chat", retry=True)
def ask(prompt: str) -> str:
    response = httpx.post(...)
    response.raise_for_status()   # now the 429 is visible
    return response.json()
```

Or drive the coordinator directly on a call it does not wrap:

```python
from baldur.services.rate_limit_coordinator import RateLimitCoordinator

coordinator = RateLimitCoordinator.get_instance()

@coordinator.rate_limit_aware("openai_chat")
def ask(prompt: str):
    return httpx.post(...)          # inspected for a 429 on the way back
```

That decorator waits out an active cooldown before calling, reads the returned
response for a 429, and reports it. If the remaining cooldown is longer than it
is willing to sleep, it raises instead of calling — a decorator cannot return
"nothing," so refusing is its only honest option.

## What this is not

It is not a fleet-wide request quota. Sharing a *cooldown* is not the same as
sharing a *counter*, and Baldur does not pretend to enforce a hard total across
your workers on the request path — that belongs at a gateway that sees all the
traffic in one place. [Rate limiting in
Baldur](concepts/foundations/rate-limiting.md) is the honest map of which
mechanism counts per instance and which counts across the fleet.

It also does not make your quota bigger. Nothing here buys you throughput. What
it buys is that a limit you already hit costs you one backoff instead of N, and
that the rest of the fleet stops arriving during the window the first worker is
waiting out.

## Where to go next

- [Rate limiting in Baldur](concepts/foundations/rate-limiting.md) — every
  mechanism, and whether it counts per instance or across the fleet
- [Idempotency keys for Python services](concepts/oss/idempotency.md) — the
  other half, for calls a retry must not run twice
- [Retry with exponential backoff in Python](concepts/oss/retry.md) — the retry
  stage itself, and what composes with it
- [Circuit breaker for Python](concepts/oss/circuit-breaker.md) — what happens
  when the 429s stop being a blip
- [Getting Started](getting-started/index.md) — `pip install baldur-framework`
  and a protected call
