"""
Leader Election Base 인터페이스 테스트.

LeadershipState, LeaderInfo 등 기본 데이터 클래스 테스트.
"""

from datetime import UTC, datetime

from baldur.coordination.base import (
    LeaderInfo,
    LeadershipState,
)


class TestLeadershipState:
    """LeadershipState Enum 테스트."""

    def test_all_states_exist(self):
        """모든 상태가 정의되어 있는지 확인."""
        assert LeadershipState.NOT_STARTED.value == "not_started"
        assert LeadershipState.FOLLOWER.value == "follower"
        assert LeadershipState.LEADER.value == "leader"
        assert LeadershipState.STOPPING.value == "stopping"
        assert LeadershipState.STOPPED.value == "stopped"

    def test_state_count(self):
        """상태 개수 확인."""
        assert len(LeadershipState) == 5


class TestLeaderInfo:
    """LeaderInfo 데이터 클래스 테스트."""

    def test_creation(self):
        """LeaderInfo 생성 테스트."""
        now = datetime.now(UTC)
        expires = datetime.now(UTC)

        info = LeaderInfo(
            node_id="test-node",
            elected_at=now,
            lease_expires_at=expires,
            fencing_token=42,
            region_priority=10,
            is_self=True,
        )

        assert info.node_id == "test-node"
        assert info.elected_at == now
        assert info.lease_expires_at == expires
        assert info.fencing_token == 42
        assert info.region_priority == 10
        assert info.is_self is True

    def test_default_values(self):
        """기본값 확인."""
        now = datetime.now(UTC)

        info = LeaderInfo(
            node_id="test-node",
            elected_at=now,
            lease_expires_at=now,
        )

        assert info.fencing_token == 0
        assert info.region_priority == 100
        assert info.is_self is False
