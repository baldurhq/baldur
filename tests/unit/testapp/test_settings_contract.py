"""Contract tests for testapp Django settings.

Verifies that the minimal Django configuration matches
the design spec in 324_TEST_APP_AND_MIGRATION.md §2.3.

No Django initialization required — settings.py is a plain Python module.
"""

from __future__ import annotations

from tests.testapp import settings


class TestSettingsContract:
    """testapp settings contract values from §2.3."""

    def test_secret_key(self):
        """SECRET_KEY matches design spec."""
        assert settings.SECRET_KEY == "test-secret-key-for-baldur"

    def test_database_engine_is_sqlite(self):
        """Database engine is SQLite for fast test execution."""
        assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"

    def test_database_name_is_memory(self):
        """Database is in-memory for test isolation."""
        assert settings.DATABASES["default"]["NAME"] == ":memory:"

    def test_auth_user_model_is_testapp_user(self):
        """AUTH_USER_MODEL points to testapp.TestUser."""
        assert settings.AUTH_USER_MODEL == "testapp.TestUser"

    def test_root_urlconf(self):
        """ROOT_URLCONF points to testapp URLs."""
        assert settings.ROOT_URLCONF == "tests.testapp.urls"

    def test_installed_apps_contains_baldur_adapter(self):
        """baldur Django adapter is in INSTALLED_APPS."""
        assert "baldur.adapters.django" in settings.INSTALLED_APPS

    def test_installed_apps_contains_testapp(self):
        """testapp itself is in INSTALLED_APPS."""
        assert "tests.testapp" in settings.INSTALLED_APPS

    def test_installed_apps_contains_rest_framework(self):
        """DRF is in INSTALLED_APPS for API views."""
        assert "rest_framework" in settings.INSTALLED_APPS

    def test_core_domains_match_spec(self):
        """BALDUR_CORE_DOMAINS matches §2.3: payment, order, test."""
        assert settings.BALDUR_CORE_DOMAINS == ["payment", "order", "test"]

    def test_auto_middleware_disabled(self):
        """BALDUR_AUTO_CONFIG_MIDDLEWARE is False for manual control in tests."""
        assert settings.BALDUR_AUTO_CONFIG_MIDDLEWARE is False

    def test_middleware_health_bridge_before_baldur(self):
        """HealthBridgeMiddleware precedes BaldurMiddleware per spec."""
        mw = settings.MIDDLEWARE
        hb_idx = mw.index("baldur.api.django.middleware.HealthBridgeMiddleware")
        sh_idx = mw.index("baldur.api.django.middleware.BaldurMiddleware")
        assert hb_idx < sh_idx
