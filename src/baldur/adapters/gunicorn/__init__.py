"""Gunicorn integration for baldur.

Provides hook callables suitable for gunicorn ``-c`` configuration so that
baldur's :class:`GracefulShutdownCoordinator` fires registered handlers
(Audit WAL flush, leader-election release, bulkhead drain, etc.) when the
worker receives SIGTERM/SIGQUIT.

Usage A — re-export in your gunicorn.conf.py::

    # gunicorn.conf.py
    from baldur.adapters.gunicorn.hooks import (
        post_worker_init, worker_int, worker_exit,
    )

Usage B — point ``-c`` directly at the shipped hooks module::

    gunicorn -c "$(python -c 'from baldur.adapters.gunicorn import hooks; print(hooks.__file__)')" wsgi:app

Without one of these wiring patterns, baldur's registered shutdown
handlers will NOT fire on SIGTERM in a gunicorn deployment — gunicorn's
worker-level signal handler invokes ``worker_int`` only when the user has
configured one.

Status: Public
"""

from __future__ import annotations

from baldur.adapters.gunicorn.hooks import (
    post_worker_init,
    worker_exit,
    worker_int,
)

__all__ = ["post_worker_init", "worker_exit", "worker_int"]
