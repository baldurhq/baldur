---
title: Retry and backoff for LLM API rate limits in Python
description: >-
  Exponential backoff lives inside one process. When several workers call the
  same rate-limited API, each one discovers the 429 alone and waits alone. What
  that costs, and how one wrapped client puts a single cooldown in front of all
  of them, moves to another endpoint, and keeps the jobs no endpoint answered.
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

That half is a different mechanism, and it covers only part of the problem. An
idempotency key on the protected call blocks a repeat of a call that returned:
the repeat raises `IdempotencyDuplicateError` instead of running, and your code
treats that as "already done" — Baldur does not hand back the first call's
result. A call that raised after the provider did the work (the SDK's read
timeout, say) releases its key, so the repeat runs again; only a key the
provider itself honors covers that case. It has its own page: [Idempotency
keys for Python services](concepts/oss/idempotency.md).

## One cooldown in front of all of them

The fix for the first half is to stop making each worker learn the limit for
itself. Put the cooldown somewhere all of them can see, and have them wait on
that instead.

In Baldur that is one line around the client, where you create it:

```python
import baldur
from openai import OpenAI

client = baldur.llm.wrap(OpenAI())


def ask(prompt: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
```

The call sites do not change. Every call the wrapped client makes runs under
Baldur, named after where it goes — here `llm.api_openai_com.gpt_4o_mini`, the
host and the model. That name is the coordination key: when any worker takes a
429 on it, the cooldown is written to shared storage, and every other worker
that reaches the same host and model waits on that same deadline instead of
discovering the limit on its own. The same name keys a retry ladder and a
circuit breaker, so the provider gets one coordinated backoff rather than one
per worker.

The wrap covers the OpenAI Python SDK — and through it Azure OpenAI and every
OpenAI-compatible server (DeepSeek, Groq, OpenRouter, Ollama, a self-hosted
model) — the Anthropic SDK, and the Google Gen AI SDK (`google-genai`). An
`AsyncOpenAI` / `AsyncAnthropic` client, or `client.aio` on Gemini, works the
same; the wait is then an `asyncio.sleep`, so it costs that request its latency
and nothing else on the worker.

The SDKs retry on their own: the OpenAI and Anthropic clients retry twice per
worker by default, each worker on its own clock. Inside the wrap those retries
are switched off on a copy of the client (the one you passed in is unchanged),
so Baldur's coordinated retry is the only loop. A `google-genai` client retries
nothing unless you set `HttpOptions(retry_options=...)`; the wrap cannot switch
those off, and they run inside each of Baldur's attempts, so leave them unset on
a client you wrap. On an OpenAI or Anthropic
client, `wrap(client, timeout=60.0)` sets the SDK's request timeout on the same
copy. When Baldur is switched off (the [kill
switch](concepts/oss/system-control.md), for one), a wrapped call is tried once
on each endpoint with no retries at all, because the SDK's own stay off on the
copy.

If you would rather decorate the function, `@baldur.protected("openai_chat",
retry=True)` around a raw SDK call coordinates on `"openai_chat"` the same way
and classifies the SDK's errors the same way below — but the SDK's own retries
then still run inside each attempt, before Baldur sees the error.

Whether that holds for your deployment comes down to storage, classification,
and the provider's own hint.

**Shared means shared storage.** Out of the box Baldur runs on an in-memory
backend, and an in-memory cooldown is shared with nobody — it is still one
process learning alone. Point it at Redis and the same code starts
coordinating across the fleet. See [storage
backends](concepts/foundations/storage-backends.md) for the trade-offs.

**Classification reads the provider's answer, not its wording.** An error
raised by one of the three SDKs is classified by the HTTP status it carries,
plus the error code where a status alone is ambiguous:

| The provider answered | Read as | Retried on the same endpoint | Every worker waits | Counts against the breaker | Moves to the next endpoint |
|---|---|---|---|---|---|
| 429 whose code is `insufficient_quota`, a Gemini per-day quota, any 402, Anthropic's "credit balance" 400 | quota exhausted | no | no | yes | at once |
| any other 429 | rate limit | yes, after the wait | yes, at least as long as the provider asked | when retries run out, and each one toward the storm trip below | when the wait is longer than a call may sleep, or retries run out |
| 529, 503 | overload | yes, after the wait | yes, on Baldur's escalating wait (the provider's hint as a floor) | when retries run out, and each one toward the storm trip below | when the wait is longer than a call may sleep, or retries run out |
| 401, 403 | key refused | no | no | yes | at once |
| 408, 409, 500, 502, 504, other 5xx, a connection error or timeout | transient | yes, on the retry ladder | no | when retries run out | when retries run out |
| any other 4xx (400, 404, 413, 422, …) | request rejected | no | no | no | no — the error is raised to your code |

