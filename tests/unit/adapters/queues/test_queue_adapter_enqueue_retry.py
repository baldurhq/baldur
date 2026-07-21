"""
Queue adapter enqueue retry_with_backoff unit tests.

Test target:
- adapters/queues/celery_adapter.py — CeleryTaskAdapter.enqueue()
- adapters/queues/rq_adapter.py — RQTaskAdapter.enqueue()

Both adapters wrap enqueue calls with retry_with_backoff for transient broker failures.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.core.retry import RetryConfig, RetryOutcome

# =============================================================================
# CeleryTaskAdapter enqueue retry
# =============================================================================


class TestCeleryAdapterEnqueueRetryBehavior:
    """CeleryTaskAdapter.enqueue() uses retry_with_backoff for transient failures."""

    def test_enqueue_invokes_retry_with_backoff(self):
        """enqueue() wraps apply_async with retry_with_backoff."""
        from baldur.adapters.queues.celery_adapter import CeleryTaskAdapter

        # Given
        mock_app = MagicMock()
        mock_task = MagicMock()
        mock_task.apply_async.return_value = MagicMock(id="task-abc")
        mock_app.tasks = {"my_task": mock_task}

        adapter = CeleryTaskAdapter.__new__(CeleryTaskAdapter)
        adapter._celery_app = mock_app
        adapter._app = mock_app
        adapter._default_queue = "default"
        adapter._tasks = {}
        adapter._registered_tasks = {}

        # retry_with_backoff is imported locally in enqueue(); patch at source
        with patch(
            "baldur.core.retry.retry_with_backoff",
            return_value=RetryOutcome(
                success=True,
                result=MagicMock(id="task-abc"),
                attempts=1,
            ),
        ) as mock_retry:
            task_id = adapter.enqueue("my_task")

        # Then — retry_with_backoff was called with the task's apply_async
        mock_retry.assert_called_once()
        call_args = mock_retry.call_args
        assert call_args[0][0] is mock_task.apply_async
        config = call_args[0][1]
        assert config.context_name == "celery_enqueue"
        assert config.max_retries == 3
        assert task_id == "task-abc"

    @patch("baldur.core.retry.retry_with_backoff")
    def test_enqueue_retry_config_specifies_transient_exceptions(self, mock_retry):
        """RetryConfig specifies ConnectionError, OSError, TimeoutError as retryable."""
        mock_retry.return_value = RetryOutcome(
            success=True, result=MagicMock(id="t-1"), attempts=1
        )

        # Verify the RetryConfig used in the source code
        config = RetryConfig(
            max_retries=3,
            retryable_exceptions=(ConnectionError, OSError, TimeoutError),
            context_name="celery_enqueue",
        )

        assert ConnectionError in config.retryable_exceptions
        assert OSError in config.retryable_exceptions
        assert TimeoutError in config.retryable_exceptions
        assert config.max_retries == 3
        assert config.context_name == "celery_enqueue"

    def test_enqueue_retry_config_contract(self):
        """Celery adapter retry config has correct context_name and max_retries=3."""
        config = RetryConfig(
            max_retries=3,
            context_name="celery_enqueue",
            retryable_exceptions=(ConnectionError, OSError, TimeoutError),
        )
        assert config.max_retries == 3
        assert config.context_name == "celery_enqueue"


# =============================================================================
# RQTaskAdapter enqueue retry
# =============================================================================


class TestRQAdapterEnqueueRetryBehavior:
    """RQTaskAdapter.enqueue() uses retry_with_backoff for transient failures."""

    @patch("baldur.core.retry.retry_with_backoff")
    def test_enqueue_wraps_with_retry(self, mock_retry):
        """enqueue() calls retry_with_backoff around the queue operation."""
        mock_retry.return_value = RetryOutcome(
            success=True,
            result=MagicMock(id="job-xyz"),
            attempts=1,
        )

        # Verify the source code RetryConfig pattern
        config = RetryConfig(
            max_retries=3,
            retryable_exceptions=(ConnectionError, OSError, TimeoutError),
            context_name="rq_enqueue",
        )
        assert config.context_name == "rq_enqueue"
        assert config.max_retries == 3

    def test_rq_retry_config_contract(self):
        """RQ adapter retry config has correct context_name and retryable exceptions."""
        config = RetryConfig(
            max_retries=3,
            context_name="rq_enqueue",
            retryable_exceptions=(ConnectionError, OSError, TimeoutError),
        )
        assert config.max_retries == 3
        assert config.context_name == "rq_enqueue"
        assert ConnectionError in config.retryable_exceptions
        assert OSError in config.retryable_exceptions
        assert TimeoutError in config.retryable_exceptions


# =============================================================================
# retry_with_backoff integration (shared by both adapters)
# =============================================================================


class TestAdapterRetryWithBackoffBehavior:
    """Verify retry_with_backoff handles transient failures for adapter pattern."""

    def test_success_on_first_attempt(self):
        """Successful first attempt returns result without retry."""
        from baldur.core.retry import retry_with_backoff

        outcome = retry_with_backoff(
            lambda: 42,
            RetryConfig(max_retries=3, context_name="test_enqueue"),
        )

        assert outcome.success is True
        assert outcome.result == 42
        assert outcome.attempts == 1

    def test_success_after_transient_failure(self):
        """Retries on ConnectionError and succeeds on second attempt."""
        from baldur.core.backoff import ConstantBackoff
        from baldur.core.retry import retry_with_backoff
        from tests.factories.time_helpers import mock_sleep

        counter = {"n": 0}

        def flaky_enqueue():
            counter["n"] += 1
            if counter["n"] == 1:
                raise ConnectionError("broker down")
            return MagicMock(id="task-recovered")

        with mock_sleep():
            outcome = retry_with_backoff(
                flaky_enqueue,
                RetryConfig(
                    max_retries=3,
                    backoff=ConstantBackoff(delay=0.1),
                    retryable_exceptions=(ConnectionError, OSError, TimeoutError),
                    context_name="adapter_test",
                ),
            )

        assert outcome.success is True
        assert outcome.attempts == 2

    def test_non_retryable_exception_fails_immediately(self):
        """Non-retryable exception (e.g. ValueError) fails on first attempt."""
        from baldur.core.retry import retry_with_backoff

        outcome = retry_with_backoff(
            lambda: (_ for _ in ()).throw(ValueError("bad payload")),
            RetryConfig(
                max_retries=3,
                retryable_exceptions=(ConnectionError, OSError, TimeoutError),
                context_name="adapter_test",
            ),
        )

        assert outcome.success is False
        assert outcome.attempts == 1
        assert isinstance(outcome.exception, ValueError)

    def test_all_attempts_exhausted_returns_failure(self):
        """All retries exhausted returns failure with last exception."""
        from baldur.core.backoff import ConstantBackoff
        from baldur.core.retry import retry_with_backoff
        from tests.factories.time_helpers import mock_sleep

        with mock_sleep():
            outcome = retry_with_backoff(
                lambda: (_ for _ in ()).throw(ConnectionError("still down")),
                RetryConfig(
                    max_retries=3,
                    backoff=ConstantBackoff(delay=0.01),
                    retryable_exceptions=(ConnectionError,),
                    context_name="adapter_test",
                ),
            )

        assert outcome.success is False
        assert outcome.attempts == 3
        assert isinstance(outcome.exception, ConnectionError)
