# What Baldur costs your service

> Baldur runs inside your application process, so its cost lands on your bill, not ours. This page publishes what we measured, states plainly which numbers we refuse to publish and why, and ships a command that measures your deployment instead of asking you to trust ours.

## What is it?

Baldur is a library you import, not a sidecar you deploy next to your service. There is no
separate container to size, no agent process, no extra network hop. The flip side is that every
byte and every microsecond it uses comes out of your application's own budget, and "how much?"
is a fair thing to ask before you add it to a service you already operate.

This page is the answer, split into three kinds of cost that behave very differently:

- **Resident cost.** What a worker process holds while doing nothing: memory, threads, idle CPU.
  Paid once per worker, whether you serve one request an hour or a thousand a second.
- **Per-request cost.** What protecting a call adds to that call.
- **Per-entry storage cost.** What Baldur writes into Redis for each captured failure and each
  tracked request.

## The numbers

Every figure is a delta against the same application running without Baldur, measured on the
protocol described further down. Read the "What it means" column before quoting the number.

| Cost | Figure | Measured | What it means |
|---|---|---|---|
| Per protected call, in-memory | **~39 µs** | 2026-08-26 | What the decorator itself adds to one call, on the default chain — circuit breaker only, in-memory state, no network anywhere in the path. Measured in-process on the same developer workstation. What transfers is the order of magnitude, tens of microseconds; the exact figure is ours, not yours. Point Baldur at Redis and this stops being the dominant term — see the round trips below. |
| Per-request overhead, below saturation | **+1.1%** | 2026-07-14 | Server-side throughput cost of the full protected path, measured with headroom left on the host. Above saturation this stops being the right question; see the section on the saturation knee. |
| Dead-letter entry, stored | **~953 B** | 2026-05-14 | Redis bytes per captured failure, end to end through the real capture path. Compressed, so a very different payload shape moves it; backlog memory tracks `entries x 953 B`, with the per-entry figure itself drifting by up to about 180 B between runs. |
| Rate-limit tracker, per tracked request | **~124 B** | 2026-07-14 | Redis bytes per request inside the tracker's retention window. Rises slowly with tracker size, so it is measured at production-scale set sizes rather than small ones. |
| Resident cost per worker | *pending* | — | Measured so far only on an image that also had the PRO package installed, which is not what an open-source-only install runs. The clean-image measurement is in progress; this row lands when it does, rather than shipping a derived estimate. |

The last row is empty on purpose. We have a number for it, and it is not the number an
open-source user would pay, so it is not published as though it were.

## What we don't publish, and why

There is one class of number we can produce easily, that readers ask for constantly, and that we
deliberately keep off this page: **absolute throughput**. Requests per second, operations per
second, entries drained per second.

The reason is that those numbers do not transfer. Our measurements run on a single developer
workstation, and every absolute we have measured there turned out to be bound by that host
rather than by Baldur. Publishing "N requests per second" would tell you what our laptop does,
dressed up as a property of the framework, and you would have no way to tell the difference.

So the table above carries only quantities that survive a change of hardware: deltas, ratios,
and per-entry byte costs. Where a throughput-shaped question has a real answer, the transferable
form is a model rather than a measurement. Replay drain rate is the clearest example: the rate is
dominated by Redis round trips, roughly a dozen per entry, so `1 / (fixed cost + 12 x your RTT)`
predicts your deployment far better than any number from our host would. A figure measured on an
in-process store, with no network at all, is faster by more than an order of magnitude and
describes nothing you would run.

The protected request path has the same shape. A healthy request through the Django middleware
chain, with the breaker closed and warm, issues **nine Redis commands**: two to check whether the
breaker is open, four to record the success, and three for the health snapshot the middleware
refreshes. That count comes from a counter sitting where the socket would be, not from reading
the code, and it was re-counted on the current release. Nine is a property of the code path
rather than of our hardware, so `9 x your RTT` is the Redis wait each protected request adds in
your deployment. On a network-backed setup that wait, not the framework's own Python work, is
what the saturation-knee section below is about.

If you want an absolute for your own capacity planning, measure it on your own hardware. The
command at the bottom of this page is a start; a load test against your own service is the rest.

## Overhead at the saturation knee

The `+1.1%` in the table is measured with headroom on the host. Push the same service until it
saturates and the picture changes sharply, so here is that number with everything you need to
read it.

**At the saturation knee, the total framework cost is a 36.21% shift in the ceiling** — a single
synchronous worker's peak throughput drops by that much once Baldur is fully wired in. Most of the
cost is middleware plus initialization and background work rather than the protective decorator
itself, but the split between those two is not measured precisely enough to put a ratio on, so we
do not publish one.

Four qualifiers, all load-bearing:

1. **The posture is one synchronous worker.** The measurement runs a single sync worker
   (`-w 1`) with the application and Redis on the same host. That is the configuration where
   the effect is largest and cleanest to isolate, not the configuration most people deploy.
