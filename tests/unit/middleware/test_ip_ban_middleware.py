"""
IPBanMiddleware unit tests.

Loads ip_ban.py directly via importlib (no Django settings required).
"""

from __future__ import annotations

import json
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from unittest.mock import MagicMock, Mock, patch

import pytest

# ============================================================
# ip_ban.py direct module load (bypass Django dependency)
# ============================================================
# baldur.api.django.middleware.__init__.py imports other middleware
# and initializes Django, so ip_ban.py is loaded in isolation.
# Same pattern as test_response_meta_region.py.


def _load_ip_ban_module():
    """Load only the ip_ban.py module directly, without Django dependencies."""
    ip_ban_path = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "src",
            "baldur",
            "api",
            "django",
            "middleware",
            "ip_ban.py",
        )
    )

    spec = spec_from_file_location("baldur.api.django.middleware.ip_ban", ip_ban_path)
    module = module_from_spec(spec)
    sys.modules["baldur.api.django.middleware.ip_ban"] = module
    spec.loader.exec_module(module)
    return module


try:
    _ip_ban_module = _load_ip_ban_module()
    IPBanMiddleware = _ip_ban_module.IPBanMiddleware
    _MODULE_LOADED = True
except Exception as e:
    _MODULE_LOADED = False
    _LOAD_ERROR = str(e)


