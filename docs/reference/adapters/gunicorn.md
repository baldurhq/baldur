# baldur.adapters.gunicorn — Gunicorn Lifecycle Hooks

Hook callables suitable for gunicorn `-c` configuration so Baldur's
`GracefulShutdownCoordinator` fires registered handlers (Audit WAL flush,
leader-election release, bulkhead drain, etc.) when the worker receives
SIGTERM / SIGQUIT.

::: baldur.adapters.gunicorn
