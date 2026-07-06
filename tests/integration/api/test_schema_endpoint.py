"""Integration tests for /schema/, /docs/, /redoc/ — 530 Wave 6F.

Full DRF + drf-spectacular pipeline:
  Django URL conf → drf-spectacular SchemaGenerator → DRF response

Verifies 530 success criteria:
- /schema/ returns valid OpenAPI 3.x JSON
- /docs/ serves Swagger UI HTML
- /redoc/ serves ReDoc HTML
- BALDUR_OPENAPI_ENABLED=0 → /schema/ unreachable (D11 first gate)
- All three routes are authenticated (D11)

drf-spectacular is an optional extras dep — skip the whole file when absent.
Uses Django's RequestFactory + URL resolver to dispatch directly through
the view callable, avoiding the testapp's migration-less ORM setup.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("drf_spectacular")
pytest.importorskip("django")

import django  # noqa: E402

if not django.apps.apps.ready:
    django.setup()

from django.test import RequestFactory, override_settings  # noqa: E402
from django.urls import resolve  # noqa: E402

URLCONF = "tests.integration.api.urls_530"

# drf-spectacular's SpectacularSwaggerView / SpectacularRedocView are
# TemplateView subclasses — Django needs an APP_DIRS=True loader to find
# the bundled drf_spectacular/{swagger_ui,redoc}.html templates. The
# testapp's base settings omit TEMPLATES, so the docs/redoc tests
# augment it via override_settings.
_TEMPLATES_CONFIG = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {},
    },
]


@pytest.fixture
def _reset_openapi_settings():
    """OpenAPISettings singleton is read at URL-conf import time — reset
    before/after each test so env-driven flips are observed cleanly."""
    from baldur.settings.openapi import reset_openapi_settings

    reset_openapi_settings()
    yield
    reset_openapi_settings()


def _reload_baldur_urls():
    """Re-import the baldur URL modules so the OpenAPISettings gate runs again.

    ``urls/schema.py`` evaluates ``get_openapi_settings().enabled`` at module
    import time (530 D11 first gate). When tests flip ``BALDUR_OPENAPI_ENABLED``,
    Python's cached module still holds the previously-evaluated ``urlpatterns``,
    so the gate must be re-evaluated by reloading the module.
    """
    import baldur.api.django.urls as baldur_urls
    import baldur.api.django.urls.schema as schema_mod
    import tests.integration.api.urls_530 as urls_530

    importlib.reload(schema_mod)
    importlib.reload(baldur_urls)
    importlib.reload(urls_530)


def _admin_user():
    """Stand-in for an authenticated admin user — bypasses DB."""
    return SimpleNamespace(
        is_authenticated=True,
        is_active=True,
        is_superuser=True,
        is_staff=True,
        is_anonymous=False,
    )


def _anonymous_user():
    return SimpleNamespace(
        is_authenticated=False,
        is_active=False,
        is_superuser=False,
        is_staff=False,
        is_anonymous=True,
    )


def _dispatch(path: str, *, user=None):
    """Resolve `path` against the test URLCONF and invoke the view callable.

    The URL resolver cache is cleared first so reload-driven gate flips
    (BALDUR_OPENAPI_ENABLED) are observed in fresh URL conf state.
    """
    from django.urls import clear_url_caches

    # resolve() rejects query strings; strip them and re-attach via RequestFactory.
    if "?" in path:
        url_path, _, query = path.partition("?")
        factory = RequestFactory()
        request = factory.get(
            url_path, data=dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        )
    else:
        url_path = path
        factory = RequestFactory()
        request = factory.get(path)
    if user is not None:
        request.user = user
    with override_settings(ROOT_URLCONF=URLCONF):
        clear_url_caches()
        match = resolve(url_path)
        return match.func(request, *match.args, **match.kwargs)


# =============================================================================
# /schema/ — happy path with auth bypass
# =============================================================================


class TestSchemaEndpointIntegration:
    """Full request cycle for the OpenAPI schema endpoint (530 success criteria)."""

    def test_schema_returns_valid_openapi(self, _reset_openapi_settings, monkeypatch):
        """GET /api/baldur/schema/ → HTTP 200 with valid OpenAPI 3.x body."""
        monkeypatch.setenv("DISABLE_BALDUR_AUTH", "true")
        _reload_baldur_urls()

        response = _dispatch("/api/baldur/schema/?format=json")

        assert response.status_code == 200
        # drf-spectacular emits the schema via a custom renderer; .data is the
        # parsed dict (OpenAPI 3.x), while .content is the rendered bytes.
        body = response.data
        assert "openapi" in body
        assert body["openapi"].startswith("3.")
        assert isinstance(body.get("paths"), dict)
        assert len(body["paths"]) > 0

    def test_schema_paths_include_baldur_features_route(
        self, _reset_openapi_settings, monkeypatch
    ):
        """The freshly-introduced /features/ route is discoverable in the spec
        (smoke that 530's own endpoint shows up in the generated schema)."""
        monkeypatch.setenv("DISABLE_BALDUR_AUTH", "true")
        _reload_baldur_urls()

        response = _dispatch("/api/baldur/schema/?format=json")
        body = response.data

        assert "/api/baldur/features/" in body["paths"]

    def test_swagger_ui_served(self, _reset_openapi_settings, monkeypatch):
        """GET /api/baldur/docs/ → HTML containing Swagger UI bundle script tag."""
        monkeypatch.setenv("DISABLE_BALDUR_AUTH", "true")
        _reload_baldur_urls()

        with override_settings(TEMPLATES=_TEMPLATES_CONFIG):
            response = _dispatch("/api/baldur/docs/")
            assert response.status_code == 200
            response.render()
            body = response.content.decode("utf-8").lower()
        # drf-spectacular's SpectacularSwaggerView serves an HTML page that
        # embeds the Swagger UI bundle.
        assert "swagger" in body

    def test_redoc_served(self, _reset_openapi_settings, monkeypatch):
        """GET /api/baldur/redoc/ → HTML containing ReDoc bundle."""
        monkeypatch.setenv("DISABLE_BALDUR_AUTH", "true")
        _reload_baldur_urls()

        with override_settings(TEMPLATES=_TEMPLATES_CONFIG):
            response = _dispatch("/api/baldur/redoc/")
            assert response.status_code == 200
            response.render()
            body = response.content.decode("utf-8").lower()
        assert "redoc" in body


# =============================================================================
# /schema/ — disabled gate (BALDUR_OPENAPI_ENABLED=0 per 530 D11)
# =============================================================================


class TestSchemaDisabledGateIntegration:
    """When the settings gate is off, the URL module contributes an empty
    pattern list — /schema/ resolves to 404 (URL not registered)."""

    def test_schema_disabled_removes_route_from_urlconf(
        self, _reset_openapi_settings, monkeypatch
    ):
        """BALDUR_OPENAPI_ENABLED=0 → /schema/ has no registered handler."""
        from django.urls import Resolver404

        monkeypatch.setenv("BALDUR_OPENAPI_ENABLED", "0")
        _reload_baldur_urls()
        try:
            with override_settings(ROOT_URLCONF=URLCONF):
                with pytest.raises(Resolver404):
                    resolve("/api/baldur/schema/")
        finally:
            monkeypatch.delenv("BALDUR_OPENAPI_ENABLED", raising=False)
            _reload_baldur_urls()

    def test_docs_disabled_removes_route_from_urlconf(
        self, _reset_openapi_settings, monkeypatch
    ):
        """BALDUR_OPENAPI_ENABLED=0 → /docs/ has no registered handler."""
        from django.urls import Resolver404

        monkeypatch.setenv("BALDUR_OPENAPI_ENABLED", "0")
        _reload_baldur_urls()
        try:
            with override_settings(ROOT_URLCONF=URLCONF):
                with pytest.raises(Resolver404):
                    resolve("/api/baldur/docs/")
        finally:
            monkeypatch.delenv("BALDUR_OPENAPI_ENABLED", raising=False)
            _reload_baldur_urls()


# =============================================================================
# /schema/ — auth gate (PermissionLevel.AUTHENTICATED per 530 D11)
# =============================================================================


class TestSchemaAuthGateIntegration:
    """530 D11: /schema/ is AUTHENTICATED — anonymous access denied."""

    def test_schema_denies_anonymous_when_auth_enforced(
        self, _reset_openapi_settings, monkeypatch
    ):
        """No DISABLE_BALDUR_AUTH → anonymous user gets 401/403."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)
        _reload_baldur_urls()

        response = _dispatch("/api/baldur/schema/?format=json", user=_anonymous_user())

        # IsBaldurAuthenticated rejects anonymous — DRF returns 401 or 403
        # depending on whether the auth pipeline includes session auth.
        assert response.status_code in (401, 403)

    def test_schema_allows_authenticated_user(
        self, _reset_openapi_settings, monkeypatch
    ):
        """Authenticated (any) user can read /schema/ per D11."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)
        _reload_baldur_urls()

        response = _dispatch("/api/baldur/schema/?format=json", user=_admin_user())

        assert response.status_code == 200
