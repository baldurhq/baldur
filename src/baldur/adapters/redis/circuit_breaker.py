# verified-by: test_wal_survives_memory_clear
"""
Redis-based Circuit Breaker State Repository.

Implements CircuitBreakerStateRepository interface using ResilientStorageBackend.
Provides zero data loss guarantees through WAL-First protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
)
from baldur.utils.serialization import fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.adapters.resilient.backend import ResilientStorageBackend

logger = structlog.get_logger()


# 476 D2/C2/C4: atomic HALF_OPEN slot acquisition.
#
# KEYS[1] = full hash key for the CB (e.g. "baldur:cb:payment-api")
# ARGV[1] = limit (int as string)
# ARGV[2] = stuck_timeout_seconds (int as string)
# ARGV[3] = now_iso (string, written as updated_at)
# ARGV[4] = now_unix (float seconds as string, written as
#           half_open_window_started_at and used for stuck-recovery age check)
#
# Returns a 4-element table {allowed, previous_state, new_state, marker}
# where ``allowed`` is 1 or 0 and ``marker`` is one of:
# "transition" | "increment" | "rejected" | "stuck_recovery" | "no_op".
# The caller (Python wrapper) returns only the first three; the marker is
# used by LayeredCircuitBreakerStateRepository to emit observability metrics.
_LUA_TRY_ACQUIRE_HALF_OPEN_SLOT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local stuck_timeout = tonumber(ARGV[2])
local now_iso = ARGV[3]
local now_unix = tonumber(ARGV[4])

local fields = redis.call('HMGET', key,
    'state', 'half_open_request_count', 'half_open_window_started_at')
local state = fields[1]
if state == false or state == nil then
    state = 'closed'
end
local count = tonumber(fields[2])
if count == nil then count = 0 end
local watermark = tonumber(fields[3])

if state == 'half_open' and count >= limit then
    if watermark ~= nil and (now_unix - watermark) > stuck_timeout then
        redis.call('HSET', key,
            'state', 'half_open',
            'success_count', '0',
            'half_open_request_count', '1',
            'half_open_window_started_at', tostring(now_unix),
            'updated_at', now_iso)
        return {1, 'half_open', 'half_open', 'stuck_recovery'}
    end
    return {0, 'half_open', 'half_open', 'rejected'}
end

if state == 'open' then
    redis.call('HSET', key,
        'state', 'half_open',
        'success_count', '0',
        'half_open_request_count', '1',
        'half_open_window_started_at', tostring(now_unix),
        'updated_at', now_iso)
    return {1, 'open', 'half_open', 'transition'}
end

if state == 'half_open' and count < limit then
    redis.call('HINCRBY', key, 'half_open_request_count', 1)
    redis.call('HSET', key, 'updated_at', now_iso)
    return {1, 'half_open', 'half_open', 'increment'}
end

return {0, state, state, 'no_op'}
"""


# 498 D1: atomic HALF_OPEN -> CLOSED close-check.
#
# KEYS[1] = full hash key for the CB
# ARGV[1] = success_threshold (int as string)
# ARGV[2] = now_iso (string, written as updated_at)
#
# Returns {did_close (0|1), state, success_count}. Branches:
#   - state == 'half_open': increment success_count; if it reaches the
#     threshold, transition to 'closed' and zero counters/watermarks
#     (did_close=1). Otherwise persist the new count (did_close=0).
#   - state == 'closed': race-loser / post-crash convergence -- no write,
#     return (0, 'closed', 0). The Layered wrapper writes back L1=closed
#     without emission.
#   - state in {'open', 'missing', other}: stale relative to caller's
#     half_open expectation. No write; the wrapper falls back to L1's
#     atomic close path.
_LUA_RECORD_SUCCESS_WITH_CLOSE_CHECK = """
local key = KEYS[1]
local threshold = tonumber(ARGV[1])
local now_iso = ARGV[2]

local fields = redis.call('HMGET', key, 'state', 'success_count')
local state = fields[1]
if state == false or state == nil then
    state = 'missing'
end
local success_count = tonumber(fields[2])
if success_count == nil then success_count = 0 end

if state == 'half_open' then
    local new_count = success_count + 1
    if new_count >= threshold then
        redis.call('HSET', key,
            'state', 'closed',
            'failure_count', '0',
            'success_count', '0',
            'opened_at', '',
            'half_open_request_count', '0',
            'half_open_window_started_at', '',
            'updated_at', now_iso)
        return {1, 'closed', 0}
    end
    redis.call('HSET', key,
        'success_count', tostring(new_count),
        'updated_at', now_iso)
    return {0, 'half_open', new_count}
end

if state == 'closed' then
    return {0, 'closed', 0}
end

return {0, state, 0}
"""


