---
title: Why I built Baldur
description: >-
  The question that started it, what the existing libraries did not answer, and
  four decisions behind the project — including the number I took off this site.
---

# Why I built Baldur

## It started as a question, not an outage

I was building a shopping app in Django. An ordinary CRUD project — products,
cart, checkout. Somewhere around the payment step I asked myself something I
could not answer:

**What happens if the payment gateway fails right here?**

Not "what if it is slow". What if the call goes out, the charge maybe lands,
and the request dies in the middle of it.

I want to be exact about this, because it is the part people usually inflate: I
have never lost a payment in production. I have never run this at production
scale at all. It was a side project and nobody was paying me. I just could not
stop looking at the hole once I had seen it.

So I wrote a circuit breaker by hand.

## Then I found out it already existed

Some time later I discovered `pybreaker` and `tenacity`. That was a deflating
afternoon.

But reading them clarified something. A retry library answers *should I try
again?* A circuit breaker answers *should I call at all?* Both are good answers
to their own question. Neither answers the one that had bothered me in the
first place:

**When the breaker is open and the call never happens, where does that
customer's order go?**

In most codebases the honest answer is: into a log line, and then nowhere. The
outage ends, the dashboards go green, and the work that failed during it is
simply gone. Recovering it means grepping logs and reconstructing orders by
hand, if it can be done at all.

That gap is the reason there is a project here rather than a `pip install
tenacity` in my own app.

## What I actually built

One decorator that composes the patterns, and a place for the work that still
fails at the end:

```python
import baldur


@baldur.protected("charge-customer", retry=True, dlq=True)
def charge(order_id: str) -> dict:
    return payment_gateway.charge(order_id)
```

None of the individual patterns are novel — circuit breaker, retry, fallback,
and dead-letter queues are all textbook. What is not textbook is having them
wired to each other: the breaker knows what the retry did, the fallback covers
both the exhausted retry and the open breaker, and the work that survives all
of that lands somewhere durable instead of evaporating. Then it replays when
the dependency comes back.

## Four decisions I would defend

### The dead-letter queue is free

It did not start that way. Capture originally lived in the paid tier, and in
July I moved it — capture, browsing, and single-entry retry, resolve, and
force-redrive — into the open-source core.

The reasoning is that not losing work is the entire premise of the project. If
the premise is behind a license key, the free tier is a demo of a problem
rather than a solution to it. What stays paid is *operating* the queue at
scale: batch replay from the console, archive and purge retention, a
disk-durable outbox, compressed-summary overflow.

### A decorator, not a sidecar

A service mesh retries HTTP between services, and it does that well. It cannot
re-queue a Celery task, return a cached price instead of an error, or know that
this particular failure belongs to order 12345. Those decisions need the
arguments in hand, which means running inside the process.

If you already run a mesh, this is not a replacement for it. It handles the
layer the mesh cannot see.

### It has to work with nothing installed

The default backend is in-memory. No Redis, no Docker, no environment
variables. Point it at Redis when you need workers to share state — which in
production you will — but the first run has to work on a laptop with nothing
else running.

A reliability tool that requires infrastructure before you can try it is a tool
you evaluate on a Friday afternoon and never open again.

### I took a number off this site

The [resource budget](concepts/foundations/resource-budget.md) page used to
carry a saturation-knee overhead figure. It is not there any more, and the page
says why.

Two things happened to it. A change to the circuit breaker removed a state
write that ran on every successful request — and that write turned out to be
most of what the original measurement was measuring. Then the re-measurement
that established this ran on a host that was not quiet enough, so its own
comparison sat inside its own noise. Evidence against the old number, nothing
licensed to replace it.

So the page now says the figure is withdrawn pending re-measurement. I would
rather publish nothing there than a number I have evidence against, and I would
rather you know that this is how numbers are handled here than have you find
out later.

The one cost figure I do quote is **~39 µs per protected call** on the default
chain, in memory, with no network in the path.

## What this is not

- **It is not battle-tested by a war story.** I have no production incident to
  tell you about. What I can point at is the test suite, the architecture gates
  that run on every commit, and the [measured
  envelope](concepts/foundations/resource-budget.md) with its methodology.
- **It is early access.** The API surface is stable, but a minor version can
  still carry a breaking change, with a changelog entry.
- **It runs in a single region.** Redis Sentinel for high availability is a
  PRO feature; anything wider than one region is out of scope.
- **It is not a hosted service.** It runs inside your process, makes no calls
  home, and sends no telemetry.

## Where to go next

- [What is Baldur?](what-is-baldur.md) — the product orientation, without the
  autobiography
- [Getting Started](getting-started/index.md) — protect your first call
- [How Baldur compares to tenacity](comparison/tenacity.md) — including when
  tenacity is the better choice
- [Pricing](pricing.md) — what is free and what is not
