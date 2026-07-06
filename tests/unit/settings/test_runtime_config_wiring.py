"""Runtime-config editor wiring proof (686 D1/D2/D4).

These tests prove that a value *applied* to the ``RuntimeConfigManager`` (what a
console editor edit persists) is *observed* by each formerly-echo-only domain's
behavioral consumer without a process restart — the end-to-end guarantee the
console advertises. They register a duck-typed fake at
``ProviderRegistry.runtime_config_manager`` (the OSS Protocol,
``interfaces/runtime_config.py``) so no ``baldur_pro`` import is needed and no
``requires_pro`` marker applies.

Distinct from the repointed dispatch tests: those prove each consumer reads the
*right seam*; these prove a manager-applied value flows through that seam to the
consumer (SC2). Every test name carries ``runtime_wiring`` (SC2's ``-k`` filter).

Verification techniques applied:
- Dependency interaction: consumer reads the registered manager's value
- State/fallback: fail-open to the env base on manager-absent / manager-error
  (686 D2, CROSS_SERVICE_STANDARDS §3 Optional integration)
- Data-driven parametrize: seam parity across all 6 wired domains (686 D5)
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from baldur.core.idempotency_gate import IdempotencyGate
from baldur.factory.registry import ProviderRegistry
from baldur.services.security.models import SecurityConfig
from baldur.settings.forensic import ForensicSettings
from baldur.settings.idempotency import IdempotencySettings
from baldur.settings.layered_provider import (
    get_layered_settings,
    reset_layered_settings_cached,
)
from baldur.settings.metrics import MetricsSettings
from baldur.settings.notification import NotificationSettings
from baldur.settings.security import SecuritySettings
from baldur.settings.sla import SLASettings


class _FakeRuntimeConfigManager:
    """Duck-typed OSS stand-in for the PRO RuntimeConfigManager.

    Implements only ``_get_config`` — the single method the layered provider
    calls to overlay runtime (console-applied) values.
    """

    def __init__(self, configs: dict[str, dict[str, Any]]):
        self._configs = configs

    def _get_config(self, section: str) -> dict[str, Any]:
        return dict(self._configs.get(section, {}))


class _RaisingRuntimeConfigManager:
    """Manager whose backend read fails — exercises the D2 fail-open path."""

    def _get_config(self, section: str) -> dict[str, Any]:
        raise RuntimeError("simulated runtime-config backend failure")


@pytest.fixture(autouse=True)
def _reset_layered_cache():
    reset_layered_settings_cached()
    yield
    reset_layered_settings_cached()


# ── SC2 representative 1: security (safety-critical) ──────────────────────────


class TestSecurityRuntimeWiringBehavior:
    def test_security_runtime_wiring_applied_ban_threshold_observed_without_restart(
        self,
    ):
        # Given an operator-applied security edit on the manager
        base = SecuritySettings().permanent_ban_threshold
        applied = 42
        assert applied != base  # the edit must actually change the value
        fake = _FakeRuntimeConfigManager(
            {"security": {"permanent_ban_threshold": applied}}
        )

        # When the security consumer loads its config (no restart)
        with ProviderRegistry.runtime_config_manager.override(fake):
            config = SecurityConfig.from_settings()

        # Then it observes the applied value
        assert config.permanent_ban_threshold == applied

    def test_security_runtime_wiring_falls_open_to_env_when_manager_absent(self):
        # Given no RuntimeConfigManager registered (the OSS-install baseline)
        base = SecuritySettings().permanent_ban_threshold

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            config = SecurityConfig.from_settings()

        # Then the consumer reads the env base — identical to pre-editor behavior
        assert config.permanent_ban_threshold == base

    def test_security_runtime_wiring_falls_open_to_env_when_manager_raises(self):
        # Given a manager whose backend read fails
        base = SecuritySettings().permanent_ban_threshold
        fake = _RaisingRuntimeConfigManager()

        with ProviderRegistry.runtime_config_manager.override(fake):
            config = SecurityConfig.from_settings()

        # Then the read fails open to the env base (no safety hole)
        assert config.permanent_ban_threshold == base


# ── SC2 representative 2: idempotency (safety-critical, hot-path cached) ───────


class TestIdempotencyRuntimeWiringBehavior:
    def test_idempotency_runtime_wiring_applied_memory_ttl_observed_within_cache_ttl(
        self,
    ):
        # Given a settings-driven gate (no constructor override) and an applied edit
        base = IdempotencySettings().gate_memory_ttl_seconds
        applied = 4242
        assert applied != base
        gate = IdempotencyGate(cache=None)
        fake = _FakeRuntimeConfigManager(
            {"idempotency": {"gate_memory_ttl_seconds": applied}}
        )

        # When the gate resolves its effective dedup window on the next read
        with ProviderRegistry.runtime_config_manager.override(fake):
            reset_layered_settings_cached()  # fresh read under the override
            effective = gate._effective_memory_ttl()

        # Then it observes the applied value through the cached layered seam
        assert effective == timedelta(seconds=applied)

    def test_idempotency_runtime_wiring_falls_open_to_env_when_manager_absent(self):
        # Given no manager registered
        base = IdempotencySettings().gate_memory_ttl_seconds
        gate = IdempotencyGate(cache=None)

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            reset_layered_settings_cached()
            effective = gate._effective_memory_ttl()

        # Then the effective window is the env base
        assert effective == timedelta(seconds=base)

    def test_idempotency_runtime_wiring_constructor_ttl_ignores_manager_edit(self):
        """686 D4 reach class: a construction-captured ``memory_ttl_seconds``
        bypasses the layered read entirely, so a manager edit is NOT observed
        until the gate is reconstructed (worker restart)."""
        # Given a gate whose window was pinned at construction
        pinned = 900
        drift = 4242
        assert pinned != drift
        gate = IdempotencyGate(cache=None, memory_ttl_seconds=pinned)
        fake = _FakeRuntimeConfigManager(
            {"idempotency": {"gate_memory_ttl_seconds": drift}}
        )

        # When a divergent value is applied on the manager
        with ProviderRegistry.runtime_config_manager.override(fake):
            reset_layered_settings_cached()
            effective = gate._effective_memory_ttl()

        # Then the constructor value wins; the manager edit is not observed
        assert effective == timedelta(seconds=pinned)


# ── T4: seam parity across all 6 wired domains (686 D5) ───────────────────────

# (domain, SettingsClass, representative editable field, applied value != default)
_SEAM_CASES = [
    ("sla", SLASettings, "default_hours", 99),
    ("security", SecuritySettings, "permanent_ban_threshold", 42),
    ("idempotency", IdempotencySettings, "gate_memory_ttl_seconds", 4242),
    ("notification", NotificationSettings, "critical_threshold", 77),
    ("forensic", ForensicSettings, "error_message_max_length", 1234),
    ("metrics", MetricsSettings, "max_registered_domains", 123),
]
_SEAM_IDS = [case[0] for case in _SEAM_CASES]


class TestSixDomainSeamRuntimeWiringBehavior:
    """Every one of the 6 formerly-echo-only domains honors the layered
    provider's manager-wins / env-fallback contract (686 D1/D2)."""

    @pytest.mark.parametrize(
        ("domain", "settings_class", "field", "applied"), _SEAM_CASES, ids=_SEAM_IDS
    )
    def test_seam_runtime_wiring_manager_value_overrides_env(
        self, domain, settings_class, field, applied
    ):
        base = getattr(settings_class(), field)
        assert applied != base  # meaningful override
        fake = _FakeRuntimeConfigManager({domain: {field: applied}})

        with ProviderRegistry.runtime_config_manager.override(fake):
            result = get_layered_settings(settings_class, domain)

        assert getattr(result, field) == applied

    @pytest.mark.parametrize(
        ("domain", "settings_class", "field", "applied"), _SEAM_CASES, ids=_SEAM_IDS
    )
    def test_seam_runtime_wiring_manager_absent_falls_back_to_env(
        self, domain, settings_class, field, applied
    ):
        expected = getattr(settings_class(), field)

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            result = get_layered_settings(settings_class, domain)

        assert getattr(result, field) == expected

    @pytest.mark.parametrize(
        ("domain", "settings_class", "field", "applied"), _SEAM_CASES, ids=_SEAM_IDS
    )
    def test_seam_runtime_wiring_manager_raises_falls_open_to_env(
        self, domain, settings_class, field, applied
    ):
        expected = getattr(settings_class(), field)
        fake = _RaisingRuntimeConfigManager()

        with ProviderRegistry.runtime_config_manager.override(fake):
            result = get_layered_settings(settings_class, domain)

        assert getattr(result, field) == expected
