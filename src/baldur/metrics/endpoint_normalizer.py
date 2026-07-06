"""
HTTP Endpoint normalization.

Replaces path parameters with patterns to limit metric cardinality.
Uses Django URL resolver first, falls back to regex-based normalization.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict

import structlog

logger = structlog.get_logger()

__all__ = [
    "normalize_endpoint",
    "get_endpoint_normalizer",
    "reset_endpoint_normalizer",
]


# Default normalization patterns (fallback when Django URL resolver is unavailable)
# Note: hex hash pattern excluded from defaults due to false positive risk
# with English words like /api/cafe/, /api/facade/. Opt-in via custom_patterns.
_DEFAULT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # UUID: /api/items/550e8400-e29b-41d4-a716-446655440000 -> /api/items/{uuid}
    (
        re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
        "/{uuid}",
    ),
    # Numeric ID: /api/users/123 -> /api/users/{id}
    (re.compile(r"/\d+"), "/{id}"),
]

# Paths excluded from normalization (health checks, etc.)
_EXCLUDED_PREFIXES = frozenset(
    {
        "/health",
        "/ready",
        "/metrics",
        "/favicon.ico",
    }
)


class EndpointNormalizer:
    """
    HTTP endpoint normalizer.

    Uses Django URL resolver when available, otherwise falls back to
    regex-based path parameter replacement.
    """

    def __init__(
        self,
        custom_patterns: list[tuple[str, str]] | None = None,
        use_django_resolver: bool = True,
        max_distinct_endpoints: int = 500,
        cache_size: int = 2048,
    ):
        self._use_django_resolver = use_django_resolver
        self._max_distinct = max_distinct_endpoints

        # path -> normalized cache (LRU, O(1) return)
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_size = cache_size
        self._cache_lock = threading.Lock()

        # Distinct endpoint tracking (LRU, thread-safe)
        self._seen_endpoints: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()

        # Custom patterns applied first
        self._patterns: list[tuple[re.Pattern, str]] = []
        if custom_patterns:
            for pattern_str, replacement in custom_patterns:
                self._patterns.append((re.compile(pattern_str), replacement))
        self._patterns.extend(_DEFAULT_PATTERNS)

    def normalize(self, path: str, request: object | None = None) -> str:
        """
        Normalize an HTTP path.

        Args:
            path: Raw HTTP path (e.g., "/api/users/123/orders/456")
            request: Django HttpRequest (optional, for resolver)

        Returns:
            Normalized path (e.g., "/api/users/{id}/orders/{id}")
        """
        for prefix in _EXCLUDED_PREFIXES:
            if path.startswith(prefix):
                return path

        # Cache hit -> O(1) immediate return
        with self._cache_lock:
            cached = self._cache.get(path)
            if cached is not None:
                self._cache.move_to_end(path)
                return cached

        # Cache miss -> perform normalization
        result = self._resolve_and_normalize(path, request)

        # Store in cache (LRU eviction)
        with self._cache_lock:
            if len(self._cache) >= self._cache_size:
                self._cache.popitem(last=False)
            self._cache[path] = result
        return result

    def _resolve_and_normalize(self, path: str, request: object | None) -> str:
        """Normalize via Django resolve or regex fallback."""
        # Primary: Django URL resolver
        if self._use_django_resolver:
            resolved = self._try_django_resolve(path, request)
            if resolved:
                return self._track_and_bound(resolved)
            # Resolve failure = not a registered URL pattern -> scan attack defense
            return "UNMATCHED_ROUTE"

        # Regex fallback (non-Django environments only)
        normalized = path
        for pattern, replacement in self._patterns:
            normalized = pattern.sub(replacement, normalized)

        return self._track_and_bound(normalized)

    def _try_django_resolve(self, path: str, request: object | None) -> str | None:
        """Extract URL pattern via Django URL resolver."""
        try:
            from django.urls import resolve

            match = resolve(path)
            if match and match.route:
                return "/" + match.route.lstrip("/")
        except Exception:
            pass
        return None

    def _track_and_bound(self, endpoint: str) -> str:
        """
        Track distinct endpoint count (LRU, thread-safe).

        When max_distinct is exceeded, evicts the oldest endpoint and registers the new one.
        Only Django-resolved paths enter here, so scan attacks cannot pollute _seen_endpoints.
        """
        with self._seen_lock:
            if endpoint in self._seen_endpoints:
                self._seen_endpoints.move_to_end(endpoint)
                return endpoint

            if len(self._seen_endpoints) >= self._max_distinct:
                evicted, _ = self._seen_endpoints.popitem(last=False)
                logger.debug(
                    "metrics.endpoint_evicted",
                    endpoint=evicted,
                )

            self._seen_endpoints[endpoint] = None
            return endpoint


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_normalizer: EndpointNormalizer | None = None
_normalizer_lock = threading.Lock()


def get_endpoint_normalizer() -> EndpointNormalizer:
    """Return singleton EndpointNormalizer."""
    global _normalizer
    if _normalizer is None:
        with _normalizer_lock:
            if _normalizer is None:
                try:
                    from baldur.settings.layered_provider import (
                        get_layered_settings,
                    )
                    from baldur.settings.metrics import MetricsSettings

                    # Layered read so a console edit of the metrics domain takes
                    # effect at next normalizer build (686 D1/D5); env base when
                    # no RuntimeConfigManager is registered.
                    settings = get_layered_settings(MetricsSettings, "metrics")
                    max_endpoints = settings.max_distinct_endpoints
                    cache_size = settings.endpoint_cache_size
                except Exception:
                    logger.debug("metrics.endpoint_normalizer_settings_fallback")
                    max_endpoints = 500
                    cache_size = 2048
                _normalizer = EndpointNormalizer(
                    max_distinct_endpoints=max_endpoints,
                    cache_size=cache_size,
                )
    return _normalizer


def reset_endpoint_normalizer() -> None:
    """Reset singleton (for testing)."""
    global _normalizer
    _normalizer = None


def normalize_endpoint(path: str, request: object | None = None) -> str:
    """Convenience function."""
    return get_endpoint_normalizer().normalize(path, request)
