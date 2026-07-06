# baldur.api.admin — Framework-Free Admin HTTP Server

Stdlib `http.server.ThreadingHTTPServer`-based management API. Runs in a
daemon thread, dispatches to framework-agnostic handler functions, and
integrates with `ShutdownCoordinator` for graceful drain. Auto-starts via
`baldur.init()` when `BALDUR_ADMIN_AUTOSTART=1` (the default).

::: baldur.api.admin