@pytest.mark.skipif(
    not _MODULE_LOADED,
    reason=f"ip_ban module load failed: {_LOAD_ERROR if not _MODULE_LOADED else ''}",
)
class TestIPBanMiddlewareBehavior:
    """IPBanMiddleware behavior verification tests."""

    def _make_middleware(self, ban_info=None, cache_error=False):
        """Test middleware factory.

        Uses the direct-mock-injection pattern.
        """
        mock_response = Mock()
        mock_response.status_code = 200
        middleware = IPBanMiddleware(get_response=lambda r: mock_response)

        mock_cache = MagicMock()
        if cache_error:
            mock_cache.get.side_effect = Exception("Redis down")
        else:
            mock_cache.get.return_value = ban_info

        middleware._cache = mock_cache
        middleware._initialized = True
        return middleware, mock_response

    def _make_request(self, path="/api/test/", ip="192.168.1.100"):
        """Create a test request Mock."""
        request = MagicMock()
        request.path = path
        request.META = {
            "HTTP_X_FORWARDED_FOR": ip,
            "REMOTE_ADDR": ip,
        }
        return request

    # =================================================================
    # Banned IP blocking behavior
    # =================================================================

    def test_temporary_banned_ip_returns_403(self):
        """Temporarily banned IP -> 403 response + IP_BANNED code."""
        middleware, _ = self._make_middleware(
            ban_info={"banned": True, "type": "temporary"}
        )
        request = self._make_request()

        response = middleware(request)

        assert response.status_code == 403
        body = json.loads(response.content)
        assert body["code"] == "IP_BANNED"
        assert body["error"] == "Access denied"

    def test_permanent_banned_ip_returns_403(self):
        """Permanently banned IP -> 403 response."""
        middleware, _ = self._make_middleware(
            ban_info={"banned": True, "type": "permanent"}
        )
        request = self._make_request()

        response = middleware(request)

        assert response.status_code == 403

    # =================================================================
    # Normal pass-through behavior
    # =================================================================

    def test_non_banned_ip_passes_through(self):
        """Non-banned IP -> returns the get_response result."""
        middleware, mock_response = self._make_middleware(ban_info=None)
        request = self._make_request()

        response = middleware(request)

        assert response is mock_response

    def test_banned_false_passes_through(self):
        """banned=False -> returns the get_response result."""
        middleware, mock_response = self._make_middleware(
            ban_info={"banned": False, "type": "temporary"}
        )
        request = self._make_request()

        response = middleware(request)

        assert response is mock_response

    def test_non_dict_cache_value_passes_through(self):
        """Cache value is not a dict -> returns the get_response result."""
        middleware, mock_response = self._make_middleware(ban_info="invalid_string")
        request = self._make_request()

        response = middleware(request)

        assert response is mock_response

    # =================================================================
    # Health-check path exemption behavior
    # =================================================================

    def test_health_path_exempt_from_ban(self):
        """/health/ path -> passes through regardless of ban status."""
        middleware, mock_response = self._make_middleware(
            ban_info={"banned": True, "type": "permanent"}
        )
        request = self._make_request(path="/health/l3/")

        response = middleware(request)

        assert response is mock_response
        middleware._cache.get.assert_not_called()

    def test_readiness_path_exempt_from_ban(self):
        """/readiness/ path -> passes through regardless of ban status."""
        middleware, mock_response = self._make_middleware(
            ban_info={"banned": True, "type": "permanent"}
        )
        request = self._make_request(path="/readiness/")

        response = middleware(request)

        assert response is mock_response
        middleware._cache.get.assert_not_called()

    def test_liveness_path_exempt_from_ban(self):
        """/liveness/ path -> passes through regardless of ban status."""
        middleware, mock_response = self._make_middleware(
            ban_info={"banned": True, "type": "permanent"}
        )
        request = self._make_request(path="/liveness/")

        response = middleware(request)

        assert response is mock_response
        middleware._cache.get.assert_not_called()

    # =================================================================
    # Fail-Open behavior
    # =================================================================

    def test_redis_exception_allows_request(self):
        """Redis failure (exception raised) -> request allowed (Fail-Open)."""
        middleware, mock_response = self._make_middleware(cache_error=True)
        request = self._make_request()

        response = middleware(request)

        assert response is mock_response

    def test_cache_none_allows_request(self):
        """Cache is None -> request allowed (Fail-Open)."""
        mock_response = Mock()
        middleware = IPBanMiddleware(get_response=lambda r: mock_response)
        middleware._cache = None
        middleware._initialized = True

        request = self._make_request()
        response = middleware(request)

        assert response is mock_response

    # =================================================================
    # Security behavior
    # =================================================================

    def test_403_response_hides_ban_type(self):
        """403 response body excludes ban_type (prevents leaking info to attackers)."""
        middleware, _ = self._make_middleware(
            ban_info={"banned": True, "type": "permanent"}
        )
        request = self._make_request()

        response = middleware(request)

        body = json.loads(response.content)
        assert "type" not in body
        assert "ban_type" not in body
        assert "permanent" not in json.dumps(body)
        assert "temporary" not in json.dumps(body)

    def test_ban_type_written_to_log(self):
        """ban_type is recorded via logger.warning."""
        middleware, _ = self._make_middleware(
            ban_info={"banned": True, "type": "temporary"}
        )
        request = self._make_request(path="/api/test/")

        with patch.object(_ip_ban_module, "logger") as mock_logger:
            middleware(request)

        mock_logger.warning.assert_called_once()
        log_message = mock_logger.warning.call_args[0][0]
        assert log_message == "ip_ban_middleware.blocked_banned_ip"
        assert mock_logger.warning.call_args[1]["ban_type"] == "temporary"

    # =================================================================
    # IP extraction behavior
    # =================================================================

    def test_cache_key_uses_extracted_ip(self):
        """The IP extracted by extract_client_ip is used in the cache key."""
        middleware, _ = self._make_middleware(
            ban_info={"banned": True, "type": "temporary"}
        )
        request = self._make_request(ip="10.0.0.1")

        middleware(request)

        middleware._cache.get.assert_called_once_with("security:banned_ip:10.0.0.1")

    def test_x_forwarded_for_first_ip_used(self):
        """X-Forwarded-For with multiple IPs -> the first IP is used in the cache key."""
        middleware, _ = self._make_middleware(
            ban_info={"banned": True, "type": "temporary"}
        )
        request = MagicMock()
        request.path = "/api/test/"
        request.META = {
            "HTTP_X_FORWARDED_FOR": "1.2.3.4, 5.6.7.8",
            "REMOTE_ADDR": "127.0.0.1",
        }

        middleware(request)

        middleware._cache.get.assert_called_once_with("security:banned_ip:1.2.3.4")

    def test_non_exempt_path_triggers_ban_check(self):
        """Non-exempt path -> cache.get is called."""
        middleware, _ = self._make_middleware(ban_info=None)
        request = self._make_request(path="/api/orders/")

        middleware(request)

        middleware._cache.get.assert_called_once()

    # =================================================================
    # Lazy initialization behavior
    # =================================================================

    def test_lazy_init_skips_when_already_initialized(self):
        """_initialized=True -> _lazy_init internal logic does not run."""
        middleware = IPBanMiddleware(get_response=lambda r: Mock())
        middleware._initialized = True
        middleware._cache = MagicMock()
        middleware._config = MagicMock()

        # Calling _lazy_init when already initialized does not retry config/cache loading
        original_cache = middleware._cache
        original_config = middleware._config
        middleware._lazy_init()

        assert middleware._cache is original_cache
        assert middleware._config is original_config

    def test_get_cache_retries_on_none(self):
        """_cache=None -> _get_cache() retries via ProviderRegistry."""
        middleware = IPBanMiddleware(get_response=lambda r: Mock())
        middleware._cache = None
        middleware._initialized = True

        mock_cache = MagicMock()
        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_cache.return_value = mock_cache
            result = middleware._get_cache()

        assert result is mock_cache

    def test_get_cache_returns_existing_cache(self):
        """_cache already exists -> returned immediately (ProviderRegistry not called)."""
        middleware = IPBanMiddleware(get_response=lambda r: Mock())
        existing_cache = MagicMock()
        middleware._cache = existing_cache

        result = middleware._get_cache()

        assert result is existing_cache

    # =================================================================
    # Key prefix behavior
    # =================================================================

    def test_prefix_fallback_matches_security_config_default(self):
        """config=None -> fallback prefix equals the SecurityConfig default."""
        from baldur.services.security.models import SecurityConfig

        middleware = IPBanMiddleware(get_response=lambda r: Mock())
        middleware._config = None

        default_config = SecurityConfig()
        assert (
            middleware._get_banned_ip_prefix() == default_config.banned_ip_cache_prefix
        )

    def test_prefix_from_injected_config(self):
        """config present -> config.banned_ip_cache_prefix is used."""
        middleware = IPBanMiddleware(get_response=lambda r: Mock())
        mock_config = MagicMock()
        mock_config.banned_ip_cache_prefix = "custom:prefix:"
        middleware._config = mock_config

        assert middleware._get_banned_ip_prefix() == "custom:prefix:"


