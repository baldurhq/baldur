"""
Hash Chain Core 테스트 공통 설정.

이 패키지의 모든 테스트에서 사용하는 fixtures.

Refactored to use Factory Pattern (Phase 3):
- MockRedisClient → factories.MockRedisClient
- MockPipeline → factories.MockPipeline
"""

import pytest

# Factory Pattern imports - 중복된 Mock 클래스 대신 통합 Factory 사용
from tests.factories import MockRedisClient

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    """테스트용 Mock Redis 클라이언트."""
    return MockRedisClient()


@pytest.fixture
def failing_redis():
    """실패하는 Mock Redis 클라이언트."""
    return MockRedisClient(should_fail=True)


@pytest.fixture
def temp_log_dir(tmp_path):
    """임시 로그 디렉토리."""
    log_dir = tmp_path / "audit"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir
