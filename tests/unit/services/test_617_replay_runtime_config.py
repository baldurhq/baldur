"""Unit tests for 617 D1/D2 — RuntimeConfig reader severity split + helper.

Covers the ``_get_replay_automation_config`` resolve-helper that replaced the
six copy-pasted RuntimeConfig resolve blocks (D2), and the D1 severity split:

- **Absent** (no PRO ``RuntimeConfigManager`` registered — the OSS-normal
  state): DEBUG ``replay_service.runtime_config_absent`` at most once per
  service instance.
- **Read failure** (manager present and ``get_config()`` raises, or provider
  resolution itself raises): WARNING ``replay_service.runtime_config_read_failed``
  on every occurrence, falling back to the absent default (None).

The six readers (``_is_adaptive_enabled`` ... ``_get_adaptive_config``) are
verified against their None-defaults (the absent/raising path collapses to the
same None at the reader boundary; the distinct severity is asserted in the
behavior class above).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.factory.registry import ProviderRegistry
from baldur.services.adaptive_replay import AdaptiveReplayConfig
from baldur.services.replay_service import ReplayService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def service() -> ReplayService:
    """ReplayService with an injected mock repository (no infra)."""
    return ReplayService(repository=MagicMock())


def _present_manager(config_value: dict | None) -> MagicMock:
    """A stand-in RuntimeConfigManager whose ``get_config`` returns a dict.

    Spec-less: the real ``RuntimeConfigManager`` lives in ``baldur_pro`` and is
    not importable in an OSS checkout (§6.2 Exception 1).
    """
    manager = MagicMock()
    manager.get_config.return_value = config_value
    return manager


# =============================================================================
# D1/D2 — _get_replay_automation_config resolve + severity split
# =============================================================================


class TestReplayRuntimeConfigBehavior:
    """``_get_replay_automation_config`` resolve + D1 severity/cadence."""

    def test_manager_absent_returns_none_and_logs_debug_once_per_instance(
        self, service
    ):
        """Absent manager → None, DEBUG runtime_config_absent at most once."""
        # Given — the runtime_config_manager slot resolves to None (OSS-normal)
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
        ):
            # When — the reader helper is invoked twice on the same instance
            with capture_logs() as logs:
                first = service._get_replay_automation_config()
                second = service._get_replay_automation_config()

        # Then — both calls return None (the absent default)
        assert first is None
        assert second is None

        # And the absent marker is DEBUG and emitted exactly once (cadence)
        absent = [
            e for e in logs if e.get("event") == "replay_service.runtime_config_absent"
        ]
        assert len(absent) == 1
        assert absent[0]["log_level"] == "debug"
        assert service._runtime_config_absent_logged is True

        # And no WARNING read-failure event is emitted on the absent path
        assert not [
            e
            for e in logs
            if e.get("event") == "replay_service.runtime_config_read_failed"
        ]

    def test_manager_present_returns_config_block_via_public_accessor(self, service):
        """Present manager → get_config('replay_automation') result is returned."""
        # Given — a registered manager returning a replay_automation block
        config_block = {"adaptive_enabled": True}
        manager = _present_manager(config_block)

        # When
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=manager
        ):
            result = service._get_replay_automation_config()

        # Then — the public accessor is consulted with the documented key
        assert result == config_block
        manager.get_config.assert_called_once_with("replay_automation")

    def test_get_config_raises_logs_warning_every_call_and_returns_none(self, service):
        """get_config() raising → WARNING read_failed on EVERY call, None default."""
        # Given — a present manager whose get_config raises
        manager = MagicMock()
        manager.get_config.side_effect = RuntimeError("config store down")

        # When — invoked twice
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=manager
        ):
            with capture_logs() as logs:
                first = service._get_replay_automation_config()
                second = service._get_replay_automation_config()

        # Then — fail-safe default (None) both times
        assert first is None
        assert second is None

        # And the read-failure is WARNING on every occurrence (not once-per-instance)
        failures = [
            e
            for e in logs
            if e.get("event") == "replay_service.runtime_config_read_failed"
        ]
        assert len(failures) == 2
        assert all(e["log_level"] == "warning" for e in failures)

    def test_provider_resolution_raising_logs_warning_and_returns_none(self, service):
        """safe_get() itself raising → WARNING read_failed, None default.

        Covers the third case (provider construction failure) the helper folds
        into the same try/except as get_config — a registered provider whose
        resolution raises must fail safe, not crash the replay path.
        """
        # Given — provider resolution raises (construction failure)
        with patch.object(
            ProviderRegistry.runtime_config_manager,
            "safe_get",
            side_effect=RuntimeError("provider build failed"),
        ):
            with capture_logs() as logs:
                result = service._get_replay_automation_config()

        # Then — fail-safe default and a WARNING read-failure
        assert result is None
        failures = [
            e
            for e in logs
            if e.get("event") == "replay_service.runtime_config_read_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"

    def test_late_manager_registration_is_picked_up_per_call(self, service):
        """Resolution is per-call — a late PRO registration is picked up.

        Absent on the first call, present on the second: the helper must
        re-resolve (not cache the absence), so the configured block surfaces.
        """
        config_block = {"priority_enabled": True}
        manager = _present_manager(config_block)

        # First call — absent
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
        ):
            assert service._get_replay_automation_config() is None

        # Second call — manager now registered
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=manager
        ):
            assert service._get_replay_automation_config() == config_block


# =============================================================================
# D2 — the six readers over their None-defaults
# =============================================================================

# (reader callable, expected None-default) — the absent/raising path
_READER_NONE_DEFAULTS = [
    (lambda svc: svc._is_adaptive_enabled(), False),
    (lambda svc: svc._is_priority_enabled(), False),
    (lambda svc: svc._get_domain_priorities(), {}),
    (lambda svc: svc._get_domain_max_retries("payment"), None),
    (lambda svc: svc._load_failure_type_map(), {}),
]

_READER_NONE_DEFAULT_IDS = [
    "is_adaptive_enabled",
    "is_priority_enabled",
    "get_domain_priorities",
    "get_domain_max_retries",
    "load_failure_type_map",
]

# (reader callable, config block, expected mapped value)
_READER_PRESENT_CASES = [
    (lambda svc: svc._is_adaptive_enabled(), {"adaptive_enabled": True}, True),
    (lambda svc: svc._is_priority_enabled(), {"priority_enabled": True}, True),
    (
        lambda svc: svc._get_domain_priorities(),
        {"domain_priorities": {"payment": "critical"}},
        {"payment": "critical"},
    ),
    (
        lambda svc: svc._get_domain_max_retries("payment"),
        {"domain_max_retries": {"payment": 7}},
        7,
    ),
    (
        lambda svc: svc._load_failure_type_map(),
        {"service_failure_type_map": {"payment_api": ["PG_TIMEOUT"]}},
        {"payment_api": ["PG_TIMEOUT"]},
    ),
]


class TestReplayRuntimeConfigReaders:
    """The six RuntimeConfig readers map config keys / fall back to defaults."""

    @pytest.mark.parametrize(
        ("reader", "expected_default"),
        _READER_NONE_DEFAULTS,
        ids=_READER_NONE_DEFAULT_IDS,
    )
    def test_reader_returns_none_default_when_config_unavailable(
        self, service, reader, expected_default
    ):
        """Reader falls back to its None-default when the config block is None.

        The absent and read-failure paths both surface as a None config block
        at the reader boundary, so this single None-stub covers both.
        """
        with patch.object(service, "_get_replay_automation_config", return_value=None):
            assert reader(service) == expected_default

    @pytest.mark.parametrize(
        ("reader", "config_block", "expected"),
        _READER_PRESENT_CASES,
        ids=_READER_NONE_DEFAULT_IDS,
    )
    def test_reader_returns_mapped_value_when_config_present(
        self, service, reader, config_block, expected
    ):
        """Reader returns the configured value when the config block is present."""
        with patch.object(
            service, "_get_replay_automation_config", return_value=config_block
        ):
            assert reader(service) == expected

    def test_get_adaptive_config_returns_default_config_when_unavailable(self, service):
        """_get_adaptive_config falls back to a default AdaptiveReplayConfig."""
        with patch.object(service, "_get_replay_automation_config", return_value=None):
            result = service._get_adaptive_config()

        assert isinstance(result, AdaptiveReplayConfig)
        assert result == AdaptiveReplayConfig()

    def test_get_adaptive_config_maps_config_keys_when_present(self, service):
        """_get_adaptive_config maps RuntimeConfig keys to config fields."""
        config_block = {
            "adaptive_min_items": 5,
            "adaptive_max_items": 200,
            "adaptive_initial_items": 25,
            "adaptive_failure_threshold": 0.5,
        }

        with patch.object(
            service, "_get_replay_automation_config", return_value=config_block
        ):
            result = service._get_adaptive_config()

        assert result.min_items == 5
        assert result.max_items == 200
        assert result.initial_items == 25
        assert result.failure_threshold == 0.5
