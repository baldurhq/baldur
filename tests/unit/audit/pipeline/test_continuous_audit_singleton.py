"""Unit tests for ContinuousAuditRecorder singleton management (369).

Tests the get_continuous_audit_recorder() / reset_continuous_audit_recorder()
singleton pair added in 369 — Audit API Relocation.

Verification techniques:
- Singleton/lifecycle (§8.10): caching, reset, re-creation
- Thread safety (§8.7): concurrent get returns same instance
- Dependency interaction (§8.5): delegates to get_audit_adapter()
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.continuous_audit import (
    ContinuousAuditRecorder,
    get_continuous_audit_recorder,
    reset_continuous_audit_recorder,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure singleton is reset before and after each test."""
    reset_continuous_audit_recorder()
    yield
    reset_continuous_audit_recorder()


class TestContinuousAuditRecorderSingletonBehavior:
    """get_continuous_audit_recorder() / reset_continuous_audit_recorder() lifecycle."""

    @patch(
        "baldur.adapters.audit.singleton.get_audit_adapter",
        autospec=True,
    )
    def test_get_returns_recorder_instance(self, mock_get_adapter):
        """get_continuous_audit_recorder() returns a ContinuousAuditRecorder."""
        mock_get_adapter.return_value = MagicMock()
        recorder = get_continuous_audit_recorder()
        assert isinstance(recorder, ContinuousAuditRecorder)

    @patch(
        "baldur.adapters.audit.singleton.get_audit_adapter",
        autospec=True,
    )
    def test_get_returns_same_instance_on_repeated_calls(self, mock_get_adapter):
        """Repeated calls return the same cached instance."""
        mock_get_adapter.return_value = MagicMock()
        first = get_continuous_audit_recorder()
        second = get_continuous_audit_recorder()
        assert first is second

    @patch(
        "baldur.adapters.audit.singleton.get_audit_adapter",
        autospec=True,
    )
    def test_get_calls_get_audit_adapter(self, mock_get_adapter):
        """Singleton creation delegates to get_audit_adapter()."""
        mock_get_adapter.return_value = MagicMock()
        get_continuous_audit_recorder()
        mock_get_adapter.assert_called_once()

    @patch(
        "baldur.adapters.audit.singleton.get_audit_adapter",
        autospec=True,
    )
    def test_reset_clears_cached_instance(self, mock_get_adapter):
        """reset_continuous_audit_recorder() clears cached instance."""
        mock_get_adapter.return_value = MagicMock()
        first = get_continuous_audit_recorder()
        reset_continuous_audit_recorder()
        second = get_continuous_audit_recorder()
        assert first is not second

    @patch(
        "baldur.adapters.audit.singleton.get_audit_adapter",
        autospec=True,
    )
    def test_concurrent_get_returns_same_instance(self, mock_get_adapter):
        """Multiple threads calling get_continuous_audit_recorder() get the same instance."""
        mock_get_adapter.return_value = MagicMock()
        results = []

        def worker():
            results.append(get_continuous_audit_recorder())

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(r is results[0] for r in results)
