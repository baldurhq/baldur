"""Integration tests for /api/baldur/features/ — 530 Wave 6F.

Full DRF dispatch pipeline:
  Django URL conf → DRF auth → IsBaldurAdmin permission → HandlerAPIView →
  features_summary → manifest loader × entitlement → JSON response

Uses RequestFactory + URL resolver to dispatch directly (testapp has no
ORM migrations, so Client()-based tests can't migrate auth tables).

Verifies 530 success criteria:
- Admin user → HTTP 200 with the D9 response shape
- Authenticated non-admin user → HTTP 403 (530 D2)
- Anonymous client → HTTP 401/403
- Response carries one entry per manifest row (count parity, 530 D10)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("django")

import django  # noqa: E402

if not django.apps.apps.ready:
    django.setup()

from django.test import RequestFactory, override_settings  # noqa: E402
from django.urls import resolve  # noqa: E402

from baldur.core.entitlement import (  # noqa: E402
    EntitlementClaims,
    EntitlementResult,
    EntitlementStatus,
)

URLCONF = "tests.integration.api.urls_530"


# =============================================================================
# Helpers
# =============================================================================


class _FakeUser(SimpleNamespace):
    """Stand-in user that responds to .groups.filter(name=...).exists()."""

    def __init__(
        self,
        *,
        is_authenticated=True,
        is_active=True,
        is_superuser=False,
        is_staff=False,
        groups=(),
    ):
        super().__init__(
            is_authenticated=is_authenticated,
            is_active=is_active,
            is_superuser=is_superuser,
            is_staff=is_staff,
            is_anonymous=not is_authenticated,
        )
        self._group_names = set(groups)

    @property
    def groups(self):
        names = self._group_names

        class _Manager:
            @staticmethod
            def filter(name=None, name__in=None):
                if name is not None:
                    matched = {n for n in names if n == name}
                elif name__in is not None:
                    matched = names & set(name__in)
                else:
                    matched = set()

                class _Query:
                    @staticmethod
                    def exists():
                        return bool(matched)

                    @staticmethod
                    def values_list(_field, flat=True):
                        return list(matched)

                return _Query()

        return _Manager()


def _superuser() -> _FakeUser:
    return _FakeUser(is_authenticated=True, is_superuser=True, is_staff=True)


def _baldur_admin_group_member() -> _FakeUser:
    return _FakeUser(is_authenticated=True, groups={"baldur_admin"})


def _normal_user() -> _FakeUser:
    return _FakeUser(is_authenticated=True)


def _anonymous() -> _FakeUser:
    return _FakeUser(is_authenticated=False)


def _dispatch(path: str, *, user) -> object:
    """Resolve `path` and invoke the view callable with `user` attached."""
    from django.urls import clear_url_caches

    factory = RequestFactory()
    request = factory.get(path)
    request.user = user
    with override_settings(ROOT_URLCONF=URLCONF):
        clear_url_caches()
        match = resolve(path)
        return match.func(request, *match.args, **match.kwargs)


@pytest.fixture
def _active_entitlement():
    """Inject an ACTIVE entitlement result for the request cycle."""
    claims = EntitlementClaims(
        customer_id="cust_a1b2c3",
        org="acme",
        tier="PRO",
        plan="monthly",
        issued_at="2026-04-01",
        expires="2099-01-01",
    )
    result = EntitlementResult(status=EntitlementStatus.ACTIVE, claims=claims)
    with patch(
        "baldur.core.entitlement.get_entitlement_status",
        return_value=result,
    ):
        yield result


@pytest.fixture
def _missing_entitlement():
    """Inject MISSING entitlement (OSS default)."""
    result = EntitlementResult(status=EntitlementStatus.MISSING)
    with patch(
        "baldur.core.entitlement.get_entitlement_status",
        return_value=result,
    ):
        yield result


def _json_body(response) -> dict:
    """Extract dict body — DRF Response.data is the parsed dict."""
    if hasattr(response, "data"):
        return response.data
    return json.loads(response.content)


# =============================================================================
# Admin happy path
# =============================================================================


class TestFeaturesEndpointIntegration:
    """Full request cycle for /api/baldur/features/ (530 success criteria)."""

    def test_admin_user_receives_200_with_d9_shape(
        self, _active_entitlement, monkeypatch
    ):
        """Superuser → 200 + {entitlement, features} envelope per 530 D9."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)

        response = _dispatch("/api/baldur/features/", user=_superuser())

        assert response.status_code == 200
        body = _json_body(response)
        assert "entitlement" in body
        assert "features" in body
        assert isinstance(body["features"], list)

    def test_admin_response_contains_openapi_manifest_entry(
        self, _missing_entitlement, monkeypatch
    ):
        """The 530-introduced OpenAPI feature shows up in the inventory —
        smoke that the V1_LAUNCH_MANIFEST.yaml is reachable end-to-end."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)

        response = _dispatch("/api/baldur/features/", user=_superuser())
        body = _json_body(response)

        modules = {feat["module"] for feat in body["features"]}
        assert "openapi.py" in modules

        openapi_entry = next(f for f in body["features"] if f["module"] == "openapi.py")
        # 530 D4: default enabled=True, Core tier — license is always 'active'.
        assert openapi_entry["tier"] == "Core"
        assert openapi_entry["default"] is True
        assert openapi_entry["license_status"] == "active"
        assert openapi_entry["env_var"] == "BALDUR_OPENAPI_ENABLED"

    def test_entitlement_block_reports_active_status_with_claims(
        self, _active_entitlement, monkeypatch
    ):
        """ACTIVE result → block carries customer_id/org/expires/days (530 D9)."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)

        response = _dispatch("/api/baldur/features/", user=_superuser())
        block = _json_body(response)["entitlement"]

        assert block["status"] == "active"
        assert block["customer_id"] == "cust_a1b2c3"
        assert block["org"] == "acme"
        assert block["days_until_expiry"] >= 0

    def test_features_count_matches_manifest_row_count(
        self, _missing_entitlement, monkeypatch
    ):
        """One entry per V1_LAUNCH_MANIFEST.yaml row (success criteria 4)."""
        from baldur.services.feature_manifest import load_feature_manifest
        from baldur.services.feature_manifest import loader as loader_mod

        loader_mod._cache_clear()
        expected_count = len(load_feature_manifest())

        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)

        response = _dispatch("/api/baldur/features/", user=_superuser())
        body = _json_body(response)

        assert len(body["features"]) == expected_count
        assert expected_count > 0


# =============================================================================
# Auth gate — admin-only per 530 D2
# =============================================================================


class TestFeaturesEndpointAuthIntegration:
    """530 D2: /features/ is ADMIN-only; anything below admin gets 403."""

    def test_normal_authenticated_user_receives_403(
        self, _missing_entitlement, monkeypatch
    ):
        """Authenticated-but-non-admin user → 403 (fail-secure per 530 D2)."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)

        response = _dispatch("/api/baldur/features/", user=_normal_user())

        assert response.status_code == 403

    def test_anonymous_client_receives_401_or_403(
        self, _missing_entitlement, monkeypatch
    ):
        """Unauthenticated → 401/403 (depends on auth pipeline)."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)

        response = _dispatch("/api/baldur/features/", user=_anonymous())

        assert response.status_code in (401, 403)

    def test_baldur_admin_group_member_receives_200(
        self, _missing_entitlement, monkeypatch
    ):
        """Non-superuser in baldur_admin group → 200 (530 D2 group rule)."""
        monkeypatch.delenv("DISABLE_BALDUR_AUTH", raising=False)

        response = _dispatch("/api/baldur/features/", user=_baldur_admin_group_member())

        assert response.status_code == 200
