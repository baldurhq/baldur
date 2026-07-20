"""
Configuration History & Rollback Service.

Stores configuration change history in Redis and provides rollback.

Features:
- Automatic version save on every change
- Retention of the most recent N versions
- Rollback to a specific version
- Graceful degradation when Redis is down

Usage:
    from baldur.services.config_history import get_config_history_service

    service = get_config_history_service()

    # Save a version
    version = service.save_version(
        config_type="circuit_breaker",
        values={"failure_threshold": 10},
        changed_by="admin",
        reason="Increase threshold for high load",
    )

    # Query history
    history = service.get_history("circuit_breaker", limit=10)

    # Rollback
    rolled_back = service.rollback(
        config_type="circuit_breaker",
        target_version=1,
        rolled_back_by="admin",
    )

Audit:
- save_version: log_config_apply_audit(status="applied")
- rollback: log_rollback_audit(state="completed")

Reference:
    See the AuditSettings section of the configuration implementation guide.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.interfaces.config_history_store import ConfigHistoryStore

from baldur.audit.helpers import log_config_apply_audit, log_rollback_audit

from .keys import (
    _get_max_history_entries,
)
from .models import ConfigVersion

logger = structlog.get_logger()


class ConfigHistoryService:
    """
    Configuration change history management service.

    Features:
    - Automatic version save on every change
    - Retention of the most recent N versions
    - Rollback to a specific version
    - Graceful degradation when Redis is down
    """

    # Supported config_type values
    SUPPORTED_CONFIG_TYPES = [
        "circuit_breaker",
        "dlq",
        "retry",
        "sla",
        "slo",
        "rate_limit",
        "security",
        "idempotency",
        "notification",
        "forensic",
        "metrics",
        "error_budget",
        "drift_threshold",  # Drift threshold settings
        "emergency",  # Emergency Mode settings
        "logging",  # Logging settings
        "chaos",  # Chaos Engineering settings
    ]

    def __init__(self, store: ConfigHistoryStore | None = None):
        self._store = store

    @property
    def store(self) -> ConfigHistoryStore | None:
        """ConfigHistoryStore (Lazy loading via ProviderRegistry)."""
        if self._store is None:
            try:
                from baldur.factory import ProviderRegistry

                self._store = ProviderRegistry.config_history_store.get()
            except Exception as e:
                logger.warning(
                    "config_history.store_unavailable",
                    error=e,
                )
        return self._store

    def is_valid_config_type(self, config_type: str) -> bool:
        """Check whether the config_type is valid."""
        return config_type in self.SUPPORTED_CONFIG_TYPES

    def save_version(
        self,
        config_type: str,
        values: dict[str, Any],
        changed_by: str,
        reason: str = "",
    ) -> ConfigVersion | None:
        """
        Save a new configuration version.

        Args:
            config_type: Setting category (circuit_breaker, dlq, retry, ...)
            values: Setting values
            changed_by: Who made the change
            reason: Reason for the change

        Returns:
            The saved ConfigVersion, or None when Redis is down
        """
        if not self.is_valid_config_type(config_type):
            logger.error(
                "config_history.invalid",
                config_type=config_type,
            )
            return None

        if not self.store:
            logger.warning("config_history.store_unavailable_skip_save")
            return None

        try:
            # New version number (atomic increment)
            version_num = self.store.next_version(config_type)

            # Compute the hash
            config_hash = self._compute_hash(values)

            version = ConfigVersion(
                version=version_num,
                timestamp=time.time(),
                config_type=config_type,
                values=values,
                changed_by=changed_by,
                reason=reason,
                hash=config_hash,
            )

            # Atomic save (history + current)
            max_entries = _get_max_history_entries()
            self.store.save_version(config_type, version.to_dict(), max_entries)

            logger.info(
                "config_history.saved",
                config_type=config_type,
                version_num=version_num,
                changed_by=changed_by,
                reason=reason,
            )

            # === Audit record: configuration version saved ===
            log_config_apply_audit(
                pending_id=None,
                config_key=config_type,
                old_value=None,
                new_value=values,
                status="applied",
                details={
                    "version": version_num,
                    "changed_by": changed_by,
                    "reason": reason,
                    "hash": config_hash,
                },
            )

            return version

        except Exception as e:
            logger.exception(
                "config_history.save_failed",
                error=e,
            )
            return None

    def get_history(self, config_type: str, limit: int = 10) -> list[ConfigVersion]:
        """
        Query the configuration change history.

        Args:
            config_type: Setting category
            limit: Number of versions to fetch

        Returns:
            List of ConfigVersion (newest first)
        """
        if not self.is_valid_config_type(config_type):
            logger.error(
                "config_history.invalid",
                config_type=config_type,
            )
            return []

        if not self.store:
            logger.warning("config_history.store_unavailable_returning_empty")
            return []

        try:
            max_entries = _get_max_history_entries()
            entries = self.store.get_history(config_type, min(limit, max_entries))

            versions = []
            for data in entries:
                try:
                    versions.append(ConfigVersion.from_dict(data))
                except (ValueError, TypeError) as e:
                    logger.warning(
                        "config_history.parse_entry_failed",
                        error=e,
                    )
                    continue

            return versions

        except Exception as e:
            logger.exception(
                "config_history.get_history_failed",
                error=e,
            )
            return []

    def get_current_version(self, config_type: str) -> ConfigVersion | None:
        """Query the current version."""
        if not self.is_valid_config_type(config_type):
            return None

        if not self.store:
            return None

        try:
            data = self.store.get_current(config_type)
            if data:
                return ConfigVersion.from_dict(data)
            return None

        except Exception as e:
            logger.exception(
                "config_history.get_current_failed",
                error=e,
            )
            return None

    def get_version(self, config_type: str, version: int) -> ConfigVersion | None:
        """Query a specific version."""
        history = self.get_history(config_type, limit=_get_max_history_entries())

        for v in history:
            if v.version == version:
                return v

        return None

    def rollback(
        self, config_type: str, target_version: int, rolled_back_by: str
    ) -> ConfigVersion | None:
        """
        Roll back to a specific version.

        Note: this method only records the version history.
        The caller must invoke _apply_config_values() to apply the settings.

        Args:
            config_type: Setting category
            target_version: Version number to roll back to
            rolled_back_by: Who performed the rollback

        Returns:
            The newly created rollback version, or None
        """
        target = self.get_version(config_type, target_version)

        if not target:
            logger.error(
                "config_history.rollback_failed_version_found",
                target_version=target_version,
                config_type=config_type,
            )
            return None

        # A rollback is also stored as a new version
        new_version = self.save_version(
            config_type=config_type,
            values=target.values,
            changed_by=rolled_back_by,
            reason=f"Rollback to version {target_version}",
        )

        if new_version:
            logger.info(
                "config_history.rollback_successful",
                config_type=config_type,
                target_version=target_version,
                new_version=new_version.version,
                rolled_back_by=rolled_back_by,
            )

            # === Audit record: configuration rollback ===
            log_rollback_audit(
                request_id=f"config-rollback-{config_type}-{new_version.version}",
                service_name=config_type,
                state="completed",
                triggered_by=rolled_back_by,
                reason=f"Rollback to version {target_version}",
                source_version=(
                    str(new_version.version - 1) if new_version.version > 1 else None
                ),
                target_version=str(target_version),
                affected_components=[config_type],
            )

        return new_version

    def compare_versions(
        self, config_type: str, version_a: int, version_b: int
    ) -> dict[str, Any] | None:
        """
        Compare the differences between two versions.

        Returns:
            Dictionary of differences, or None
        """
        v_a = self.get_version(config_type, version_a)
        v_b = self.get_version(config_type, version_b)

        if not v_a or not v_b:
            return None

        diff: dict[str, Any] = {
            "version_a": version_a,
            "version_b": version_b,
            "config_type": config_type,
            "changes": {},
        }

        all_keys = set(v_a.values.keys()) | set(v_b.values.keys())

        for key in all_keys:
            val_a = v_a.values.get(key)
            val_b = v_b.values.get(key)

            if val_a != val_b:
                diff["changes"][key] = {
                    "from": val_a,
                    "to": val_b,
                }

        return diff

    def get_version_count(self, config_type: str) -> int:
        """Query the number of stored versions."""
        if not self.store:
            return 0

        try:
            return self.store.get_version_count(config_type)
        except Exception:
            return 0

    def clear_history(self, config_type: str) -> bool:
        """
        Delete the history of a specific config_type (test use).

        WARNING: use with care in production!
        """
        if not self.store:
            return False

        try:
            self.store.clear(config_type)
            logger.warning(
                "config_history.cleared_history",
                config_type=config_type,
            )
            return True

        except Exception as e:
            logger.exception(
                "config_history.clear_failed",
                error=e,
            )
            return False

    def _compute_hash(self, values: dict[str, Any]) -> str:
        """Compute the hash of the setting values."""
        from baldur.utils.serialization import fast_canonical_dumps

        return hashlib.sha256(fast_canonical_dumps(values)).hexdigest()[:16]


# Singleton instance
_config_history_service: ConfigHistoryService | None = None
_config_history_service_lock = threading.Lock()


def get_config_history_service() -> ConfigHistoryService:
    """Return the ConfigHistoryService singleton."""
    global _config_history_service
    if _config_history_service is None:
        with _config_history_service_lock:
            if _config_history_service is None:
                _config_history_service = ConfigHistoryService()
    return _config_history_service


def reset_config_history_service() -> None:
    """Reset the singleton instance (test use)."""
    global _config_history_service
    _config_history_service = None
