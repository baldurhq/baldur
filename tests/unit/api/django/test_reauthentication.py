"""Reauthentication framework — unit tests (523 Step 5).

Covers ``baldur.api.django.reauthentication`` end-to-end:
- ``ReauthenticationConfig`` defaults.
- ``ReauthenticationProvider.on_reauthentication_required`` user-id logging hook.
- ``NoOpReauthenticationProvider`` always-allow contract + fallback response.
- ``SessionBasedReauthProvider`` idle/session timeout branches +
  malformed-timestamp resilience + missing-session degraded path.
- ``get_reauthentication_provider`` / ``set_reauthentication_provider``
  singleton lifecycle, including custom provider loading via
  ``BALDUR_REAUTH_PROVIDER`` and the fail-open NoOp fallback.
- ``requires_reauthentication`` decorator: enabled/disabled bypass,
  required path invocation order (on_required hook + response generation),
  and FAIL-SECURE wrap when provider raises.
- ``RequiresReauthenticationPermission``: pass/fail/error branches.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from baldur.api.django.reauthentication import (
    NoOpReauthenticationProvider,
    ReauthenticationConfig,
    ReauthenticationProvider,
    RequiresReauthenticationPermission,
    SessionBasedReauthProvider,
    get_reauthentication_provider,
    requires_reauthentication,
    set_reauthentication_provider,
)
from baldur.utils.time import utc_now

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rf() -> RequestFactory:
    return RequestFactory()


@pytest.fixture(autouse=True)
def _reset_provider_singleton():
    # Ensure singleton state cannot leak between tests
    set_reauthentication_provider(None)  # type: ignore[arg-type]
    yield
    set_reauthentication_provider(None)  # type: ignore[arg-type]


# =============================================================================
# ReauthenticationConfig — dataclass defaults
# =============================================================================


class TestReauthenticationConfigContract:
    def test_defaults(self):
        config = ReauthenticationConfig()
        assert config.max_idle_minutes == 15
        assert config.max_session_minutes == 60
        assert config.enabled is True
        assert config.status_code == 403
        assert "reauthentication" in config.message.lower()

    def test_overrides(self):
        config = ReauthenticationConfig(
            max_idle_minutes=5,
            max_session_minutes=30,
            enabled=False,
            message="custom",
            status_code=401,
        )
        assert config.max_idle_minutes == 5
        assert config.enabled is False
        assert config.status_code == 401


# =============================================================================
# ReauthenticationProvider.on_reauthentication_required — base hook
# =============================================================================


class _RecordingProvider(ReauthenticationProvider):
    """Concrete subclass exposing only the abstracts so we can test the
    inherited ``on_reauthentication_required`` logging hook directly."""

    def check_reauthentication_required(self, request, config):  # type: ignore[override]
        return False

    def get_reauthentication_response(self, request, config):  # type: ignore[override]
        from django.http import JsonResponse

        return JsonResponse({"ok": True})


class TestProviderOnReauthHookBehavior:
    def test_hook_logs_user_id_when_user_present(self, rf, caplog):
        provider = _RecordingProvider()
        request = rf.get("/foo")
        request.user = SimpleNamespace(id=42)

        # Must not raise; the hook logs via structlog
        provider.on_reauthentication_required(request, ReauthenticationConfig())

    def test_hook_handles_anonymous_request(self, rf):
        provider = _RecordingProvider()
        request = rf.get("/foo")
        # No request.user attribute → falls back to "anonymous"
        provider.on_reauthentication_required(request, ReauthenticationConfig())

    def test_hook_handles_user_without_id(self, rf):
        provider = _RecordingProvider()
        request = rf.get("/foo")
        request.user = SimpleNamespace()  # no .id attribute
        provider.on_reauthentication_required(request, ReauthenticationConfig())


# =============================================================================
# NoOpReauthenticationProvider — always-allow contract
# =============================================================================


class TestNoOpProviderContract:
    def test_never_requires_reauth(self, rf):
        provider = NoOpReauthenticationProvider()
        result = provider.check_reauthentication_required(
            rf.get("/x"), ReauthenticationConfig()
        )
        assert result is False

    def test_response_returns_json_with_config_status(self, rf):
        provider = NoOpReauthenticationProvider()
        config = ReauthenticationConfig(status_code=401, message="m")
        response = provider.get_reauthentication_response(rf.get("/x"), config)
        assert response.status_code == 401
        assert b"reauthentication_required" in response.content


# =============================================================================
# SessionBasedReauthProvider — idle / session timeout branches
# =============================================================================


def _set_session(request, **values):
    """Attach a dict-style fake session to a Django RequestFactory request."""
    request.session = values  # type: ignore[attr-defined]
    return request


class TestSessionBasedProviderBehavior:
    def test_disabled_config_short_circuits(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        _set_session(request)
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig(enabled=False)
        )
        assert result is False

    def test_no_session_attribute_returns_false(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        # No request.session at all
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig()
        )
        assert result is False

    def test_idle_timeout_exceeded_requires_reauth(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        # 20 minutes ago — exceeds 15-minute default
        stale = (utc_now() - timedelta(minutes=20)).isoformat()
        _set_session(request, _baldur_last_activity=stale)
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig(max_idle_minutes=15)
        )
        assert result is True

    def test_idle_within_threshold_allows(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        fresh = (utc_now() - timedelta(minutes=5)).isoformat()
        _set_session(request, _baldur_last_activity=fresh)
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig(max_idle_minutes=15)
        )
        assert result is False

    def test_malformed_idle_timestamp_is_silently_ignored(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        _set_session(request, _baldur_last_activity="not-a-date")
        # Must not raise; falls through to session-age check (also empty)
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig()
        )
        assert result is False

    def test_session_age_exceeded_requires_reauth(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        old_auth = (utc_now() - timedelta(minutes=120)).isoformat()
        _set_session(request, _baldur_auth_time=old_auth)
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig(max_session_minutes=60)
        )
        assert result is True

    def test_session_age_within_limit_allows(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        fresh_auth = (utc_now() - timedelta(minutes=10)).isoformat()
        _set_session(request, _baldur_auth_time=fresh_auth)
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig(max_session_minutes=60)
        )
        assert result is False

    def test_malformed_auth_time_is_silently_ignored(self, rf):
        provider = SessionBasedReauthProvider()
        request = rf.get("/x")
        _set_session(request, _baldur_auth_time="garbage")
        result = provider.check_reauthentication_required(
            request, ReauthenticationConfig()
        )
        assert result is False

    def test_response_returns_json(self, rf):
        provider = SessionBasedReauthProvider()
        response = provider.get_reauthentication_response(
            rf.get("/x"), ReauthenticationConfig()
        )
        assert response.status_code == 403
        assert b"REAUTH_REQUIRED" in response.content
        assert b"reauthentication_url" in response.content


# =============================================================================
# Provider registry — get/set lifecycle
# =============================================================================


class TestProviderRegistryBehavior:
    def test_default_provider_is_noop(self):
        provider = get_reauthentication_provider()
        assert isinstance(provider, NoOpReauthenticationProvider)

    def test_returns_same_instance_on_repeated_calls(self):
        a = get_reauthentication_provider()
        b = get_reauthentication_provider()
        assert a is b

    def test_set_overrides_returned_provider(self):
        custom = SessionBasedReauthProvider()
        set_reauthentication_provider(custom)
        assert get_reauthentication_provider() is custom

    def test_custom_provider_via_settings_path(self):
        # Force a fresh resolution by clearing the cached singleton.
        set_reauthentication_provider(None)  # type: ignore[arg-type]
        from django.test import override_settings

        with override_settings(
            BALDUR_REAUTH_PROVIDER=(
                "baldur.api.django.reauthentication.SessionBasedReauthProvider"
            )
        ):
            resolved = get_reauthentication_provider()
        assert isinstance(resolved, SessionBasedReauthProvider)

    def test_load_failure_falls_back_to_noop(self):
        set_reauthentication_provider(None)  # type: ignore[arg-type]
        from django.test import override_settings

        with override_settings(BALDUR_REAUTH_PROVIDER="nonexistent.module.BadProvider"):
            resolved = get_reauthentication_provider()
        assert isinstance(resolved, NoOpReauthenticationProvider)


# =============================================================================
# @requires_reauthentication decorator
# =============================================================================


class _CountingProvider(ReauthenticationProvider):
    """Recording provider with configurable check + on_required hook."""

    def __init__(self, requires: bool = False, raise_check: bool = False):
        self._requires = requires
        self._raise_check = raise_check
        self.on_required_calls = 0
        self.response_calls = 0
        self.check_calls = 0

    def check_reauthentication_required(self, request, config):  # type: ignore[override]
        self.check_calls += 1
        if self._raise_check:
            raise RuntimeError("provider broken")
        return self._requires

    def get_reauthentication_response(self, request, config):  # type: ignore[override]
        from django.http import JsonResponse

        self.response_calls += 1
        return JsonResponse({"reauth": True}, status=config.status_code)

    def on_reauthentication_required(self, request, config):  # type: ignore[override]
        self.on_required_calls += 1


class TestRequiresReauthenticationDecorator:
    def test_disabled_decorator_bypasses_provider(self, rf):
        provider = _CountingProvider(requires=True)
        set_reauthentication_provider(provider)

        @requires_reauthentication(enabled=False)
        def view(request):
            from django.http import HttpResponse

            return HttpResponse("ok")

        response = view(rf.get("/x"))
        assert response.status_code == 200
        assert provider.check_calls == 0  # provider never called

    def test_provider_allows_invokes_view(self, rf):
        provider = _CountingProvider(requires=False)
        set_reauthentication_provider(provider)

        @requires_reauthentication()
        def view(request):
            from django.http import HttpResponse

            return HttpResponse("inside")

        response = view(rf.get("/x"))
        assert response.status_code == 200
        assert response.content == b"inside"
        assert provider.check_calls == 1
        assert provider.on_required_calls == 0  # hook not fired when allowed

    def test_provider_requires_returns_reauth_response(self, rf):
        provider = _CountingProvider(requires=True)
        set_reauthentication_provider(provider)

        @requires_reauthentication()
        def view(request):
            raise AssertionError("view must not be called when reauth required")

        response = view(rf.get("/x"))
        assert response.status_code == 403
        assert provider.on_required_calls == 1
        assert provider.response_calls == 1

    def test_provider_exception_fails_secure_with_403(self, rf):
        provider = _CountingProvider(raise_check=True)
        set_reauthentication_provider(provider)

        @requires_reauthentication()
        def view(request):
            raise AssertionError("view must not be called on FAIL-SECURE path")

        response = view(rf.get("/x"))
        assert response.status_code == 403
        assert b"reauthentication_check_failed" in response.content

    def test_wraps_preserves_view_name(self, rf):
        @requires_reauthentication()
        def my_view(request):
            from django.http import HttpResponse

            return HttpResponse("ok")

        assert my_view.__name__ == "my_view"


# =============================================================================
# RequiresReauthenticationPermission — DRF permission class
# =============================================================================


class TestRequiresReauthenticationPermission:
    def test_returns_true_when_provider_allows(self, rf):
        set_reauthentication_provider(_CountingProvider(requires=False))
        perm = RequiresReauthenticationPermission()
        assert perm.has_permission(rf.get("/x"), view=None) is True

    def test_returns_false_when_provider_requires(self, rf):
        provider = _CountingProvider(requires=True)
        set_reauthentication_provider(provider)
        perm = RequiresReauthenticationPermission()
        assert perm.has_permission(rf.get("/x"), view=None) is False
        assert provider.on_required_calls == 1

    def test_returns_false_on_provider_exception(self, rf):
        set_reauthentication_provider(_CountingProvider(raise_check=True))
        perm = RequiresReauthenticationPermission()
        # FAIL-SECURE: any error → False
        assert perm.has_permission(rf.get("/x"), view=None) is False

    def test_reads_settings_for_config_overrides(self, rf):
        captured: list[ReauthenticationConfig] = []

        class _Capturing(_CountingProvider):
            def check_reauthentication_required(self, request, config):  # type: ignore[override]
                captured.append(config)
                return False

        set_reauthentication_provider(_Capturing())

        from django.test import override_settings

        with override_settings(
            BALDUR_REAUTH_MAX_IDLE_MINUTES=7,
            BALDUR_REAUTH_MAX_SESSION_MINUTES=11,
            BALDUR_REAUTH_ENABLED=False,
        ):
            RequiresReauthenticationPermission().has_permission(rf.get("/x"), view=None)

        assert captured[0].max_idle_minutes == 7
        assert captured[0].max_session_minutes == 11
        assert captured[0].enabled is False
