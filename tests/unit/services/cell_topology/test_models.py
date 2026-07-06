"""
CellState / CellInfo 모델 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: CellState 열거값, CellInfo 기본값, 상태 우선순위 계약 검증,
            L2 sync 필드 목록, to_l2_dict Redis hash 구조 (doc 388)
- Behavior: CellInfo 인스턴스 동작 검증, LWW+MRW hybrid apply_l2_dict (doc 388),
            MRW-only fallback, metadata safe defaults, serialization roundtrip

참조 소스:
- services/cell_topology/models.py (CellState, CellInfo, CELL_STATE_PRIORITY)
"""

from __future__ import annotations

from datetime import UTC

from baldur.services.cell_topology.models import (
    CELL_STATE_PRIORITY,
    CellInfo,
    CellState,
)


class TestCellStateContract:
    """CellState 열거값 계약 검증."""

    def test_active_value(self):
        """ACTIVE 값: 'active'."""
        assert CellState.ACTIVE.value == "active"

    def test_warmup_value(self):
        """WARMUP 값: 'warmup'."""
        assert CellState.WARMUP.value == "warmup"

    def test_draining_value(self):
        """DRAINING 값: 'draining'."""
        assert CellState.DRAINING.value == "draining"

    def test_isolated_value(self):
        """ISOLATED 값: 'isolated'."""
        assert CellState.ISOLATED.value == "isolated"

    def test_state_count(self):
        """CellState는 정확히 4개 상태를 가져야 한다."""
        assert len(CellState) == 4

    def test_is_str_enum(self):
        """CellState는 str 서브클래스여야 한다."""
        assert isinstance(CellState.ACTIVE, str)


class TestCellStatePriorityContract:
    """Cell 상태 우선순위 계약 검증 (Most Restrictive Wins)."""

    def test_active_priority_0(self):
        """ACTIVE 우선순위: 0 (가장 낮음)."""
        assert CELL_STATE_PRIORITY[CellState.ACTIVE] == 0

    def test_warmup_priority_1(self):
        """WARMUP 우선순위: 1."""
        assert CELL_STATE_PRIORITY[CellState.WARMUP] == 1

    def test_draining_priority_2(self):
        """DRAINING 우선순위: 2."""
        assert CELL_STATE_PRIORITY[CellState.DRAINING] == 2

    def test_isolated_priority_3(self):
        """ISOLATED 우선순위: 3 (가장 높음)."""
        assert CELL_STATE_PRIORITY[CellState.ISOLATED] == 3

    def test_all_states_have_priority(self):
        """모든 CellState에 우선순위가 정의되어야 한다."""
        for state in CellState:
            assert state in CELL_STATE_PRIORITY


class TestCellInfoContract:
    """CellInfo 기본값 계약 검증."""

    def test_default_state_is_active(self):
        """기본 상태: ACTIVE."""
        info = CellInfo(cell_id="cell-0")
        assert info.state == CellState.ACTIVE

    def test_default_health_score_1_0(self):
        """기본 건강도: 1.0."""
        info = CellInfo(cell_id="cell-0")
        assert info.health_score == 1.0

    def test_default_warmup_percentage_0(self):
        """기본 warmup_percentage: 0.0."""
        info = CellInfo(cell_id="cell-0")
        assert info.warmup_percentage == 0.0

    def test_default_assigned_services_empty(self):
        """기본 할당 서비스: 빈 set."""
        info = CellInfo(cell_id="cell-0")
        assert info.assigned_services == set()

    def test_default_metadata_empty(self):
        """기본 메타데이터: 빈 dict."""
        info = CellInfo(cell_id="cell-0")
        assert info.metadata == {}


