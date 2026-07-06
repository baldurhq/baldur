"""
X-Test Session Manager

Stores and manages X-Test session metadata in Redis.
Provides session creation, lookup, expiration detection, and artifact registration.

Redis key layout:
- xtest:session:{session_id} - Hash: session metadata
- xtest:session:active - Set: list of active session IDs
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


@dataclass
class XTestSessionMetadata:
    """X-Test session metadata."""

    session_id: str
    created_at: datetime
    ttl_hours: int
    user: str
    components: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    @property
    def expires_at(self) -> datetime:
        """Session expiry time."""
        return self.created_at + timedelta(hours=self.ttl_hours)

    @property
    def is_expired(self) -> bool:
        """Whether the session has expired."""
        return utc_now() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "ttl_hours": self.ttl_hours,
            "user": self.user,
            "components": self.components,
            "artifacts": self.artifacts,
            "expires_at": self.expires_at.isoformat(),
            "is_expired": self.is_expired,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> XTestSessionMetadata:
        """Create an instance from a dictionary."""
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = utc_now()

        return cls(
            session_id=data.get("session_id", ""),
            created_at=created_at,
            ttl_hours=int(data.get("ttl_hours", 4)),
            user=data.get("user", "anonymous"),
            components=data.get("components", []),
            artifacts=data.get("artifacts", []),
        )


class XTestSessionManager:
    """
    X-Test session metadata manager.

    Uses Redis to handle X-Test session creation, lookup, expiration
    detection, and artifact registration.
    """

    def __init__(self, redis_client: Any | None = None):
        """
        Args:
            redis_client: Redis client (auto-created if None)
        """
        self._redis = redis_client
        self._settings = None

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
                logger.warning("x_test_session.redis_adapter_available")
                self._redis = None
        return self._redis

    def _get_session_key(self, session_id: str) -> str:
        """Build the Redis key for a session."""
        return f"{self.settings.redis_session_prefix}{session_id}"

    def _get_active_sessions_key(self) -> str:
        """Redis key for the active-sessions list."""
        return self.settings.redis_active_sessions_key

    def create_session(
        self,
        session_id: str,
        user: str = "anonymous",
        ttl_hours: int | None = None,
    ) -> XTestSessionMetadata:
        """
        Create a new X-Test session.

        Args:
            session_id: Session identifier
            user: Creating user
            ttl_hours: Session TTL (uses the configured default if None)

        Returns:
            Created session metadata
        """
        if ttl_hours is None:
            ttl_hours = self.settings.session_ttl_hours

        metadata = XTestSessionMetadata(
            session_id=session_id,
            created_at=utc_now(),
            ttl_hours=ttl_hours,
            user=user,
            components=[],
            artifacts=[],
        )

        if self.redis:
            try:
                session_key = self._get_session_key(session_id)
                active_key = self._get_active_sessions_key()

                # Store session metadata (Hash)
                self.redis.hset(
                    session_key,
                    mapping={
                        "created_at": metadata.created_at.isoformat(),
                        "ttl_hours": str(ttl_hours),
                        "user": user,
                        "components": fast_dumps_str([]),
                        "artifacts": fast_dumps_str([]),
                    },
                )
                # Set session TTL (add 1 hour of headroom)
                self.redis.expire(session_key, (ttl_hours + 1) * 3600)

                # Add to the active-sessions list
                self.redis.sadd(active_key, session_id)

                logger.info(
                    "x_test_session.created_session",
                    session_id=session_id,
                    user=user,
                    ttl_hours=ttl_hours,
                )

            except Exception as e:
                logger.exception(
                    "x_test_session.create_session_failed",
                    error=e,
                )

        return metadata

    def get_session(self, session_id: str) -> XTestSessionMetadata | None:
        """
        Look up session metadata.

        Args:
            session_id: Session identifier

        Returns:
            Session metadata (None if missing)
        """
        if not self.redis:
            return None

        try:
            session_key = self._get_session_key(session_id)
            data = self.redis.hgetall(session_key)

            if not data:
                return None

            # Convert bytes returned from Redis to strings
            str_data = {}
            for k, v in data.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                str_data[key] = val

            return XTestSessionMetadata(
                session_id=session_id,
                created_at=datetime.fromisoformat(str_data.get("created_at", "")),
                ttl_hours=int(str_data.get("ttl_hours", 4)),
                user=str_data.get("user", "anonymous"),
                components=fast_loads(str_data.get("components", "[]")),
                artifacts=fast_loads(str_data.get("artifacts", "[]")),
            )

        except Exception as e:
            logger.exception(
                "x_test_session.get_session_failed",
                session_id=session_id,
                error=e,
            )
            return None

    def update_session(
        self,
        session_id: str,
        components: list[str] | None = None,
        artifacts: list[str] | None = None,
    ) -> bool:
        """
        Update session metadata.

        Args:
            session_id: Session identifier
            components: Components list to update
            artifacts: Artifacts list to update

        Returns:
            Whether the update succeeded
        """
        if not self.redis:
            return False

        try:
            session_key = self._get_session_key(session_id)

            if not self.redis.exists(session_key):
                return False

            updates = {}
            if components is not None:
                updates["components"] = fast_dumps_str(components)
            if artifacts is not None:
                updates["artifacts"] = fast_dumps_str(artifacts)

            if updates:
                self.redis.hset(session_key, mapping=updates)

            return True

        except Exception as e:
            logger.exception(
                "x_test_session.update_session_failed",
                session_id=session_id,
                error=e,
            )
            return False

    def register_artifact(
        self,
        session_id: str,
        artifact_id: str,
        component: str,
    ) -> bool:
        """
        Register an artifact with a session.

        Args:
            session_id: Session identifier
            artifact_id: Artifact ID
            component: Producing component

        Returns:
            Whether the registration succeeded
        """
        session = self.get_session(session_id)
        if not session:
            # Auto-create the session if missing
            session = self.create_session(session_id)

        # Add artifact
        artifacts = list(set(session.artifacts + [artifact_id]))
        components = list(set(session.components + [component]))

        return self.update_session(
            session_id,
            components=components,
            artifacts=artifacts,
        )

    def get_active_sessions(self) -> list[str]:
        """
        Look up the list of active session IDs.

        Returns:
            List of active session IDs
        """
        if not self.redis:
            return []

        try:
            active_key = self._get_active_sessions_key()
            session_ids = self.redis.smembers(active_key)
            return [
                sid.decode() if isinstance(sid, bytes) else sid for sid in session_ids
            ]

        except Exception as e:
            logger.exception(
                "x_test_session.get_active_sessions_failed",
                error=e,
            )
            return []

    def get_expired_sessions(self) -> list[XTestSessionMetadata]:
        """
        Look up the list of expired sessions.

        Returns:
            List of expired session metadata
        """
        expired_sessions = []
        active_ids = self.get_active_sessions()

        for session_id in active_ids:
            session = self.get_session(session_id)
            if session and session.is_expired:
                expired_sessions.append(session)

        logger.debug(
            "x_test_session.found_expired_sessions_out",
            count=len(expired_sessions),
            active_count=len(active_ids),
        )

        return expired_sessions

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a session.

        Args:
            session_id: Session identifier

        Returns:
            Whether the deletion succeeded
        """
        if not self.redis:
            return False

        try:
            session_key = self._get_session_key(session_id)
            active_key = self._get_active_sessions_key()

            # Delete session metadata
            self.redis.delete(session_key)

            # Remove from the active list
            self.redis.srem(active_key, session_id)

            logger.info(
                "x_test_session.deleted_session",
                session_id=session_id,
            )
            return True

        except Exception as e:
            logger.exception(
                "x_test_session.delete_session_failed",
                session_id=session_id,
                error=e,
            )
            return False

    def get_sessions_count(self) -> int:
        """Look up the number of active sessions."""
        return len(self.get_active_sessions())


# =============================================================================
# Factory Function
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

(
    get_xtest_session_manager,
    configure_xtest_session_manager,
    reset_xtest_session_manager,
) = make_singleton_factory("xtest_session_manager", XTestSessionManager)

__all__ = [
    "XTestSessionMetadata",
    "XTestSessionManager",
    "get_xtest_session_manager",
    "configure_xtest_session_manager",
    "reset_xtest_session_manager",
]
