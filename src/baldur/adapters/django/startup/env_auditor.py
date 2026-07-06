"""
Hash chain consistency synchronisation between Redis and local files for
Django startup.

416 (D21): the env-var snapshot logging step was relocated to
``baldur.bootstrap.init()`` so that FastAPI/Flask/plain-Python
adapters get the same audit record without re-implementing the call.
This class now hosts only the Django-coupled hash chain sync logic.
"""

from __future__ import annotations

import structlog
from django.conf import settings

__all__ = [
    "EnvironmentAuditor",
]

logger = structlog.get_logger()


class EnvironmentAuditor:
    """Hash chain consistency synchronization for Django startup.

    Note:
        env_var snapshot logging was relocated to
        ``baldur.bootstrap.init()`` in 416 (D21). This class now hosts
        only the Django-coupled hash chain sync logic. Future Wave 5.5C
        will migrate this to a framework-agnostic settings adapter and
        the class will collapse to a module function.
    """

    @staticmethod
    def sync_hash_chain_on_startup() -> None:
        """
        Synchronize hash chain state between Redis and local files on startup.

        Handles recovery scenarios:
        - Redis behind file: Sync Redis to file state
        - File behind Redis: Normal, some writes may be pending
        - PENDING sequences: Cleanup from previous crashes

        Best-effort: If sync fails, system continues normally.
        Hash chain will be recovered on next successful write.

        Reference:
            docs/baldur/middleware_system/43_DISTRIBUTED_HASH_CHAIN_ENHANCED.md
        """
        try:
            # Check if distributed hash chain is enabled
            if not getattr(settings, "BALDUR_DISTRIBUTED_HASH_CHAIN", False):
                logger.debug("baldur.distributed_hash_chain_disabled")
                return

            from pathlib import Path

            from baldur.audit.integrity import StartupHashChainSync

            # Get Redis client
            redis_client = EnvironmentAuditor._get_redis_client_for_hash_chain()
            if redis_client is None:
                logger.debug("baldur.redis_client_unavailable")
                return

            # Get log directory from settings
            log_dir = Path(getattr(settings, "BALDUR_AUDIT_LOG_DIR", "logs/audit"))

            # Perform sync
            sync = StartupHashChainSync(
                redis_client=redis_client,
                log_dir=log_dir,
                key_prefix=getattr(settings, "BALDUR_REDIS_KEY_PREFIX", "baldur:"),
            )
            result = sync.sync()

            # Log result
            if result.get("status") == "success":
                action = result.get("action", "none")
                pending_cleaned = result.get("pending_cleaned", 0)

                if action == "synced_redis_to_file":
                    logger.warning(
                        "baldur.hash_chain_sync_redis",
                        file_sequence=result.get("file_sequence"),
                    )
                elif action == "fresh_start":
                    logger.info("baldur.hash_chain_sync_fresh")
                else:
                    logger.info(
                        "baldur.hash_chain_sync",
                        sync_action=action,
                    )

                if pending_cleaned > 0:
                    logger.info(
                        "baldur.hash_chain_sync_cleaned",
                        pending_cleaned=pending_cleaned,
                    )
            else:
                logger.warning(
                    "baldur.hash_chain_sync_failed",
                    sync_error=result.get("error", "unknown"),
                )

        except ImportError:
            logger.debug("baldur.integrity_module_unavailable")
        except Exception as e:
            # Best-effort: system continues even if this fails
            logger.warning(
                "baldur.sync_hash_chain_failed",
                error=e,
            )

    @staticmethod
    def _get_redis_client_for_hash_chain():
        """
        Get Redis client for hash chain operations.

        Attempts multiple strategies:
        1. From ResilientStorageBackend if available
        2. From django_redis cache
        3. Direct redis-py connection

        Returns:
            Redis client instance or None if unavailable
        """
        try:
            # Strategy 1: Try ResilientStorageBackend
            try:
                from baldur.adapters.resilient.backend import (
                    ResilientStorageBackend,
                )

                backend = ResilientStorageBackend()
                return backend.get_redis_client()
            except Exception:
                pass

            # Strategy 2: Try django_redis
            try:
                from django_redis import get_redis_connection

                return get_redis_connection("default")
            except Exception:
                pass

            # Strategy 3: Try direct redis connection from settings
            try:
                from baldur.adapters.redis.connection_factory import (
                    get_redis_connection_factory,
                )

                redis_url = getattr(settings, "BALDUR_REDIS_URL", None)
                if redis_url:
                    return get_redis_connection_factory().create(redis_url)
            except Exception:
                pass

            return None

        except Exception:
            return None
