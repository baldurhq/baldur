"""
``baldur init-ai`` - write the AI-assistant instruction files that teach the
``@baldur.protected("name")`` idiom.

Two files are written by default:

- ``AGENTS.md`` carries the guidance body. It is read natively by OpenAI Codex,
  Cursor, and GitHub Copilot.
- ``CLAUDE.md`` is a one-line ``@AGENTS.md`` import for Claude Code (which reads
  ``CLAUDE.md``, not ``AGENTS.md``), so both toolchains share ``AGENTS.md`` as
  the single source of truth with no content duplication.

pip-installed package files do not auto-activate in a user's repo, and
assistants do not proactively read installed dependencies. Writing the
instruction files into the user's repo root shifts the assistant's default
reach to ``@baldur.protected`` instead of a hand-rolled circuit breaker.

Each guidance body is a single module-level constant (single source of truth).
On an existing file the command refreshes only its marker-delimited block
(``<!-- baldur:start -->`` ... ``<!-- baldur:end -->``), preserving the rest;
it refuses to touch a non-marker file without ``--force``. When either target
is a non-marker file and ``--force`` is absent, the whole command refuses
before writing anything (atomic) so it never ships a half-initialized repo.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer

MARKER_START = "<!-- baldur:start -->"
MARKER_END = "<!-- baldur:end -->"

# Single source of truth for the assistant guidance. Kept marker-free here;
# `_render_block()` wraps it so existing managed blocks can be refreshed in
# place. The docs page (getting-started/ai-assistants.md) shows only a short
# excerpt and points back to this command, so there is one place to edit.
AGENTS_GUIDANCE = """\
## Baldur — resilience for this project

This project uses **Baldur** for resilience: circuit breaker, retry, fallback,
and dead-letter queue. When you make an external or otherwise-unreliable call
resilient, use Baldur's decorator instead of hand-rolling a circuit breaker, a
retry loop, or a try/except fallback.

### The one idiom

Wrap the call site in `@baldur.protected("name")`:

```python
import baldur


@baldur.protected("charge-customer")
def charge(order_id: str) -> dict:
    return payment_gateway.charge(order_id)
```

`@baldur.protected("name")` composes a circuit breaker, retry, and fallback into
one pipeline. `name` is a stable identifier for that dependency. For an inline
(non-decorator) call, use `baldur.protect("name", fn)`.

### Mental model

- **Call site** — `@baldur.protected("name")` (decorator) or
  `baldur.protect("name", fn)` (inline). This is where almost all usage lives.
- **Startup** — call `baldur.init()` once at process start. The Django, FastAPI,
  and Flask adapters do this for you; a plain script calls it explicitly.
- **Config** — `BALDUR_*` environment variables. Zero-config runs on an
  in-memory backend (no Redis). Set `BALDUR_REDIS_URL=...` (and
  `pip install baldur-framework[redis]`) so circuit-breaker state is shared
  across workers.
- **Tuning** — pass keywords to the decorator: `fallback=` (a zero-argument
  callable returning a default value), `retry=`, `circuit_breaker=`, `timeout=`,
  `dlq=True` (route final failures to a dead-letter queue), `idempotency_key=`
  (dedup so a retried or re-submitted call runs its side effect once). Defaults
  are safe; override only when needed.

### Retry re-runs your function — so guard side effects

`retry=` calls the function again on a transient failure. That is safe for a
read or a naturally-repeatable call, but **re-executing a non-idempotent side
effect** — charging a card, sending an email, creating an order — can double it.
When you make such a call resilient, also pass `idempotency_key=` so a duplicate
execution of the same logical request is blocked:

```python
@baldur.protected("charge-customer", retry=True, idempotency_key="order_id")
def charge(order_id: str) -> dict:
    return payment_gateway.charge(order_id)
```

The key is a field name on the call (e.g. `"order_id"`), or a
`Callable` for a composite key.

### Rules for assistants

- Do **not** add a new circuit-breaker / retry library or hand-roll one — Baldur
  already provides it. Wrap the call in `@baldur.protected(...)` instead.
- Reuse one stable `name` per protected dependency; do not generate random names.
- When you add `retry=` to a call with a non-idempotent side effect, add
  `idempotency_key=` too. **Propose the key and confirm it with the human** when
  it is not unambiguous from the signature — never silently guess. A wrong key
  is a silent correctness bug (keying on `user_id` would block a customer's
  legitimate second order).
- The importable package is `baldur` (`import baldur`); the PyPI distribution is
  `baldur-framework`.