# Atomic HALF_OPEN -> OPEN re-open check (656 D7). Symmetric mirror of
# _LUA_RECORD_SUCCESS_WITH_CLOSE_CHECK, scoped to the failure path. A single
# HALF_OPEN failure re-opens unconditionally (no threshold).
# Returns {did_open (0|1), state, opened_at_iso}. Branches:
#   - state == 'half_open': transition to 'open', set opened_at, zero
#     failure/success/half_open_request counts (did_open=1).
#   - state == 'open': race-loser / already-open -- no write, return
#     (0, 'open', <existing opened_at>). The Layered wrapper writes back
#     L1=open carrying the existing opened_at without emission.
#   - state in {'closed', 'missing', other}: stale relative to caller's
#     half_open expectation (or a concurrent quorum already closed). No
#     write; return (0, state, ''). The wrapper trusts L2 (closed) or falls
#     back to L1's atomic re-open path (missing/other).
_LUA_RECORD_FAILURE_WITH_OPEN_CHECK = """
local key = KEYS[1]
local now_iso = ARGV[1]

local fields = redis.call('HMGET', key, 'state', 'opened_at')
local state = fields[1]
if state == false or state == nil then
    state = 'missing'
end
local opened_at = fields[2]
if opened_at == false or opened_at == nil then
    opened_at = ''
end

if state == 'half_open' then
    redis.call('HSET', key,
        'state', 'open',
        'failure_count', '0',
        'success_count', '0',
        'opened_at', now_iso,
        'half_open_request_count', '0',
        'half_open_window_started_at', '',
        'updated_at', now_iso)
    return {1, 'open', now_iso}
end

if state == 'open' then
    return {0, 'open', opened_at}
end

return {0, state, ''}
"""


