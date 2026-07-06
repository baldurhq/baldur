"""
Meta-Watchdog URL routing tests.

Tests for Meta-Watchdog endpoints registered in urls.py.
Source code analysis based — no Django configuration needed.
"""

from tests.factories import src_root


class TestMetaWatchdogUrlRouting:
    """Meta-Watchdog URL routing tests — source code analysis based."""

    def _get_urls_source(self):
        """Read the meta-watchdog URL module source code."""
        urls_path = (
            src_root() / "baldur" / "api" / "django" / "urls" / "meta_watchdog.py"
        )

        with open(urls_path, encoding="utf-8") as f:
            return f.read()

    def test_meta_watchdog_views_imported_in_urls(self):
        """urls.py imports MetaWatchdog Views."""
        source = self._get_urls_source()

        # MetaWatchdogLivenessView, MetaWatchdogStatusView import check
        assert "MetaWatchdogLivenessView" in source
        assert "MetaWatchdogStatusView" in source

    def test_meta_watchdog_liveness_url_pattern_exists(self):
        """health/meta-watchdog/ URL path is registered."""
        source = self._get_urls_source()

        # URL pattern check
        assert "health/meta-watchdog" in source
        assert "meta-watchdog-liveness" in source

    def test_meta_watchdog_status_url_pattern_exists(self):
        """meta/status/ URL path is registered."""
        source = self._get_urls_source()

        # URL pattern check
        assert "meta/status" in source
        assert "meta-watchdog-status" in source

    def test_url_patterns_contain_meta_watchdog_paths(self):
        """urlpatterns includes Meta-Watchdog path() calls."""
        source = self._get_urls_source()

        # path() call check (may span multiple lines)
        assert '"health/meta-watchdog/"' in source
        assert '"meta/status/"' in source

    def test_meta_watchdog_views_imported_from_correct_module(self):
        """Views are imported from the meta_watchdog module."""
        source = self._get_urls_source()

        # from ... import statement check
        assert "from baldur.api.django.views.meta_watchdog import" in source


class TestMetaWatchdogViewModule:
    """meta_watchdog.py view module tests — source code analysis based."""

    def _get_view_source(self):
        """Read meta_watchdog.py source code."""
        view_path = (
            src_root() / "baldur" / "api" / "django" / "views" / "meta_watchdog.py"
        )

        with open(view_path, encoding="utf-8") as f:
            return f.read()

    def _get_handler_source(self):
        """Read meta_watchdog handler source code."""
        handler_path = src_root() / "baldur" / "api" / "handlers" / "meta_watchdog.py"

        with open(handler_path, encoding="utf-8") as f:
            return f.read()

    def test_meta_watchdog_liveness_view_class_exists(self):
        """MetaWatchdogLivenessView class is defined."""
        source = self._get_view_source()

        assert "class MetaWatchdogLivenessView" in source

    def test_meta_watchdog_status_view_class_exists(self):
        """MetaWatchdogStatusView class is defined."""
        source = self._get_view_source()

        assert "class MetaWatchdogStatusView" in source

    def test_liveness_view_inherits_from_handler_api_view(self):
        """MetaWatchdogLivenessView inherits from HandlerAPIView."""
        source = self._get_view_source()

        assert "class MetaWatchdogLivenessView(HandlerAPIView)" in source

    def test_status_view_inherits_from_handler_api_view(self):
        """MetaWatchdogStatusView inherits from HandlerAPIView."""
        source = self._get_view_source()

        assert "class MetaWatchdogStatusView(HandlerAPIView)" in source

    def test_liveness_view_has_handler_assigned(self):
        """MetaWatchdogLivenessView has a handler function assigned."""
        source = self._get_view_source()

        # HandlerAPIView delegates to handler functions
        assert "handler = meta_watchdog_liveness" in source

    def test_permission_level_is_public(self):
        """Views are publicly accessible (for K8s Probes)."""
        source = self._get_view_source()

        # HandlerAPIView uses permission_level instead of permission_classes
        assert "permission_level = PermissionLevel.PUBLIC" in source

    def test_handler_uses_watchdog(self):
        """Handler module resolves the watchdog via the OSS->PRO slot."""
        source = self._get_handler_source()

        # 519 PR 2 (c): the legacy `get_selfhealer_watchdog` import was
        # replaced by `ProviderRegistry.selfhealer_watchdog.safe_get()`.
        assert "selfhealer_watchdog" in source
        assert "ProviderRegistry" in source