- More: https://baldur.sh/ — run `baldur init-ai` to refresh this file.
"""

# Body of the managed block in CLAUDE.md: a single import that points Claude
# Code at AGENTS.md. Claude Code reads CLAUDE.md (not AGENTS.md), strips the
# HTML-comment markers, and expands ``@AGENTS.md`` relative to CLAUDE.md's
# location, so the sibling AGENTS.md stays the single source of truth.
CLAUDE_GUIDANCE = "@AGENTS.md\n"

__all__ = [
    "init_ai",
    "AGENTS_GUIDANCE",
    "CLAUDE_GUIDANCE",
    "MARKER_START",
    "MARKER_END",
]


class _PlanAction(str, Enum):
    """What ``init-ai`` will do to one target file.

    A module-private control-flow token that crosses the ``_plan`` -> ``_apply``
    boundary. Deliberately absent from ``__all__``: it is never serialized and
    has no display coupling (``_apply`` produces the user-facing status string).
    """

    WRITE = "write"  # no file exists -> write the fresh managed block
    REFRESH = "refresh"  # marker file whose rendered block differs -> swap it
    SKIP = "skip"  # marker file already byte-identical -> true no-op
    APPEND = "append"  # non-marker file + --force -> append the block
    REFUSE = "refuse"  # non-marker file, no --force -> refuse the whole command


def _render_block(body: str) -> str:
    """Return ``body`` wrapped in the managed block, markers included.

    The returned string is newline-terminated and the same shape for either
    guidance body (``AGENTS_GUIDANCE`` or ``CLAUDE_GUIDANCE``).
    """
    return f"{MARKER_START}\n{body}{MARKER_END}\n"


def _refresh_marker_block(existing: str, block: str) -> str:
    """Replace the existing marker block in ``existing`` with ``block``.

    The region from ``MARKER_START`` through ``MARKER_END`` (inclusive) is
    swapped; everything outside the markers is preserved verbatim.
    """
    start = existing.index(MARKER_START)
    end = existing.index(MARKER_END) + len(MARKER_END)
    # Drop a single trailing newline after the old end marker so the
    # block's own trailing newline does not accumulate on each refresh.
    tail = existing[end:]
    if tail.startswith("\n"):
        tail = tail[1:]
    return existing[:start] + block + tail


def _plan(target: Path, block: str, force: bool) -> _PlanAction:
    """Classify what ``init-ai`` would do to ``target`` without mutating it.

    ``block`` is the rendered managed block for this target; it is needed to
    compute the would-be refresh and compare it against the current content,
    which is the ``SKIP``-vs-``REFRESH`` discriminator (a re-run on an
    already-current marker file is a true no-op, not a rewrite).
    """
    if not target.exists():
        return _PlanAction.WRITE

    existing = target.read_text(encoding="utf-8")
    if MARKER_START in existing and MARKER_END in existing:
        refreshed = _refresh_marker_block(existing, block)
        return _PlanAction.SKIP if refreshed == existing else _PlanAction.REFRESH

    return _PlanAction.APPEND if force else _PlanAction.REFUSE


def _apply(target: Path, block: str, action: _PlanAction) -> str:
    """Perform ``action`` on ``target`` and return a user-facing status line.

    A no-op for ``SKIP``. ``REFUSE`` is resolved upstream (the command refuses
    atomically before any ``_apply`` runs), so it never reaches here.
    """
    if action is _PlanAction.WRITE:
        target.write_text(block, encoding="utf-8")
        return f"Wrote {target}"

    if action is _PlanAction.REFRESH:
        existing = target.read_text(encoding="utf-8")
        target.write_text(_refresh_marker_block(existing, block), encoding="utf-8")
        return f"Refreshed Baldur block in {target}"

    if action is _PlanAction.SKIP:
        return f"{target} already up to date"

    if action is _PlanAction.APPEND:
        existing = target.read_text(encoding="utf-8")
        separator = "" if existing.endswith("\n") else "\n"
        target.write_text(f"{existing}{separator}\n{block}", encoding="utf-8")
        return f"Appended Baldur block to {target}"

    raise ValueError(f"_apply received an unresolved action: {action}")


def init_ai(
    dir: str | None = typer.Option(
        None,
        "--dir",
        "-d",
        help="Target directory for AGENTS.md/CLAUDE.md (default: current directory).",
        metavar="PATH",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite an existing AGENTS.md or CLAUDE.md that has no Baldur marker block.",
    ),
) -> None:
    """Write AGENTS.md + CLAUDE.md teaching the @baldur.protected idiom to AI assistants.

    AGENTS.md carries the guidance (read by OpenAI Codex, Cursor, and GitHub
    Copilot); CLAUDE.md is a single @AGENTS.md import for Claude Code. Both are
    refreshed in place when they already carry a Baldur marker block; a
    non-marker file needs --force, and the gate is atomic across both targets.
    """
    target_dir = Path(dir) if dir is not None else Path.cwd()
    targets = [
        (target_dir / "AGENTS.md", _render_block(AGENTS_GUIDANCE)),
        (target_dir / "CLAUDE.md", _render_block(CLAUDE_GUIDANCE)),
    ]

    # Phase 1 — plan every target without touching the filesystem (D1/D4).
    plans = [(target, block, _plan(target, block, force)) for target, block in targets]

    # Atomic refuse: if any target is a blocked non-marker file, name every
    # blocked file and write nothing — never a half-initialized repo (D4).
    refused = [target for target, _, action in plans if action is _PlanAction.REFUSE]
    if refused:
        for target in refused:
            typer.secho(
                f"{target} exists and has no Baldur marker block. "
                "Re-run with --force to append one (existing content is preserved).",
                fg=typer.colors.RED,
                err=True,
            )
        raise typer.Exit(code=1)

    # Phase 2 — apply each target, printing its status as _apply returns so a
    # partial-write I/O error still leaves the prior file's success on screen.
    for target, block, action in plans:
        typer.secho(_apply(target, block, action), fg=typer.colors.GREEN)
