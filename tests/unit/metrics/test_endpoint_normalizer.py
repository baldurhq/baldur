"""
EndpointNormalizer unit tests.

Tests HTTP endpoint normalization for metric cardinality control.

Reference:
    docs/baldur/middleware_system/332_METRIC_CARDINALITY_GUARD.md §3.1, §7
    src/baldur/metrics/endpoint_normalizer.py
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from baldur.metrics.endpoint_normalizer import (
    _DEFAULT_PATTERNS,
    _EXCLUDED_PREFIXES,
    EndpointNormalizer,
    get_endpoint_normalizer,
    normalize_endpoint,
    reset_endpoint_normalizer,
)

# =============================================================================
# Contract Tests
# =============================================================================


class TestEndpointNormalizerContract:
    """Design contract verification for EndpointNormalizer constants and defaults."""

    def test_default_patterns_count(self):
        """Default patterns: exactly 2 (UUID, numeric ID)."""
        assert len(_DEFAULT_PATTERNS) == 2

    def test_default_patterns_uuid_replacement(self):
        """First default pattern replaces UUID with {uuid}."""
        _, replacement = _DEFAULT_PATTERNS[0]
        assert replacement == "/{uuid}"

    def test_default_patterns_numeric_id_replacement(self):
        """Second default pattern replaces numeric ID with {id}."""
        _, replacement = _DEFAULT_PATTERNS[1]
        assert replacement == "/{id}"

    def test_excluded_prefixes_contract(self):
        """Excluded prefixes: /health, /ready, /metrics, /favicon.ico."""
        assert _EXCLUDED_PREFIXES == frozenset(
            {
                "/health",
                "/ready",
                "/metrics",
                "/favicon.ico",
            }
        )

    def test_excluded_prefixes_count(self):
        """Exactly 4 excluded prefixes."""
        assert len(_EXCLUDED_PREFIXES) == 4

    def test_default_max_distinct_endpoints(self):
        """Default max_distinct_endpoints is 500."""
        normalizer = EndpointNormalizer(use_django_resolver=False)
        assert normalizer._max_distinct == 500

    def test_default_cache_size(self):
        """Default cache_size is 2048."""
        normalizer = EndpointNormalizer(use_django_resolver=False)
        assert normalizer._cache_size == 2048

    def test_hex_hash_not_in_default_patterns(self):
        """Hex hash pattern excluded from defaults to avoid false positives."""
        normalizer = EndpointNormalizer(use_django_resolver=False)
        # /api/cafe/ should NOT be normalized (cafe looks like hex)
        result = normalizer.normalize("/api/cafe")
        assert result == "/api/cafe"

    def test_unmatched_route_constant(self):
        """Django resolve failure returns 'UNMATCHED_ROUTE' string."""
        normalizer = EndpointNormalizer(use_django_resolver=True)
        with patch.object(normalizer, "_try_django_resolve", return_value=None):
            result = normalizer.normalize("/random/scan/path")
        assert result == "UNMATCHED_ROUTE"


# =============================================================================
# Behavior Tests — Regex Fallback Normalization
# =============================================================================


class TestEndpointNormalizerRegexBehavior:
    """Behavior verification for regex-based endpoint normalization."""

    def setup_method(self):
        """Create normalizer with Django resolver disabled."""
        self._normalizer = EndpointNormalizer(use_django_resolver=False)

    def test_numeric_id_replaced(self):
        """/api/users/123 → /api/users/{id}."""
        result = self._normalizer.normalize("/api/users/123")
        assert result == "/api/users/{id}"

    def test_uuid_replaced(self):
        """UUID in path is replaced with {uuid}."""
        result = self._normalizer.normalize(
            "/api/items/550e8400-e29b-41d4-a716-446655440000"
        )
        assert result == "/api/items/{uuid}"

    def test_multiple_params_in_path(self):
        """/api/users/1/orders/2 → /api/users/{id}/orders/{id}."""
        result = self._normalizer.normalize("/api/users/1/orders/2")
        assert result == "/api/users/{id}/orders/{id}"

    def test_uuid_and_numeric_in_same_path(self):
        """Mixed UUID and numeric ID in one path."""
        result = self._normalizer.normalize(
            "/api/items/550e8400-e29b-41d4-a716-446655440000/reviews/42"
        )
        assert result == "/api/items/{uuid}/reviews/{id}"

    def test_path_without_params_unchanged(self):
        """Static path without parameters is unchanged."""
        result = self._normalizer.normalize("/api/users/list")
        assert result == "/api/users/list"

    def test_root_path_unchanged(self):
        """Root path '/' is unchanged."""
        result = self._normalizer.normalize("/")
        assert result == "/"

    def test_custom_patterns_take_priority(self):
        """Custom patterns are applied before default patterns."""
        custom = [
            (r"/slug-[a-z]+-[a-z]+", "/{slug}"),
        ]
        normalizer = EndpointNormalizer(
            custom_patterns=custom,
            use_django_resolver=False,
        )
        result = normalizer.normalize("/api/slug-hello-world")
        assert result == "/api/{slug}"

    def test_hex_hash_via_custom_patterns(self):
        """Hex hash can be opt-in via custom_patterns."""
        custom = [
            (r"/[0-9a-f]{6,40}", "/{hash}"),
        ]
        normalizer = EndpointNormalizer(
            custom_patterns=custom,
            use_django_resolver=False,
        )
        result = normalizer.normalize("/api/abc123def456")
        assert result == "/api/{hash}"


# =============================================================================
# Behavior Tests — Excluded Paths
# =============================================================================


class TestEndpointNormalizerExcludedPathsBehavior:
    """Behavior verification: excluded prefixes bypass normalization."""

    def setup_method(self):
        self._normalizer = EndpointNormalizer(use_django_resolver=False)

    def test_health_path_unchanged(self):
        """/health is excluded from normalization."""
        assert self._normalizer.normalize("/health") == "/health"

    def test_health_subpath_unchanged(self):
        """/health/live is excluded (prefix match)."""
        assert self._normalizer.normalize("/health/live") == "/health/live"

    def test_ready_path_unchanged(self):
        """/ready is excluded from normalization."""
        assert self._normalizer.normalize("/ready") == "/ready"

    def test_metrics_path_unchanged(self):
        """/metrics is excluded from normalization."""
        assert self._normalizer.normalize("/metrics") == "/metrics"

    def test_favicon_path_unchanged(self):
        """/favicon.ico is excluded from normalization."""
        assert self._normalizer.normalize("/favicon.ico") == "/favicon.ico"

    def test_non_excluded_path_is_normalized(self):
        """/api/users/123 is NOT excluded, so it gets normalized."""
        result = self._normalizer.normalize("/api/users/123")
        assert result == "/api/users/{id}"


# =============================================================================
# Behavior Tests — Django Resolver
# =============================================================================


class TestEndpointNormalizerDjangoResolverBehavior:
    """Behavior verification for Django URL resolver path."""

    def test_django_resolve_success_uses_route_pattern(self):
        """When Django resolve succeeds, uses match.route as normalized path."""
        normalizer = EndpointNormalizer(use_django_resolver=True)
        mock_match = MagicMock()
        mock_match.route = "api/users/<int:pk>/"

        with patch.object(
            normalizer, "_try_django_resolve", return_value="/api/users/<int:pk>/"
        ):
            result = normalizer.normalize("/api/users/123")
        assert result == "/api/users/<int:pk>/"

    def test_django_resolve_failure_returns_unmatched_route(self):
        """When Django resolve fails, returns UNMATCHED_ROUTE (scan attack defense)."""
        normalizer = EndpointNormalizer(use_django_resolver=True)
        with patch.object(normalizer, "_try_django_resolve", return_value=None):
            result = normalizer.normalize("/random/nonexistent/path")
        assert result == "UNMATCHED_ROUTE"

    def test_scan_attack_does_not_pollute_seen_endpoints(self):
        """Scan attack paths (resolve failure) do NOT enter _seen_endpoints."""
        normalizer = EndpointNormalizer(
            use_django_resolver=True,
            max_distinct_endpoints=5,
        )
        with patch.object(normalizer, "_try_django_resolve", return_value=None):
            for i in range(100):
                normalizer.normalize(f"/scan/attempt/{i}")

        # _seen_endpoints should be empty — UNMATCHED_ROUTE bypasses tracking
        assert len(normalizer._seen_endpoints) == 0

    def test_try_django_resolve_returns_none_without_django(self):
        """_try_django_resolve returns None when Django is not available."""
        normalizer = EndpointNormalizer(use_django_resolver=True)
        with patch(
            "baldur.metrics.endpoint_normalizer.resolve",
            side_effect=ImportError("No module named 'django'"),
            create=True,
        ):
            # The method catches all exceptions
            result = normalizer._try_django_resolve("/api/test", None)
        assert result is None


# =============================================================================
# Behavior Tests — Cache
# =============================================================================


class TestEndpointNormalizerCacheBehavior:
    """Behavior verification for LRU cache."""

    def test_cache_hit_returns_without_resolve(self):
        """Cached path returns immediately without re-resolving."""
        normalizer = EndpointNormalizer(use_django_resolver=False)

        # First call: populates cache
        result1 = normalizer.normalize("/api/users/123")
        assert result1 == "/api/users/{id}"

        # Second call: should use cache
        result2 = normalizer.normalize("/api/users/123")
        assert result2 == "/api/users/{id}"
        assert "/api/users/123" in normalizer._cache

    def test_cache_lru_eviction(self):
        """When cache_size exceeded, oldest entry is evicted."""
        normalizer = EndpointNormalizer(
            use_django_resolver=False,
            cache_size=3,
        )

        # Fill cache to capacity
        normalizer.normalize("/api/a/1")
        normalizer.normalize("/api/b/2")
        normalizer.normalize("/api/c/3")
        assert len(normalizer._cache) == 3

        # One more → evicts oldest (/api/a/1)
        normalizer.normalize("/api/d/4")
        assert len(normalizer._cache) == 3
        assert "/api/a/1" not in normalizer._cache
        assert "/api/d/4" in normalizer._cache

    def test_cache_hit_moves_to_end(self):
        """Cache hit moves entry to end (LRU refresh)."""
        normalizer = EndpointNormalizer(
            use_django_resolver=False,
            cache_size=3,
        )

        normalizer.normalize("/api/a/1")
        normalizer.normalize("/api/b/2")
        normalizer.normalize("/api/c/3")

        # Access oldest entry → moves to end
        normalizer.normalize("/api/a/1")

        # Now /api/b/2 is oldest; adding new entry should evict it
        normalizer.normalize("/api/d/4")
        assert "/api/a/1" in normalizer._cache  # was refreshed
        assert "/api/b/2" not in normalizer._cache  # evicted as oldest

    def test_cache_stores_correct_normalized_value(self):
        """Cache maps raw path to normalized result."""
        normalizer = EndpointNormalizer(use_django_resolver=False)
        normalizer.normalize("/api/users/999")

        assert normalizer._cache["/api/users/999"] == "/api/users/{id}"


# =============================================================================
# Behavior Tests — Seen Endpoints LRU
# =============================================================================


class TestEndpointNormalizerSeenEndpointsBehavior:
    """Behavior verification for distinct endpoint tracking with LRU."""

    def test_seen_endpoints_lru_eviction(self):
        """When max_distinct exceeded, oldest endpoint is evicted (not replaced by OTHER)."""
        normalizer = EndpointNormalizer(
            use_django_resolver=False,
            max_distinct_endpoints=3,
            cache_size=100,
        )

        # Register 3 distinct endpoints
        normalizer.normalize("/api/a")
        normalizer.normalize("/api/b")
        normalizer.normalize("/api/c")
        assert len(normalizer._seen_endpoints) == 3

        # 4th endpoint → evicts oldest (/api/a)
        normalizer.normalize("/api/d")
        assert len(normalizer._seen_endpoints) == 3
        assert "/api/a" not in normalizer._seen_endpoints
        assert "/api/d" in normalizer._seen_endpoints

    def test_seen_endpoints_refresh_on_access(self):
        """Accessing an existing endpoint refreshes its LRU position."""
        normalizer = EndpointNormalizer(
            use_django_resolver=False,
            max_distinct_endpoints=3,
            cache_size=100,
        )

        normalizer.normalize("/api/a")
        normalizer.normalize("/api/b")
        normalizer.normalize("/api/c")

        # Refresh /api/a by normalizing a path that maps to it
        # We need to use cache_size large enough and a new raw path
        # that normalizes to /api/a. Since /api/a is static, access it directly.
        normalizer._cache.clear()  # force re-normalization
        normalizer.normalize("/api/a")

        # Now /api/b is oldest; adding new should evict it
        normalizer.normalize("/api/d")
        assert "/api/a" in normalizer._seen_endpoints
        assert "/api/b" not in normalizer._seen_endpoints

    def test_seen_endpoints_returns_endpoint_not_other(self):
        """LRU eviction returns the new endpoint, not a fallback like OTHER."""
        normalizer = EndpointNormalizer(
            use_django_resolver=False,
            max_distinct_endpoints=2,
            cache_size=100,
        )

        normalizer.normalize("/api/a")
        normalizer.normalize("/api/b")

        # 3rd endpoint triggers eviction, but still returns the new endpoint
        result = normalizer.normalize("/api/c")
        assert result == "/api/c"

    def test_eviction_logs_debug_message(self):
        """LRU eviction emits metrics.endpoint_evicted debug log."""
        normalizer = EndpointNormalizer(
            use_django_resolver=False,
            max_distinct_endpoints=1,
            cache_size=100,
        )

        normalizer.normalize("/api/a")
        with patch("baldur.metrics.endpoint_normalizer.logger") as mock_logger:
            normalizer._cache.clear()
            normalizer.normalize("/api/b")
            mock_logger.debug.assert_called_with(
                "metrics.endpoint_evicted",
                endpoint="/api/a",
            )


# =============================================================================
# Behavior Tests — Singleton & Lifecycle
# =============================================================================


class TestEndpointNormalizerSingletonBehavior:
    """Behavior verification for singleton get/reset lifecycle."""

    def setup_method(self):
        reset_endpoint_normalizer()

    def teardown_method(self):
        reset_endpoint_normalizer()

    def test_get_returns_same_instance(self):
        """get_endpoint_normalizer() returns the same instance."""
        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(
                max_distinct_endpoints=500,
                endpoint_cache_size=2048,
            )
            first = get_endpoint_normalizer()
            second = get_endpoint_normalizer()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset_endpoint_normalizer() → next call creates new instance."""
        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(
                max_distinct_endpoints=500,
                endpoint_cache_size=2048,
            )
            first = get_endpoint_normalizer()
            reset_endpoint_normalizer()
            second = get_endpoint_normalizer()
        assert first is not second

    def test_singleton_settings_fallback(self):
        """When settings loading fails, falls back to default 500/2048."""
        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
            side_effect=Exception("settings unavailable"),
        ):
            normalizer = get_endpoint_normalizer()
        assert normalizer._max_distinct == 500
        assert normalizer._cache_size == 2048

    def test_normalize_endpoint_convenience_function(self):
        """normalize_endpoint() delegates to singleton."""
        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(
                max_distinct_endpoints=500,
                endpoint_cache_size=2048,
            )
            result = normalize_endpoint("/api/users/42")
        # Since Django resolver is True by default and Django is not configured,
        # it will fall through to UNMATCHED_ROUTE or regex fallback
        assert isinstance(result, str)


