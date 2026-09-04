# baldur.adapters.fastapi — FastAPI Adapter

`fastapi_lifespan` async context manager + `BaldurMiddleware` ASGI middleware.
The adapter intentionally stays thin — decisions live in
`baldur.api.middleware`; this wrapper only translates between FastAPI's
`Request` / `Response` and Baldur's `RequestContext` / `ResponseContext`.

The middleware also reports each in-flight request to the graceful-shutdown
drain, so a SIGTERM waits for the requests already running — up to the drain
window — before Baldur's audit and DLQ subsystems close.

The adapter manages no host-app authentication — your app owns endpoint auth,
and Baldur's own admin server is gated by key-based roles. Unlike the Django
adapter, it registers no Django permission groups.

!!! note "See also"
    [FastAPI quickstart](../../getting-started/fastapi.md)

::: baldur.adapters.fastapi
