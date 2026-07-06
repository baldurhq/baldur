"""
Tier Registry - Service Layer.

Thread-safe singleton for managing API tiering configuration.
Handles tier definitions, mappings, and overrides with fallback chain.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

import structlog

from baldur.utils.time import utc_now

from .circuit_breaker import get_tiering_circuit_breaker
from .defaults import (
    DEFAULT_TIER_DEFINITIONS,
    DEFAULT_TIER_MAPPINGS,
    DEFAULT_TIER_OVERRIDES,
    STATIC_CRITICAL_PATHS,
    STATIC_CRITICAL_PREFIXES,
)
from .enums import OverrideIdentifierType, TierFallbackReason
from .models import TierDefinition, TierMapping, TierOverride, TierResult
from .validator import TierConfigValidator, TierValidationResult

logger = structlog.get_logger()


class TierRegistry:
    """
    Tier Registry - manages tier definitions, mappings, and overrides.

    Thread-safe singleton for managing API tiering configuration.
    """

    _instance: TierRegistry | None = None
    _lock = threading.Lock()

    def __new__(cls) -> TierRegistry:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init()
                    cls._instance = instance
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    def _init(self):
        """Initialize the registry with default values."""
        self._tiers: dict[str, TierDefinition] = {}
        self._mappings: list[TierMapping] = []
        self._overrides: list[TierOverride] = []
        self._validator: TierConfigValidator = TierConfigValidator()
        self._data_lock = threading.RLock()

        # Before Mutation Snapshot: store the previous state for rollback (up to 10)
        self._previous_configs: list[dict[str, Any]] = []

        # (path, method) → TierDefinition lookup result cache (LRU Eviction)
        self._path_tier_cache: OrderedDict[
            tuple[str, str | None], TierDefinition | None
        ] = OrderedDict()
        self._PATH_CACHE_MAX_SIZE = 1024

        # Load defaults
        self._load_defaults()

    def _load_defaults(self):
        """Load default tier configuration (clears existing first)."""
        self._tiers.clear()

        for tier in DEFAULT_TIER_DEFINITIONS:
            self._tiers[tier.id] = tier
        self._mappings = list(DEFAULT_TIER_MAPPINGS)
        self._overrides = list(DEFAULT_TIER_OVERRIDES)

        # Sort mappings: priority desc → method-specific first
        self._mappings.sort(
            key=lambda m: (m.priority, 1 if m.methods is not None else 0),
            reverse=True,
        )

    # -------------------------------------------------------------------------
    # Before Mutation Snapshot (rollback support)
    # -------------------------------------------------------------------------

    def _save_previous_config(self, action: str):
        """
        Save a snapshot before a configuration change.

        Saves the previous configuration so that rollback is possible.
        Retains up to 10 entries.
        """

        snapshot = {
            "config": self.export_config(),
            "action": action,
            "timestamp": utc_now().isoformat(),
        }
        self._previous_configs.append(snapshot)

        # Retain up to 10
        if len(self._previous_configs) > 10:
            self._previous_configs = self._previous_configs[-10:]

        logger.debug(
            "tier_registry.saved_pre_mutation_snapshot",
            mutation_action=action,
        )

    def get_previous_configs(self) -> list[dict[str, Any]]:
        """
        Look up the list of previous configuration snapshots.

        Returns:
            List of snapshots (most recent first)
        """
        with self._data_lock:
            return list(reversed(self._previous_configs))

    def rollback_to_previous(self, index: int = 0) -> TierValidationResult | None:
        """
        Roll back to a previous configuration.

        Args:
            index: snapshot index to roll back to (0=most recent, 1=the one before, ...)

        Returns:
            TierValidationResult (on success), None on failure
        """
        with self._data_lock:
            if not self._previous_configs:
                logger.warning("tier_registry.no_previous_config_rollback")
                return None

            # Reverse index (0=most recent)
            actual_index = len(self._previous_configs) - 1 - index
            if actual_index < 0:
                logger.warning(
                    "tier_registry.invalid_rollback_index",
                    index=index,
                )
                return None

            snapshot = self._previous_configs[actual_index]
            old_config = snapshot["config"]

            # Save the current state to a snapshot (rollback of a rollback is possible)
            self._save_previous_config("rollback")

            # Restore the configuration (using import_config)
            logger.warning(
                "tier_registry.rolling_back_snapshot_original",
                snapshot=snapshot["timestamp"],
                action=snapshot["action"],
            )

            # Direct restore (prevents infinite loop when calling import_config)
            tiers = [TierDefinition.from_dict(t) for t in old_config.get("tiers", [])]
            mappings = [
                TierMapping.from_dict(m) for m in old_config.get("mappings", [])
            ]
            overrides = [
                TierOverride.from_dict(o) for o in old_config.get("overrides", [])
            ]

            self._tiers = {t.id: t for t in tiers}
            self._mappings = sorted(
                mappings,
                key=lambda m: (m.priority, 1 if m.methods is not None else 0),
                reverse=True,
            )
            self._overrides = overrides
            self._invalidate_path_cache()

            return TierValidationResult(
                is_valid=True, errors=[], warnings=["Rolled back"]
            )

    # -------------------------------------------------------------------------
    # Tier Definition Methods
    # -------------------------------------------------------------------------

    def get_tier(self, tier_id: str) -> TierDefinition | None:
        """Get a tier definition by ID."""
        with self._data_lock:
            return self._tiers.get(tier_id)

    def get_all_tiers(self) -> list[TierDefinition]:
        """Get all tier definitions."""
        with self._data_lock:
            return list(self._tiers.values())

    def set_tiers(self, tiers: list[TierDefinition]) -> TierValidationResult:
        """
        Replace all tier definitions.

        Args:
            tiers: New tier definitions

        Returns:
            TierValidationResult
        """
        result = self._validator.validate_tiers(tiers)
        if not result.is_valid:
            return result

        with self._data_lock:
            # Before Mutation Snapshot: store the pre-change state
            self._save_previous_config("set_tiers")

            self._tiers = {t.id: t for t in tiers}
            self._invalidate_path_cache()
            self._log_change("tiers", [t.to_dict() for t in tiers])

        return result

    # -------------------------------------------------------------------------
    # Tier Mapping Methods
    # -------------------------------------------------------------------------

    def get_all_mappings(self) -> list[TierMapping]:
        """Get all tier mappings."""
        with self._data_lock:
            return list(self._mappings)

    def set_mappings(self, mappings: list[TierMapping]) -> TierValidationResult:
        """
        Replace all tier mappings.

        Args:
            mappings: New tier mappings

        Returns:
            TierValidationResult
        """
        with self._data_lock:
            tier_ids = list(self._tiers.keys())

        result = self._validator.validate_mappings(mappings, tier_ids)
        if not result.is_valid:
            return result

        with self._data_lock:
            # Before Mutation Snapshot: store the pre-change state
            self._save_previous_config("set_mappings")

            self._mappings = sorted(
                mappings,
                key=lambda m: (m.priority, 1 if m.methods is not None else 0),
                reverse=True,
            )
            self._invalidate_path_cache()
            self._log_change("mappings", [m.to_dict() for m in mappings])

        return result

    def get_tier_for_request(
        self,
        path: str,
        method: str | None = None,
    ) -> TierDefinition | None:
        """
        Get the tier for an API request (path + optional HTTP method).

        Uses an LRU cache to avoid O(n) iteration on repeated calls.
        method-specific mappings match with priority over path-only mappings.

        Args:
            path: API path (e.g., "/api/baldur/control/")
            method: HTTP method (e.g., "GET", "POST"). None means path-only matching.

        Returns:
            TierDefinition or None if no mapping matches
        """
        cache_key = (path, method.upper() if method else None)

        with self._data_lock:
            # Cache Hit → LRU update (move to most recently accessed)
            if cache_key in self._path_tier_cache:
                self._path_tier_cache.move_to_end(cache_key)
                return self._path_tier_cache[cache_key]

            # Cache Miss → iterate mappings
            result = None
            for mapping in self._mappings:
                if mapping.matches(path, method):
                    result = self._tiers.get(mapping.tier_id)
                    break

            # Cache Update + LRU Eviction
            self._path_tier_cache[cache_key] = result
            if len(self._path_tier_cache) > self._PATH_CACHE_MAX_SIZE:
                self._path_tier_cache.popitem(last=False)

            return result

    def get_tier_for_path(self, path: str) -> TierDefinition | None:
        """
        Get the tier for an API path (path-only, backward compatible).

        Args:
            path: API path (e.g., "/api/baldur/control/")

        Returns:
            TierDefinition or None if no mapping matches
        """
        return self.get_tier_for_request(path, method=None)

    def _invalidate_path_cache(self) -> None:
        """Invalidate the path lookup cache when mappings/tiers change."""
        self._path_tier_cache.clear()

    # -------------------------------------------------------------------------
    # Tier Override Methods
    # -------------------------------------------------------------------------

    def get_all_overrides(self) -> list[TierOverride]:
        """Get all tier overrides."""
        with self._data_lock:
            return [o for o in self._overrides if not o.is_expired()]

    def set_overrides(self, overrides: list[TierOverride]) -> TierValidationResult:
        """
        Replace all tier overrides.

        Args:
            overrides: New tier overrides

        Returns:
            TierValidationResult
        """
        with self._data_lock:
            tier_ids = list(self._tiers.keys())

        result = self._validator.validate_overrides(overrides, tier_ids)
        if not result.is_valid:
            return result

        with self._data_lock:
            # Before Mutation Snapshot: store the pre-change state
            self._save_previous_config("set_overrides")

            self._overrides = list(overrides)
            self._log_change("overrides", [o.to_dict() for o in overrides])

        return result

    def get_override_tier(
        self,
        client_ip: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
    ) -> TierDefinition | None:
        """
        Get tier override for a client.

        Args:
            client_ip: Client IP address
            user_id: User ID
            api_key: API key

        Returns:
            TierDefinition if override exists, None otherwise
        """
        with self._data_lock:
            for override in self._overrides:
                if override.is_expired():
                    continue

                if client_ip and override.matches(client_ip, OverrideIdentifierType.IP):
                    return self._tiers.get(override.tier_id)

                if user_id and override.matches(
                    user_id, OverrideIdentifierType.USER_ID
                ):
                    return self._tiers.get(override.tier_id)

                if api_key and override.matches(
                    api_key, OverrideIdentifierType.API_KEY
                ):
                    return self._tiers.get(override.tier_id)

        return None

    # -------------------------------------------------------------------------
    # Combined Resolution
    # -------------------------------------------------------------------------

    def resolve_tier(
        self,
        path: str,
        client_ip: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        method: str | None = None,
    ) -> TierDefinition | None:
        """
        Resolve the effective tier for a request.

        Override tier takes precedence over path-based tier.

        Args:
            path: API path
            client_ip: Client IP address
            user_id: User ID
            api_key: API key
            method: HTTP method (GET, POST, etc.) — None means path-only matching

        Returns:
            TierDefinition or None
        """
        override_tier = self.get_override_tier(
            client_ip=client_ip,
            user_id=user_id,
            api_key=api_key,
        )
        if override_tier:
            return override_tier

        return self.get_tier_for_request(path, method=method)

    def _is_static_critical(self, path: str) -> bool:
        """
        Check if path is in the static critical list (L1 Defense).
        """
        if path in STATIC_CRITICAL_PATHS:
            return True
        return path.startswith(STATIC_CRITICAL_PREFIXES)

    def _static_or_default_tier(
        self,
        path: str,
        reason: TierFallbackReason,
    ) -> TierResult:
        """
        Fallback tier resolution: Static Critical → Default (Fail-Closed).
        """
        if self._is_static_critical(path):
            return TierResult(
                tier_id="critical",
                multiplier=0.5,
                is_fallback=True,
                fallback_reason=TierFallbackReason.STATIC_PATH_MATCH,
                latency_ms=0.0,
            )

        return TierResult(
            tier_id="non_essential",
            multiplier=0.0,
            is_fallback=True,
            fallback_reason=reason,
            latency_ms=0.0,
        )

    def resolve_tier_with_fallback(
        self,
        path: str,
        client_ip: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        method: str | None = None,
    ) -> TierResult:
        """
        Resolve tier with Defense-in-Depth fallback chain.

        This is the RECOMMENDED method for production use.

        Args:
            path: API path
            client_ip: Client IP address
            user_id: User ID
            api_key: API key
            method: HTTP method (GET, POST, etc.) — None means path-only matching
        """
        start_time = time.perf_counter()
        circuit_breaker = get_tiering_circuit_breaker()

        if circuit_breaker.is_open:
            result = self._static_or_default_tier(
                path,
                TierFallbackReason.CIRCUIT_OPEN,
            )
            result.latency_ms = (time.perf_counter() - start_time) * 1000
            self._log_fallback_audit(path, result)
            return result

        try:
            tier = self.resolve_tier(
                path=path,
                client_ip=client_ip,
                user_id=user_id,
                api_key=api_key,
                method=method,
            )

            latency_ms = (time.perf_counter() - start_time) * 1000
            circuit_breaker.record_success(latency_ms)

            if tier is not None:
                return TierResult(
                    tier_id=tier.id,
                    multiplier=tier.multiplier,
                    is_fallback=False,
                    fallback_reason=TierFallbackReason.NONE,
                    latency_ms=latency_ms,
                )

            result = self._static_or_default_tier(
                path,
                TierFallbackReason.CONFIG_MISSING,
            )
            result.latency_ms = latency_ms
            self._log_fallback_audit(path, result)
            return result

        except Exception as e:
            circuit_breaker.record_failure(e)

            result = self._static_or_default_tier(
                path,
                TierFallbackReason.ENGINE_ERROR,
            )
            result.latency_ms = (time.perf_counter() - start_time) * 1000
            self._log_fallback_audit(path, result, error=e)
            return result

    def _log_fallback_audit(
        self,
        path: str,
        result: TierResult,
        error: Exception | None = None,
    ):
        """Log fallback event to Shadow Audit."""
        try:
            from baldur.audit import log_config_change

            log_config_change(
                config_type="tiering_fallback",
                config_key=path,
                old_value=None,
                new_value={
                    "tier_id": result.tier_id,
                    "reason": result.fallback_reason.value,
                    "error": str(error) if error else None,
                    "severity": "warning" if not error else "error",
                    "tag": "TIERING_FALLBACK",
                    "latency_ms": result.latency_ms,
                },
                user="system",
            )
        except Exception as audit_error:
            logger.exception(
                "tier_registry.shadow_audit_failed",
                audit_error=audit_error,
            )

    def resolve_tier_safe(
        self,
        path: str,
        client_ip: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        default_multiplier: float = 1.0,
    ) -> TierDefinition:
        """
        Resolve tier with Fail-Safe guarantee (LEGACY).

        WARNING: This uses Fail-Open strategy. Consider using
        resolve_tier_with_fallback() instead.
        """
        try:
            tier = self.resolve_tier(
                path=path,
                client_ip=client_ip,
                user_id=user_id,
                api_key=api_key,
            )

            if tier is not None:
                return tier

            return TierDefinition(
                id="_default",
                name="Default (No Match)",
                multiplier=default_multiplier,
                priority=0,
                description="No tiering mapping - allow by default",
            )

        except Exception as e:
            logger.warning(
                "tier_registry.fail_safe_activated_returning",
                error=e,
                path=path,
            )
            return TierDefinition(
                id="_failsafe",
                name="Fail-Safe",
                multiplier=default_multiplier,
                priority=0,
                description="Tiering system failure - Fail-Safe mode",
            )

    # -------------------------------------------------------------------------
    # Dry Run / Simulation
    # -------------------------------------------------------------------------

    def simulate(
        self,
        tiers: list[TierDefinition],
        mappings: list[TierMapping],
        test_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Simulate tier configuration changes.

        Does NOT apply changes, just shows what would happen.
        """
        result = self._validator.validate_tiers(tiers)
        if not result.is_valid:
            return {
                "status": "error",
                "validation": result.to_dict(),
            }

        tier_ids = [t.id for t in tiers]
        result = self._validator.validate_mappings(mappings, tier_ids)
        if not result.is_valid:
            return {
                "status": "error",
                "validation": result.to_dict(),
            }

        sorted_mappings = sorted(
            mappings,
            key=lambda m: (m.priority, 1 if m.methods is not None else 0),
            reverse=True,
        )
        tier_dict = {t.id: t for t in tiers}

        if not test_paths:
            test_paths = [
                "/api/baldur/control/",
                "/api/baldur/allow/test/",
                "/api/baldur/block/test/",
                "/api/baldur/config/circuit-breaker/",
                "/api/baldur/dlq/replay/",
                "/api/baldur/dashboard/summary/",
                "/api/baldur/metrics/",
                "/api/baldur/audit/",
            ]

        affected_paths = []
        for path in test_paths:
            current_tier = self.get_tier_for_path(path)
            current_tier_id = current_tier.id if current_tier else None

            new_tier_id = None
            for mapping in sorted_mappings:
                if mapping.matches(path):
                    new_tier_id = mapping.tier_id
                    break

            new_tier = tier_dict.get(new_tier_id) if new_tier_id else None

            affected_paths.append(
                {
                    "path": path,
                    "current_tier": current_tier_id,
                    "new_tier": new_tier_id,
                    "changed": current_tier_id != new_tier_id,
                    "new_multiplier": new_tier.multiplier if new_tier else None,
                }
            )

        changed_count = sum(1 for p in affected_paths if p["changed"])

        return {
            "status": "success",
            "affected_paths": affected_paths,
            "statistics": {
                "total_paths": len(affected_paths),
                "changed_count": changed_count,
                "unchanged_count": len(affected_paths) - changed_count,
            },
            "validation": {
                "is_valid": True,
                "errors": [],
                "warnings": result.warnings,
            },
        }

    # -------------------------------------------------------------------------
    # Persistence / Audit
    # -------------------------------------------------------------------------

    def _log_change(self, config_type: str, changes: Any):
        """Log configuration change to audit service."""
        try:
            from baldur.audit import log_config_change

            log_config_change(
                config_type=f"tiering_{config_type}",
                config_key="tier_config",
                old_value=None,
                new_value=changes,
                user="TierRegistry",
            )
        except Exception as e:
            logger.warning(
                "tier_registry.log_change_failed",
                error=e,
            )

    def export_config(self) -> dict[str, Any]:
        """Export current configuration."""
        with self._data_lock:
            return {
                "tiers": [t.to_dict() for t in self._tiers.values()],
                "mappings": [m.to_dict() for m in self._mappings],
                "overrides": [
                    o.to_dict() for o in self._overrides if not o.is_expired()
                ],
            }

    def import_config(self, config: dict[str, Any]) -> TierValidationResult:
        """
        Import configuration.

        Args:
            config: Configuration dictionary

        Returns:
            TierValidationResult
        """
        tiers = [TierDefinition.from_dict(t) for t in config.get("tiers", [])]
        mappings = [TierMapping.from_dict(m) for m in config.get("mappings", [])]
        overrides = [TierOverride.from_dict(o) for o in config.get("overrides", [])]

        result = self._validator.validate_all(tiers, mappings, overrides)
        if not result.is_valid:
            return result

        with self._data_lock:
            # Before Mutation Snapshot: store the pre-change state
            self._save_previous_config("import_config")

            self._tiers = {t.id: t for t in tiers}
            self._mappings = sorted(
                mappings,
                key=lambda m: (m.priority, 1 if m.methods is not None else 0),
                reverse=True,
            )
            self._overrides = overrides
            self._invalidate_path_cache()
            self._log_change("full_config", config)

        return result

    def reset_to_defaults(self):
        """Reset to default configuration."""
        with self._data_lock:
            # Before Mutation Snapshot: store the pre-change state
            self._save_previous_config("reset_to_defaults")

            self._load_defaults()
            self._invalidate_path_cache()
            self._log_change("reset", {"action": "reset_to_defaults"})


def get_tier_registry() -> TierRegistry:
    """Get the singleton TierRegistry instance."""
    return TierRegistry()
