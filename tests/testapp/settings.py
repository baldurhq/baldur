"""Minimal Django settings for baldur integration tests.

Usage:
    pytest tests/ --ds=tests.testapp.settings
    DJANGO_SETTINGS_MODULE=tests.testapp.settings pytest tests/
"""

import logging as _logging
import os
from typing import Any

import structlog as _structlog

# Test-only: silence noisy startup events emitted by baldur modules
# during AppConfig.ready(). Historical context: these events became
# visible in the test suite starting with 68b15ed (feat 416), which
# wired baldur.init() into apps.ready(). Before that commit,
# ready() only performed simple config validation; the new init() runs
# 7 framework-agnostic bootstrap stages (audit pipeline startup,
# env_snapshot recording, PRO entry-point hook discovery, shutdown
# handlers, event bus subscriptions, ...) — each emitting structlog
# output. These messages are intentional in production (operator
# visibility) and are silenced ONLY in the unit test suite.
#
# Timing: pytest-django imports this settings module before
# tests/conftest.py module-level code runs — and crucially before
# django.setup() invokes AppConfig.ready(). This is therefore the only
# reliable point to influence baldur's logging behavior during
# Django startup.
#
# Two-layer silencing:
#   1. WARNING-level filtering_bound_logger — drops all INFO/DEBUG
#      startup trail (event_bus subscriptions, handler registrations,
#      background thread starts, etc.) at structlog's wrapper level,
#      before any processor runs.
#   2. DropEvent processor — surgically drops WARNING/ERROR events
#      that are known test-noise but would otherwise pass the level
#      filter.
#
# conftest.py's pytest_configure() re-runs structlog.configure() with
# a different wrapper_class after collection begins, so this WARNING
# filter is scoped to the AppConfig.ready() window only — caplog-based
# tests during test runtime see the conftest configuration, not this one.
_NOISY_EVENT_NAMES = frozenset(
    {
        "running_quarantine_mode",
        "baldur.init_correlation_engine_failed",
        "baldur.start_correlation_engine_loop_failed",
        "baldur.celery_signals_not_registered",
        # cluster_identity.py emits this as a literal message, not a
        # structured event name — match the full string.
        "[QuarantineMode] System running in Quarantine Mode. "
        "Cross-cluster operations will be disabled.",
    }
)


def _drop_noisy_startup_events(logger, method_name, event_dict):
    if event_dict.get("event") in _NOISY_EVENT_NAMES:
        raise _structlog.DropEvent
    return event_dict


