"""
Portable high-performance health checker (Platinum SLA optimization).

Automatically selects the best strategy per OS.
Abstraction layer + automatic fallback.
"""

import platform
import socket
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable

import structlog

from baldur.core.ttl_cache import TTLCacheBase

__all__ = [
    "HealthCheckStrategy",
    "TTLCacheStrategy",
    "LinuxTCPInfoStrategy",
    "PortableHealthChecker",
]

logger = structlog.get_logger()


class HealthCheckStrategy(ABC):
    """Health check strategy interface."""

    @abstractmethod
    def check(self, target: str) -> bool:
        """
        Health-check the target service.

        Args:
            target: Target address (e.g., "localhost:8080")

        Returns:
            Health status (True=healthy, False=unhealthy)
        """
        pass

    @abstractmethod
    def get_name(self) -> str:
        """Return the strategy name."""
        pass


class TTLCacheStrategy(HealthCheckStrategy):
    """
    TTL-based cache strategy (universal).

    Works on every OS with sufficient performance (~0.01ms cache hit).
    Concurrent same-target misses run the check callback exactly once
    (TTLCacheBase.get_or_compute singleflight).
    """

    def __init__(
        self,
        check_callback: Callable[[str], bool] | None = None,
        ttl: float | None = None,
    ):
        """
        Args:
            check_callback: Callback performing the actual health check
            ttl: Cache TTL in seconds. None uses the HealthCheckSettings
                default.
        """
        if ttl is None:
            from baldur.settings.health_check import get_health_check_settings

            ttl = get_health_check_settings().checker_cache_ttl_seconds
        self._ttl_cache: TTLCacheBase[str, bool] = TTLCacheBase(ttl_seconds=ttl)
        self._check_callback = check_callback

    def configure(
        self, check_callback: Callable[[str], bool], ttl: float = 5.0
    ) -> None:
        """Set the callback and TTL."""
        self._check_callback = check_callback
        self._ttl_cache = TTLCacheBase(ttl_seconds=ttl)

    def check(self, target: str) -> bool:
        cached = self._ttl_cache.get(target)
        if cached is not None:
            return cached

        if not self._check_callback:
            return True  # no callback -> assume healthy (uncached, as before)

        # Cache miss: dedup concurrent checks for the same target.
        # _do_check never raises and never returns None, so a failed
        # check caches False (as before). get_or_compute's V|None return
        # therefore never yields None on this path.
        result = self._ttl_cache.get_or_compute(target, lambda: self._do_check(target))
        assert result is not None
        return result

    def get_name(self) -> str:
        return "TTLCacheStrategy"

    def invalidate(self, target: str) -> None:
        """Invalidate the cache entry for a specific target."""
        self._ttl_cache.invalidate(target)

    def invalidate_all(self) -> None:
        """Invalidate the entire cache."""
        self._ttl_cache.invalidate_all()

    def _do_check(self, target: str) -> bool:
        """Run the check callback; a raised exception maps to False."""
        # Only reached from check() after its `if not self._check_callback`
        # guard, so the callback is always set on this path.
        assert self._check_callback is not None
        try:
            return self._check_callback(target)
        except Exception as e:
            logger.debug(
                "ttl_cache_strategy.health_check_failed",
                target_service=target,
                error=e,
            )
            return False


class LinuxTCPInfoStrategy(HealthCheckStrategy):
    """
    Linux-only TCP_INFO strategy.

    Reads socket state directly from the kernel (~0.01ms).
    Works only on Linux; raises NotImplementedError elsewhere.
    """

    TCP_INFO = 11  # Linux TCP_INFO socket option
    TCP_ESTABLISHED = 1

    def __init__(self, timeout: float | None = None):
        """
        Args:
            timeout: Connection timeout in seconds. None uses the
                HealthCheckSettings default.
        """
        if timeout is None:
            from baldur.settings.health_check import get_health_check_settings

            timeout = get_health_check_settings().tcp_info_timeout_seconds
        self._timeout = timeout

        # Verify we are on Linux
        if platform.system() != "Linux":
            raise NotImplementedError("LinuxTCPInfoStrategy is only available on Linux")

    def check(self, target: str) -> bool:
        try:
            host, port_str = target.split(":")
            port = int(port_str)

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)

            try:
                sock.connect((host, port))

                # Read the state from the TCP_INFO struct
                info = sock.getsockopt(socket.IPPROTO_TCP, self.TCP_INFO, 104)
                state = struct.unpack("B", info[0:1])[0]

                return state == self.TCP_ESTABLISHED
            finally:
                sock.close()

        except Exception as e:
            logger.debug(
                "linux_tcp_info_strategy.health_check_failed",
                target_service=target,
                error=e,
            )
            return False

    def get_name(self) -> str:
        return "LinuxTCPInfoStrategy"