@pytest.mark.skipif(
    not _MODULE_LOADED,
    reason=f"ip_ban module load failed: {_LOAD_ERROR if not _MODULE_LOADED else ''}",
)
class TestIPBanMiddlewareContract:
    """IPBanMiddleware design contract verification tests."""

    def test_exempt_path_prefixes_includes_health(self):
        """/health/ is included in the exempt paths."""
        assert "/health/" in IPBanMiddleware.EXEMPT_PATH_PREFIXES

    def test_exempt_path_prefixes_includes_readiness(self):
        """/readiness/ is included in the exempt paths."""
        assert "/readiness/" in IPBanMiddleware.EXEMPT_PATH_PREFIXES

    def test_exempt_path_prefixes_includes_liveness(self):
        """/liveness/ is included in the exempt paths."""
        assert "/liveness/" in IPBanMiddleware.EXEMPT_PATH_PREFIXES

    def test_exempt_path_prefixes_count(self):
        """There are exactly 3 exempt paths."""
        assert len(IPBanMiddleware.EXEMPT_PATH_PREFIXES) == 3

    def test_default_prefix_value(self):
        """config=None fallback prefix is 'security:banned_ip:'."""
        middleware = IPBanMiddleware(get_response=lambda r: Mock())
        middleware._config = None
        assert middleware._get_banned_ip_prefix() == "security:banned_ip:"
