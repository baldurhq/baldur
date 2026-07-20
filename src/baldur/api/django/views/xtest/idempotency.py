"""
X-Test-Mode Idempotency Views

APIs for observing idempotency service behavior under X-Test-Mode.

Endpoints:
- POST /api/baldur/xtest/idempotency/generate-key/ - generate a key and preview its hash
- POST /api/baldur/xtest/idempotency/check-duplicate/ - test duplicate request detection
- GET  /api/baldur/xtest/idempotency/status/ - query currently registered key state
- POST /api/baldur/xtest/idempotency/register/ - manually register a key for testing
- POST /api/baldur/xtest/idempotency/clear/ - delete test keys

Components:
- IdempotencyKey: idempotency key generation (entity_type, entity_id, action)
- IdempotencyDomain: domain enum (EXTERNAL_SERVICE, ASYNC_TASK, etc.)
- IdempotencyService: duplicate check and registration service

Security:
- X-Test-Mode: chaos-monkey header required
- DEBUG or the CHAOS_ENABLED environment variable required
- Fully blocked in production environments
"""

from typing import Any

import structlog
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin, collect_system_snapshot

logger = structlog.get_logger()


# Constants identifying keys created by X-Test-Mode
XTEST_SOURCE = "x-test-mode"
XTEST_METADATA_KEY = "idempotency:xtest:keys"  # Tracks keys created by X-Test
DEFAULT_TTL_SECONDS = 3600  # Default TTL of 1 hour
MAX_STATUS_RESULTS = 50  # Maximum number of results in a status query


def _get_xtest_tracked_keys() -> list[str]:
    """Query the list of keys registered by X-Test-Mode."""
    try:
        keys = cache.get(XTEST_METADATA_KEY) or []
        return list(keys)
    except Exception:
        return []


def _track_xtest_key(cache_key: str) -> None:
    """Add a key created by X-Test-Mode to the tracking list."""
    try:
        keys = _get_xtest_tracked_keys()
        if cache_key not in keys:
            keys.append(cache_key)
            cache.set(XTEST_METADATA_KEY, keys, timeout=86400)  # 24 hours
    except Exception as e:
        logger.warning(
            "test.idempotency_failed_track",
            error=e,
        )


def _untrack_xtest_key(cache_key: str) -> None:
    """Remove a key from the tracking list."""
    try:
        keys = _get_xtest_tracked_keys()
        if cache_key in keys:
            keys.remove(cache_key)
            cache.set(XTEST_METADATA_KEY, keys, timeout=86400)
    except Exception as e:
        logger.warning(
            "test.idempotency_failed_untrack",
            error=e,
        )


# =============================================================================
# Idempotency Key Generation View
# =============================================================================


