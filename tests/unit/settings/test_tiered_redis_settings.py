"""
TieredRedisSettings Unit Tests.

Tests TieredRedisSettings class and related functions.

Reference: 422 § D5
"""

from unittest.mock import patch


class TestTieredRedisSettingsContract:
    """TieredRedisSettings contract value verification."""

    def test_local_url_default_contract(self):
        """local_url default: redis://localhost:6379/0."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings()
        assert settings.local_url == "redis://localhost:6379/0"

    def test_local_password_default_contract(self):
        """local_password default: None."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings()
        assert settings.local_password is None

    def test_global_password_default_contract(self):
        """global_password default: None."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings()
        assert settings.global_password is None

    def test_env_prefix_contract(self):
        """env_prefix: BALDUR_TIERED_REDIS_."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        assert TieredRedisSettings.model_config["env_prefix"] == "BALDUR_TIERED_REDIS_"

    def test_all_exports_contract(self):
        """__all__: 3 exports."""
        from baldur.settings import tiered_redis

        assert set(tiered_redis.__all__) == {
            "TieredRedisSettings",
            "get_tiered_redis_settings",
            "reset_tiered_redis_settings",
        }


class TestTieredRedisSettingsBehavior:
    """TieredRedisSettings behavior verification."""

    def setup_method(self):
        """Reset singleton before each test."""
        from baldur.settings.tiered_redis import reset_tiered_redis_settings

        reset_tiered_redis_settings()

    def teardown_method(self):
        """Reset singleton after each test."""
        from baldur.settings.tiered_redis import reset_tiered_redis_settings

        reset_tiered_redis_settings()

    def test_global_url_defaults_to_local_url_when_none(self):
        """global_url falls back to local_url when None."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings(
            local_url="redis://local:6379/0",
            global_url=None,
        )
        assert settings.global_url == "redis://local:6379/0"

    def test_global_url_preserves_explicit_value(self):
        """global_url preserved when explicitly set."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings(
            local_url="redis://local:6379/0",
            global_url="redis://global:6379/0",
        )
        assert settings.global_url == "redis://global:6379/0"

    def test_passwords_are_independent(self):
        """local_password and global_password are independent."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings(
            local_password="local_pw",
            global_password="global_pw",
        )
        assert settings.local_password == "local_pw"
        assert settings.global_password == "global_pw"

    def test_singleton_returns_same_instance(self):
        """Singleton returns same instance."""
        from baldur.settings.tiered_redis import (
            get_tiered_redis_settings,
            reset_tiered_redis_settings,
        )

        reset_tiered_redis_settings()
        s1 = get_tiered_redis_settings()
        s2 = get_tiered_redis_settings()
        assert s1 is s2

    def test_reset_clears_singleton(self):
        """New instance created after reset."""
        from baldur.settings.tiered_redis import (
            get_tiered_redis_settings,
            reset_tiered_redis_settings,
        )

        s1 = get_tiered_redis_settings()
        reset_tiered_redis_settings()
        s2 = get_tiered_redis_settings()
        assert s1 is not s2

    def test_env_var_loading(self, monkeypatch):
        """Load settings from environment variables."""
        from baldur.settings.tiered_redis import (
            TieredRedisSettings,
            reset_tiered_redis_settings,
        )

        reset_tiered_redis_settings()
        monkeypatch.setenv("BALDUR_TIERED_REDIS_LOCAL_URL", "redis://env-local:6379/0")
        monkeypatch.setenv(
            "BALDUR_TIERED_REDIS_GLOBAL_URL", "redis://env-global:6379/0"
        )
        monkeypatch.setenv("BALDUR_TIERED_REDIS_LOCAL_PASSWORD", "env_local_pw")

        settings = TieredRedisSettings()

        assert settings.local_url == "redis://env-local:6379/0"
        assert settings.global_url == "redis://env-global:6379/0"
        assert settings.local_password == "env_local_pw"

    def test_reset_idempotent_when_not_initialized(self):
        """No exception when reset called on uninitialized state."""
        from baldur.settings.tiered_redis import reset_tiered_redis_settings

        # Given: Multiple reset calls
        # When/Then: Completes without exception
        reset_tiered_redis_settings()
        reset_tiered_redis_settings()
        reset_tiered_redis_settings()


class TestTieredRedisSettingsSideEffects:
    """TieredRedisSettings side effect verification."""

    def test_single_redis_mode_logs_info(self):
        """Info log output when local_url == global_url."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        with patch("baldur.settings.tiered_redis.logger") as mock_logger:
            TieredRedisSettings(
                local_url="redis://same:6379/0",
                global_url="redis://same:6379/0",
            )

            mock_logger.info.assert_called_once_with(
                "tiered_redis_settings.single_redis_mode",
                hint="LOCAL and GLOBAL Redis are identical",
            )

    def test_single_redis_mode_logs_when_global_defaults_to_local(self):
        """Log output when global_url falls back to local_url."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        with patch("baldur.settings.tiered_redis.logger") as mock_logger:
            TieredRedisSettings(
                local_url="redis://local:6379/0",
                global_url=None,  # defaults to local_url
            )

            mock_logger.info.assert_called_once_with(
                "tiered_redis_settings.single_redis_mode",
                hint="LOCAL and GLOBAL Redis are identical",
            )

    def test_different_urls_no_log(self):
        """No log when local_url != global_url."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        with patch("baldur.settings.tiered_redis.logger") as mock_logger:
            TieredRedisSettings(
                local_url="redis://local:6379/0",
                global_url="redis://global:6379/0",
            )

            mock_logger.info.assert_not_called()


class TestTieredRedisSettingsEdgeCases:
    """TieredRedisSettings edge cases."""

    def test_sentinel_url_supported(self):
        """Sentinel URL format can be stored."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings(
            local_url="redis+sentinel://mymaster@sentinel1:26379,sentinel2:26379/0",
        )
        assert "redis+sentinel://" in settings.local_url

    def test_cluster_url_supported(self):
        """Cluster URL format can be stored."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings(
            local_url="redis+cluster://node1:7000,node2:7000",
        )
        assert "redis+cluster://" in settings.local_url

    def test_empty_password_string_stored_as_is(self):
        """Empty string password stored as empty string."""
        from baldur.settings.tiered_redis import TieredRedisSettings

        settings = TieredRedisSettings(
            local_password="",
        )
        assert settings.local_password == ""
