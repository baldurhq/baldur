"""Minimal FastAPI app wired with Baldur.

``fastapi_lifespan`` calls ``baldur.init()`` on startup; ``BaldurMiddleware``
adds rate-limit + backpressure + circuit-breaker pre-flight at the ASGI edge.
The ``/demo`` route is protected by the marquee ``@baldur.protected`` facade.
Zero infrastructure: in-memory fallback, no Redis, no env vars.

Run the server with ``uvicorn app:app --reload`` from this directory, then
``curl http://127.0.0.1:8000/demo``. See ``docs/getting-started/fastapi.md``.
"""

from __future__ import annotations

from fastapi import FastAPI

import baldur
from baldur.adapters.fastapi import BaldurMiddleware, fastapi_lifespan

app = FastAPI(lifespan=fastapi_lifespan)
app.add_middleware(BaldurMiddleware)


@app.get("/demo")
@baldur.protected("demo")
def demo() -> dict[str, str]:
    """Return a JSON payload through Baldur's resilience pipeline."""
    return {"status": "ok", "service": "demo"}
