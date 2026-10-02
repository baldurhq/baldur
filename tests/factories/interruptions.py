"""Interruptions that cut a waiting thread short, and a seam that aims one.

A Celery soft time limit (billiard's ``SoftTimeLimitExceeded``, an
``Exception``) and a gevent timeout (``gevent.Timeout``, a ``BaseException``)
are raised into whatever the thread is waiting on — never by the function the
thread waits for. The stand-ins keep those class shapes, so a test runs where
neither celery nor gevent is installed.

A signal cannot be aimed at a wait deterministically, so
:func:`interrupted_timeout_wait` replaces only the caller's ``result()`` on the
next future the shared timeout executor hands out: the work itself runs on the
real executor, and the wait is cut once the work has entered.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

__all__ = [
    "INTERRUPTIONS",
    "INTERRUPTION_IDS",
    "GeventTimeout",
    "SoftTimeLimitExceeded",
    "interrupted_timeout_wait",
]

# Upper bound on the wait for the work to enter before the cut.
_ENTER_WAIT_S = 5.0


class SoftTimeLimitExceeded(Exception):  # noqa: N818 - mirrors billiard's name
    """Shaped like Celery's soft time limit: an ``Exception`` raised into the wait."""


class GeventTimeout(BaseException):  # noqa: N818 - mirrors gevent's name
    """Shaped like ``gevent.Timeout``: a ``BaseException`` raised into the wait."""


INTERRUPTIONS = (SoftTimeLimitExceeded, GeventTimeout, KeyboardInterrupt)
INTERRUPTION_IDS = ("soft_time_limit", "gevent_timeout", "keyboard_interrupt")


@contextmanager
def interrupted_timeout_wait(
    interruption: BaseException, *, entered: threading.Event
) -> Iterator[list[Future[Any]]]:
    """Cut the next ``TimeoutPolicy`` wait short with ``interruption``.

    Only the first future submitted inside the block is cut, once ``entered``
    is set (the work began); every later submit waits normally. Yields the
    futures that were cut.
    """
    from baldur.resilience.policies.timeout import TimeoutPolicy

    real = TimeoutPolicy._get_executor()
    pending = [interruption]
    cut: list[Future[Any]] = []

    class _CuttingExecutor:
        def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Future[Any]:
            future = real.submit(fn, *args, **kwargs)
            if pending:
                raised = pending.pop()

                def _interrupted(timeout: float | None = None) -> Any:
                    entered.wait(_ENTER_WAIT_S)
                    raise raised

                future.result = _interrupted  # type: ignore[method-assign]
                cut.append(future)
            return future

    with patch.object(TimeoutPolicy, "_get_executor", return_value=_CuttingExecutor()):
        yield cut