class TestCellInfoBehavior:
    """CellInfo 동작 검증."""

    def test_cell_id_is_stored(self):
        """cell_id가 올바르게 저장되어야 한다."""
        info = CellInfo(cell_id="cell-5")
        assert info.cell_id == "cell-5"

    def test_created_at_is_utc(self):
        """생성 시각은 UTC여야 한다."""
        info = CellInfo(cell_id="cell-0")
        assert info.created_at.tzinfo == UTC

    def test_assigned_services_are_independent(self):
        """서로 다른 CellInfo의 assigned_services는 독립적이어야 한다."""
        info1 = CellInfo(cell_id="cell-0")
        info2 = CellInfo(cell_id="cell-1")
        info1.assigned_services.add("service-a")
        assert "service-a" not in info2.assigned_services

    def test_metadata_are_independent(self):
        """서로 다른 CellInfo의 metadata는 독립적이어야 한다."""
        info1 = CellInfo(cell_id="cell-0")
        info2 = CellInfo(cell_id="cell-1")
        info1.metadata["key"] = "value"
        assert "key" not in info2.metadata

    def test_state_can_be_changed(self):
        """상태를 변경할 수 있어야 한다."""
        info = CellInfo(cell_id="cell-0")
        info.state = CellState.DRAINING
        assert info.state == CellState.DRAINING

    def test_warmup_state_with_percentage(self):
        """WARMUP 상태에서 percentage를 설정할 수 있어야 한다."""
        info = CellInfo(
            cell_id="cell-0",
            state=CellState.WARMUP,
            warmup_percentage=30.0,
        )
        assert info.state == CellState.WARMUP
        assert info.warmup_percentage == 30.0


# =============================================================================
# E. L2 Sync Protocol — Contract (doc 388, Q6)
# =============================================================================


class TestCellInfoL2SyncFieldsContract:
    """_L2_SYNCED_FIELDS / _L2_SYNCED_METADATA ClassVar 계약 검증."""

    def test_l2_synced_fields_values(self):
        """_L2_SYNCED_FIELDS: state, health_score, warmup_percentage."""
        assert CellInfo._L2_SYNCED_FIELDS == (
            "state",
            "health_score",
            "warmup_percentage",
        )

    def test_l2_synced_metadata_values(self):
        """_L2_SYNCED_METADATA: last_state_change, last_state_change_time."""
        assert CellInfo._L2_SYNCED_METADATA == (
            "last_state_change",
            "last_state_change_time",
        )


class TestCellInfoToL2DictContract:
    """to_l2_dict() Redis Hash 구조 계약 검증."""

    def test_required_keys_present(self):
        """to_l2_dict() must contain state, health_score, warmup_percentage, updated_at."""
        cell = CellInfo(cell_id="cell-0")
        data = cell.to_l2_dict()
        assert "state" in data
        assert "health_score" in data
        assert "warmup_percentage" in data
        assert "updated_at" in data

    def test_all_values_are_strings(self):
        """Redis Hash 값은 모두 문자열이어야 한다."""
        cell = CellInfo(cell_id="cell-0", health_score=0.85, warmup_percentage=25.0)
        data = cell.to_l2_dict()
        for key, value in data.items():
            assert isinstance(value, str), (
                f"Key '{key}' value is not str: {type(value)}"
            )

    def test_metadata_keys_use_meta_prefix(self):
        """Metadata fields use 'meta:' prefix in Redis Hash."""
        cell = CellInfo(cell_id="cell-0")
        cell.metadata["last_state_change"] = {"from": "active", "to": "draining"}
        cell.metadata["last_state_change_time"] = 1711382400.0
        data = cell.to_l2_dict()
        assert "meta:last_state_change" in data
        assert "meta:last_state_change_time" in data


# =============================================================================
# F. L2 Sync Protocol — Behavior (doc 388, Q19, Q21)
# =============================================================================


class TestCellInfoToL2DictBehavior:
    """to_l2_dict() 직렬화 동작 검증."""

    def test_state_serializes_as_enum_value(self):
        """state는 CellState.value (문자열)로 직렬화된다."""
        cell = CellInfo(cell_id="cell-0", state=CellState.DRAINING)
        data = cell.to_l2_dict()
        assert data["state"] == CellState.DRAINING.value

    def test_metadata_absent_when_not_set(self):
        """metadata가 없으면 meta: 키가 to_l2_dict에 포함되지 않는다."""
        cell = CellInfo(cell_id="cell-0")
        data = cell.to_l2_dict()
        assert "meta:last_state_change" not in data
        assert "meta:last_state_change_time" not in data

    def test_metadata_last_state_change_serialized_as_json(self):
        """last_state_change는 JSON 문자열로 직렬화된다."""
        from baldur.utils.serialization import fast_loads

        cell = CellInfo(cell_id="cell-0")
        change = {"from": "active", "to": "draining", "reason": "test"}
        cell.metadata["last_state_change"] = change
        data = cell.to_l2_dict()
        restored = fast_loads(data["meta:last_state_change"])
        assert restored == change