An exhausted quota is not waited out, because nothing changes until someone
tops up the account; a rejected request is neither retried, counted, nor sent
anywhere else, because the same request would be rejected anywhere. "A call may
sleep" is a minute by default, or the time the call has left when it runs under
a retry budget or a deadline. An exception
from any other library, and one the three SDKs raise on their own before sending
anything (an argument check), keeps the older rule: it counts as a rate limit
when its message or type name says so (`429`, `rate limit`, `ratelimit`, `too many
requests`, `throttle`, `quota exceeded`), and every such failure is retried and
counted. `google.api_core` errors are deliberately in that second group — every
Google Cloud client library raises them, not only LLM calls.

Rate limits and overloads also count, one by one, toward the breaker's
[rate-limit cascade](concepts/oss/circuit-breaker.md#when-a-dependency-answers-429),
the storm trip in the table. With the defaults, ten of them inside a minute open
the endpoint's breaker once they are at least a tenth of that minute's calls and
the minute holds at least twenty, counted per worker process. That happens even
while every call is still being answered on its retry, and from then on calls
move past that endpoint at once until its breaker recovers.

**The provider's hint is honored when it sends one.** Baldur reads
`retry-after-ms` (OpenAI and Anthropic send it), then `Retry-After` in both
forms the HTTP spec allows — a number of seconds, or a date — then the
`RetryInfo.retryDelay` Gemini puts in its error body instead of a header. The
provider's number is a floor under the cooldown, not a replacement for it:
Baldur never waits less than the provider asked, and its own escalating
cooldown can still be the longer of the two. The provider knows the earliest
legal time; it knows nothing about how many of you are still colliding. A
Gemini per-day quota is the exception: it is an exhausted quota even when the
body also names a short delay, since waiting a minute cannot restore a daily
quota.

Overload waits are counted in the same series as rate limits:
`baldur_rate_limit_429_total` carries them with `status_code="429"`.

## When waiting cannot save the call

Pass more clients of the same SDK, and a call that cannot be answered where it
is moves to the next one:

```python
client = baldur.llm.wrap(
    OpenAI(),
    fallbacks=[
        baldur.llm.Endpoint(
            OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY),
            model="openai/gpt-4o-mini",
        ),
    ],
)
```

The table above says when a call moves: at once on an exhausted quota or a
refused key, after the retries on a rate limit, an overload or a failure, and
at once when an endpoint's breaker is open or its wait is longer than a call may
sleep. A rejected request never moves. `Endpoint(model=...)` replaces the call's
`model=` on that endpoint, so a fallback provider gets the name it knows the
model by; only a call that names a model moves at all — listing models or
uploading a file runs on the primary alone. Every endpoint has its own name, so
its own wait and its own breaker; two endpoints of one wrap that would always
share a name (two keys on one host and model) are refused with a `ValueError`
when you wrap them, because the fallback would wait out the primary's wait and
stand behind its breaker — give one of them `Endpoint(name=...)`.

Each move past an endpoint that answered with an error writes one WARNING
`llm.endpoint_call_failed` naming the endpoint, the next one, and what the
provider said — an expired key reads differently from an outage. A move past an
open breaker or a long wait writes no such line; the breaker state and the
cooldown already say why. A call a later endpoint answered counts once in
`baldur_protect_fallback_total` under the first endpoint's name.

When no endpoint answers, the call raises `baldur.LLMUnavailableError`, chained
to the last endpoint's error, with `attempts` listing each endpoint and why it
did not answer. It is not retried by an enclosing retry stage: every endpoint
already ran its own.

The wrapped object answers like the client but is not an instance of the SDK's
class: a library that type-checks the client it is given (an agent framework's
model adapter, for one) needs the raw client and gets none of this.
A helper that makes its request only after it returns — `messages.stream(...)`,
`chat.completions.stream(...)`, anything under `with_streaming_response` — runs
on the client you passed in, with the SDK's own retries and none of Baldur's;
`create(stream=True)` is covered up to the response headers, and a stream that
breaks halfway does not move.

## The job no endpoint could answer

A wrapped call that raises `LLMUnavailableError` still fails the job that made
it. Mark the job, and Baldur keeps it and runs it again later:

```python
@baldur.protected("summarize", replay=True)
def summarize(doc_id: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": load_document(doc_id)}],
    )
    return response.choices[0].message.content
```

`replay=True` implies `dlq=True`: a call that failed because no endpoint
answered is parked with its arguments, under the job's name. Once that name's
breaker opens, later calls are rejected without calling the provider and are
parked too. When the provider is back, the recovery sweep re-runs every parked
call of both kinds, with the same arguments, under the same retry, breaker,
timeout and idempotency settings as the decorator — without capture or the
fallback, so a replay that fails again adds no second entry. The sweep starts
when the job's breaker closes, or when the recovery trial finds the provider
answering: about once a minute it re-runs one parked job of the name — less
often while that keeps failing, nine minutes apart at most — so jobs come back
after an outage too short to open the breaker, or one that ended with no later
job to close it. A trial that finds the provider still down costs the job none
of its replay attempts. Arguments up to 256 KiB are kept for such a job by
default, so a prompt or a document is not cut short.

Everything else parked under the same name — a rejected request, an error in
the job's own code, a job-level `timeout=` whose work may still be running — is
left for you to replay from the console. None of it is replayed automatically
unless you map its failure type to the job's name
(`BALDUR_REPLAY_AUTOMATION_SERVICE_FAILURE_TYPE_MAP`).

The decorator refuses, when your module is imported rather than when the job
fails, a job it could not re-run exactly: every parameter must be passable by
keyword and be a `str`, `int`, `float`, `bool` or `None` (or an `Optional` of
one); no parameter may be named like something the DLQ redacts (`max_tokens`,
`author_id`, anything with `token`, `secret`, `auth` in it); and the name must
not already be replayed by another function or a handler you registered. A
`retry=RetryPolicyConfig(domain=...)` naming anything but the job is refused
too: its failures would be parked under that domain, where the job's replay
never looks.

A replay runs the whole job again, so the job must be safe to run twice —
the same contract as `retry=`, and `idempotency_key=` is honored on replay.
Do not also let your task queue retry a `replay=True` task (`autoretry_for`,
`self.retry`): each queue attempt that fails parks its own copy, and each copy
is replayed.

Where the jobs do **not** come back on their own:

- **A worker that never imports the job.** The sweep re-runs the function the
  decorator registered in the worker's own process. A Celery worker that does
  not import the module defining the job (through its task modules or Celery's
  `include`) has nothing to run it with: the parked jobs stay for the console,
  and the sweep logs `replay_service.circuit_close_replay_blocked` with
  `block_reason=no_replay_handler_registered`.
- **No Celery worker on the `dlq_processing` queue.** The recovery sweep and
  the recovery trial run as Celery tasks on that queue, so a worker has to
  consume it (`celery -A your_app worker -Q dlq_processing`, or a `-Q` list
  that names it). Without one the parked jobs stay parked, and the console
  replays each one with a click.
- **Separate processes on the in-memory backend.** A job parked in one process
  is invisible to the sweep in another. Across processes the parked jobs need
  Redis or a SQL store — as the shared cooldown needs Redis.
- **Open-circuit capture switched off.** With
  `BALDUR_DLQ_OPEN_CIRCUIT_CAPTURE_ENABLED=false`, a call the open breaker
  rejects is not parked.

A recovery sweep bounds each replay by the time its task has left, and passes
that bound to the SDK as the request timeout, so one slow generation cannot run
the sweep into its time limit; a replay cut short stays parked for the next
pass. A `google-genai` call takes no per-request timeout, so a Gemini replay is
bounded only between attempts.

To watch it end to end on your machine, against the real `openai` SDK and a
local fake provider — a rate limit, then an outage, then the replay:

```bash
pip install "baldur-framework[celery]" openai
python -m baldur.scripts.demo_llm_outage
```

## What a returned 429 gets, and what it does not

A 429 reaches Baldur in one of two shapes: an exception the client raises
(`RateLimitError` from the OpenAI SDK, `httpx.HTTPStatusError` after
`raise_for_status()`), or a response object the client hands back with
`status_code == 429` — `requests` or `httpx` without `raise_for_status()`.

Both shapes install the shared cooldown, except a raised 429 the table above
reads as an exhausted quota. Where a raised exception is classified as above, a
returned response is classified by its status code (`429` by default;
`BALDUR_MIDDLEWARE_RATE_LIMIT_CODES` is the list). Either way the cooldown is
written, and the rest of the fleet waits on it.

The difference is the retry. A raised 429 is a failure, so the retry ladder
runs and, unless it is an exhausted quota, the call is tried again once the
cooldown has passed. A returned 429
is a value, so the retry stage hands it back to your code as the result:
coordinated, but not retried. If you want the retry as well, make the client
raise:

```python
@baldur.protected("openai_chat", retry=True)
def ask(prompt: str) -> str:
    response = httpx.post(...)
    response.raise_for_status()   # now the 429 is a failure the ladder retries
    return response.json()
```

For a call you do not want under `@baldur.protected` at all, the coordinator
can be driven directly:

```python
from baldur.services.rate_limit_coordinator import RateLimitCoordinator

coordinator = RateLimitCoordinator.get_instance()

@coordinator.rate_limit_aware("openai_chat")
def ask(prompt: str):
    return httpx.post(...)          # inspected for a 429 on the way back
```

That decorator waits out an active cooldown before calling, reads the returned
response (or the raised error) for a 429, and reports it. It does not retry
either. If the remaining cooldown is longer than it is willing to sleep, it
raises instead of calling — a decorator cannot return "nothing," so refusing is
its only honest option.

## What this is not

It is not a fleet-wide request quota. Sharing a *cooldown* is not the same as
sharing a *counter*, and Baldur does not pretend to enforce a hard total across
your workers on the request path — that belongs at a gateway that sees all the
traffic in one place. [Rate limiting in
Baldur](concepts/foundations/rate-limiting.md) is the honest map of which
mechanism counts per instance and which counts across the fleet.

It is not a gateway either: it does not translate between providers' request
formats (an endpoint's fallbacks speak the same SDK), hold your keys, or track
spend.

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
