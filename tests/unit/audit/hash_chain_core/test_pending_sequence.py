"""
PendingSequenceManager 테스트.
"""


class TestPendingSequenceManager:
    """PendingSequenceManager 테스트."""

    def test_reserve_sequence_success(self, mock_redis):
        """시퀀스 예약 성공 테스트."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        result = manager.reserve_sequence(1, "hash123")

        assert result is True
        pending_key = "test:audit:hash_chain:pending:1"
        assert mock_redis.get(pending_key) is not None

    def test_reserve_sequence_duplicate_blocked(self, mock_redis):
        """동일 시퀀스 중복 예약 차단 테스트."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        # 첫 예약 성공
        assert manager.reserve_sequence(1, "hash123") is True

        # 같은 시퀀스 재예약 차단
        assert manager.reserve_sequence(1, "hash456") is False

    def test_commit_sequence(self, mock_redis):
        """시퀀스 커밋 테스트 (PENDING 제거)."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        # 예약
        manager.reserve_sequence(1, "hash123")

        # 커밋
        result = manager.commit_sequence(1)

        assert result is True
        pending_key = "test:audit:hash_chain:pending:1"
        assert mock_redis.get(pending_key) is None

    def test_abort_sequence(self, mock_redis):
        """시퀀스 중단 테스트 (PENDING -> ORPHANED)."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        # 예약
        manager.reserve_sequence(1, "hash123")

        # 중단
        result = manager.abort_sequence(1)

        assert result is True
        pending_key = "test:audit:hash_chain:pending:1"
        orphaned_key = "test:audit:hash_chain:orphaned:1"
        assert mock_redis.get(pending_key) is None
        assert mock_redis.get(orphaned_key) is not None

    def test_get_pending_sequences(self, mock_redis):
        """PENDING 시퀀스 목록 조회 테스트."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        # 여러 시퀀스 예약
        manager.reserve_sequence(1, "hash1")
        manager.reserve_sequence(3, "hash3")
        manager.reserve_sequence(2, "hash2")

        # 조회 (정렬되어 반환)
        pending = manager.get_pending_sequences()

        assert pending == [1, 2, 3]

    def test_get_orphaned_sequences(self, mock_redis):
        """ORPHANED 시퀀스 목록 조회 테스트."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        # 예약 후 중단
        manager.reserve_sequence(1, "hash1")
        manager.abort_sequence(1)
        manager.reserve_sequence(3, "hash3")
        manager.abort_sequence(3)

        # 조회
        orphaned = manager.get_orphaned_sequences()

        assert 1 in orphaned
        assert 3 in orphaned

    def test_get_expected_hash(self, mock_redis):
        """예상 해시 조회 테스트."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        # PENDING 상태
        manager.reserve_sequence(1, "pending_hash")
        assert manager.get_expected_hash(1) == "pending_hash"

        # ORPHANED 상태로 전환
        manager.abort_sequence(1)
        assert manager.get_expected_hash(1) == "pending_hash"

    def test_clear_orphaned(self, mock_redis):
        """ORPHANED 정리 테스트."""
        from baldur.audit.integrity import PendingSequenceManager

        manager = PendingSequenceManager(mock_redis, key_prefix="test:")

        manager.reserve_sequence(1, "hash1")
        manager.abort_sequence(1)

        # ORPHANED 정리
        result = manager.clear_orphaned(1)

        assert result is True
        assert manager.get_expected_hash(1) is None
