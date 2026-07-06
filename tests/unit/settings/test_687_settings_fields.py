"""Unit tests for the new operational-value settings fields (687).

Contract tests pin the design-doc default and range of every new BALDUR_* field
introduced by the operational-value-configurability sweep:

- CircuitBreakerSettings.self_ddos_backoff_{base_seconds,max_seconds,jitter_factor}
- MetaWatchdogSettings.docker_{restart,scale}_timeout_seconds
- RateLimitSettings.memory_cleanup_interval_ops
- ThrottleSettings.audit_queue_maxsize

RedisSettings.probe_connect_timeout and HttpClientSettings.webhook_retry_* keep
their own contract tests alongside their existing settings-test files; the
consolidated ``TestNewFieldBoundsContract`` at the bottom re-asserts the
out-of-range rejection for all ten new fields in one place (the negative-bounds
success criterion anchor).
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from pydantic import ValidationError

from baldur.settings.circuit_breaker import (
    CircuitBreakerSettings,
    reset_circuit_breaker_settings,
)
from baldur.settings.http_client import HttpClientSettings
from baldur.settings.meta_watchdog import MetaWatchdogSettings
from baldur.settings.rate_limit import RateLimitSettings
from baldur.settings.redis import RedisSettings
from baldur.settings.throttle import ThrottleSettings


class TestCircuitBreakerBackoffSettingsContract:
    """CircuitBreakerSettings self-DDoS adaptive-backoff trio — 687 D1."""

    def test_backoff_base_seconds_default_is_1(self):
        """self_ddos_backoff_base_seconds default: 1.0s."""
        reset_circuit_breaker_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            assert CircuitBreakerSettings().self_ddos_backoff_base_seconds == 1.0

    def test_backoff_max_seconds_default_is_60(self):
        """self_ddos_backoff_max_seconds default: 60.0s."""
        reset_circuit_breaker_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            assert CircuitBreakerSettings().self_ddos_backoff_max_seconds == 60.0

    def test_backoff_jitter_factor_default_is_025(self):
        """self_ddos_backoff_jitter_factor default: 0.25."""
        reset_circuit_breaker_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            assert CircuitBreakerSettings().self_ddos_backoff_jitter_factor == 0.25

    def test_backoff_trio_fields_exist(self):
        """All three new fields are declared on the settings model."""
        fields = CircuitBreakerSettings.model_fields
        assert "self_ddos_backoff_base_seconds" in fields
        assert "self_ddos_backoff_max_seconds" in fields
        assert "self_ddos_backoff_jitter_factor" in fields

    def test_backoff_base_seconds_env_override(self):
        """BALDUR_CB_SELF_DDOS_BACKOFF_BASE_SECONDS overrides the default."""
        reset_circuit_breaker_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_CB_SELF_DDOS_BACKOFF_BASE_SECONDS": "2.5"},
            clear=True,
        ):
            assert CircuitBreakerSettings().self_ddos_backoff_base_seconds == 2.5


class TestMetaWatchdogDockerTimeoutSettingsContract:
    """MetaWatchdogSettings docker recovery subprocess timeouts — 687 D3."""

    def test_docker_restart_timeout_default_is_60(self):
        """docker_restart_timeout_seconds default: 60.0s."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert MetaWatchdogSettings().docker_restart_timeout_seconds == 60.0

    def test_docker_scale_timeout_default_is_120(self):
        """docker_scale_timeout_seconds default: 120.0s."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert MetaWatchdogSettings().docker_scale_timeout_seconds == 120.0

    def test_docker_restart_timeout_minimum_boundary(self):
        """docker_restart_timeout_seconds ge=5.0: 4.9 fails, 5.0 passes."""
        with pytest.raises(ValidationError):
            MetaWatchdogSettings(docker_restart_timeout_seconds=4.9)
        assert MetaWatchdogSettings(docker_restart_timeout_seconds=5.0)

    def test_docker_scale_timeout_maximum_boundary(self):
        """docker_scale_timeout_seconds le=600.0: 600.0 passes, 601.0 fails."""
        assert MetaWatchdogSettings(docker_scale_timeout_seconds=600.0)
        with pytest.raises(ValidationError):
            MetaWatchdogSettings(docker_scale_timeout_seconds=601.0)


class TestRateLimitMemoryCleanupSettingsContract:
    """RateLimitSettings.memory_cleanup_interval_ops — 687 D4."""

    def test_memory_cleanup_interval_default_is_100(self):
        """memory_cleanup_interval_ops default: 100 operations."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert RateLimitSettings().memory_cleanup_interval_ops == 100

    def test_memory_cleanup_interval_minimum_boundary(self):
        """memory_cleanup_interval_ops ge=1 (LargeCount): 0 fails, 1 passes."""
        with pytest.raises(ValidationError):
            RateLimitSettings(memory_cleanup_interval_ops=0)
        assert RateLimitSettings(memory_cleanup_interval_ops=1)

    def test_memory_cleanup_interval_maximum_boundary(self):
        """memory_cleanup_interval_ops le=1000 (LargeCount): 1000 passes, 1001 fails."""
        assert RateLimitSettings(memory_cleanup_interval_ops=1000)
        with pytest.raises(ValidationError):
            RateLimitSettings(memory_cleanup_interval_ops=1001)


