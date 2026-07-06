"""Smoke test for the Baldur FastAPI quickstart (531 D5).

Uses FastAPI's in-process ``TestClient`` (backed by ``httpx``, shipped by the
``baldur-framework[fastapi]`` extra) — no running server, no infra. Bare ``pip install
pytest`` is enough on top of the extra. Entering the ``TestClient`` context
manager runs ``fastapi_lifespan``, which calls ``baldur.init()``.

``BALDUR_TEST_MODE=true`` keeps Baldur's startup deterministic and quiet — it
does not change the route's success path.
"""

from __future__ import annotations

import os

os.environ.setdefault("BALDUR_TEST_MODE", "true")

from app import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def test_protected_route_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/demo")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "demo"}
