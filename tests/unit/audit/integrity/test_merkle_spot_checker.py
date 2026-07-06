"""
MerkleSpotChecker 단위 테스트.

테스트 대상:
    - MerkleSpotChecker.spot_check: 블록 단위 스팟체크
    - MerkleSpotChecker.build_merkle_roots: 블록 머클 루트 빌드
    - MerkleSpotChecker._split_into_blocks: 시퀀스 기반 블록 분할
    - MerkleSpotChecker._compute_block_merkle_root: 블록 머클 루트 계산
    - MerkleSpotChecker._store_merkle_root: TTL 설정 참조 저장
"""

from unittest.mock import MagicMock, patch

from baldur.audit.integrity.merkle_spot_checker import (
    BLOCK_MERKLE_ROOT_KEY,
    MerkleSpotChecker,
)
from baldur.audit.integrity.models import canonical_json_bytes, compute_hash

# =============================================================================
# Helpers
# =============================================================================


def _make_entry(seq: int, data_value: str = "test") -> dict:
    """시퀀스 번호가 지정된 유효한 해시체인 엔트리 생성."""
    base = {
        "event_type": "TEST",
        "data": {"value": data_value},
        "integrity": {
            "sequence": seq,
            "previous_hash": "GENESIS" if seq == 1 else f"hash_{seq - 1}",
        },
    }
    # current_hash 계산 (compute_hash 방식과 동일)
    entry_for_hash = {
        "event_type": base["event_type"],
        "data": base["data"],
        "integrity": {
            "sequence": seq,
            "previous_hash": base["integrity"]["previous_hash"],
        },
    }
    base["integrity"]["current_hash"] = compute_hash(entry_for_hash)
    return base


def _make_entries(start: int, end: int) -> list[dict]:
    """start~end (inclusive) 시퀀스 범위의 엔트리 목록 생성."""
    return [_make_entry(seq) for seq in range(start, end + 1)]


# =============================================================================
# MerkleSpotChecker._split_into_blocks 동작 검증
# =============================================================================


class TestSplitIntoBlocksBehavior:
    """시퀀스 기반 블록 분할 동작 검증."""

    def test_single_block_for_small_entries(self):
        """block_size 이내 엔트리는 단일 블록으로 분할된다."""
        checker = MerkleSpotChecker(block_size=10)
        entries = _make_entries(1, 5)

        blocks = checker._split_into_blocks(entries)

        assert len(blocks) == 1
        assert 0 in blocks
        assert len(blocks[0]) == 5

    def test_multiple_blocks_for_large_entries(self):
        """block_size 초과 엔트리는 여러 블록으로 분할된다."""
        checker = MerkleSpotChecker(block_size=3)
        entries = _make_entries(1, 7)

        blocks = checker._split_into_blocks(entries)

        # seq 1-3 → block 0, seq 4-6 → block 1, seq 7 → block 2
        assert len(blocks) == 3
        assert len(blocks[0]) == 3
        assert len(blocks[1]) == 3
        assert len(blocks[2]) == 1

    def test_sequence_based_block_assignment(self):
        """block_id = (sequence - 1) // block_size 공식으로 블록이 결정된다."""
        checker = MerkleSpotChecker(block_size=5)
        entries = _make_entries(1, 10)

        blocks = checker._split_into_blocks(entries)

        # seq 1-5 → block 0, seq 6-10 → block 1
        assert set(blocks.keys()) == {0, 1}
        for entry in blocks[0]:
            seq = entry["integrity"]["sequence"]
            assert 1 <= seq <= 5
        for entry in blocks[1]:
            seq = entry["integrity"]["sequence"]
            assert 6 <= seq <= 10

    def test_gap_in_sequence_does_not_affect_other_blocks(self):
        """시퀀스 갭이 있어도 다른 블록에 영향을 주지 않는다."""
        checker = MerkleSpotChecker(block_size=5)
        # seq 1, 2, 3, 8, 9, 10 (seq 4-7 누락)
        entries = [_make_entry(s) for s in [1, 2, 3, 8, 9, 10]]

        blocks = checker._split_into_blocks(entries)

        # seq 1-3 → block 0, seq 8-10 → block 1
        assert 0 in blocks
        assert 1 in blocks
        assert len(blocks[0]) == 3
        assert len(blocks[1]) == 3

    def test_deleted_entry_does_not_change_other_block_composition(self):
        """중간 엔트리 삭제 시 다른 블록의 구성이 변하지 않는다."""
        checker = MerkleSpotChecker(block_size=5)

        # 원래: seq 1-10
        original_entries = _make_entries(1, 10)
        original_blocks = checker._split_into_blocks(original_entries)

        # seq 3 삭제
        modified_entries = [
            e for e in original_entries if e["integrity"]["sequence"] != 3
        ]
        modified_blocks = checker._split_into_blocks(modified_entries)

        # block 1 (seq 6-10)은 동일해야 함
        assert len(original_blocks[1]) == len(modified_blocks[1])
        for orig, mod in zip(original_blocks[1], modified_blocks[1], strict=False):
            assert orig["integrity"]["sequence"] == mod["integrity"]["sequence"]

    def test_zero_or_negative_sequence_ignored(self):
        """시퀀스가 0 이하인 엔트리는 무시된다."""
        checker = MerkleSpotChecker(block_size=5)
        entries = [
            {"integrity": {"sequence": 0}, "data": "zero"},
            {"integrity": {"sequence": -1}, "data": "negative"},
            _make_entry(1),
        ]

        blocks = checker._split_into_blocks(entries)

        assert len(blocks) == 1
        total_entries = sum(len(v) for v in blocks.values())
        assert total_entries == 1

    def test_empty_entries_returns_empty_blocks(self):
        """빈 엔트리 목록은 빈 블록 딕셔너리를 반환한다."""
        checker = MerkleSpotChecker(block_size=5)

        blocks = checker._split_into_blocks([])

        assert blocks == {}


# =============================================================================
# MerkleSpotChecker.spot_check 동작 검증
# =============================================================================


class TestSpotCheckBehavior:
    """블록 단위 스팟체크 동작 검증."""

    def test_empty_entries_returns_valid(self):
        """빈 엔트리에 대해 valid=True를 반환한다."""
        checker = MerkleSpotChecker(block_size=5)

        result = checker.spot_check([])

        assert result["valid"] is True
        assert result["strategy"] == "merkle_spot_check"
        assert result["total_entries"] == 0
        assert result["total_blocks"] == 0

    def test_first_run_stores_roots_without_failure(self):
        """첫 실행 시 저장된 루트 없으면 저장만 하고 실패는 아니다."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # 저장된 루트 없음
        checker = MerkleSpotChecker(block_size=5, redis_client=mock_redis)
        entries = _make_entries(1, 5)

        result = checker.spot_check(entries)

        assert result["valid"] is True
        assert result["blocks_failed"] == 0
        # set이 호출되어 루트가 저장됨
        assert mock_redis.set.called

    def test_valid_data_returns_no_failures(self):
        """정상 데이터로 빌드 후 동일 데이터 체크 시 valid=True."""
        mock_redis = MagicMock()
        stored_roots = {}

        def mock_get(key):
            return stored_roots.get(key)

        def mock_set(key, value, ex=None):
            stored_roots[key] = value

        mock_redis.get.side_effect = mock_get
        mock_redis.set.side_effect = mock_set

        checker = MerkleSpotChecker(
            block_size=5, redis_client=mock_redis, namespace="test"
        )
        entries = _make_entries(1, 10)

        # 첫 실행: 루트 저장
        checker.build_merkle_roots(entries)
        # 두 번째 실행: 검증
        result = checker.spot_check(entries)

        assert result["valid"] is True
        assert result["blocks_failed"] == 0
        assert result["total_blocks"] == 2

    def test_tampered_block_detected(self):
        """변조된 블록이 감지되고 failed_block_ids에 반환된다."""
        mock_redis = MagicMock()
        stored_roots = {}

        def mock_get(key):
            return stored_roots.get(key)

        def mock_set(key, value, ex=None):
            stored_roots[key] = value

        mock_redis.get.side_effect = mock_get
        mock_redis.set.side_effect = mock_set

        checker = MerkleSpotChecker(
            block_size=5, redis_client=mock_redis, namespace="test"
        )
        entries = _make_entries(1, 10)

        # 빌드: 원본 루트 저장
        checker.build_merkle_roots(entries)

        # 변조: block 0의 엔트리 수정
        entries[2]["data"] = {"value": "TAMPERED"}

        # 검증: 변조 블록 감지
        result = checker.spot_check(entries)

        assert result["valid"] is False
        assert result["blocks_failed"] >= 1
        assert 0 in result["failed_block_ids"]
        # block 1은 변조되지 않았으므로 failed에 없음
        assert 1 not in result["failed_block_ids"]

    def test_no_redis_first_run_always_valid(self):
        """Redis 없으면 stored_root=None으로 첫 실행 항상 valid."""
        checker = MerkleSpotChecker(block_size=5, redis_client=None)
        entries = _make_entries(1, 5)

        result = checker.spot_check(entries)

        assert result["valid"] is True

    def test_result_contains_required_keys(self):
        """spot_check 결과에 문서 명세 키가 모두 포함된다."""
        checker = MerkleSpotChecker(block_size=5)
        entries = _make_entries(1, 3)

        result = checker.spot_check(entries)

        required_keys = {
            "valid",
            "strategy",
            "total_entries",
            "total_blocks",
            "blocks_checked",
            "blocks_failed",
            "failed_block_ids",
            "drill_down_results",
            "errors",
            "duration_ms",
        }
        assert required_keys.issubset(result.keys())

    def test_drill_down_on_failed_block(self):
        """실패 블록에 대해 drill_down_results가 생성된다."""
        mock_redis = MagicMock()
        stored_roots = {}

        def mock_get(key):
            return stored_roots.get(key)

        def mock_set(key, value, ex=None):
            stored_roots[key] = value

        mock_redis.get.side_effect = mock_get
        mock_redis.set.side_effect = mock_set

        checker = MerkleSpotChecker(
            block_size=5, redis_client=mock_redis, namespace="test"
        )
        entries = _make_entries(1, 5)

        # 빌드
        checker.build_merkle_roots(entries)
        # 변조
        entries[0]["data"] = {"value": "TAMPERED"}
        # 검증
        result = checker.spot_check(entries)

        assert len(result["drill_down_results"]) >= 1
        drill = result["drill_down_results"][0]
        assert "block_id" in drill
        assert "block_range" in drill
        assert "actual_entries" in drill
        assert "issues" in drill


# =============================================================================
# MerkleSpotChecker.build_merkle_roots 동작 검증
# =============================================================================


class TestBuildMerkleRootsBehavior:
    """블록별 머클 루트 빌드 동작 검증."""

    def test_stores_correct_block_count(self):
        """엔트리를 올바른 블록 수만큼 저장한다."""
        mock_redis = MagicMock()
        checker = MerkleSpotChecker(
            block_size=5, redis_client=mock_redis, namespace="test"
        )
        entries = _make_entries(1, 12)

        result = checker.build_merkle_roots(entries)

        # seq 1-5 block 0, 6-10 block 1, 11-12 block 2
        assert result["blocks_stored"] == 3
        assert result["total_entries"] == 12

    def test_redis_set_called_with_ttl_from_settings(self):
        """_store_merkle_root이 settings.anchor_retention_days 기반 TTL을 사용한다."""
        mock_redis = MagicMock()
        checker = MerkleSpotChecker(
            block_size=5, redis_client=mock_redis, namespace="test"
        )
        entries = _make_entries(1, 3)

        with patch(
            "baldur.settings.audit_integrity.get_audit_integrity_settings"
        ) as mock_settings:
            mock_settings.return_value.anchor_retention_days = 90
            checker.build_merkle_roots(entries)

        # set 호출 시 ex= anchor_retention_days * 86400
        call_args = mock_redis.set.call_args
        assert call_args is not None
        # ex 파라미터 확인
        assert (
            call_args.kwargs.get("ex") == 90 * 86400
            or call_args[1].get("ex") == 90 * 86400
        )

    def test_empty_entries_returns_zero_blocks(self):
        """빈 엔트리에 대해 0블록 저장."""
        checker = MerkleSpotChecker(block_size=5)
        result = checker.build_merkle_roots([])
        assert result["blocks_stored"] == 0
        assert result["total_entries"] == 0


# =============================================================================
# MerkleSpotChecker._compute_block_merkle_root 동작 검증
# =============================================================================


class TestComputeBlockMerkleRootBehavior:
    """블록 머클 루트 계산 동작 검증."""

    def test_uses_canonical_json_bytes(self):
        """canonical_json_bytes를 사용하여 직렬화한다."""
        checker = MerkleSpotChecker(block_size=5)
        entries = _make_entries(1, 2)

        with patch(
            "baldur.audit.integrity.models.canonical_json_bytes",
            wraps=canonical_json_bytes,
        ) as mock_canonical:
            checker._compute_block_merkle_root(entries)

            assert mock_canonical.call_count == len(entries)

    def test_deterministic_root(self):
        """동일 엔트리에 대해 항상 동일 루트를 반환한다."""
        checker = MerkleSpotChecker(block_size=5)
        entries = _make_entries(1, 3)

        root1 = checker._compute_block_merkle_root(entries)
        root2 = checker._compute_block_merkle_root(entries)

        assert root1 == root2

    def test_different_entries_different_root(self):
        """다른 엔트리는 다른 루트를 생성한다."""
        checker = MerkleSpotChecker(block_size=5)
        entries_a = _make_entries(1, 3)
        entries_b = [_make_entry(seq, data_value="different") for seq in range(1, 4)]

        root_a = checker._compute_block_merkle_root(entries_a)
        root_b = checker._compute_block_merkle_root(entries_b)

        assert root_a != root_b


# =============================================================================
# MerkleSpotChecker 계약 검증
# =============================================================================


class TestMerkleSpotCheckerContract:
    """MerkleSpotChecker 설계 계약값 검증."""

    def test_default_block_size(self):
        """기본 블록 크기는 1000이다."""
        checker = MerkleSpotChecker()
        assert checker._block_size == 1000

    def test_default_namespace(self):
        """기본 네임스페이스는 'global'이다."""
        checker = MerkleSpotChecker()
        assert checker._namespace == "global"

    def test_block_merkle_root_key_format(self):
        """Redis 키 형식이 'baldur:{namespace}:audit:merkle_block:{block_id}'이다."""
        expected = "baldur:{namespace}:audit:merkle_block:{block_id}"
        assert BLOCK_MERKLE_ROOT_KEY == expected

    def test_strategy_name_in_result(self):
        """spot_check 결과의 strategy는 'merkle_spot_check'이다."""
        checker = MerkleSpotChecker(block_size=5)
        result = checker.spot_check(_make_entries(1, 3))
        assert result["strategy"] == "merkle_spot_check"