class RedisCircuitBreakerStateRepository(
    CircuitBreakerStateRepository
):  # verified-by: test_degraded_mode_writes_wal
    """
    Redis-based Circuit Breaker State Repository.

    Uses ResilientStorageBackend for:
    - Normal mode: Redis storage
    - Degraded mode: Memory + WAL (zero data loss)

    Redis Key Structure:
    - cb:{service_name} → Hash with state fields

    Multi-Cluster Support:
    - Namespace-aware key prefixing via NamespaceSettings
    - Key pattern: {namespace}:cb:{service_name} (when enabled)
    """

    _BASE_PREFIX = "cb"

    def __init__(self, backend: ResilientStorageBackend):
        """
        Initialize Redis Circuit Breaker Repository.

        Args:
            backend: ResilientStorageBackend instance
        """
        self._backend = backend
        self._key_prefix = self._build_key_prefix()

        # 476: marker for the most recent try_acquire_half_open_slot result.
        # See InMemoryCircuitBreakerStateRepository for the contract.
        self._last_acquire_marker: str = ""

    def _build_key_prefix(self) -> str:
        """
        Build component key prefix.

        Note: Namespace prefixing is handled by ResilientStorageBackend.config.key_prefix.
        This method returns only the component-level prefix (e.g., "cb:").

        Final key format in Redis:
        - Backend.key_prefix + CB.key_prefix + service_name
        - e.g., "baldur:seoul:" + "cb:" + "payment-api"

        Returns:
            Component key prefix like "cb:"
        """
        # CB always returns only the base prefix.
        # Namespace prefixing is handled by ResilientStorageBackend.
        return f"{self._BASE_PREFIX}:"

    @property
    def KEY_PREFIX(self) -> str:
        """
        Backward compatible property for KEY_PREFIX.

        Returns dynamically built prefix for namespace support.
        """
        return self._key_prefix

    def _make_key(self, service_name: str) -> str:
        """Generate storage key for service."""
        return f"{self._key_prefix}{service_name}"

    # =========================================================================
    # Interface Implementation
    # =========================================================================

    def get_state(self, service_name: str) -> CircuitBreakerStateData | None:
        """
        Get circuit breaker state for a service.

        Args:
            service_name: Service identifier

        Returns:
            CircuitBreakerStateData if exists, None otherwise
        """
        data = self._backend.hgetall(self._make_key(service_name))
        if not data:
            return None
        return self._to_data(service_name, data)

    def get_or_create(self, service_name: str) -> CircuitBreakerStateData:
        """
        Get existing state or create with defaults.

        Args:
            service_name: Service identifier

        Returns:
            CircuitBreakerStateData (existing or newly created)
        """
        existing = self.get_state(service_name)
        if existing:
            return existing

        # Create with default CLOSED state
        now = utc_now().isoformat()
        default_data = {
            "state": CircuitBreakerStateEnum.CLOSED.value,
            "failure_count": "0",
            "success_count": "0",
            "half_open_request_count": "0",
            "manually_controlled": "False",
            "control_reason": "",
            "created_at": now,
            "updated_at": now,
        }

        self._backend.hset(self._make_key(service_name), default_data)

        return self._to_data(service_name, default_data)

    def update_state(
        self,
        service_name: str,
        state: str,
        failure_count: int | None = None,
        success_count: int | None = None,
        opened_at: datetime | None = None,
        last_failure_at: datetime | None = None,
        half_open_request_count: int | None = None,
        reset_half_open_count: bool = False,
    ) -> bool:
        """
        Update circuit breaker state.

        Args:
            service_name: Service identifier
            state: New state (closed, open, half_open)
            failure_count: Optional failure count
            success_count: Optional success count
            opened_at: Optional time when circuit was opened
            last_failure_at: Optional last failure time
            half_open_request_count: Optional half-open request count
            reset_half_open_count: If True, atomically clear the HALF_OPEN
                counter and watermark in the same write (476 D9). Takes
                precedence over ``half_open_request_count``.

        Returns:
            True on success
        """
        # Normalize CircuitBreakerStateEnum -> .value at the wire boundary.
        # Many CB service callsites pass the Enum directly; under Python 3.11+
        # str(Enum) returns "CircuitBreakerStateEnum.OPEN", not "open", so any
        # external inspector (Grafana, jq) sees the qualified name.
        if isinstance(state, CircuitBreakerStateEnum):
            state = state.value

        now = utc_now()

        updates: dict[str, str] = {
            "state": state,
            "updated_at": now.isoformat(),
        }

        if failure_count is not None:
            updates["failure_count"] = str(failure_count)
        if success_count is not None:
            updates["success_count"] = str(success_count)
        if opened_at is not None:
            updates["opened_at"] = opened_at.isoformat()
        if last_failure_at is not None:
            updates["last_failure_at"] = last_failure_at.isoformat()

        if reset_half_open_count:
            updates["half_open_request_count"] = "0"
            updates["half_open_window_started_at"] = ""
        elif half_open_request_count is not None:
            updates["half_open_request_count"] = str(half_open_request_count)

        return self._backend.hset(self._make_key(service_name), updates)

    def try_acquire_half_open_slot(
        self,
        service_name: str,
        limit: int,
        stuck_timeout_seconds: int,
    ) -> tuple[bool, str, str]:
        """Atomic HALF_OPEN slot acquisition via Lua eval (476 D2/D8)."""
        now = utc_now()
        now_iso = now.isoformat()
        now_unix = now.timestamp()
        full_key = self._backend._get_full_key(self._make_key(service_name))

        try:
            # ResilientStorageBackend caller invariant: try_acquire_half_open_slot
            # is invoked only from LayeredCircuitBreakerStateRepository, which
            # gates on backend.is_degraded == False before delegating.
            redis_client = self._backend.raw_redis_client
            assert redis_client is not None
            result = redis_client.eval(
                _LUA_TRY_ACQUIRE_HALF_OPEN_SLOT,
                1,
                full_key,
                limit,
                stuck_timeout_seconds,
                now_iso,
                now_unix,
            )
        except Exception as e:
            self._last_acquire_marker = ""
            logger.warning(
                "redis_cb_repo.try_acquire_half_open_slot_failed",
                service=service_name,
                error=e,
            )
            raise

        def _decode(value: Any) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value) if value is not None else ""

        allowed = bool(result[0])
        prev_state = _decode(result[1])
        new_state = _decode(result[2])
        marker = _decode(result[3])
        self._last_acquire_marker = marker
        return (allowed, prev_state, new_state)

    def reset_half_open_count(self, service_name: str) -> None:
        """Reset HALF_OPEN counter and clear window watermark (476 G8)."""
        now = utc_now()
        self._backend.hset(
            self._make_key(service_name),
            {
                "half_open_request_count": "0",
                "half_open_window_started_at": "",
                "updated_at": now.isoformat(),
            },
        )

    def increment_failure(self, service_name: str) -> int:
        """
        Increment failure count.

        Args:
            service_name: Service identifier

        Returns:
            New failure count
        """
        # Get current count
        current = self.get_or_create(service_name)
        new_count = current.failure_count + 1

        now = utc_now()
        self._backend.hset(
            self._make_key(service_name),
            {
                "failure_count": str(new_count),
                "last_failure_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )

        return new_count

    def increment_success(self, service_name: str) -> int:
        """
        Increment success count.

        Args:
            service_name: Service identifier

        Returns:
            New success count
        """
        current = self.get_or_create(service_name)
        new_count = current.success_count + 1

        now = utc_now()
        self._backend.hset(
            self._make_key(service_name),
            {
                "success_count": str(new_count),
                "updated_at": now.isoformat(),
            },
        )

        return new_count

    def get_all_states(self) -> list[CircuitBreakerStateData]:
        """
        Get all circuit breaker states.

        Note: This is an expensive operation in Redis.
        Consider using a set to track service names for efficiency.

        Returns:
            List of all CircuitBreakerStateData
        """
        # In degraded mode, we can only return what's in memory
        if self._backend.is_degraded:
            return self._get_all_from_memory()

        # In Redis mode, scan for keys
        try:
            assert (
                self._backend._redis is not None
            )  # is_degraded == False ⇒ Redis ready
            pattern = f"{self._backend.config.key_prefix}{self.KEY_PREFIX}*"
            keys = self._backend.raw_redis_client.keys(pattern)

            results = []
            for key in keys:
                if isinstance(key, bytes):
                    key = key.decode()

                # Extract service name
                prefix = f"{self._backend.config.key_prefix}{self.KEY_PREFIX}"
                service_name = key[len(prefix) :]

                data = self.get_state(service_name)
                if data:
                    results.append(data)

            return results

        except Exception as e:
            logger.exception(
                "redis_cb_repo.error",
                error=e,
            )
            return self._get_all_from_memory()

    def get_open_states(
        self, limit: int | None = None
    ) -> list[CircuitBreakerStateData]:
        """Get OPEN circuit breaker states using SCAN (non-blocking).

        Uses cursor-based SCAN instead of KEYS to avoid blocking Redis.
        Safety guards prevent unbounded iteration in sparse keyspaces.
        """
        if self._backend.is_degraded:
            return self._get_open_from_memory(limit)

        try:
            import time

            assert (
                self._backend._redis is not None
            )  # is_degraded == False ⇒ Redis ready
            pattern = f"{self._backend.config.key_prefix}{self.KEY_PREFIX}*"
            cursor: int = 0
            results: list[CircuitBreakerStateData] = []
            iterations = 0
            max_iterations = 1000
            deadline = time.monotonic() + 2.0  # 2s soft timeout

            while True:
                cursor, keys = self._backend.raw_redis_client.scan(
                    cursor=cursor,
                    match=pattern,
                    count=100,
                )
                for key in keys:
                    if isinstance(key, bytes):
                        key = key.decode()

                    prefix = f"{self._backend.config.key_prefix}{self.KEY_PREFIX}"
                    service_name = key[len(prefix) :]

                    data = self.get_state(service_name)
                    if data and data.state == CircuitBreakerStateEnum.OPEN.value:
                        results.append(data)

                iterations += 1
                if (limit and len(results) >= limit) or cursor == 0:
                    break
                if iterations >= max_iterations or time.monotonic() > deadline:
                    logger.info(
                        "redis_cb_repo.scan_early_termination",
                        iterations=iterations,
                        found=len(results),
                    )
                    break

            # Sort oldest-first by opened_at
            results.sort(key=lambda s: s.opened_at or datetime.min.replace(tzinfo=UTC))
            if limit:
                return results[:limit]
            return results

        except Exception as e:
            logger.exception(
                "redis_cb_repo.get_open_states_error",
                error=e,
            )
            return self._get_open_from_memory(limit)

    def cleanup_stale_keys(self, retention_days: int) -> int:  # noqa: C901, PLR0912
        """Delete CB state hashes whose ``updated_at`` is older than retention.

        Removes orphan keys left behind by service-rename or service-decom.
        Mirrors ``get_open_states()`` SCAN guards (``max_iterations=1000`` and
        2-second deadline) to keep the operation non-blocking on Redis.

        Args:
            retention_days: Delete entries with ``updated_at`` strictly older
                than ``utc_now() - retention_days``.

        Returns:
            Number of CB state entries deleted across Redis and (in degraded
            mode) the in-memory fallback store.
        """
        threshold = utc_now() - timedelta(days=retention_days)
        deleted = 0

        if self._backend.is_degraded:
            for data in self._get_all_from_memory():
                if (
                    data.updated_at is not None
                    and data.updated_at < threshold
                    and self.delete_state(data.service_name)
                ):
                    deleted += 1
            return deleted

        try:
            import time as _time

            assert (
                self._backend._redis is not None
            )  # is_degraded == False ⇒ Redis ready
            pattern = f"{self._backend.config.key_prefix}{self.KEY_PREFIX}*"
            cursor: int = 0
            iterations = 0
            max_iterations = 1000
            deadline = _time.monotonic() + 2.0
            prefix = f"{self._backend.config.key_prefix}{self.KEY_PREFIX}"

            while True:
                cursor, keys = self._backend.raw_redis_client.scan(
                    cursor=cursor,
                    match=pattern,
                    count=100,
                )
                for key in keys:
                    if isinstance(key, bytes):
                        key = key.decode()
                    service_name = key[len(prefix) :]
                    # Rename to avoid mypy carrying the non-None binding from
                    # `for data in self._get_all_from_memory():` at the degraded
                    # branch above into this Optional return-type assignment.
                    state_data = self.get_state(service_name)
                    if state_data is None or state_data.updated_at is None:
                        continue
                    if state_data.updated_at < threshold and self.delete_state(
                        service_name
                    ):
                        deleted += 1

                iterations += 1
                if cursor == 0:
                    break
                if iterations >= max_iterations or _time.monotonic() > deadline:
                    logger.info(
                        "redis_cb_repo.cleanup_scan_early_termination",
                        iterations=iterations,
                        deleted=deleted,
                    )
                    break

            return deleted

        except Exception as e:
            logger.exception(
                "redis_cb_repo.cleanup_stale_keys_error",
                error=e,
            )
            return deleted

    def _get_open_from_memory(
        self, limit: int | None = None
    ) -> list[CircuitBreakerStateData]:
        """Get OPEN states from memory (degraded mode fallback)."""
        results = []
        for key, value in self._backend._memory.items():
            if key.startswith(self.KEY_PREFIX):
                service_name = key[len(self.KEY_PREFIX) :]
                if isinstance(value, dict):
                    data = self._to_data(service_name, value)
                    if data.state == CircuitBreakerStateEnum.OPEN.value:
                        results.append(data)
        results.sort(key=lambda s: s.opened_at or datetime.min.replace(tzinfo=UTC))
        if limit is not None:
            return results[:limit]
        return results

    def _get_all_from_memory(self) -> list[CircuitBreakerStateData]:
        """Get all states from memory (degraded mode)."""
        results = []
        for key, value in self._backend._memory.items():
            if key.startswith(self.KEY_PREFIX):
                service_name = key[len(self.KEY_PREFIX) :]
                if isinstance(value, dict):
                    results.append(self._to_data(service_name, value))
        return results

    def set_manual_control(
        self,
        service_name: str,
        state: str,
        controlled_by_id: int | None = None,
        reason: str = "",
        expires_at: datetime | None = None,
    ) -> bool:
        """
        Set manual control on circuit breaker.

        Args:
            service_name: Service identifier
            state: State to set (e.g., 'open', 'closed')
            controlled_by_id: User ID who set control
            reason: Reason for manual control
            expires_at: When manual control expires

        Returns:
            True on success
        """
        if isinstance(state, CircuitBreakerStateEnum):
            state = state.value

        now = utc_now()

        updates = {
            "state": state,
            "manually_controlled": "True",
            "control_reason": reason,
            "updated_at": now.isoformat(),
        }

        if controlled_by_id is not None:
            updates["controlled_by_id"] = str(controlled_by_id)

        if expires_at is not None:
            updates["manual_override_expires_at"] = expires_at.isoformat()

        return self._backend.hset(self._make_key(service_name), updates)

    def delete_state(self, service_name: str) -> bool:
        """
        Delete circuit breaker state.

        Args:
            service_name: Service identifier

        Returns:
            True on success
        """
        return self._backend.delete(self._make_key(service_name))

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _to_data(
        self,
        service_name: str,
        data: dict[str, Any],
    ) -> CircuitBreakerStateData:
        """Convert dict to CircuitBreakerStateData."""
        raw_metadata = data.get("metadata", "{}")
        try:
            metadata = (
                fast_loads(raw_metadata)
                if isinstance(raw_metadata, str)
                else raw_metadata
            )
        except (ValueError, TypeError):
            metadata = {}

        return CircuitBreakerStateData(
            service_name=service_name,
            id=None,  # Redis doesn't use numeric IDs
            state=data.get("state", CircuitBreakerStateEnum.CLOSED.value),
            failure_count=self._parse_int(data.get("failure_count", 0)),
            success_count=self._parse_int(data.get("success_count", 0)),
            last_failure_at=self._parse_datetime(data.get("last_failure_at")),
            opened_at=self._parse_datetime(data.get("opened_at")),
            manually_controlled=self._parse_bool(
                data.get("manually_controlled", "False")
            ),
            controlled_by_id=self._parse_int(data.get("controlled_by_id")),
            control_reason=data.get("control_reason", ""),
            manual_override_expires_at=self._parse_datetime(
                data.get("manual_override_expires_at")
            ),
            half_open_request_count=self._parse_int(
                data.get("half_open_request_count", 0)
            ),
            half_open_window_started_at=self._parse_unix_timestamp(
                data.get("half_open_window_started_at")
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=self._parse_datetime(data.get("created_at")),
            updated_at=self._parse_datetime(data.get("updated_at")),
        )

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        """Parse ISO datetime string."""
        if not value or value == "":
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_unix_timestamp(value: Any) -> datetime | None:
        """Parse a Unix timestamp seconds string (float) into a UTC datetime.

        ``half_open_window_started_at`` is stored as Unix seconds for in-Lua
        arithmetic (476 D8). Empty / missing values resolve to None.
        """
        if value is None or value == "":
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_int(value: Any) -> int:
        """Parse integer value."""
        if value is None or value == "":
            return 0
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        """Parse boolean value."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)

    # =========================================================================
    # Additional Interface Methods (Aliases and Required Abstract Methods)
    # =========================================================================

    def get_by_service_name(self, service_name: str) -> CircuitBreakerStateData | None:
        """
        Get circuit breaker state by service name (alias for get_state).

        Args:
            service_name: Service identifier

        Returns:
            CircuitBreakerStateData if exists, None otherwise
        """
        return self.get_state(service_name)

    def record_failure(self, service_name: str) -> CircuitBreakerStateData:
        """
        Record a failure and return updated state.

        Args:
            service_name: Service identifier

        Returns:
            Updated CircuitBreakerStateData
        """
        self.increment_failure(service_name)
        return self.get_or_create(service_name)

    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        """
        Record a success and return updated state.

        Args:
            service_name: Service identifier

        Returns:
            Updated CircuitBreakerStateData
        """
        self.increment_success(service_name)
        return self.get_or_create(service_name)

    def record_success_with_close_check(self, service_name, success_threshold):
        """Atomic HALF_OPEN -> CLOSED close-check via Lua eval (498 D1/D2).

        Replaces the ABC's race-unsafe two-call default. Lua executes
        HMGET-decide-HSET as one Redis command, so concurrent gunicorn
        workers / K8s replicas observing HALF_OPEN see exactly one
        ``did_close=True`` per logical recovery -- the worker whose script
        invocation crossed the threshold. All other branches return
        ``did_close=False``:

        - ``state='closed'`` with no write: another worker already closed,
          OR L2 is the cluster-wide source-of-truth with state==closed
          (post-crash convergence). The Layered wrapper writes back L1
          without emitting.
        - ``state in {open, missing, unknown}``: stale relative to caller's
          HALF_OPEN expectation. The wrapper's stale-L2 guard falls back
          to L1's atomic close path.

        Auxiliary state fields (failure_count, opened_at, etc.) are
        synthesized as defaults rather than fetched in a second RTT --
        callers read only ``did_close`` (service.py:1079) and the Layered
        writeback (D6) reads only ``state.state`` / ``state.success_count``.
        """
        from baldur.interfaces.repositories import CircuitBreakerCloseAttempt

        now_iso = utc_now().isoformat()
        full_key = self._backend._get_full_key(self._make_key(service_name))

        try:
            redis_client = self._backend.raw_redis_client
            result = redis_client.eval(
                _LUA_RECORD_SUCCESS_WITH_CLOSE_CHECK,
                1,
                full_key,
                success_threshold,
                now_iso,
            )
        except Exception as e:
            logger.warning(
                "redis_cb_repo.record_success_with_close_check_failed",
                service=service_name,
                error=e,
            )
            raise

        def _decode(value):
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value) if value is not None else ""

        did_close = bool(result[0])
        state_str = _decode(result[1])
        success_count = self._parse_int(result[2])

        state_data = CircuitBreakerStateData(
            service_name=service_name,
            id=None,
            state=state_str,
            failure_count=0,
            success_count=success_count,
            last_failure_at=None,
            opened_at=None,
            manually_controlled=False,
            controlled_by_id=None,
            control_reason="",
            manual_override_expires_at=None,
            half_open_request_count=0,
            half_open_window_started_at=None,
            metadata={},
            created_at=None,
            updated_at=None,
        )
        return CircuitBreakerCloseAttempt(state=state_data, did_close=did_close)

    def record_failure_with_open_check(self, service_name):
        """Atomic HALF_OPEN -> OPEN re-open check via Lua eval (656 D7).

        Replaces the ABC's race-unsafe read-then-write default. Lua executes
        HMGET-decide-HSET as one Redis command, so concurrent gunicorn
        workers / K8s replicas observing HALF_OPEN see exactly one
        ``did_open=True`` per logical re-open -- the worker whose script
        invocation performed the transition. All other branches return
        ``did_open=False``:

        - ``state='open'`` with no write: another worker already re-opened,
          OR this worker is the race-loser. The returned ``opened_at`` is the
          existing one so the Layered wrapper writes back L1=open without
          losing the OPEN-era timestamp.
        - ``state='closed'``: a concurrent quorum of HALF_OPEN successes closed
          the cluster while this worker's trial failed. The wrapper trusts L2
          and writes back L1=closed (no re-open).
        - ``state in {missing, unknown}``: stale relative to caller's HALF_OPEN
          expectation; the wrapper falls back to L1's atomic re-open path.

        Auxiliary state fields (failure_count, success_count, etc.) are
        synthesized as defaults rather than fetched in a second RTT --
        callers read only ``did_open`` and the Layered writeback reads only
        ``state.state`` / ``state.opened_at``.
        """
        from baldur.interfaces.repositories import CircuitBreakerOpenAttempt

        now_iso = utc_now().isoformat()
        full_key = self._backend._get_full_key(self._make_key(service_name))

        try:
            redis_client = self._backend.raw_redis_client
            result = redis_client.eval(
                _LUA_RECORD_FAILURE_WITH_OPEN_CHECK,
                1,
                full_key,
                now_iso,
            )
        except Exception as e:
            logger.warning(
                "redis_cb_repo.record_failure_with_open_check_failed",
                service=service_name,
                error=e,
            )
            raise

        def _decode(value):
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value) if value is not None else ""

        did_open = bool(result[0])
        state_str = _decode(result[1])
        opened_at = self._parse_datetime(_decode(result[2]))

        state_data = CircuitBreakerStateData(
            service_name=service_name,
            id=None,
            state=state_str,
            failure_count=0,
            success_count=0,
            last_failure_at=None,
            opened_at=opened_at,
            manually_controlled=False,
            controlled_by_id=None,
            control_reason="",
            manual_override_expires_at=None,
            half_open_request_count=0,
            half_open_window_started_at=None,
            metadata={},
            created_at=None,
            updated_at=None,
        )
        return CircuitBreakerOpenAttempt(state=state_data, did_open=did_open)

    def clear_manual_control(
        self, service_name: str, preserve_reason: bool = False
    ) -> bool:
        """
        Clear manual control from circuit breaker.

        Args:
            service_name: Service identifier
            preserve_reason: If True, keep existing control_reason

        Returns:
            True on success
        """
        now = utc_now()

        updates = {
            "manually_controlled": "False",
            "controlled_by_id": "",
            "manual_override_expires_at": "",
            "updated_at": now.isoformat(),
        }

        if not preserve_reason:
            updates["control_reason"] = ""

        return self._backend.hset(self._make_key(service_name), updates)

    def reset(self, service_name: str) -> bool:
        """
        Reset circuit breaker to CLOSED state.

        Args:
            service_name: Service identifier

        Returns:
            True on success
        """
        now = utc_now()

        reset_data = {
            "state": CircuitBreakerStateEnum.CLOSED.value,
            "failure_count": "0",
            "success_count": "0",
            "half_open_request_count": "0",
            "half_open_window_started_at": "",
            "opened_at": "",
            "last_failure_at": "",
            "updated_at": now.isoformat(),
        }

        return self._backend.hset(self._make_key(service_name), reset_data)

    # =========================================================================
    # Atomic Operations for Concurrency Safety
    # =========================================================================

    def atomic_force_open(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int = 90,
    ) -> tuple[bool, str, str]:
        """
        Atomically force open a circuit breaker.

        Note: Redis provides atomicity at command level. For full atomic
        operations, consider using Lua scripts.

        Args:
            service_name: Service identifier
            reason: Reason for opening
            controlled_by_id: User ID who initiated
            ttl_minutes: TTL for manual override

        Returns:
            Tuple of (success, previous_state, new_state)
        """
        # Get current state
        current = self.get_or_create(service_name)
        previous_state = current.state
        new_state = CircuitBreakerStateEnum.OPEN.value

        now = utc_now()
        expires_at = now + timedelta(minutes=ttl_minutes)

        updates = {
            "state": new_state,
            "manually_controlled": "True",
            "control_reason": reason,
            "opened_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "manual_override_expires_at": expires_at.isoformat(),
        }

        if controlled_by_id is not None:
            updates["controlled_by_id"] = str(controlled_by_id)

        success = self._backend.hset(self._make_key(service_name), updates)

        return (success, previous_state, new_state)

    def atomic_force_close(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        """
        Atomically force close a circuit breaker.

        Args:
            service_name: Service identifier
            reason: Reason for closing
            controlled_by_id: User ID who initiated

        Returns:
            Tuple of (success, previous_state, new_state)
        """
        current = self.get_or_create(service_name)
        previous_state = current.state
        new_state = CircuitBreakerStateEnum.CLOSED.value

        now = utc_now()

        updates = {
            "state": new_state,
            "failure_count": "0",
            "success_count": "0",
            "half_open_request_count": "0",
            "half_open_window_started_at": "",
            "manually_controlled": "True",
            "control_reason": reason,
            "opened_at": "",
            "updated_at": now.isoformat(),
        }

        if controlled_by_id is not None:
            updates["controlled_by_id"] = str(controlled_by_id)

        success = self._backend.hset(self._make_key(service_name), updates)

        return (success, previous_state, new_state)

    def atomic_reset(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        """
        Atomically reset a circuit breaker to initial state.

        Args:
            service_name: Service identifier
            reason: Reason for reset
            controlled_by_id: User ID who initiated

        Returns:
            Tuple of (success, previous_state, new_state)
        """
        current = self.get_or_create(service_name)
        previous_state = current.state
        new_state = CircuitBreakerStateEnum.CLOSED.value

        now = utc_now()

        updates = {
            "state": new_state,
            "failure_count": "0",
            "success_count": "0",
            "half_open_request_count": "0",
            "half_open_window_started_at": "",
            "manually_controlled": "False",
            "control_reason": "",
            "controlled_by_id": "",
            "opened_at": "",
            "manual_override_expires_at": "",
            "updated_at": now.isoformat(),
        }

        success = self._backend.hset(self._make_key(service_name), updates)

        return (success, previous_state, new_state)


# Singleton
import threading

_redis_cb_repo: RedisCircuitBreakerStateRepository | None = None
_redis_cb_repo_lock = threading.Lock()


def get_redis_circuit_breaker_repo(
    backend: ResilientStorageBackend | None = None,
) -> RedisCircuitBreakerStateRepository:
    """
    Get singleton Redis Circuit Breaker Repository.

    Args:
        backend: ResilientStorageBackend (uses default if not provided)

    Returns:
        RedisCircuitBreakerStateRepository instance
    """
    global _redis_cb_repo

    if _redis_cb_repo is None:
        with _redis_cb_repo_lock:
            if _redis_cb_repo is None:
                if backend is None:
                    from baldur.adapters.resilient.backend import (
                        get_storage_backend,
                    )

                    backend = get_storage_backend()
                _redis_cb_repo = RedisCircuitBreakerStateRepository(backend)

    return _redis_cb_repo


def reset_redis_circuit_breaker_repo() -> None:
    """Reset singleton (for testing)."""
    global _redis_cb_repo
    with _redis_cb_repo_lock:
        _redis_cb_repo = None
