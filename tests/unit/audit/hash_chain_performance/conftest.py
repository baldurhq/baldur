"""
Hash Chain Performance Test Fixtures.

Provides MockRedisClient and MockPipeline for testing.
Uses lazy imports to avoid Prometheus registry conflicts.

Refactored to use Factory Pattern (Phase 4):
- MockRedisClient → factories.MockRedisClient
- MockPipeline → factories.MockPipeline
"""

import pytest

# Factory Pattern imports
from tests.factories import MockRedisClient


@pytest.fixture
def mock_redis():
    """Provide a MockRedisClient for testing."""
    return MockRedisClient()


@pytest.fixture
def mock_redis_failing():
    """Provide a failing MockRedisClient for testing error paths."""
    return MockRedisClient(should_fail=True)
