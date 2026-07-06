"""Minimal Django settings for the Baldur quickstart.

Zero infrastructure: in-memory SQLite + Baldur's in-memory fallback (no Redis,
no env vars). Adding ``baldur.adapters.django`` to ``INSTALLED_APPS`` calls
``baldur.init()`` automatically on startup via the app's ``ready()`` hook.

Production: see the "Add Redis for production" appendix in
``docs/getting-started/django.md`` — the in-memory fallback is single-process
only and is NOT safe for multi-worker deployments.
"""

from __future__ import annotations

SECRET_KEY = "quickstart-insecure-key-do-not-use-in-production"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # Baldur Django integration — calls baldur.init() on startup.
    "baldur.adapters.django",
]

ROOT_URLCONF = "urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

USE_TZ = True
