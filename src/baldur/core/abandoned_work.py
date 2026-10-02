"""Abandoned work — what a caller stopped waiting for but could not stop.

A timeout on a thread cannot stop the thread: ``Future.cancel()`` succeeds only
on work that never started, and running work keeps running after its caller
was freed — by its own timeout, or by an interruption of its wait (a Celery
soft time limit, a gevent timeout, ``KeyboardInterrupt``). A keyed call (an
idempotency key) must not release its key while such work may still produce
the side effect the key guards, and a replay must not hand its entry back
while the job it ran may still be running.

A **work scope** is that call's hold on what it abandoned. It is carried in a
``ContextVar`` — published once, then mutated in place, so a copied context
(a timeout worker, a thread-pool compartment worker, ``asyncio.to_thread``, a
task) still reaches the scope that opened it:

- :func:`open_work_scope` opens a scope for one call, chained to the enclosing
  call's scope.
- :func:`record_abandoned` (called by a site that stopped waiting on running
  work whose cancel failed) adds
  the running future to every scope in the chain that has not settled; the
  future's own end folds it into each scope's summary and drops it, so a scope
  retains only work still running.
- :func:`close_work_scope` (called when the call ends) closes the scope. With
  nothing running it settles at once; otherwise it keeps holding — work
  abandoned from inside abandoned work joins the hold — and settles exactly
  once, when the last running piece ends.

A piece is the call's **own** work when it was recorded with the very origin
object the scope was opened with (a keyed call's own timeout stage, whether its
timeout or an interruption ended the wait); every other piece only extends the
hold. A scope or a piece whose origin is ``None`` is never own work.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import structlog

from baldur.core.process_utils import fork_safe_lock

logger = structlog.get_logger()

__all__ = [
    "WorkScope",
    "WorkSummary",
    "close_work_scope",
    "current_work_scope",
    "open_work_scope",
    "record_abandoned",
]


@dataclass(frozen=True)
class WorkSummary:
    """How a scope's own abandoned work ended.

    Attributes:
        own_finished: At least one own piece was recorded and has ended.
        own_failed: An own piece was cancelled or raised.
    """

    own_finished: bool = False
    own_failed: bool = False

    @property
    def own_succeeded(self) -> bool:
        """Own work was recorded and every own piece returned normally."""
        return self.own_finished and not self.own_failed


_current_scope: ContextVar[WorkScope | None] = ContextVar(
    "baldur_abandoned_work_scope", default=None
)


class WorkScope:
    """One call's hold on the work it abandoned.

    Entries are added only while the scope has not settled; each is dropped
    when its future ends. ``on_settled`` (handed over by the close) runs
    exactly once, outside the scope's lock, when the scope is closed and no
    entry is running.
    """

    def __init__(self, origin: Any, parent: WorkScope | None) -> None:
        self._origin = origin
        self._parent = parent
        self._lock = fork_safe_lock()
        self._running: dict[int, tuple[Future[Any], Any]] = {}
        self._own_finished = False
        self._own_failed = False
        self._closed = False
        self._sealed = False
        self._on_settled: Callable[[WorkSummary], None] | None = None

    @property
    def parent(self) -> WorkScope | None:
        """The enclosing call's scope."""
        return self._parent

    @property
    def running_count(self) -> int:
        """Pieces still running (a snapshot)."""
        with self._lock:
            return len(self._running)

    @property
    def settled(self) -> bool:
        """True once the scope settled; it then refuses every entry."""
        return self._sealed

    @property
    def closed(self) -> bool:
        """True once the call that opened the scope closed it."""
        return self._closed

    def _is_own(self, origin: Any) -> bool:
        return self._origin is not None and origin is self._origin

    def _summary_locked(self) -> WorkSummary:
        return WorkSummary(own_finished=self._own_finished, own_failed=self._own_failed)

    def _add(self, future: Future[Any], origin: Any) -> bool:
        with self._lock:
            if self._sealed:
                return False
            self._running[id(future)] = (future, origin)
            return True

    def _fold(self, future: Future[Any]) -> None:
        """Done-callback body: fold an ended piece into the summary, drop it."""
        settle: Callable[[WorkSummary], None] | None = None
        with self._lock:
            entry = self._running.pop(id(future), None)
            if entry is None:
                return
            if self._is_own(entry[1]):
                self._own_finished = True
                if future.cancelled() or future.exception() is not None:
                    self._own_failed = True
            if self._closed and not self._running and not self._sealed:
                self._sealed = True
                settle = self._on_settled
                self._on_settled = None
                summary = self._summary_locked()
        if settle is not None:
            settle(summary)

    def _close(
        self, on_settled: Callable[[WorkSummary], None] | None
    ) -> WorkSummary | None:
        """Close; return the summary if settled now, else keep ``on_settled``."""
        with self._lock:
            self._closed = True
            if self._sealed:
                return None
            if not self._running:
                self._sealed = True
                return self._summary_locked()
            self._on_settled = on_settled
            return None