class TestCellInfoApplyL2DictLWWBehavior:
    """apply_l2_dict() LWW+MRW hybrid 비교 동작 검증 (Q19)."""

    def test_newer_l2_timestamp_accepts_less_restrictive_state(self):
        """l2_updated_at > l1: LWW 승리 — 덜 제한적인 상태도 수락 (recovery)."""
        # Given
        cell = CellInfo(cell_id="cell-0", state=CellState.ISOLATED, updated_at=100.0)

        # When — L2 has ACTIVE with newer timestamp
        result = cell.apply_l2_dict(
            {
                "state": "active",
                "updated_at": "200.0",
                "health_score": "1.0",
                "warmup_percentage": "0.0",
            }
        )

        # Then
        assert result is True
        assert cell.state == CellState.ACTIVE
        assert cell.updated_at == 200.0

    def test_stale_l2_timestamp_rejects_update(self):
        """l2_updated_at < l1: stale 데이터 거부."""
        cell = CellInfo(cell_id="cell-0", state=CellState.ACTIVE, updated_at=200.0)

        result = cell.apply_l2_dict(
            {
                "state": "isolated",
                "updated_at": "100.0",
            }
        )

        assert result is False
        assert cell.state == CellState.ACTIVE

    def test_equal_timestamp_more_restrictive_wins(self):
        """l2_updated_at == l1: tie-break — 더 제한적인 상태 수락."""
        cell = CellInfo(cell_id="cell-0", state=CellState.ACTIVE, updated_at=100.0)

        result = cell.apply_l2_dict(
            {
                "state": "draining",
                "updated_at": "100.0",
                "health_score": "1.0",
                "warmup_percentage": "0.0",
            }
        )

        assert result is True
        assert cell.state == CellState.DRAINING

    def test_equal_timestamp_less_restrictive_rejected(self):
        """l2_updated_at == l1: tie-break — 덜 제한적인 상태 거부."""
        cell = CellInfo(cell_id="cell-0", state=CellState.ISOLATED, updated_at=100.0)

        result = cell.apply_l2_dict(
            {
                "state": "active",
                "updated_at": "100.0",
            }
        )

        assert result is False
        assert cell.state == CellState.ISOLATED

    def test_bytes_keys_and_values_accepted(self):
        """Redis hgetall()이 bytes로 반환해도 처리된다."""
        cell = CellInfo(cell_id="cell-0", state=CellState.ACTIVE, updated_at=100.0)

        result = cell.apply_l2_dict(
            {
                b"state": b"draining",
                b"updated_at": b"200.0",
                b"health_score": b"0.7",
                b"warmup_percentage": b"0.0",
            }
        )

        assert result is True
        assert cell.state == CellState.DRAINING
        assert cell.health_score == 0.7

    def test_missing_state_returns_false(self):
        """state 필드가 없으면 거부."""
        cell = CellInfo(cell_id="cell-0", updated_at=100.0)
        result = cell.apply_l2_dict({"updated_at": "200.0"})
        assert result is False

    def test_invalid_updated_at_returns_false(self):
        """updated_at이 유효하지 않은 값이면 거부."""
        cell = CellInfo(cell_id="cell-0", updated_at=100.0)
        result = cell.apply_l2_dict(
            {
                "state": "active",
                "updated_at": "not-a-number",
            }
        )
        assert result is False

    def test_health_score_clamped_to_range(self):
        """health_score가 0.0~1.0 범위로 클램핑된다."""
        cell = CellInfo(cell_id="cell-0", updated_at=100.0)
        cell.apply_l2_dict(
            {
                "state": "active",
                "updated_at": "200.0",
                "health_score": "1.5",
                "warmup_percentage": "0.0",
            }
        )
        assert cell.health_score == 1.0

    def test_warmup_percentage_clamped_to_range(self):
        """warmup_percentage가 0.0~100.0 범위로 클램핑된다."""
        cell = CellInfo(cell_id="cell-0", updated_at=100.0)
        cell.apply_l2_dict(
            {
                "state": "warmup",
                "updated_at": "200.0",
                "health_score": "1.0",
                "warmup_percentage": "150.0",
            }
        )
        assert cell.warmup_percentage == 100.0


