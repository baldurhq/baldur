# Using Baldur with AI assistants

If you build with an AI coding assistant (Claude Code, Cursor, GitHub Copilot,
OpenAI Codex), you can teach it to reach for Baldur's one-line idiom instead of
hand-rolling a circuit breaker every time it makes a call resilient.

## `baldur init-ai`

Run this once in your repository root:

```bash
baldur init-ai
```

It writes two instruction files into your repo root:

- **`AGENTS.md`** — the guidance itself, read natively by OpenAI Codex, Cursor,
  and GitHub Copilot (Copilot since August 2025).
- **`CLAUDE.md`** — a one-line `@AGENTS.md` import for Claude Code, which reads
  `CLAUDE.md` rather than `AGENTS.md`. The import keeps `AGENTS.md` the single
  source of truth, so both toolchains share one guidance file with no
  duplication.

Together they tell every assistant how this project uses Baldur. The core of
that guidance is one rule:

> When you make an external or unreliable call resilient, wrap it in
> `@baldur.protected("name")` instead of hand-rolling a circuit breaker, retry
> loop, or try/except fallback.

```python
import baldur


@baldur.protected("charge-customer")
def charge(order_id: str) -> dict:
    return payment_gateway.charge(order_id)
```

The generated file also gives the assistant the small mental model it needs:
the call site (`@baldur.protected` / `baldur.protect`), startup (`baldur.init()`,
done for you by the framework adapters), configuration (`BALDUR_*` env vars, plus
Redis for shared state across workers), and tuning keywords (`fallback=`,
`retry=`, `dlq=True`, `idempotency_key=`, …).

It also teaches the one correctness rule that is easy to miss: **`retry=`
re-runs your function**, so a non-idempotent side effect (charging a card,
sending a message) should also carry `idempotency_key=` to dedup a duplicate
execution. Because the right key is domain knowledge — and a wrong key is a
silent bug (keying on `user_id` blocks a customer's legitimate second order) —
the guidance tells the assistant to *propose the key and confirm it with you*
rather than guess.

## Options

- `--dir PATH` — write the files into another directory (default: current
  directory).
- `--force` — when an `AGENTS.md` *or* `CLAUDE.md` already exists **without** a
  Baldur block (a hand-authored `CLAUDE.md` is common), append the managed block
  to it (your existing content is preserved). The gate is **atomic**: if either
  file is a non-marker file and you omit `--force`, the command refuses the
  whole run and writes nothing, naming the blocked file(s). Re-run once with
  `--force` to write the fresh file and append the block to the existing one.

The command is idempotent. The Baldur guidance in each file is wrapped in
`<!-- baldur:start -->` / `<!-- baldur:end -->` markers, so re-running
`baldur init-ai` refreshes those blocks in place and leaves the rest of your
files untouched. A re-run on already-current files is a true no-op — nothing is
rewritten. Claude Code strips the comment markers and expands the `@AGENTS.md`
import before loading context.

The first time Claude Code loads a `CLAUDE.md` with an `@AGENTS.md` import, it
shows a one-time, per-project approval dialog for the imported file. Approve it
once and the guidance loads on every session.

Commit both `AGENTS.md` and `CLAUDE.md` so your teammates and CI share the same
project context.
