"""
Graceful Degradation test fixtures and mock classes.

All tests in this directory are auto-marked ``pytest.mark.dormant``
via the ``pytest_collection_modifyitems`` hook below.  They run in the
nightly CI job, not the default PR suite.

Refactored to use Factory Pattern (Phase 3):
- MockRedisClient → factories.MockRedisClient
- MockPipeline → factories.MockPipeline
- MockDistributedLock → factories.MockDistributedLock
"""

import gc
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.is_relative_to(_THIS_DIR):
            item.add_marker(pytest.mark.dormant)


# Factory Pattern imports - 중복된 Mock 클래스 대신 통합 Factory 사용
from tests.factories import MockRedisClient

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    """Create mock Redis client."""
    return MockRedisClient()


@pytest.fixture
def failing_redis():
    """Create failing Redis client."""
    return MockRedisClient(should_fail=True)


@pytest.fixture
def temp_dir():
    """Create temporary directory for WAL files."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    # Close disk buffer before temp dir is removed (Windows file locking)
    try:
        from baldur.audit.persistence.disk_buffer import reset_disk_buffer

        reset_disk_buffer()
    except Exception:
        pass
    # Force GC to close file handles held by fallback chain objects (Windows)
    gc.collect()
    # Retry cleanup to handle lingering file locks on Windows
    for attempt in range(3):
        try:
            shutil.rmtree(tmpdir, ignore_errors=False)
            break
        except PermissionError:
            gc.collect()
            time.sleep(0.1 * (attempt + 1))
    else:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def sample_entry():
    """Create sample log entry."""
    return {
        "event": "test_event",
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {"key": "value"},
    }
