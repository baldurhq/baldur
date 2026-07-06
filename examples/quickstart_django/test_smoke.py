"""Smoke test for the Baldur Django quickstart (531 D5).

Runs under a bare ``pip install pytest`` — no ``pytest-django``. Self-bootstraps
Django via ``settings.configure(...)`` + ``django.setup()`` (which sidesteps any
ambient ``DJANGO_SETTINGS_MODULE``), then drives the ``@baldur.protected`` view
through Django's in-process test ``Client`` and asserts a 200.

``BALDUR_TEST_MODE=true`` keeps Baldur's startup deterministic and quiet in CI
(memory backends, no production fail-loud) — it does not change the view's
success path.
"""

from __future__ import annotations

import os

os.environ.setdefault("BALDUR_TEST_MODE", "true")

import django  # noqa: E402
from django.conf import settings  # noqa: E402

settings.configure(
    DEBUG=True,
    SECRET_KEY="quickstart-smoke-key",  # noqa: S106
    ROOT_URLCONF="urls",
    ALLOWED_HOSTS=["*"],
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        # Same Baldur integration the documented settings.py uses — exercises
        # the INSTALLED_APPS entry that calls baldur.init() on startup.
        "baldur.adapters.django",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()

from django.test import Client  # noqa: E402  (must import after django.setup())


def test_protected_view_returns_ok() -> None:
    client = Client()
    response = client.get("/demo/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "demo"}