def current_work_scope() -> WorkScope | None:
    """Return the innermost open work scope of the current context, or None."""
    return _current_scope.get()


def open_work_scope(origin: Any) -> tuple[WorkScope, Token[WorkScope | None]]:
    """Open a work scope for one call, chained to the enclosing call's scope.

    Args:
        origin: The object that marks this call's own work — the
            ``PolicyContext`` its own timeout stage records with — or ``None``
            for a call that has no own timeout stage.

    Returns:
        ``(scope, token)`` — pass both to :func:`close_work_scope`.
    """
    scope = WorkScope(origin=origin, parent=_current_scope.get())
    return scope, _current_scope.set(scope)


def record_abandoned(future: Future[Any], origin: Any = None) -> None:
    """Record running work a site stopped waiting for.

    Called by a site that stopped waiting on running work — its own timeout
    fired, or an interruption cut the wait short — when the work's cancel
    failed. Adds the future to every scope in the current chain that has not
    settled and folds it out of each when it ends; a future already finished
    folds at once with its outcome. A no-op outside any scope.

    Args:
        future: The future whose cancel failed (the work is running, or ended
            in the instant before the cancel).
        origin: The ``PolicyContext`` the waiting stage ran with, or None.
    """
    scope = _current_scope.get()
    holders: list[WorkScope] = []
    while scope is not None:
        if scope._add(future, origin):
            holders.append(scope)
        scope = scope.parent
    if not holders:
        return

    def _on_done(done: Future[Any]) -> None:
        for holder in holders:
            try:
                holder._fold(done)
            except Exception as e:
                logger.warning(
                    "abandoned_work.fold_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                )

    future.add_done_callback(_on_done)


def close_work_scope(
    scope: WorkScope,
    token: Token[WorkScope | None] | None,
    on_settled: Callable[[WorkSummary], None] | None = None,
) -> WorkSummary | None:
    """Close a call's work scope.

    When ``scope`` is on the current chain, every scope from the current one
    down to it is closed — a nested call that ended without closing its own
    scope (cancelled from outside) is closed with it. The variable is then
    reset with ``token`` (a reset that raises, e.g. a token from another
    context, is skipped: the scopes are already closed).

    Args:
        scope: The scope :func:`open_work_scope` returned.
        token: Its token (None skips the reset).
        on_settled: Called exactly once with the summary when the last running
            piece ends — only when the scope is still holding at close.

    Returns:
        The summary when nothing the scope holds is running (it settled now;
        ``on_settled`` is not called), else None.
    """
    # Close the nested scopes left current above ``scope`` — only when
    # ``scope`` is on the current chain, so a stale scope never closes the
    # scope of a call that is still running.
    left_open: list[WorkScope] = []
    node = _current_scope.get()
    while node is not None and node is not scope:
        left_open.append(node)
        node = node.parent
    if node is scope:
        for nested in left_open:
            nested._close(None)
    if token is not None:
        try:
            _current_scope.reset(token)
        except (ValueError, RuntimeError):
            pass
    return scope._close(on_settled)
