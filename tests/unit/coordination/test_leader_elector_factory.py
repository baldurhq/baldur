"""
Leader Elector factory tests.

Target under test:
- get_leader_elector: 599 D4 rewire — (enabled x backend x
  provider-registered) resolution matrix over ProviderRegistry
- reset_leader_electors: cache clear + stop

Verification techniques:
- State-based: returned elector type per matrix cell
- Dependency interaction: provider class constructor calls
- Boundary values: supported / unsupported backends
- Singleton behaviour: same resource -> same instance
- Observability: WARNING event on enabled-without-provider
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog.testing

from baldur.coordination.config import (
    LeaderElectionSettings,
    reset_leader_election_settings,
)
from baldur.coordination.factory import (
    get_leader_elector,
    reset_leader_electors,
)
from baldur.coordination.noop_elector import NoOpLeaderElector


@pytest.fixture(autouse=True)
def cleanup():
    """Reset elector cache and settings around each test."""
    reset_leader_electors()
    reset_leader_election_settings()
    yield
    reset_leader_electors()
    reset_leader_election_settings()


@pytest.fixture
def elector_slot():
    """Snapshot/restore the leader_elector registry slot."""
    from baldur.factory.registry import ProviderRegistry

    slot = ProviderRegistry.leader_elector
    snapshot = slot.save_state()
    yield slot
    slot.restore_state(snapshot)


class TestGetLeaderElectorDisabledShortCircuitBehavior:
    """599 D4 step 1 — enabled=False (default) returns NoOp immediately."""

    def test_disabled_default_returns_noop(self):
        """Default settings (enabled=False) -> NoOpLeaderElector."""
        settings = LeaderElectionSettings(node_id="test-node")
        assert settings.enabled is False

        elector = get_leader_elector("test-resource", settings=settings)

        assert isinstance(elector, NoOpLeaderElector)

    def test_disabled_emits_no_warning(self, elector_slot):
        """Default path performs no provider lookup and logs no warning."""
        elector_slot.reset()  # even with NO provider registered at all
        settings = LeaderElectionSettings(node_id="test-node")

        with structlog.testing.capture_logs() as logs:
            elector = get_leader_elector("silent-resource", settings=settings)

        assert isinstance(elector, NoOpLeaderElector)
        assert not [
            e for e in logs if e["event"] == "leader_election.provider_unavailable"
        ]

    def test_disabled_noop_never_leads(self):
        """The default-path elector never claims leadership (start is safe)."""
        settings = LeaderElectionSettings(node_id="test-node")
        elector = get_leader_elector("never-leader", settings=settings)

        elector.start()
        assert elector.is_leader() is False
        elector.stop()


class TestGetLeaderElectorRedisBackendBehavior:
    """599 D4 steps 2-3 — enabled redis backend resolves via ProviderRegistry."""

    def test_enabled_redis_resolves_registered_provider(self, elector_slot):
        """enabled=True + registered 'redis' provider -> provider instance."""
        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        elector_slot.register("redis", mock_class)

        settings = LeaderElectionSettings(
            enabled=True, backend="redis", node_id="test-node"
        )
        elector = get_leader_elector("test-resource", settings=settings)

        mock_class.assert_called_once_with("test-resource", settings)
        assert elector is mock_instance

    def test_enabled_redis_without_provider_warns_and_returns_noop(self, elector_slot):
        """enabled=True without the redis provider -> WARNING + NoOp (fail-safe)."""
        elector_slot.reset()  # clean OSS install: no redis provider

        settings = LeaderElectionSettings(
            enabled=True, backend="redis", node_id="test-node"
        )
        with structlog.testing.capture_logs() as logs:
            elector = get_leader_elector("oss-resource", settings=settings)

        assert isinstance(elector, NoOpLeaderElector)
        warnings = [
            e for e in logs if e["event"] == "leader_election.provider_unavailable"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["backend"] == "redis"

    def test_singleton_same_resource_same_instance(self, elector_slot):
        """Same resource name returns the same instance (single construction)."""
        mock_class = MagicMock()
        mock_class.return_value = MagicMock()
        elector_slot.register("redis", mock_class)

        settings = LeaderElectionSettings(
            enabled=True, backend="redis", node_id="test-node"
        )
        elector1 = get_leader_elector("same-resource", settings=settings)
        elector2 = get_leader_elector("same-resource", settings=settings)

        assert elector1 is elector2
        assert mock_class.call_count == 1

    def test_different_resources_different_instances(self, elector_slot):
        """Different resource names construct separate instances."""
        mock_class = MagicMock(side_effect=[MagicMock(), MagicMock()])
        elector_slot.register("redis", mock_class)

        settings = LeaderElectionSettings(
            enabled=True, backend="redis", node_id="test-node"
        )
        elector1 = get_leader_elector("resource-a", settings=settings)
        elector2 = get_leader_elector("resource-b", settings=settings)

        assert elector1 is not elector2
        assert mock_class.call_count == 2


class TestGetLeaderElectorKubernetesBackendBehavior:
    """528 D10-v2 routing unchanged — explicit k8s backend."""

    def test_creates_k8s_elector_when_backend_kubernetes(self, elector_slot):
        """backend='kubernetes' resolves via ProviderRegistry.leader_elector."""
        mock_class = MagicMock()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        elector_slot.register("k8s", mock_class)

        settings = LeaderElectionSettings(
            enabled=True, backend="kubernetes", node_id="test-node"
        )
        elector = get_leader_elector("test-resource", settings=settings)

        mock_class.assert_called_once()
        assert elector is mock_instance

    def test_raises_runtime_error_when_k8s_not_registered(self, elector_slot):
        """RuntimeError when no k8s provider is registered (explicit backend)."""
        elector_slot.reset()  # clean OSS install without baldur_dormant

        settings = LeaderElectionSettings(
            enabled=True, backend="kubernetes", node_id="test-node"
        )
        with pytest.raises(RuntimeError, match="kubernetes leader elector"):
            get_leader_elector("k8s-resource", settings=settings)


class TestGetLeaderElectorUnknownBackendBehavior:
    """Boundary: unsupported backend names."""

    def test_raises_for_unknown_backend(self):
        """Unknown backends are rejected by Pydantic validation."""
        # The backend field is Literal["redis", "kubernetes"], so invalid
        # values are already rejected at settings construction.
        with pytest.raises(Exception):
            LeaderElectionSettings(backend="unknown", node_id="test")


class TestResetLeaderElectors:
    """reset_leader_electors behaviour."""

    def test_stops_all_electors(self, elector_slot):
        """Stops every cached elector."""
        mock_elector = MagicMock()
        mock_class = MagicMock(return_value=mock_elector)
        elector_slot.register("redis", mock_class)

        settings = LeaderElectionSettings(
            enabled=True, backend="redis", node_id="test-node"
        )
        get_leader_elector("resource-a", settings=settings)
        get_leader_elector("resource-b", settings=settings)

        reset_leader_electors()

        assert mock_elector.stop.call_count == 2

    def test_clears_cache(self, elector_slot):
        """A fresh instance is constructed after reset."""
        mock_class = MagicMock(return_value=MagicMock())
        elector_slot.register("redis", mock_class)

        settings = LeaderElectionSettings(
            enabled=True, backend="redis", node_id="test-node"
        )
        get_leader_elector("test-resource", settings=settings)
        reset_leader_electors()

        get_leader_elector("test-resource", settings=settings)
        assert mock_class.call_count == 2