class SimpleSocketStrategy(HealthCheckStrategy):
    """
    Simple socket-connect strategy (universal).

    Health is determined by TCP connect success.
    """

    def __init__(self, timeout: float | None = None):
        """
        Args:
            timeout: Connection timeout in seconds. None uses the
                HealthCheckSettings default.
        """
        if timeout is None:
            from baldur.settings.health_check import get_health_check_settings

            timeout = get_health_check_settings().socket_timeout_seconds
        self._timeout = timeout

    def check(self, target: str) -> bool:
        try:
            host, port_str = target.split(":")
            port = int(port_str)

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)

            try:
                sock.connect((host, port))
                return True
            finally:
                sock.close()

        except Exception as e:
            logger.debug(
                "simple_socket_strategy.health_check_failed",
                target_service=target,
                error=e,
            )
            return False

    def get_name(self) -> str:
        return "SimpleSocketStrategy"


class PortableHealthChecker:
    """
    Portable high-performance health checker.

    Automatically selects the best strategy for the OS/environment.

    Usage:
        checker = PortableHealthChecker()

        # Health check
        is_healthy = checker.is_healthy("localhost:8080")

        # Inspect the selected strategy
        print(checker.strategy_name)  # "TTLCacheStrategy" or "LinuxTCPInfoStrategy"
    """

    def __init__(
        self,
        check_callback: Callable[[str], bool] | None = None,
        ttl: float | None = None,
        force_strategy: str | None = None,
    ):
        """
        Args:
            check_callback: Callback for TTLCacheStrategy (falls back to
                SimpleSocketStrategy when omitted)
            ttl: TTLCacheStrategy cache TTL. None uses the
                HealthCheckSettings default.
            force_strategy: Force a specific strategy ("ttl_cache",
                "linux_tcp", "simple_socket")
        """
        if ttl is None:
            from baldur.settings.health_check import get_health_check_settings

            ttl = get_health_check_settings().checker_cache_ttl_seconds
        self._check_callback = check_callback
        self._ttl = ttl
        self._strategy = self._select_strategy(force_strategy)

    def _select_strategy(self, force_strategy: str | None) -> HealthCheckStrategy:
        """Select the best strategy for this environment."""

        if force_strategy == "linux_tcp":
            return LinuxTCPInfoStrategy()

        if force_strategy == "simple_socket":
            return SimpleSocketStrategy()

        if force_strategy == "ttl_cache":
            callback = self._check_callback or self._default_check
            return TTLCacheStrategy(check_callback=callback, ttl=self._ttl)

        # Automatic selection
        system = platform.system()

        # An explicit callback is honored on every platform — the caller
        # asked for it, so it must take precedence over the platform's
        # default probe (otherwise a custom HTTP/health check would be
        # silently replaced by a bare TCP-connect probe on Linux).
        if self._check_callback:
            return TTLCacheStrategy(check_callback=self._check_callback, ttl=self._ttl)

        # No explicit callback. Linux: TTL cache + kernel TCP_INFO probe.
        if system == "Linux":
            try:
                strategy = LinuxTCPInfoStrategy()
                # Wrap in a TTL cache
                return TTLCacheStrategy(check_callback=strategy.check, ttl=self._ttl)
            except NotImplementedError:
                pass

        # Default: socket strategy wrapped in a TTL cache
        simple_strategy = SimpleSocketStrategy()
        return TTLCacheStrategy(check_callback=simple_strategy.check, ttl=self._ttl)

    def _default_check(self, target: str) -> bool:
        """Default health check (socket connect)."""
        return SimpleSocketStrategy().check(target)

    def is_healthy(self, target: str) -> bool:
        """
        Health-check the target service.

        Args:
            target: Target address (e.g., "localhost:8080")

        Returns:
            Health status (True=healthy, False=unhealthy)
        """
        return self._strategy.check(target)

    @property
    def strategy_name(self) -> str:
        """Name of the strategy currently in use."""
        return self._strategy.get_name()

    def invalidate(self, target: str) -> None:
        """Invalidate a specific target's cache entry (TTLCacheStrategy only)."""
        if isinstance(self._strategy, TTLCacheStrategy):
            self._strategy.invalidate(target)

    def invalidate_all(self) -> None:
        """Invalidate the entire cache (TTLCacheStrategy only)."""
        if isinstance(self._strategy, TTLCacheStrategy):
            self._strategy.invalidate_all()
