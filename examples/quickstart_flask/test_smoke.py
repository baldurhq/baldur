"""Smoke test for the Baldur Flask quickstart (531 D5).

Uses Flask's in-process test client — no running server, no infra. Bare
``pip install pytest`` is enough. Importing ``app`` runs ``init_flask(app)``,
which calls ``baldur.init()``.

``BALDUR_TEST_MODE=true`` keeps Baldur's startup deterministic and quiet — it
does not change the route's success path.
"""

from __future__ import annotations

import os

os.environ.setdefault("BALDUR_TEST_MODE", "true")

from app import app  # noqa: E402


def test_protected_route_returns_ok() -> None:
    client = app.test_client()
    response = client.get("/demo")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "service": "demo"}
