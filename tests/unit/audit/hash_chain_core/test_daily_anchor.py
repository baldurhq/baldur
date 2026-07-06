"""
DailyHashAnchor 테스트.
"""

from datetime import UTC, datetime


class TestDailyHashAnchor:
    """DailyHashAnchor 테스트."""

    def test_create_anchor(self, mock_redis):
        """앵커 생성 테스트."""
        from baldur.audit.integrity import DailyHashAnchor

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        result = anchor.create_anchor(
            date="2026-01-18", sequence=100, hash_value="abc123"
        )

        assert result["date"] == "2026-01-18"
        assert result["sequence"] == "100"
        assert result["hash"] == "abc123"
        assert "created_at" in result

    def test_create_anchor_auto_state(self, mock_redis):
        """현재 상태에서 자동 앵커 생성 테스트."""
        from baldur.audit.integrity import DailyHashAnchor

        # Redis에 상태 설정
        state_key = "test:audit:hash_chain:state"
        mock_redis.hset(
            state_key, mapping={"sequence": "50", "previous_hash": "auto_hash_value"}
        )

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        # 시퀀스/해시 없이 생성
        result = anchor.create_anchor(date="2026-01-18")

        assert result["sequence"] == "50"
        assert result["hash"] == "auto_hash_value"

    def test_get_anchor(self, mock_redis):
        """앵커 조회 테스트."""
        from baldur.audit.integrity import DailyHashAnchor

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        # 앵커 생성
        anchor.create_anchor(date="2026-01-18", sequence=100, hash_value="abc123")

        # 조회
        result = anchor.get_anchor("2026-01-18")

        assert result is not None
        assert result["sequence"] == 100
        assert result["hash"] == "abc123"

    def test_get_anchor_not_found(self, mock_redis):
        """없는 앵커 조회 테스트."""
        from baldur.audit.integrity import DailyHashAnchor

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        result = anchor.get_anchor("1999-12-31")

        assert result is None

    def test_verify_from_anchor_success(self, mock_redis, temp_log_dir):
        """앵커 기반 검증 성공 테스트."""
        from baldur.audit.integrity import DailyHashAnchor, compute_hash

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        # 앵커 생성
        anchor_hash = "previous_day_hash"
        anchor.create_anchor(date="2026-01-17", sequence=10, hash_value=anchor_hash)

        # 앵커 이후 엔트리 생성
        entries = []
        prev_hash = anchor_hash
        for i in range(3):
            entry = {
                "event": f"event_{i}",
                "integrity": {
                    "sequence": 11 + i,
                    "previous_hash": prev_hash,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            }
            current_hash = compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash
            entries.append(entry)
            prev_hash = current_hash

        # 검증
        is_valid, error = anchor.verify_from_anchor(entries, "2026-01-17")

        assert is_valid is True
        assert error is None

    def test_verify_from_anchor_chain_broken(self, mock_redis):
        """앵커 경계 체인 깨짐 감지 테스트."""
        from baldur.audit.integrity import DailyHashAnchor

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        # 앵커 생성
        anchor.create_anchor(
            date="2026-01-17", sequence=10, hash_value="correct_anchor_hash"
        )

        # 잘못된 previous_hash를 가진 엔트리
        entries = [
            {
                "event": "event_0",
                "integrity": {
                    "sequence": 11,
                    "previous_hash": "WRONG_HASH",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "current_hash": "some_hash",
                },
            }
        ]

        # 검증
        is_valid, error = anchor.verify_from_anchor(entries, "2026-01-17")

        assert is_valid is False
        assert "broken" in error.lower()

    def test_list_anchors(self, mock_redis):
        """최근 앵커 목록 조회 테스트."""
        from baldur.audit.integrity import DailyHashAnchor

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        # 오늘 앵커 생성
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        anchor.create_anchor(date=today, sequence=1, hash_value="today_hash")

        # 목록 조회
        anchors = anchor.list_anchors(days=7)

        assert len(anchors) >= 1
        assert any(a["date"] == today for a in anchors)

    def test_delete_anchor(self, mock_redis):
        """앵커 삭제 테스트."""
        from baldur.audit.integrity import DailyHashAnchor

        anchor = DailyHashAnchor(mock_redis, key_prefix="test:")

        # 생성
        anchor.create_anchor(date="2026-01-18", sequence=1, hash_value="hash")
        assert anchor.get_anchor("2026-01-18") is not None

        # 삭제
        result = anchor.delete_anchor("2026-01-18")

        assert result is True
        assert anchor.get_anchor("2026-01-18") is None