class GenerateKeyView(XTestModeMixin, APIView):
    """
    API for generating an idempotency key and previewing its hash.

    POST /api/baldur/xtest/idempotency/generate-key/

    Request Body:
        {
            "entity_type": "order",
            "entity_id": "123",
            "action": "process",
            "domain": "EXTERNAL_SERVICE"  // optional, has a default
        }

    Response:
        {
            "status": "success",
            "key_string": "order:123:process",
            "cache_key": "idempotency:external_service:order:123:process",
            "key_hash": "a1b2c3d4e5f6...",
            "domain": "EXTERNAL_SERVICE",
            "ttl_seconds": 3600,
            "components": {
                "entity_type": "order",
                "entity_id": "123",
                "operation": "process"
            }
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
            get_idempotency_service,
        )

        # Validate required parameters
        entity_type = request.data.get("entity_type")
        entity_id = request.data.get("entity_id")
        action = request.data.get("action")

        missing_fields = []
        if not entity_type:
            missing_fields.append("entity_type")
        if not entity_id:
            missing_fields.append("entity_id")
        if not action:
            missing_fields.append("action")

        if missing_fields:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_fields",
                    "message": f"Required fields: {', '.join(missing_fields)}",
                    "missing": missing_fields,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Parse the domain (default: EXTERNAL_SERVICE)
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        try:
            domain = IdempotencyDomain[domain_str]
        except KeyError:
            valid_domains = [d.name for d in IdempotencyDomain]
            return Response(
                {
                    "status": "error",
                    "error": "invalid_domain",
                    "message": f"Invalid domain: {domain_str}",
                    "valid_domains": valid_domains,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Generate the key
        try:
            # Try to convert entity_id from string to int
            try:
                entity_id_int = int(entity_id)
            except (ValueError, TypeError):
                # Use a custom key when conversion fails
                key = IdempotencyKey.custom(
                    f"{entity_type}:{entity_id}:{action}",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action=action,
                )
                key.domain = domain
            else:
                key = IdempotencyKey.for_operation(
                    entity_type=entity_type,
                    entity_id=entity_id_int,
                    operation=action,
                    domain=domain,
                )

            # Read the TTL
            service = get_idempotency_service()
            ttl = service.cache_ttl

            response_data = {
                "status": "success",
                "key_string": key.key,
                "cache_key": key.cache_key,
                "key_hash": key.hash,
                "domain": domain.name,
                "ttl_seconds": ttl,
                "components": key.components,
                "timestamp": timezone.now().isoformat(),
            }

            # WAL audit record
            self.log_xtest_audit(
                request=request,
                action="generate_key",
                component="idempotency",
                details={"key_string": key.key, "domain": domain.name},
                result="success",
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_key_generation",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "key_generation_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# Duplicate Detection Simulation View
# =============================================================================


class CheckDuplicateView(XTestModeMixin, APIView):
    """
    API for testing duplicate request detection behavior.

    POST /api/baldur/xtest/idempotency/check-duplicate/

    Request Body:
        {
            "key": "order:123:process",
            "domain": "EXTERNAL_SERVICE",  // optional
            "register": false  // optional, register after checking when true
        }

    Response:
        {
            "status": "success",
            "is_duplicate": true,
            "first_seen_at": "2026-01-26T10:00:00Z",  // when duplicate
            "ttl_remaining": 3540,  // remaining TTL (seconds)
            "registered": false,  // whether registration was performed
            "cache_key": "idempotency:external_service:order:123:process"
        }
    """

    def post(self, request: Request) -> Response:  # noqa: C901
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
            get_idempotency_service,
        )

        # Validate required parameters
        key_string = request.data.get("key")
        if not key_string:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_fields",
                    "message": "Required field: key",
                    "missing": ["key"],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Parse the domain
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        try:
            domain = IdempotencyDomain[domain_str]
        except KeyError:
            valid_domains = [d.name for d in IdempotencyDomain]
            return Response(
                {
                    "status": "error",
                    "error": "invalid_domain",
                    "message": f"Invalid domain: {domain_str}",
                    "valid_domains": valid_domains,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        register = request.data.get("register", False)

        try:
            # Build the IdempotencyKey (custom type)
            key = IdempotencyKey.custom(key_string, raw_key=key_string)
            key.domain = domain
            cache_key = key.cache_key

            # Check the cache
            cached_value = None
            is_duplicate = False
            first_seen_at = None
            ttl_remaining = None

            try:
                cached_value = cache.get(cache_key)
                if cached_value:
                    is_duplicate = True
                    # Extract first_seen_at from the metadata
                    if isinstance(cached_value, dict):
                        first_seen_at = cached_value.get("first_seen_at")
                    # TTL lookup attempt (Django ``BaseCache`` does not expose a
                    # ``ttl`` method; backends that do, such as Redis, surface
                    # it dynamically — fall back to ``None`` when unavailable.)
                    ttl_fn = getattr(cache, "ttl", None)
                    if callable(ttl_fn):
                        try:
                            ttl_remaining = ttl_fn(cache_key)
                        except Exception:
                            ttl_remaining = None
            except Exception as e:
                logger.warning(
                    "test.idempotency_cache_check",
                    error=e,
                )

            # Whether registration was performed
            registered = False
            if register and not is_duplicate:
                try:
                    service = get_idempotency_service()
                    metadata = {
                        "first_seen_at": timezone.now().isoformat(),
                        "source": XTEST_SOURCE,
                    }
                    cache.set(cache_key, metadata, timeout=service.cache_ttl)
                    _track_xtest_key(cache_key)
                    registered = True
                    ttl_remaining = service.cache_ttl
                except Exception as e:
                    logger.warning(
                        "test.idempotency_registration_failed",
                        error=e,
                    )

            response_data = {
                "status": "success",
                "is_duplicate": is_duplicate,
                "first_seen_at": first_seen_at,
                "ttl_remaining": ttl_remaining,
                "registered": registered,
                "cache_key": cache_key,
                "key_string": key_string,
                "domain": domain.name,
                "timestamp": timezone.now().isoformat(),
            }

            # WAL audit record
            self.log_xtest_audit(
                request=request,
                action="check_duplicate",
                component="idempotency",
                details={
                    "key_string": key_string,
                    "is_duplicate": is_duplicate,
                    "registered": registered,
                },
                result="success",
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_duplicate_check",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "check_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# Idempotency Status Helpers (Complexity Reduction)
# =============================================================================


def _filter_tracked_keys(
    tracked_keys: list[str],
    domain_filter: str,
    prefix_filter: str,
) -> list[str]:
    """Apply the domain and prefix filters."""
    from baldur.services.idempotency import IdempotencyDomain

    result = tracked_keys

    if domain_filter:
        try:
            domain = IdempotencyDomain[domain_filter]
            result = [k for k in result if f":{domain.value}:" in k]
        except KeyError:
            pass

    if prefix_filter:
        result = [k for k in result if prefix_filter in k]

    return result


def _aggregate_by_domain(tracked_keys: list[str]) -> dict[str, int]:
    """Aggregate key counts per domain."""
    from baldur.services.idempotency import IdempotencyDomain

    by_domain: dict[str, int] = {}
    for key in tracked_keys:
        for domain in IdempotencyDomain:
            if f":{domain.value}:" in key:
                by_domain[domain.name] = by_domain.get(domain.name, 0) + 1
                break
    return by_domain


def _get_recent_keys_details(
    tracked_keys: list[str], limit: int
) -> list[dict[str, Any]]:
    """Query details for the most recent keys."""
    recent_keys = []
    for cache_key in tracked_keys[:limit]:
        try:
            cached_value = cache.get(cache_key)
            first_seen_at = None
            if isinstance(cached_value, dict):
                first_seen_at = cached_value.get("first_seen_at")
            recent_keys.append(
                {
                    "cache_key": cache_key,
                    "first_seen_at": first_seen_at,
                    "has_value": cached_value is not None,
                }
            )
        except Exception:
            recent_keys.append(
                {
                    "cache_key": cache_key,
                    "first_seen_at": None,
                    "has_value": False,
                }
            )
    return recent_keys


def _get_cache_backend_name() -> str:
    """Query the cache backend name."""
    from django.conf import settings

    try:
        cache_config = settings.CACHES.get("default", {})
        return str(cache_config.get("BACKEND", "unknown"))
    except Exception:
        return "unknown"


# =============================================================================
# Idempotency Status View
# =============================================================================


class IdempotencyStatusView(XTestModeMixin, APIView):
    """
    API for querying the state of currently registered idempotency keys.

    GET /api/baldur/xtest/idempotency/status/

    Query Parameters:
        domain: domain filter (optional)
        prefix: key prefix filter (optional)
        limit: number of results (default 50)

    Response:
        {
            "status": "success",
            "total_xtest_keys": 10,
            "by_domain": {"EXTERNAL_SERVICE": 5, "ASYNC_TASK": 5},
            "recent_keys": [
                {
                    "cache_key": "idempotency:external_service:...",
                    "first_seen_at": "2026-01-26T10:00:00Z",
                    "has_value": true
                }
            ],
            "cache_backend": "django.core.cache.backends.redis.RedisCache"
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        # Parse query parameters
        domain_filter = request.query_params.get("domain", "").upper()
        prefix_filter = request.query_params.get("prefix", "")
        try:
            limit = min(
                int(request.query_params.get("limit", MAX_STATUS_RESULTS)),
                MAX_STATUS_RESULTS,
            )
        except (ValueError, TypeError):
            limit = MAX_STATUS_RESULTS

        try:
            # Query and filter keys registered by X-Test
            tracked_keys = _get_xtest_tracked_keys()
            tracked_keys = _filter_tracked_keys(
                tracked_keys, domain_filter, prefix_filter
            )

            # Aggregate per domain
            by_domain = _aggregate_by_domain(tracked_keys)

            # Details of the most recent keys
            recent_keys = _get_recent_keys_details(tracked_keys, limit)

            # Cache backend information
            cache_backend = _get_cache_backend_name()

            response_data = {
                "status": "success",
                "total_xtest_keys": len(tracked_keys),
                "by_domain": by_domain,
                "recent_keys": recent_keys,
                "cache_backend": cache_backend,
                "filters_applied": {
                    "domain": domain_filter or None,
                    "prefix": prefix_filter or None,
                    "limit": limit,
                },
                "snapshot": collect_system_snapshot(),
                "timestamp": timezone.now().isoformat(),
            }

            # WAL audit record
            self.log_xtest_audit(
                request=request,
                action="query_status",
                component="idempotency",
                details={"total_xtest_keys": len(tracked_keys)},
                result="success",
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_status_retrieval",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "status_retrieval_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# Idempotency Key Registration View
# =============================================================================


class RegisterKeyView(XTestModeMixin, APIView):
    """
    API for manually registering an idempotency key for testing.

    POST /api/baldur/xtest/idempotency/register/

    Request Body:
        {
            "key": "order:123:process",
            "domain": "EXTERNAL_SERVICE",  // optional
            "ttl_seconds": 3600,  // optional, has a default
            "result_data": {"order_id": 123}  // optional, data to store
        }

    Response:
        {
            "status": "success",
            "registered": true,
            "cache_key": "idempotency:external_service:order:123:process",
            "expires_at": "2026-01-26T11:00:00Z"
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
            get_idempotency_service,
        )

        # Validate required parameters
        key_string = request.data.get("key")
        if not key_string:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_fields",
                    "message": "Required field: key",
                    "missing": ["key"],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Parse the domain
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        try:
            domain = IdempotencyDomain[domain_str]
        except KeyError:
            valid_domains = [d.name for d in IdempotencyDomain]
            return Response(
                {
                    "status": "error",
                    "error": "invalid_domain",
                    "message": f"Invalid domain: {domain_str}",
                    "valid_domains": valid_domains,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # TTL configuration
        service = get_idempotency_service()
        try:
            ttl_seconds = int(request.data.get("ttl_seconds", service.cache_ttl))
        except (ValueError, TypeError):
            ttl_seconds = service.cache_ttl

        result_data = request.data.get("result_data", {})

        try:
            # Generate the key
            key = IdempotencyKey.custom(key_string, raw_key=key_string)
            key.domain = domain
            cache_key = key.cache_key

            # Register in the cache
            now = timezone.now()
            metadata = {
                "first_seen_at": now.isoformat(),
                "source": XTEST_SOURCE,
                "result_data": result_data,
            }

            cache.set(cache_key, metadata, timeout=ttl_seconds)
            _track_xtest_key(cache_key)

            # Compute the expiry time
            from datetime import timedelta

            expires_at = now + timedelta(seconds=ttl_seconds)

            response_data = {
                "status": "success",
                "registered": True,
                "cache_key": cache_key,
                "key_string": key_string,
                "domain": domain.name,
                "ttl_seconds": ttl_seconds,
                "expires_at": expires_at.isoformat(),
                "metadata": {
                    "source": XTEST_SOURCE,
                    "has_result_data": bool(result_data),
                },
                "timestamp": now.isoformat(),
            }

            # WAL audit record
            self.log_xtest_injection(
                request=request,
                component="idempotency",
                injection_type="register",
                count=1,
                target_ids=[cache_key],
            )

            return Response(response_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception(
                "test.idempotency_registration_failed",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "registration_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# Idempotency Key Deletion View
# =============================================================================


class ClearKeysView(XTestModeMixin, APIView):
    """
    API for cleaning up test keys.

    POST /api/baldur/xtest/idempotency/clear/

    Request Body:
        {
            "key": "order:123:process",  // delete a specific key
            "domain": "EXTERNAL_SERVICE",  // domain used when deleting a key
            // OR
            "clear_all_xtest": true  // delete all X-Test created keys only
        }

    Response:
        {
            "status": "success",
            "cleared_count": 10,
            "cleared_keys": ["idempotency:..."]  // list of deleted keys
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
        )

        key_string = request.data.get("key")
        domain_str = request.data.get("domain", "EXTERNAL_SERVICE").upper()
        clear_all_xtest = request.data.get("clear_all_xtest", False)

        cleared_keys: list[str] = []
        errors: list[str] = []

        try:
            if clear_all_xtest:
                # Delete all X-Test created keys
                tracked_keys = _get_xtest_tracked_keys()
                for cache_key in tracked_keys:
                    try:
                        cache.delete(cache_key)
                        cleared_keys.append(cache_key)
                    except Exception as e:
                        errors.append(f"{cache_key}: {str(e)}")

                # Reset the tracking list
                try:
                    cache.delete(XTEST_METADATA_KEY)
                except Exception:
                    pass

            elif key_string:
                # Delete a specific key
                try:
                    domain = IdempotencyDomain[domain_str]
                except KeyError:
                    valid_domains = [d.name for d in IdempotencyDomain]
                    return Response(
                        {
                            "status": "error",
                            "error": "invalid_domain",
                            "message": f"Invalid domain: {domain_str}",
                            "valid_domains": valid_domains,
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                key = IdempotencyKey.custom(key_string, raw_key=key_string)
                key.domain = domain
                cache_key = key.cache_key

                try:
                    cache.delete(cache_key)
                    _untrack_xtest_key(cache_key)
                    cleared_keys.append(cache_key)
                except Exception as e:
                    errors.append(f"{cache_key}: {str(e)}")

            else:
                return Response(
                    {
                        "status": "error",
                        "error": "missing_parameters",
                        "message": "Provide either 'key' or 'clear_all_xtest=true'",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            response_data = {
                "status": "success",
                "cleared_count": len(cleared_keys),
                "cleared_keys": cleared_keys[:MAX_STATUS_RESULTS],  # show at most 50
                "errors": errors if errors else None,
                "timestamp": timezone.now().isoformat(),
            }

            # WAL audit record
            self.log_xtest_cleanup(
                request=request,
                component="idempotency",
                cleaned_count=len(cleared_keys),
                cleaned_ids=cleared_keys[:20],
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(
                "test.idempotency_clear_failed",
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "clear_failed",
                    "message": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
