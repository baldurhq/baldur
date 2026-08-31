---
title: I read 99 hand-written circuit breakers
description: >-
  A survey of hand-written circuit breakers in public Python code: what they get
  wrong, why nobody notices, and why asking an AI to write one does not fix it.
---

# I read 99 hand-written circuit breakers

I wanted to settle an argument I was having with myself.

Python developers use retries constantly and circuit breakers rarely. The usual
explanation is that they have not heard of the pattern. The other explanation is
that they do not need it. I could not tell which, so I went and counted.

Neither turned out to be true. People know the pattern well enough to implement
it from scratch, at roughly the same rate as they import a library for it. What
they do not have is any way to find out whether the one they wrote works.

## What I did

I searched public Python code for `class CircuitBreaker`, took the top 100
results, and downloaded the files. One of them turned out to be a wrapper around
an existing library rather than an implementation, so it came out. That leaves
**99 hand-written circuit breakers**, median 80 lines of class body.

Then I checked each one against six things a circuit breaker has to get right.

## What I found

| | |
|---|---:|
| Breaker state lives in one process only | **98 / 99** |
| No time window on failure counting | 88 / 99 |
| No timeout applied to the call being protected | 84 / 99 |
| Has a half-open state, but no limit on how many calls enter it | 56 / 82 |
| Mutates state without a lock (synchronous code only) | 51 / 69 |
| No half-open state at all | 17 / 99 |
| **Correct on all six** | **0 / 99** |

Most files had four of the six. The best one in the sample had one, and that one
was still per-process.

## What those actually mean

The table is jargon. Here is the same thing in the terms that decide whether it
matters to you.

**State lives in one process.** You run four workers, so you have four different
opinions about whether the API is down. Worker 1 stops calling it. Workers 2, 3,
and 4 carry on. Then you deploy, and all four forget.

**No time window.** Five failures from yesterday can still be sitting in the
counter today. Or the opposite: the counter resets on every success, so a
dependency failing half the time never trips anything, because it never fails
five times in a row.

**No timeout on the protected call.** This is the one that surprises people. If
the dependency dies, you get an exception and the breaker counts it. If the
dependency gets *slow* instead, you get no exception at all — so nothing is
counted, nothing opens, and the breaker sits there closed while your worker pool
drains into a service that is answering in ninety seconds. The failure mode that
takes a site down is usually slow, not dead.

**No limit on half-open.** The dependency comes back. Every request that was
waiting goes at it simultaneously, and it goes down a second time.

None of these produce an error message that says the breaker is at fault.

## Why nobody notices

Ten of the 99 emit a metric. Seventy-five write a log line somewhere, and
twenty-two do neither.

A log line in one worker's stdout is not an operational signal. There is no
dashboard anywhere that says whether the breaker is blocking right now, and no
alert that fires when it should have opened and did not.

So consider what each of those defects looks like from the outside:

- No timeout, dependency slow → *"the site got slow"* → you blame the dependency
- Per-process state → *"the outage lasted longer than it should have"* → you
  blame the dependency
- No time window → *"it blocked when nothing was wrong"* → you blame the breaker
  once, add a bigger threshold, and move on
- Unlimited half-open → *"it came back and then died again"* → you blame the
  dependency

**A circuit breaker that fails produces the same symptom as the failure it was
supposed to contain.** There is no path from the symptom back to the cause,
which is why 99 out of 99 can be wrong and nobody is complaining about it.

## The part I did not expect

I assumed the older code would be worse. It is not, meaningfully.

Of the repositories I could date, 28 were created in 2025 and 45 so far in 2026
— about 86% of the sample comes from the last two years, which is to say from
the period when most of us started writing code with an assistant open. The
defect rate in that code is the same as in the code from before it.

Repository popularity does not change it either. The twenty-five repositories
with more than a thousand stars average 3.88 defects out of five. The whole
sample averages 3.98.

I think the reason is that the dominant defect is not a coding mistake. Ask any
competent assistant for a circuit breaker in Python and you will get a correct
in-process circuit breaker. The problem is that "in-process" is wrong for your
deployment, and nothing in the request said how many workers you run, how often
you deploy, or whether the thing you are calling is rate-limited. Adding a Redis
dependency is an architecture decision, and it will not make one on your behalf.
The 98-out-of-99 defect is a deployment-shape problem wearing a code costume.

## What I am not claiming

- **This is a sample of public code, ranked by relevance.** It is not a random
  sample and it is not "Python". Read every number above as "of the top 99
  results", not as a population estimate.
- **I did not measure harm.** Not one of these is known to have caused an
  incident. At four workers, with short outages and infrequent deploys, a
  per-process breaker may well be fine forever. The cost concentrates where
  calls are expensive, rate-limited, and slow to time out — which is where a lot
  of this code now lives; 57 of the 99 sit in LLM or agent infrastructure.
- **I am not naming repositories.** The ranking favours popular projects, so
  picking on individual ones would be both unkind and statistically weak. Some
  of these are from teams with far more resources than mine.
- **My own tooling was wrong twice** before it was right. It first counted a
  library wrapper as a hand-written breaker, and it first matched
  `CircuitBreakerConfig` instead of the breaker class. Both were caught by
  reading the code rather than trusting the grep, which is the same failure mode
  this whole article is about.

## The question this leaves

If you have a circuit breaker in your codebase, you can check five of the six in
about a minute of reading. The sixth is harder, and it is the one that matters
most:

**When your dependency last went down, did your breaker stop the calls — and how
would you know either way?**

If the honest answer is that nothing anywhere would have told you, that is the
gap. It is not that the pattern is missing. It is that nobody can see it working.

## Where to go next

- [Circuit breaker for Python](concepts/oss/circuit-breaker.md) — the states, the
  thresholds, and what the shared-state version needs
- [Combining circuit breaker, retry, and fallback](concepts/foundations/composition.md)
  — why these interact, and what breaks when they do not know about each other
- [Storage backends](concepts/foundations/storage-backends.md) — in-memory
  versus Redis, and what changes when workers share state
- [What is Baldur?](what-is-baldur.md) — what I build, if you want the context