class TestCellInfoApplyMRWOnlyBehavior:
    """_apply_mrw_only() MRW-only fallback 동작 검증."""

    def test_legacy_data_without_updated_at_uses_mrw(self):
        """updated_at 없는 legacy 데이터는 MRW로 fallback."""
        cell = CellInfo(cell_id="cell-0", state=CellState.ACTIVE)

        result = cell.apply_l2_dict(
            {
                "state": "draining",
                "health_score": "0.5",
            }
        )

        assert result is True
        assert cell.state == CellState.DRAINING

    def test_legacy_mrw_rejects_less_restrictive(self):
        """MRW fallback에서 덜 제한적인 상태는 거부."""
        cell = CellInfo(cell_id="cell-0", state=CellState.ISOLATED)

        result = cell.apply_l2_dict(
            {
                "state": "active",
                "health_score": "1.0",
            }
        )

        assert result is False
        assert cell.state == CellState.ISOLATED


class TestCellInfoL2MetadataSafeDefaultsBehavior:
    """_apply_l2_metadata() safe defaults 동작 검증 (Q21)."""

    def test_valid_metadata_deserialized(self):
        """유효한 metadata JSON이 정상 역직렬화된다."""
        from baldur.utils.serialization import fast_dumps_str

        cell = CellInfo(cell_id="cell-0", updated_at=100.0)
        change = {"from": "active", "to": "draining", "reason": "test"}
        cell.apply_l2_dict(
            {
                "state": "draining",
                "updated_at": "200.0",
                "meta:last_state_change": fast_dumps_str(change),
                "meta:last_state_change_time": "1711382400.123",
            }
        )
        assert cell.metadata["last_state_change"] == change
        assert cell.metadata["last_state_change_time"] == 1711382400.123

    def test_corrupt_metadata_resets_to_safe_defaults(self):
        """손상된 metadata JSON은 safe defaults로 리셋된다."""
        cell = CellInfo(cell_id="cell-0", updated_at=100.0)
        cell.apply_l2_dict(
            {
                "state": "draining",
                "updated_at": "200.0",
                "meta:last_state_change": "{invalid json",
            }
        )
        assert cell.metadata["last_state_change"] == {}
        assert cell.metadata.get("last_state_change_time") is None

    def test_corrupt_time_resets_to_none(self):
        """손상된 last_state_change_time은 None으로 리셋된다."""
        cell = CellInfo(cell_id="cell-0", updated_at=100.0)
        cell.apply_l2_dict(
            {
                "state": "draining",
                "updated_at": "200.0",
                "meta:last_state_change_time": "not-a-float",
            }
        )
        assert cell.metadata.get("last_state_change_time") is None


class TestCellInfoL2SerializationRoundtripBehavior:
    """to_l2_dict → apply_l2_dict 직렬화 왕복 검증."""

    def test_roundtrip_preserves_state_and_scores(self):
        """to_l2_dict → apply_l2_dict 왕복 시 state, health_score, warmup이 보존된다."""
        # Given
        original = CellInfo(
            cell_id="cell-0",
            state=CellState.DRAINING,
            health_score=0.75,
            warmup_percentage=50.0,
            updated_at=12345.0,
        )
        data = original.to_l2_dict()

        # When
        target = CellInfo(cell_id="cell-0", updated_at=0.0)
        result = target.apply_l2_dict(data)

        # Then
        assert result is True
        assert target.state == original.state
        assert target.health_score == original.health_score
        assert target.warmup_percentage == original.warmup_percentage
        assert target.updated_at == original.updated_at

    def test_roundtrip_preserves_metadata(self):
        """to_l2_dict → apply_l2_dict 왕복 시 metadata가 보존된다."""
        original = CellInfo(cell_id="cell-0", updated_at=100.0)
        original.metadata["last_state_change"] = {"from": "active", "to": "draining"}
        original.metadata["last_state_change_time"] = 1711382400.0
        data = original.to_l2_dict()

        target = CellInfo(cell_id="cell-0", updated_at=0.0)
        target.apply_l2_dict(data)

        assert (
            target.metadata["last_state_change"]
            == original.metadata["last_state_change"]
        )
        assert (
            target.metadata["last_state_change_time"]
            == original.metadata["last_state_change_time"]
        )