2. **The cost is I/O wait, and concurrency hides it.** What binds the ceiling is time spent
   waiting on Redis round trips. A single sync worker has nothing else to do while it waits, so
   the wait converts directly into lost throughput. Add workers, threads, or async and some of
   that wait overlaps with useful work. How much depends on your configuration, which is
   precisely why we cannot publish a corrected number for you.
3. **It does not transfer.** Faster storage, a different worker model, or a service whose own
   work dominates its request time will all read differently. Treat 36.21% as "what this costs
   in the worst reasonable case we could construct", not as a number to plug into a capacity
   plan.
4. **It is a conservative floor, not a ceiling.** In the comparison run, the database saturated
   on the bare reference side too, which flatters the bare arm. The real shift is at least this
   large.

The honest summary: below saturation the cost is around a percent, and at a single sync worker's
saturation knee it is roughly a third of the ceiling. Both are true, they answer different
questions, and quoting either one without saying which is which is how a real number turns into
a misleading one.

## How these numbers were measured

The setup, so you can judge the numbers rather than take them:

- **Host.** One six-core developer workstation, 16 GB of RAM, running the application, its
  database, and Redis together in containers. Not a production-shaped machine, which is exactly
  why the published set excludes absolutes.
- **Application.** A Django service under a synchronous worker, with the protected payment path
  as the workload.
- **Control arm.** The same application, same image, same configuration, with Baldur's
  middleware and startup hooks removed. Early runs of this comparison were wrong because the
  "framework-free" arm was still starting Baldur's background threads through a worker hook; the
  control now asserts positively that no framework thread exists before a number is accepted.
- **Repetition.** Three or more runs per arm, with the spread reported alongside the mean. A
  difference smaller than the observed spread is reported as "below the noise floor" rather than
  as a number.
- **In-process measurements.** The per-call figure and the round-trip count come from inside a
  single process rather than from a load test. The per-call cost is ten thousand timed calls
  after a warm-up, on a machine checked to be otherwise idle before the run, cross-checked
  against a second timing path that agreed with it to within about two percent on every run. The round-trip count
  comes from a stand-in sitting where the Redis socket would be: it keeps the client's real
  encoding work, answers with canned replies, opens no network connection, and counts the
  commands the real middleware chain issues for one request.
- **Version.** The per-request and per-entry figures were measured on the 1.1 line in July 2026;
  the saturation-knee figure was re-measured on the 1.6 line in August 2026. The per-call figure
  and the round-trip count were measured on the 1.8 line in August 2026. The dead-letter
  entry cost dates to May 2026 and describes the compressed encoding still in use.

We re-measure when something plausibly moves a figure, and the dates above are the record of
when that last happened. Re-measurement has moved published numbers before: the saturation-knee
figure read 34.58% until a defect in the control arm was fixed, at which point it went up. It
also confirms figures: the round-trip count was re-counted for the 1.8 release because a change
to how the breaker writes its mirrored state could have moved it. It had not, and we know that
because we counted rather than because we read the diff.

## Measure it on your own hardware

Specs differ, configurations differ more. Rather than ask you to map our workstation onto your
cluster, the framework ships a probe that measures the process it runs in:

```bash
python -m baldur.scripts.measure_footprint
```

It samples the process four times — bare interpreter, after the import, the moment
`baldur.init()` returns, and again once the process has settled — and prints the memory, thread,
and CPU delta between each pair, along with an echo of the configuration and host it ran on. It
generates no load and changes nothing about your setup except binding the admin server to an
ephemeral port.

Three things to know before you read its output:

- **Use the settled reading, not the one at startup.** `init()` returns while background threads
  are still starting, so the memory figure at that instant is a transient peak, measurably
  higher than what the process actually holds a few seconds later. The probe prints both and
  labels the peak; only the settled line is worth quoting.
- **It measures a plain process, not a web worker.** A Django or FastAPI worker also imports its
  URL configuration and everything the application itself pulls in, none of which a bare probe
  pays. Compare the probe against itself across configuration changes. Do not compare it against
  a table measured on a different stack.
- **Output after the boundary line is shutdown noise.** The probe ends with a
  `--- measurement complete ---` line. Anything the framework or an exporter logs after that is
  teardown work, not measured cost. If a trace exporter is configured with no collector
  listening, that teardown can take several seconds and print errors; set
  `BALDUR_OBSERVABILITY_PROFILE=local` if it gets in the way.

Reproducing the overhead percentages needs more than a probe: a load generator, plus a run of
your own application with Baldur removed as the control. That is a real project, and shipping a
half-working benchmark harness would do more harm than publishing the method. The method is the
section above.

## See also

- [OSS vs PRO: which tier do you need?](tier-model.md) — what you get for the cost on each tier
- [Storage backends: in-memory, Redis, SQL](storage-backends.md) — what Baldur stores, and where
- [DLQ + Replay](dlq-replay.md) — the feature behind the per-entry storage cost
- [Rate limiting in Baldur](rate-limiting.md) — the feature behind the per-request tracker cost
- [Environment Variables](../../reference/env-vars.md) — the knobs that move these numbers
