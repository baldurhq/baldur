"""
Unit tests for sync_worker.py fix(356) — correct checkpoint function call.

Tests:
L. _get_checkpoint_strategy calls get_default_checkpoint_strategy() (not
   nonexistent CheckpointStrategyRegistry.get_default()).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestSyncWorkerCheckpointStrategyBehavior:
    """_get_checkpoint_strategy must use get_default_checkpoint_strategy()."""

    def _make_worker(self):
        """Create a minimal AuditSyncWorker for testing _get_checkpoint_strategy."""
        from baldur.audit.sync_worker import AuditSyncWorker

        worker = AuditSyncWorker.__new__(AuditSyncWorker)
        worker._checkpoint_strategy = None
        return worker

    def test_calls_get_default_checkpoint_strategy(self) -> None:
        """_get_checkpoint_strategy calls get_default_checkpoint_strategy()."""
        worker = self._make_worker()

        mock_strategy = MagicMock()

        with patch(
            "baldur.audit.checkpoint.get_default_checkpoint_strategy",
            return_value=mock_strategy,
        ) as mock_fn:
            result = worker._get_checkpoint_strategy()

        mock_fn.assert_called_once()
        assert result is mock_strategy

    def test_caches_strategy_after_first_call(self) -> None:
        """Strategy is cached after first successful retrieval."""
        worker = self._make_worker()

        mock_strategy = MagicMock()

        with patch(
            "baldur.audit.checkpoint.get_default_checkpoint_strategy",
            return_value=mock_strategy,
        ) as mock_fn:
            result1 = worker._get_checkpoint_strategy()
            result2 = worker._get_checkpoint_strategy()

        mock_fn.assert_called_once()
        assert result1 is result2 is mock_strategy

    def test_returns_none_on_import_error(self) -> None:
        """Returns None when get_default_checkpoint_strategy import fails."""
        worker = self._make_worker()

        with patch(
            "baldur.audit.checkpoint.get_default_checkpoint_strategy",
            side_effect=ImportError("no checkpoint module"),
        ):
            result = worker._get_checkpoint_strategy()

        assert result is None

    def test_returns_none_on_runtime_error(self) -> None:
        """Returns None when get_default_checkpoint_strategy raises."""
        worker = self._make_worker()

        with patch(
            "baldur.audit.checkpoint.get_default_checkpoint_strategy",
            side_effect=RuntimeError("redis down"),
        ):
            result = worker._get_checkpoint_strategy()

        assert result is None

    def test_returns_injected_strategy_without_calling_default(self) -> None:
        """When strategy is injected via set_checkpoint_strategy, default is not called."""
        worker = self._make_worker()
        injected = MagicMock()
        worker.set_checkpoint_strategy(injected)

        with patch(
            "baldur.audit.checkpoint.get_default_checkpoint_strategy",
        ) as mock_fn:
            result = worker._get_checkpoint_strategy()

        mock_fn.assert_not_called()
        assert result is injected