# =============================================================================
# Behavior Tests — Idempotency
# =============================================================================


class TestEndpointNormalizerIdempotencyBehavior:
    """Behavior verification: same input always produces same output."""

    def test_repeated_normalization_returns_same_result(self):
        """Normalizing the same path N times returns identical results."""
        normalizer = EndpointNormalizer(use_django_resolver=False)
        results = [normalizer.normalize("/api/users/123") for _ in range(10)]
        assert all(r == "/api/users/{id}" for r in results)

    def test_excluded_path_idempotent(self):
        """Excluded paths return identically on repeated calls."""
        normalizer = EndpointNormalizer(use_django_resolver=False)
        results = [normalizer.normalize("/health") for _ in range(10)]
        assert all(r == "/health" for r in results)


# =============================================================================
# Behavior Tests — Thread Safety
# =============================================================================


class TestEndpointNormalizerThreadSafetyBehavior:
    """Behavior verification: concurrent normalize calls don't corrupt data."""

    def test_concurrent_normalize_no_data_corruption(self):
        """10 threads normalizing concurrently produce no errors."""
        normalizer = EndpointNormalizer(
            use_django_resolver=False,
            max_distinct_endpoints=50,
            cache_size=100,
        )
        errors = []

        def worker(thread_id):
            try:
                for i in range(20):
                    path = f"/api/t{thread_id}/item/{i}"
                    result = normalizer.normalize(path)
                    assert isinstance(result, str)
                    assert len(result) > 0
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_concurrent_cache_access_consistent(self):
        """Concurrent access to the same path returns consistent results."""
        normalizer = EndpointNormalizer(use_django_resolver=False)
        results = []

        def worker():
            result = normalizer.normalize("/api/users/42")
            results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == "/api/users/{id}" for r in results)
