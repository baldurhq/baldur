"""
Cell Context — global cell_id propagation via ContextVar.

Set by CellTaggingMiddleware, so that cell_id stays reachable outside the
middleware chain too (service layer, Celery publish time, etc.).

Existing patterns for reference:
- context/actor_context.py: the _current_actor ContextVar
- context/causation_context.py: the _current_causation ContextVar
- scaling/deadline_context.py: the _request_deadline ContextVar
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager

_current_cell_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "baldur_cell_id", default=None
)


def get_current_cell_id() -> str | None:
    """Return the cell_id of the current context."""
    return _current_cell_id.get()


def set_cell_id(cell_id: str) -> contextvars.Token[str | None]:
    """Set the cell_id. The returned token is required to restore it."""
    return _current_cell_id.set(cell_id)


@contextmanager
def cell_scope(cell_id: str) -> Generator[str, None, None]:
    """
    Context manager for cell_id.

    Example:
        with cell_scope("cell-3"):
            # inside this block get_current_cell_id() == "cell-3"
            task.delay(...)  # before_task_publish propagates cell_id
    """
    token = _current_cell_id.set(cell_id)
    try:
        yield cell_id
    finally:
        _current_cell_id.reset(token)