class TestThrottleAuditQueueSettingsContract:
    """ThrottleSettings.audit_queue_maxsize — 687 D6."""

    def test_audit_queue_maxsize_default_is_10000(self):
        """audit_queue_maxsize default: 10000."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert ThrottleSettings().audit_queue_maxsize == 10000

    def test_audit_queue_maxsize_minimum_boundary(self):
        """audit_queue_maxsize ge=100: 99 fails, 100 passes."""
        with pytest.raises(ValidationError):
            ThrottleSettings(audit_queue_maxsize=99)
        assert ThrottleSettings(audit_queue_maxsize=100)

    def test_audit_queue_maxsize_maximum_boundary(self):
        """audit_queue_maxsize le=100000: 100000 passes, 100001 fails."""
        assert ThrottleSettings(audit_queue_maxsize=100000)
        with pytest.raises(ValidationError):
            ThrottleSettings(audit_queue_maxsize=100001)


class TestNewFieldBoundsContract:
    """Every new 687 field rejects out-of-range values (negative-bounds SC).

    One row per (below ge, above le) boundary for all ten new fields across the
    five settings classes. References every new field name so the settings-suite
    ValidationError coverage is provably complete.
    """

    @pytest.mark.parametrize(
        ("settings_cls", "field", "bad_value"),
        [
            (CircuitBreakerSettings, "self_ddos_backoff_base_seconds", 0.05),
            (CircuitBreakerSettings, "self_ddos_backoff_base_seconds", 60.1),
            (CircuitBreakerSettings, "self_ddos_backoff_max_seconds", 0.5),
            (CircuitBreakerSettings, "self_ddos_backoff_max_seconds", 3601.0),
            (CircuitBreakerSettings, "self_ddos_backoff_jitter_factor", -0.1),
            (CircuitBreakerSettings, "self_ddos_backoff_jitter_factor", 1.1),
            (RedisSettings, "probe_connect_timeout", 0.05),
            (RedisSettings, "probe_connect_timeout", 60.1),
            (MetaWatchdogSettings, "docker_restart_timeout_seconds", 4.9),
            (MetaWatchdogSettings, "docker_restart_timeout_seconds", 601.0),
            (MetaWatchdogSettings, "docker_scale_timeout_seconds", 4.9),
            (MetaWatchdogSettings, "docker_scale_timeout_seconds", 601.0),
            (RateLimitSettings, "memory_cleanup_interval_ops", 0),
            (RateLimitSettings, "memory_cleanup_interval_ops", 1001),
            (ThrottleSettings, "audit_queue_maxsize", 99),
            (ThrottleSettings, "audit_queue_maxsize", 100001),
            (HttpClientSettings, "webhook_retry_total", -1),
            (HttpClientSettings, "webhook_retry_total", 6),
            (HttpClientSettings, "webhook_retry_backoff_factor", -0.1),
            (HttpClientSettings, "webhook_retry_backoff_factor", 5.1),
        ],
    )
    def test_out_of_range_value_raises_validation_error(
        self, settings_cls, field, bad_value
    ):
        """An out-of-range value past ge/le raises pydantic ValidationError."""
        with pytest.raises(ValidationError):
            settings_cls(**{field: bad_value})
