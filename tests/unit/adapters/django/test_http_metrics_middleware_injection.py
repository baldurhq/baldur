"""Unit tests for BaldurConfig._inject_http_metrics_middleware_if_enabled (653 D1).

Django emits the OSS HTTP-RED latency histogram
(``baldur_http_request_duration_seconds``) only when ``HttpMetricsMiddleware`` is
present in ``settings.MIDDLEWARE``. The getting-started Django path adds nothing
but ``baldur.adapters.django`` to ``INSTALLED_APPS``, so ``ready()`` auto-injects
the sync middleware to reach Flask/FastAPI's out-of-the-box behavior. These tests
exercise the ``@staticmethod`` in isolation by direct call with ``django.conf.settings``
and ``get_metrics_settings()`` patched (mirrors ``test_admission_middleware_warning.py``).

The session-wide ``BALDUR_TEST_MODE=true`` (``tests/conftest.py``) short-circuits
the gate before any inject, so every positive case must
``monkeypatch.delenv("BALDUR_TEST_MODE", ...)`` — otherwise the gate returns early
and the inject silently never runs (a phantom "never prepends" pass). The gate-off
case asserts the inverse.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.django.apps import BaldurConfig
from baldur.api.django.middleware.http_metrics import (
    AsyncHttpMetricsMiddleware,
    HttpMetricsMiddleware,
)

# Expected dotted paths derived from the real classes (Behavior — not hardcoded):
# if the source literal drifts from the class location, the prepend assertion fails.
_INJECTED_PATH = (
    f"{HttpMetricsMiddleware.__module__}.{HttpMetricsMiddleware.__qualname__}"
)
_ASYNC_LONG_PATH = (
    f"{AsyncHttpMetricsMiddleware.__module__}.{AsyncHttpMetricsMiddleware.__qualname__}"
)
# Short aliases re-exported from the middleware package __init__ (Django's
# import_string resolves both forms to the same class).
_SYNC_SHORT_ALIAS = f"baldur.api.django.middleware.{HttpMetricsMiddleware.__name__}"
_ASYNC_SHORT_ALIAS = (
    f"baldur.api.django.middleware.{AsyncHttpMetricsMiddleware.__name__}"
)

_OTHER_MW = "django.middleware.common.CommonMiddleware"

# Sentinel for "settings has no MIDDLEWARE attribute at all" (the real zero-config
# case — the quickstart settings module declares no MIDDLEWARE block).
_UNSET = object()


class _FakeDjangoSettings:
    """Writable stand-in for ``django.conf.settings`` with a controllable MIDDLEWARE.

    Constructed with no ``middleware`` (the ``_UNSET`` sentinel), the instance has
    no ``MIDDLEWARE`` attribute, so ``getattr(settings, "MIDDLEWARE", None)``
    returns ``None`` — the zero-config case. Replacing the real ``LazySettings``
    keeps the write contained: the function mutates this fake, never global state.
    """

    def __init__(self, middleware=_UNSET):
        if middleware is not _UNSET:
            self.MIDDLEWARE = middleware


class TestHttpMetricsMiddlewareInjectionBehavior:
    """Auto-inject the sync HTTP-RED middleware on the zero-config Django path."""

    @pytest.mark.parametrize(
        ("given", "expected_rest"),
        [
            ([_OTHER_MW], [_OTHER_MW]),
            (_UNSET, []),
            ([], []),
            ((_OTHER_MW,), [_OTHER_MW]),
        ],
        ids=[
            "with_existing",
            "middleware_unset",
            "middleware_empty",
            "middleware_tuple_coerced",
        ],
    )
    def test_inject_prepends_middleware_at_position_zero_preserving_existing(
        self, given, expected_rest, monkeypatch
    ):
        """Path absent -> sync middleware prepended at index 0; existing entries kept.

        Covers the unset, empty, and tuple-typed MIDDLEWARE boundaries — the tuple
        case proves the ``list(...)`` coercion (a bare ``[path] + tuple`` would raise).
        """
        # Given
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        fake = _FakeDjangoSettings(given)

        # When
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch("django.conf.settings", fake),
        ):
            BaldurConfig._inject_http_metrics_middleware_if_enabled()

        # Then
        assert fake.MIDDLEWARE == [_INJECTED_PATH, *expected_rest]
        assert fake.MIDDLEWARE[0] == _INJECTED_PATH
        assert isinstance(fake.MIDDLEWARE, list)

    def test_inject_emits_injected_debug_log_on_success(self, monkeypatch):
        """A successful prepend records the ``..._injected`` debug event."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        fake = _FakeDjangoSettings([])

        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch("django.conf.settings", fake),
            patch("baldur.adapters.django.apps.logger") as mock_logger,
        ):
            BaldurConfig._inject_http_metrics_middleware_if_enabled()

        mock_logger.debug.assert_called_once_with(
            "baldur.http_metrics_middleware_injected"
        )

    @pytest.mark.parametrize(
        "listed_path",
        [_INJECTED_PATH, _SYNC_SHORT_ALIAS, _ASYNC_LONG_PATH, _ASYNC_SHORT_ALIAS],
        ids=["sync_long", "sync_short_alias", "async_long", "async_short_alias"],
    )
    def test_inject_idempotent_when_red_middleware_already_listed(
        self, listed_path, monkeypatch
    ):
        """Either RED class already listed (any of 4 alias forms) -> no prepend.

        The dotted-path-suffix guard catches the long/short sync alias and the
        async variant an ASGI operator may list explicitly, so an existing entry
        never double-records.
        """
        # Given
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        original = [listed_path, _OTHER_MW]
        fake = _FakeDjangoSettings(list(original))

        # When
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch("django.conf.settings", fake),
            patch("baldur.adapters.django.apps.logger") as mock_logger,
        ):
            BaldurConfig._inject_http_metrics_middleware_if_enabled()

        # Then — list unchanged (no prepend, no double-add), inject path not taken
        assert fake.MIDDLEWARE == original
        mock_logger.debug.assert_not_called()

    def test_inject_skipped_when_metrics_disabled(self, monkeypatch):
        """metrics.enabled=False -> the observability off-switch suppresses inject."""
        # Given
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        fake = _FakeDjangoSettings([_OTHER_MW])

        # When
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=MagicMock(enabled=False),
            ),
            patch("django.conf.settings", fake),
            patch("baldur.adapters.django.apps.logger") as mock_logger,
        ):
            BaldurConfig._inject_http_metrics_middleware_if_enabled()

        # Then
        assert fake.MIDDLEWARE == [_OTHER_MW]
        mock_logger.debug.assert_not_called()

    def test_inject_skipped_under_test_mode_before_settings_probe(self, monkeypatch):
        """BALDUR_TEST_MODE=true short-circuits before the metrics probe even runs."""
        # Given
        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        fake = _FakeDjangoSettings([_OTHER_MW])

        # When
        with (
            patch("baldur.settings.metrics.get_metrics_settings") as mock_get_settings,
            patch("django.conf.settings", fake),
        ):
            BaldurConfig._inject_http_metrics_middleware_if_enabled()

        # Then — gated out before the settings read; MIDDLEWARE untouched
        mock_get_settings.assert_not_called()
        assert fake.MIDDLEWARE == [_OTHER_MW]

    def test_inject_best_effort_swallows_exception_and_warns(self, monkeypatch):
        """A wiring failure logs ``..._inject_failed`` and never breaks ready()."""
        # Given
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)

        # When / Then — must not raise
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                side_effect=RuntimeError("boom"),
            ),
            patch("baldur.adapters.django.apps.logger") as mock_logger,
        ):
            BaldurConfig._inject_http_metrics_middleware_if_enabled()

        mock_logger.warning.assert_called_once()
        assert (
            mock_logger.warning.call_args[0][0]
            == "baldur.http_metrics_middleware_inject_failed"
        )
