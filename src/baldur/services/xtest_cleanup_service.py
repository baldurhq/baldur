"""
X-Test Artifact Cleanup Service

Service that automatically cleans up test artifacts left over after an
X-Test session ends.

Cleanup targets:
- Circuit Breaker: restore xtest_mode=True states to CLOSED
- DLQ: delete entries with source="x-test-mode"
- Idempotency: delete xtest:idempotency:* keys
- Rate Limiter: delete xtest:rate_limit:* counters
- Scenario Results: clear in-memory _scenario_results

Thin Task, Fat Service principle:
- Tasks act only as thin delegators
- All business logic lives in this service
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.audit.helpers import log_xtest_cleanup_audit
from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# X-Test-Mode data identifiers
XTEST_SOURCE = "x-test-mode"
XTEST_IDEMPOTENCY_PREFIX = "xtest:idempotency:"
XTEST_RATE_LIMIT_PREFIX = "xtest:rate_limit:"


@dataclass
class XTestCleanupResult(SerializableMixin):
    """X-Test cleanup operation result."""

    success: bool
    sessions_cleaned: int = 0
    cb_states_restored: int = 0
    dlq_entries_purged: int = 0
    idempotency_keys_cleared: int = 0
    rate_limit_counters_reset: int = 0
    scenario_results_cleared: int = 0
    errors: list[str] = field(default_factory=list)
    cleaned_session_ids: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())


class XTestCleanupService:
    """
    X-Test artifact auto-cleanup service.

    Automatically cleans up test artifacts from expired X-Test sessions to
    prevent system contamination.
    """

    def __init__(self, redis_client: Any | None = None):
        """
        Args:
            redis_client: Redis client (auto-created if None)
        """
        self._redis = redis_client
        self._settings = None
        self._session_manager = None

    @property
    def settings(self):
        """Lazy-load settings."""
        if self._settings is None:
            from baldur.settings.xtest_cleanup import get_xtest_cleanup_settings

            self._settings = get_xtest_cleanup_settings()
        return self._settings

    @property
    def redis(self):
        """Lazy-load the Redis client."""
        if self._redis is None:
            try:
                from baldur.adapters.redis import get_redis_client

                self._redis = get_redis_client()
            except ImportError:
                logger.warning("x_test_cleanup.redis_adapter_available")
                self._redis = None
        return self._redis

    @property
    def session_manager(self):
        """Lazy-load the session manager."""
        if self._session_manager is None:
            from baldur.services.xtest_session_manager import (
                get_xtest_session_manager,
            )

            self._session_manager = get_xtest_session_manager()
        return self._session_manager

    def cleanup_expired_sessions(self) -> XTestCleanupResult:
        """
        Clean up expired X-Test sessions and related artifacts.

        Returns:
            Cleanup result
        """
        result = XTestCleanupResult(success=True)

        try:
            # Query the list of expired sessions
            expired_sessions = self.session_manager.get_expired_sessions()

            if not expired_sessions:
                logger.debug("x_test_cleanup.no_expired_sessions_found")
                return result

            logger.info(
                "x_test_cleanup.found_expired_sessions",
                count=len(expired_sessions),
            )

            for session in expired_sessions:
                try:
                    # Clean per-session artifacts
                    self._cleanup_session_artifacts(session, result)

                    # Delete the session
                    self.session_manager.delete_session(session.session_id)
                    result.sessions_cleaned += 1
                    result.cleaned_session_ids.append(session.session_id)

                    logger.info(
                        "x_test_cleanup.cleaned_session",
                        session=session.session_id,
                    )

                except Exception as e:
                    error_msg = f"Failed to clean session {session.session_id}: {e}"
                    logger.exception(
                        "x_test_cleanup.event",
                        error_msg=error_msg,
                    )
                    result.errors.append(error_msg)

            # Clean up global X-Test artifacts (items not tied to a session)
            self._cleanup_orphaned_artifacts(result)

            # Audit logging
            log_xtest_cleanup_audit(
                session_id="system",
                component="cleanup_service",
                cleaned_count=result.sessions_cleaned,
                cleaned_ids=result.cleaned_session_ids[:20],
                user="system",
            )

        except Exception as e:
            error_msg = f"Cleanup failed: {e}"
            logger.exception(
                "x_test_cleanup.event",
                error_msg=error_msg,
            )
            result.success = False
            result.errors.append(error_msg)

        return result

    def _cleanup_session_artifacts(
        self,
        session,
        result: XTestCleanupResult,
    ) -> None:
        """Clean up artifacts related to a session."""
        # Restore CB state
        if self.settings.cb_auto_restore:
            restored = self.restore_cb_states(session.components)
            result.cb_states_restored += restored

        # Delete DLQ entries
        if self.settings.dlq_auto_purge:
            purged = self.purge_dlq_entries(session.artifacts)
            result.dlq_entries_purged += purged

        # Delete Idempotency keys
        if self.settings.idempotency_auto_clear:
            cleared = self.clear_idempotency_keys(session.session_id)
            result.idempotency_keys_cleared += cleared

        # Reset Rate Limit counters
        if self.settings.rate_limit_auto_reset:
            reset = self.reset_rate_limit_counters(session.session_id)
            result.rate_limit_counters_reset += reset

    def _cleanup_orphaned_artifacts(self, result: XTestCleanupResult) -> None:
        """Clean up orphan artifacts not tied to a session."""
        # Clean up scenario results
        cleared = self.clear_scenario_results()
        result.scenario_results_cleared = cleared

    def restore_cb_states(self, components: list[str] | None = None) -> int:
        """
        Restore Circuit Breaker states changed by X-Test mode to CLOSED.

        Args:
            components: List of components to restore (all if None)

        Returns:
            Number of CBs restored
        """
        restored_count = 0

        try:
            from baldur.services.circuit_breaker import (
                CircuitState,  # noqa: F401
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()

            # Query all CB states (returns list of dicts with service_name field)
            all_states = cb_service.get_all_states()

            for state_info in all_states:
                service_name = state_info.get("service_name", "")
                # Only restore entries flagged with xtest_mode
                if state_info.get("metadata", {}).get("xtest_mode", False):
                    try:
                        cb_service.force_close(
                            service_name=service_name,
                            reason="xtest cleanup restore",
                        )
                        restored_count += 1
                        logger.debug(
                            "x_test_cleanup.restored_cb",
                            service_name=service_name,
                        )
                    except Exception as e:
                        logger.warning(
                            "x_test_cleanup.restore_cb_failed",
                            service_name=service_name,
                            error=e,
                        )

        except ImportError:
            logger.debug("x_test_cleanup.circuit_breaker_service_available")
        except Exception as e:
            logger.exception(
                "x_test_cleanup.cb_restore_failed",
                error=e,
            )

        return restored_count

    def purge_dlq_entries(self, artifact_ids: list[str] | None = None) -> int:
        """
        Delete DLQ entries created by X-Test mode.

        Args:
            artifact_ids: List of entry IDs to delete (filter by source if None)

        Returns:
            Number of entries deleted
        """
        purged_count = 0

        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
            if dlq_service is None:
                raise RuntimeError("baldur_pro DLQService not registered")

            # Delete entries with source x-test-mode
            if hasattr(dlq_service, "delete_by_source"):
                purged_count = dlq_service.delete_by_source(XTEST_SOURCE)
            elif artifact_ids:
                # Delete each by artifact_ids
                for entry_id in artifact_ids:
                    try:
                        if hasattr(dlq_service, "delete_entry"):
                            dlq_service.delete_entry(entry_id)
                            purged_count += 1
                    except Exception:
                        pass

            if purged_count > 0:
                logger.info(
                    "x_test_cleanup.purged_dlq_entries",
                    purged_count=purged_count,
                )

        except ImportError:
            logger.debug("x_test_cleanup.dlq_service_available")
        except Exception as e:
            logger.exception(
                "x_test_cleanup.dlq_purge_failed",
                error=e,
            )

        return purged_count

    def clear_idempotency_keys(self, session_id: str | None = None) -> int:
        """
        Delete Idempotency keys created by X-Test mode.

        Args:
            session_id: Session ID (all xtest keys if None)

        Returns:
            Number of keys deleted
        """
        cleared_count = 0

        if not self.redis:
            return 0

        try:
            # Look up keys by pattern
            pattern = f"{XTEST_IDEMPOTENCY_PREFIX}*"
            if session_id:
                pattern = f"{XTEST_IDEMPOTENCY_PREFIX}{session_id}:*"

            keys = self.redis.keys(pattern)

            if keys:
                self.redis.delete(*keys)
                cleared_count = len(keys)
                logger.info(
                    "x_test_cleanup.cleared_idempotency_keys",
                    cleared_count=cleared_count,
                )

        except Exception as e:
            logger.exception(
                "x_test_cleanup.idempotency_clear_failed",
                error=e,
            )

        return cleared_count

    def reset_rate_limit_counters(self, session_id: str | None = None) -> int:
        """
        Reset Rate Limit counters used by X-Test mode.

        Args:
            session_id: Session ID (all xtest counters if None)

        Returns:
            Number of counters reset
        """
        reset_count = 0

        if not self.redis:
            return 0

        try:
            # Look up keys by pattern
            pattern = f"{XTEST_RATE_LIMIT_PREFIX}*"
            if session_id:
                pattern = f"{XTEST_RATE_LIMIT_PREFIX}{session_id}:*"

            keys = self.redis.keys(pattern)

            if keys:
                self.redis.delete(*keys)
                reset_count = len(keys)
                logger.info(
                    "x_test_cleanup.reset_rate_limit_counters",
                    reset_count=reset_count,
                )

        except Exception as e:
            logger.exception(
                "x_test_cleanup.rate_limit_reset_failed",
                error=e,
            )

        return reset_count

    def clear_scenario_results(self) -> int:
        """
        Clear in-memory scenario results.

        Returns:
            Number of results cleared
        """
        cleared_count = 0

        try:
            from baldur.api.django.views.xtest.scenarios import (
                clear_scenario_results,
            )

            cleared_count = clear_scenario_results()
            if cleared_count > 0:
                logger.info(
                    "x_test_cleanup.cleared_scenario_results",
                    cleared_count=cleared_count,
                )

        except ImportError:
            logger.debug("x_test_cleanup.scenario_module_available")
        except Exception as e:
            logger.exception(
                "x_test_cleanup.scenario_clear_failed",
                error=e,
            )

        return cleared_count

    def get_cleanup_stats(self) -> dict[str, Any]:
        """
        Query current cleanup-target statistics.

        Returns:
            Cleanup-target statistics
        """
        stats = {
            "active_sessions": 0,
            "expired_sessions": 0,
            "pending_cb_restores": 0,
            "pending_dlq_purges": 0,
            "pending_idempotency_clears": 0,
            "pending_rate_limit_resets": 0,
        }

        try:
            # Session statistics
            stats["active_sessions"] = self.session_manager.get_sessions_count()
            stats["expired_sessions"] = len(self.session_manager.get_expired_sessions())

            # Redis key statistics
            if self.redis:
                idempotency_keys = self.redis.keys(f"{XTEST_IDEMPOTENCY_PREFIX}*")
                rate_limit_keys = self.redis.keys(f"{XTEST_RATE_LIMIT_PREFIX}*")
                stats["pending_idempotency_clears"] = (
                    len(idempotency_keys) if idempotency_keys else 0
                )
                stats["pending_rate_limit_resets"] = (
                    len(rate_limit_keys) if rate_limit_keys else 0
                )

        except Exception as e:
            logger.exception(
                "x_test_cleanup.stats_collection_failed",
                error=e,
            )

        return stats


# =============================================================================
# Factory Function
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

(
    get_xtest_cleanup_service,
    configure_xtest_cleanup_service,
    reset_xtest_cleanup_service,
) = make_singleton_factory("xtest_cleanup_service", XTestCleanupService)

__all__ = [
    "XTEST_SOURCE",
    "XTEST_IDEMPOTENCY_PREFIX",
    "XTEST_RATE_LIMIT_PREFIX",
    "XTestCleanupResult",
    "XTestCleanupService",
    "get_xtest_cleanup_service",
    "configure_xtest_cleanup_service",
    "reset_xtest_cleanup_service",
]
