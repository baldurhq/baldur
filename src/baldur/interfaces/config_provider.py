"""
Configuration Provider Interface

Abstract interface for configuration providers, decoupling the baldur
core from specific configuration sources (Django settings, environment variables,
YAML files, etc.)

Design Principles:
1. Pure Python - no framework dependencies
2. Pluggable configuration sources
3. Type-safe configuration access
4. Default values for all settings

Usage:
    from baldur.interfaces import ConfigProviderInterface

    class EnvConfigProvider(ConfigProviderInterface):
        def get(self, key: str, default=None) -> Any:
            return os.environ.get(key, default)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ConfigProviderInterface(ABC):
    """
    Abstract interface for configuration providers.

    Implementations can load config from:
    - Django settings
    - Environment variables
    - YAML/JSON files
    - Consul/etcd
    - AWS Parameter Store
    """

    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value.

        Args:
            key: Configuration key (dot notation supported, e.g., "BALDUR.SLA.DEFAULT_HOURS")
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        pass

    @abstractmethod
    def get_nested(self, *keys: str, default: Any = None) -> Any:
        """
        Get a nested configuration value.

        Args:
            keys: Path to the configuration value
            default: Default value if path not found

        Returns:
            Configuration value or default

        Example:
            provider.get_nested("BALDUR", "SLA", "DEFAULT_HOURS", default=24)
        """
        pass

    @abstractmethod
    def get_section(self, section: str) -> dict[str, Any]:
        """
        Get an entire configuration section as a dictionary.

        Args:
            section: Section name (e.g., "BALDUR")

        Returns:
            Dictionary of configuration values
        """
        pass

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a boolean configuration value."""
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    def get_int(self, key: str, default: int = 0) -> int:
        """Get an integer configuration value."""
        value = self.get(key, default)
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        """Get a float configuration value."""
        value = self.get(key, default)
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def get_str(self, key: str, default: str = "") -> str:
        """Get a string configuration value."""
        value = self.get(key, default)
        return str(value) if value is not None else default


class DictConfigProvider(ConfigProviderInterface):
    """
    Simple dictionary-based configuration provider.

    Useful for testing and simple deployments.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self._config = config or {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value using dot notation."""
        keys = key.split(".")
        return self._get_from_dict(self._config, keys, default)

    def get_nested(self, *keys: str, default: Any = None) -> Any:
        """Get a nested configuration value."""
        return self._get_from_dict(self._config, list(keys), default)

    def get_section(self, section: str) -> dict[str, Any]:
        """Get an entire configuration section."""
        value = self.get(section, {})
        return value if isinstance(value, dict) else {}

    def _get_from_dict(self, d: dict, keys: list[str], default: Any) -> Any:
        """Recursively get a value from nested dictionary."""
        if not keys:
            return d

        key = keys[0]
        if not isinstance(d, dict) or key not in d:
            return default

        if len(keys) == 1:
            return d[key]

        return self._get_from_dict(d[key], keys[1:], default)

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value (for testing)."""
        keys = key.split(".")
        d = self._config
        for k in keys[:-1]:
            if k not in d:
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    def update(self, config: dict[str, Any]) -> None:
        """Update configuration with new values."""
        self._deep_update(self._config, config)

    def _deep_update(self, base: dict, updates: dict) -> None:
        """Recursively update nested dictionary."""
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_update(base[key], value)
            else:
                base[key] = value


class EnvConfigProvider(ConfigProviderInterface):
    """
    Environment variable based configuration provider.

    Converts nested keys to environment variable format:
    - "BALDUR.SLA.DEFAULT_HOURS" -> "BALDUR__SLA__DEFAULT_HOURS"

    Supports JSON parsing for complex values.
    """

    def __init__(self, prefix: str = "", separator: str = "__"):
        import os

        self._os = os
        self._prefix = prefix
        self._separator = separator

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value from environment."""
        env_key = self._to_env_key(key)
        value = self._os.environ.get(env_key)

        if value is None:
            return default

        return self._parse_value(value)

    def get_nested(self, *keys: str, default: Any = None) -> Any:
        """Get a nested configuration value."""
        key = ".".join(keys)
        return self.get(key, default)

    def get_section(self, section: str) -> dict[str, Any]:
        """
        Get all environment variables with given prefix as a dict.

        Note: This is limited compared to file-based config.
        """
        prefix = self._to_env_key(section) + self._separator
        result: dict[str, Any] = {}

        for key, value in self._os.environ.items():
            if key.startswith(prefix):
                # Remove prefix and convert back to nested keys
                remaining = key[len(prefix) :]
                keys = remaining.split(self._separator)
                self._set_nested(result, keys, self._parse_value(value))

        return result

    def _to_env_key(self, key: str) -> str:
        """Convert dot notation to environment variable format."""
        env_key = key.replace(".", self._separator).upper()
        if self._prefix:
            return f"{self._prefix}{self._separator}{env_key}"
        return env_key

    def _parse_value(self, value: str) -> Any:
        """Parse environment variable value (handles JSON, booleans, numbers)."""
        import json

        # Try JSON first
        if value.startswith("{") or value.startswith("["):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass

        # Boolean
        if value.lower() in ("true", "false"):
            return value.lower() == "true"

        # Number
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            pass

        return value

    def _set_nested(self, d: dict, keys: list[str], value: Any) -> None:
        """Set a nested value in dictionary."""
        for key in keys[:-1]:
            key = key.lower()
            if key not in d:
                d[key] = {}
            d = d[key]
        d[keys[-1].lower()] = value