_structlog.configure(
    processors=[
        _drop_noisy_startup_events,
        _structlog.contextvars.merge_contextvars,
        _structlog.processors.add_log_level,
        _structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        _structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=_structlog.make_filtering_bound_logger(_logging.WARNING),
    cache_logger_on_first_use=False,
)

# Test-only: inject dummy secrets to silence missing-secret warnings emitted
# from the centralized boot gate baldur.init() -> _validate_critical_secrets()
# -> validate_required_secrets(). These are intentional production safety
# guards; in unit tests where real secrets are not configured they produce
# noise without value.
#
# This module-level injection runs at the moment Django imports the settings
# module — strictly BEFORE django.setup() invokes AppConfig.ready(). It is
# therefore the only injection point that reliably beats pytest-django's
# import sequence (tests/conftest.py module-level code runs too late, after
# baldur has already been imported by Django setup).
#
# setdefault preserves any real value provided externally — so production
# behavior is unchanged when these env vars are set by the operator.
for _secret_key in (
    "ENCRYPTION_KEY",
    "AUDIT_SIGNING_KEY",
    "DATABASE_PASSWORD",
    "REDIS_PASSWORD",
    "TOSS_SECRET_KEY",
    "SLACK_WEBHOOK_TOKEN",
    "SLACK_BOT_TOKEN",
    "PAGERDUTY_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
):
    os.environ.setdefault(f"BALDUR_SECRETS_{_secret_key}", "test-value")

# ClusterIdentity validation defaults — defence-in-depth against
# quarantine mode leaking across xdist workers.  conftest sets
# BALDUR_TEST_MODE=true which normally skips validation, but several
# tests temporarily remove it (monkeypatch.delenv / @patch.dict).
# If get_cluster_identity() fires during that window, validation
# fails (cluster_id="default", region=None) and _quarantine_mode is
# set to True globally.  This leaks into subsequent tests on the
# same worker, causing propagator tests to return False unexpectedly.
# By providing valid defaults here — the earliest import point —
# validation passes even when BALDUR_TEST_MODE is absent.
os.environ.setdefault("BALDUR_CLUSTER_ID", "test-cluster-001")
os.environ.setdefault("BALDUR_NAMESPACE_REGION", "test-region")

# 453 D5a: BaldurRuntime eagerly reads BALDUR_TEST_MODE in __init__, and
# pytest-django's pytest_load_initial_conftests triggers django.setup() →
# AppConfig.ready() → baldur.init() → BaldurRuntime() *before* the project's
# tests/conftest.py pytest_configure runs. Seeding here — at the testapp
# settings module-load point — is the earliest reliable seed for the
# eager-read to observe "true".
os.environ.setdefault("BALDUR_TEST_MODE", "true")

# 469 D1: same timing rationale as BALDUR_TEST_MODE above. The host pytest
# process spawning a LeaderScheduler / admin HTTP server is dead weight at
# best, noise-source at worst — the unit-test process has no Redis to elect
# against, and examples/tests/'s testbed runs entirely inside the django_app
# container. setdefault preserves CI overrides (e.g., a scheduler-exercising
# test sets the var to "1" for its scope).
os.environ.setdefault("BALDUR_SCHEDULER_AUTOSTART", "0")
os.environ.setdefault("BALDUR_ADMIN_AUTOSTART", "0")

# 524: the observability profile defaults to ``auto``, which resolves to
# ``otel_collector`` whenever the OTel SDK + Prometheus bridge are importable
# (both present in the monorepo dev env). Under that resolution
# ``configure_baldur()`` (called at the bottom of this module) would
# auto-initialize the OTel SDK and instrument Django during django.setup(),
# leaking real OTel state (e.g. OTEL_PYTHON_DJANGO_EXCLUDED_URLS) into the test
# process. Same timing rationale as BALDUR_TEST_MODE above: this is the earliest
# reliable seed (before AppConfig.ready() → baldur.init()), beating
# tests/conftest.py's module-level setdefault. Pin to ``local`` so the default
# backend stays prometheus (the pre-524 default); tests that exercise OTel
# resolution set the profile explicitly within their own scope.
os.environ.setdefault("BALDUR_OBSERVABILITY_PROFILE", "local")

SECRET_KEY = "test-secret-key-for-baldur"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "baldur.adapters.django",
    "tests.testapp",
]

# 530 Wave 6F — drf-spectacular is an optional extras dep. When present,
# wire it into INSTALLED_APPS so the /schema/ + /docs/ + /redoc/ routes
# resolve at request-time in the schema integration tests.
try:
    import drf_spectacular  # noqa: F401
except ImportError:
    pass
else:
    INSTALLED_APPS.append("drf_spectacular")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "testapp.TestUser"

ROOT_URLCONF = "tests.testapp.urls"

MIDDLEWARE = [
    "baldur.api.django.middleware.HealthBridgeMiddleware",
    "baldur.api.django.middleware.BaldurMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

REST_FRAMEWORK: dict[str, Any] = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# 530 Wave 6F — when drf-spectacular is available, point DEFAULT_SCHEMA_CLASS
# at it so the /schema/ endpoint's SchemaGenerator can introspect each view's
# .schema descriptor uniformly (otherwise drf-spectacular's generator calls
# DRF's default AutoSchema with a wider signature and crashes on the type
# mismatch). Mirrors the conditional INSTALLED_APPS block above.
try:
    import drf_spectacular  # noqa: F401, F811
except ImportError:
    pass
else:
    REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"] = "drf_spectacular.openapi.AutoSchema"

# baldur minimal settings
BALDUR_CORE_DOMAINS = ["payment", "order", "test"]
BALDUR_AUTO_CONFIG_MIDDLEWARE = False
