---
title: Using tenacity with a circuit breaker in Python
description: >-
  tenacity retries a failed call. This page is about where retry alone stops
  being enough in a Python service, and how to add a circuit breaker without
  giving up tenacity.
---

# Using tenacity with a circuit breaker in Python

> tenacity is the best-known way to retry a call in Python. This page is about where retry alone
> stops being enough — and what to do about it without giving up tenacity.

## What tenacity does well

tenacity does one thing and does it thoroughly. You compose a stop condition, a wait strategy, and
a predicate, and you get a retry loop that covers the cases people get wrong by hand — exponential
backoff with jitter, retrying only on the exception types you meant, a bounded number of attempts,
sync and async call styles. It has no required runtime dependencies, so adding it to a project
costs almost nothing.

If a call fails now and then and succeeds a moment later, that is exactly the failure tenacity is
built for, and a retry decorator is the whole answer:

```python
from tenacity import retry, stop_after_attempt, wait_exponential


@retry(stop=stop_after_attempt(3), wait=wait_exponential())
def fetch_quote(symbol: str) -> dict:
    return quotes_api.get(symbol)
```

**If retry is all you need, stop reading here.** Adding a framework to a project that needs one
decorator is a bad trade, and the rest of this page will not argue otherwise.

## The failure retry alone cannot fix

Retry assumes the dependency is coming back. The failure it cannot help with is the one where the
dependency stays down.

When that happens, every caller keeps running its full attempt budget against a service that is not
answering. Your own latency now includes the entire backoff schedule before you return an error,
your workers sit in `sleep` instead of serving other requests, and the failing dependency receives
*more* traffic than it did while healthy — from every instance you run, at once. Tuning the backoff
does not fix this, because the problem is not how you wait. It is that you are still asking.

That failure needs a different decision — stop calling for a while — and an answer for the work
that will not succeed on this attempt:

- A **circuit breaker** notices the dependency is down and fails fast, so callers stop paying for
  attempts that cannot work. Later it lets a few trial calls through to find out when it is back.
- A **fallback** serves a safe answer to the caller instead of an error.
- A **dead-letter queue** keeps the work that must not be dropped, so you can run it again after
  recovery rather than hear about it from a customer.

These are separate patterns, and the order they nest in changes what they do — retry belongs
*inside* the breaker, so an open breaker stops the retries before they run rather than after.
[Composing with `@baldur.protected`](../concepts/foundations/composition.md) covers why that order
is the way it is.

That composition is Baldur's job:

```python
import baldur


@baldur.protected(
    "quotes-api",
    retry=True,
    circuit_breaker=True,
    fallback=lambda: last_known_quote(),
)
def fetch_quote(symbol: str) -> dict:
    return quotes_api.get(symbol)
```

## Keep tenacity, add the rest

You do not have to choose between them. Baldur ships a tenacity bridge, and it comes at two depths.

**Observe first, change nothing.** One call at startup routes every `tenacity.Retrying` created
afterwards through Baldur's event stream, so retries that run out of attempts stop being invisible:

```python
from baldur.bridges.tenacity import instrument_tenacity

instrument_tenacity()
```

Be clear about what that does and does not buy you. It is observation only: it records that a retry
was exhausted. It does **not** record retry metrics, and it does not apply Baldur's retry budget or
rate-limit coordination. Your tenacity code behaves exactly as it did before.

**Then compose, keeping your retry configuration.** When you want a breaker, a fallback, and a
dead-letter queue around that same retry, hand your tenacity strategy to Baldur as a policy instead
of rewriting it:

```python
import baldur
from baldur.bridges.tenacity import TenacityBridgePolicy
from tenacity import stop_after_attempt, wait_exponential

policy = TenacityBridgePolicy(
    stop=stop_after_attempt(3),
    wait=wait_exponential(),
    domain="quotes-api",
)


@baldur.protected("quotes-api", retry=policy, circuit_breaker=True, dlq=True)
def fetch_quote(symbol: str) -> dict:
    return quotes_api.get(symbol)
```

The stop and wait strategies are still tenacity's. What changes is what surrounds them. On an
`async def` function the same policy works — Baldur converts the bridge to its async twin rather
than running a sync retry loop unawaited.

The bridge needs the extra: `pip install baldur-framework[tenacity]`.

## What Baldur costs you

**Dependencies.** tenacity has no required runtime dependencies. Baldur has several — settings,
structured logging, an HTTP client, a CLI. If your budget for a retry helper is "one pure-Python
module", Baldur does not fit inside it, and that is a real difference rather than a rounding error.

**Setup.** tenacity is one decorator and no initialization. Baldur expects `baldur.init()` to run
once at startup, so breaker state, storage, and metrics have somewhere to live. The framework
adapters do that for you on Django, FastAPI, Flask, and Celery — but wiring the adapter is still
a step tenacity does not have.

**Runtime cost.** Composing several patterns is not free: a protected call does work a bare retry
decorator does not. What it costs depends on your call profile more than on any single headline
number, so [What Baldur costs your service](../concepts/foundations/resource-budget.md) describes
how to measure it against your own workload instead of asking you to trust a benchmark taken on
ours.

**So use tenacity** for a script, a CLI, a one-off job, a library you ship to other people, or any
service where a dependency going down is somebody else's incident. The trade only starts paying
when it is yours.

## What about pybreaker?

pybreaker is the same comparison from the other side, and it is a solid library: a focused circuit
breaker with listeners for state changes, no required runtime dependencies, and — worth saying
plainly, because it is often assumed otherwise — **circuit state shared across processes** through
its `CircuitRedisStorage` backend. If you need a circuit breaker and nothing else, pybreaker does
that job.

The gap has the same shape as tenacity's. pybreaker breaks circuits; it does not retry, does not
fall back, and does not keep the work that failed. Its async support is Tornado's `call_async`
rather than native `async`/`await`. Run tenacity and pybreaker together and you have two of the
patterns — nesting them in an order that makes them cooperate, and deciding what happens to the
request that neither one saves, is the part left in your hands.

Baldur has no pybreaker bridge. Its breaker is its own, so moving over replaces pybreaker rather
than wrapping it.

## Which one should you use?

| What you need | Reach for |
| --- | --- |
| Retry a flaky call, nothing more | **tenacity** |
| Stop calling a dependency that is down, nothing more | **pybreaker** |
| Retry *and* breaker *and* fallback *and* somewhere for the failed work to go — composed in the right order, visible from one place | **Baldur** |

Baldur is the largest commitment of the three, and it should be — it answers a bigger question. If
the smaller answer covers your case, take the smaller answer.

## See also

- [Composing with `@baldur.protected`](../concepts/foundations/composition.md) — why the patterns nest in that order
- [Circuit Breaker](../concepts/oss/circuit-breaker.md) and [Retry](../concepts/oss/retry.md) — the two patterns this page compares against
- [DLQ + Replay](../concepts/foundations/dlq-replay.md) — where failed work goes
- [Getting Started](../getting-started/index.md) — a protected endpoint in five minutes
