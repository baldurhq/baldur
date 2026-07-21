"""
Unit tests for release_stale_replaying Celery task.

The task resolves its repository through the canonical DLQ backing chain, so it
runs on every tier — these tests patch that chain, not a tier-specific provider.

Covers:
- Task calls repository.release_stale_replaying with correct timeout
- Returns released count on success
- Returns error dict on exception
- Log level: WARNING when entries released, DEBUG when none found
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.release_stale_replaying.return_value = 0
    return repo


@pytest.fixture
def mock_backing(mock_repository):
    """The resolved DLQ backing — the task reads ``.repository`` off it.

    Specced on the OSS capture service: the PRO backing IS-A one, so the spec
    holds for whichever tier the chain resolves to.
    """
    from baldur.services.dlq_capture.service import DLQCaptureService

    backing = MagicMock(spec=DLQCaptureService)
    backing.repository = mock_repository
    return backing


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.stale_replaying_timeout_minutes = 30
    return settings


class TestReleaseStaleReplayingTaskBehavior:
    """Behavior: release_stale_replaying task delegates to repository."""

    def test_calls_repository_with_settings_timeout(
        self, mock_backing, mock_repository, mock_settings
    ):
        """Task passes stale_replaying_timeout_minutes from settings to repository."""
        mock_settings.stale_replaying_timeout_minutes = 45

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.dlq_capture.service.resolve_dlq_backing",
                return_value=mock_backing,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger"),
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            release_stale_replaying()

        mock_repository.release_stale_replaying.assert_called_once_with(
            older_than_minutes=45,
        )

    def test_returns_success_with_released_count(
        self, mock_backing, mock_repository, mock_settings
    ):
        """Successful execution returns success=True and released_count."""
        mock_repository.release_stale_replaying.return_value = 5

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.dlq_capture.service.resolve_dlq_backing",
                return_value=mock_backing,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger"),
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            result = release_stale_replaying()

        assert result["success"] is True
        assert result["released_count"] == 5

    def test_returns_zero_when_no_stale_entries(
        self, mock_backing, mock_repository, mock_settings
    ):
        """Returns released_count=0 when no stale entries found."""
        mock_repository.release_stale_replaying.return_value = 0

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.dlq_capture.service.resolve_dlq_backing",
                return_value=mock_backing,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger"),
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            result = release_stale_replaying()

        assert result["success"] is True
        assert result["released_count"] == 0

    def test_returns_error_on_repository_exception(
        self, mock_backing, mock_repository, mock_settings
    ):
        """Repository exception returns success=False with error message."""
        mock_repository.release_stale_replaying.side_effect = RuntimeError(
            "connection lost"
        )

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.dlq_capture.service.resolve_dlq_backing",
                return_value=mock_backing,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger"),
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            result = release_stale_replaying()

        assert result["success"] is False
        assert "connection lost" in result["error"]


class TestReleaseStaleReplayingEmptyRegistryBehavior:
    """Behavior: the task runs with both DLQ registry slots empty.

    This is the pure-OSS shape the re-point exists for. Previously the task
    read the PRO-only ``dlq_repository`` slot and raised
    ``RuntimeError("baldur_pro DLQRepository not registered")`` *before* its
    try block, so an OSS install got a task FAILURE every 15 minutes. Resolution
    now runs through the canonical backing chain, whose OSS branch always
    resolves (in-memory fallback), and it sits inside the try.
    """

    @pytest.fixture(autouse=True)
    def _empty_registry_slots(self, monkeypatch):
        """Simulate a pure-OSS registry: neither DLQ slot is registered."""
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.dlq_capture.service import reset_dlq_capture_service

        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
        monkeypatch.setattr(ProviderRegistry.dlq_repository, "safe_get", lambda: None)
        reset_dlq_capture_service()
        yield
        reset_dlq_capture_service()

    def test_returns_success_shape_with_no_exception(self, mock_settings):
        """Both slots empty → success-shaped result, nothing raised.

        The negative half of the Success Criteria: no RuntimeError on any tier.
        """
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger"),
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            result = release_stale_replaying()

        assert result["success"] is True
        assert result["released_count"] == 0

    def test_does_not_log_an_error(self, mock_settings):
        """No ERROR log — the empty-registry path is normal OSS flow, not a fault."""
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger") as mock_logger,
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            release_stale_replaying()

        bound_logger = mock_logger.bind.return_value
        bound_logger.error.assert_not_called()


class TestReleaseStaleReplayingSideEffectBehavior:
    """Behavior: logging side effects based on released count."""

    def test_logs_warning_when_entries_released(
        self, mock_backing, mock_repository, mock_settings
    ):
        """WARNING log emitted when stale entries are released (indicates worker crash)."""
        mock_repository.release_stale_replaying.return_value = 3

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.dlq_capture.service.resolve_dlq_backing",
                return_value=mock_backing,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger") as mock_logger,
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            release_stale_replaying()

        # D12 bind: logging now happens via bound_logger = logger.bind(task_id=...)
        bound_logger = mock_logger.bind.return_value
        bound_logger.warning.assert_called_once()
        call_args = bound_logger.warning.call_args
        assert call_args[0][0] == "dlq.stale_replaying_released"
        assert call_args[1]["released_count"] == 3

    def test_logs_debug_when_no_entries_found(
        self, mock_backing, mock_repository, mock_settings
    ):
        """DEBUG log emitted when no stale entries found."""
        mock_repository.release_stale_replaying.return_value = 0

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.dlq_capture.service.resolve_dlq_backing",
                return_value=mock_backing,
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger") as mock_logger,
        ):
            from baldur.celery_tasks.dlq_tasks import release_stale_replaying

            release_stale_replaying()

        # D12 bind: logging now happens via bound_logger = logger.bind(task_id=...)
        bound_logger = mock_logger.bind.return_value
        bound_logger.debug.assert_called_once()
        assert bound_logger.debug.call_args[0][0] == "dlq.stale_replaying_none_found"


class TestReleaseStaleReplayingTaskContract:
    """Contract: task registration attributes match design spec."""

    def test_task_name_matches_spec(self):
        """Task name is baldur.celery_tasks.release_stale_replaying."""
        from baldur.celery_tasks.dlq_tasks import release_stale_replaying

        assert (
            release_stale_replaying.name
            == "baldur.celery_tasks.release_stale_replaying"
        )

    def test_task_queue_is_maintenance(self):
        """Task queue is 'maintenance'."""
        from baldur.celery_tasks.dlq_tasks import release_stale_replaying

        assert release_stale_replaying.queue == "maintenance"
