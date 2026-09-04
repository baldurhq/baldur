# baldur.adapters.flask — Flask Adapter

`init_flask(app, service_name=None)` factory hook. Calls `baldur.init()`
exactly once and registers `before_request` / `after_request` handlers that
delegate to the framework-free helpers in `baldur.api.middleware`.

The hooks also report each in-flight request to the graceful-shutdown drain, so
a SIGTERM waits for the requests already running — up to the drain window —
before Baldur's audit and DLQ subsystems close.

The adapter manages no host-app authentication — your app owns endpoint auth,
and Baldur's own admin server is gated by key-based roles. Unlike the Django
adapter, it registers no Django permission groups.

!!! note "See also"
    [Flask quickstart](../../getting-started/flask.md)

::: baldur.adapters.flask
