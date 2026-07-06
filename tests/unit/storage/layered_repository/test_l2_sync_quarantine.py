"""L2-sync quarantine guard, single-submit collapse, and entry WARNING.

Behavior-class unit tests for the async L2 mirror path
(``L2SyncMixin._sync_to_l2_async``) and the quarantine-entry observability in
``ErrorHandlingMixin``. Covers the work deferred to /test by the fix:

- D3  the async mirror is skipped while L2 is quarantined (``_l2_healthy`` False)
- D4  one executor submit per async sync (no submit-within-submit), with a
      body-wide catch-all routing every failure to ``_handle_l2_error``
- D5  trigger->guard wiring: 3 real async failures flip ``_l2_healthy``, after
      which the D3 guard stops submitting
- D7  a one-shot WARNING ``layered_repo.l2_quarantined`` on the
      healthy->quarantined transition only, for both failure handlers

Determinism: the fire-and-forget ``_sync_to_l2_async`` submits to a process-wide
``ThreadPoolExecutor``. Each test swaps in ``_InlineExecutor`` so the L2 write,
``_handle_l2_success`` / ``_handle_l2_error``, and the counter deltas all
complete before ``submit`` returns -- no thread join, no sleep.
"""

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.repositories import CircuitBreakerStateData

# Event name + threshold are the contract under test (hardcoded once here).
L2_QUARANTINED_EVENT = "layered_repo.l2_quarantined"
QUARANTINE_THRESHOLD = 3  # error_handling.py: _l2_consecutive_failures >= 3


class _InlineExecutor:
    """Executor stub that runs submitted callables synchronously inline.

    Makes ``_sync_to_l2_async`` (fire-and-forget) deterministic and exposes
    ``submit_count`` so the D4 "single submit, no submit-within-submit"
    invariant is directly assertable: the collapsed body submits exactly once,
    whereas the old nested design re-entered ``submit`` for the inner
    ``_do_sync`` and pushed the count to 2.
    """

    def __init__(self):
        self.submit_count = 0

    def submit(self, fn, *args, **kwargs):
        self.submit_count += 1
        fn(*args, **kwargs)
        return MagicMock()  # discarded Future (fire-and-forget)


@pytest.fixture
def repo(mock_l2_repo):
    """Layered repo with a mock L2 (redis timeout profile), counters zeroed.

    ``mock_l2_repo`` is reset after construction so assertions see only the
    calls made by the test action, not the constructor's initial L2 load.
    """
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    r = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2_repo, adapter_type="redis")
    mock_l2_repo.reset_mock()
    r._l2_healthy = True
    r._l2_consecutive_failures = 0
    r._metrics["l2_sync_success_count"] = 0
    r._metrics["l2_sync_failure_count"] = 0
    r._metrics["l2_timeout_count"] = 0
    return r


class TestL2SyncQuarantineGuard:
    """D3 -- async mirror writes are skipped while L2 is quarantined."""

    def test_sync_to_l2_async_skips_submit_when_l2_quarantined(
        self, repo, mock_l2_repo
    ):
        # Given: L2 is quarantined (every other L2 path already gates on this)
        repo._l2_healthy = False
        mock_executor = MagicMock()

        # When: an async mirror is attempted
        with patch.object(repo, "_get_executor", return_value=mock_executor):
            repo._sync_to_l2_async("svc", CircuitBreakerStateData(service_name="svc"))

        # Then: no executor work is queued and L2 is not touched
        mock_executor.submit.assert_not_called()
        mock_l2_repo.get_or_create.assert_not_called()
        mock_l2_repo.update_state.assert_not_called()

    def test_sync_to_l2_async_submits_when_l2_healthy(self, repo):
        # Given: L2 is healthy (contrast with the quarantined skip above)
        inline = _InlineExecutor()

        # When
        with patch.object(repo, "_get_executor", return_value=inline):
            repo._sync_to_l2_async("svc", CircuitBreakerStateData(service_name="svc"))

        # Then: the healthy branch does submit
        assert inline.submit_count == 1


class TestL2SyncSingleSubmit:
    """D4 -- one executor submit per async sync (no submit-within-submit)."""

    def test_sync_to_l2_async_submits_exactly_once(self, repo):
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            repo._sync_to_l2_async("svc", CircuitBreakerStateData(service_name="svc"))

        # Exactly one submit: the collapsed body writes L2 inline rather than
        # submitting a wrapper that submits _do_sync again (which would be 2).
        assert inline.submit_count == 1

    def test_sync_to_l2_async_performs_l2_write_and_records_success(
        self, repo, mock_l2_repo
    ):
        # Given: a concrete L1 snapshot to mirror
        state = CircuitBreakerStateData(service_name="svc", failure_count=2)
        inline = _InlineExecutor()

        # When
        with patch.object(repo, "_get_executor", return_value=inline):
            repo._sync_to_l2_async("svc", state)

        # Then: the inline body does get_or_create + update_state and records
        # success via _handle_l2_success (kwargs referenced from the snapshot,
        # not hardcoded enum strings).
        mock_l2_repo.get_or_create.assert_called_once_with("svc")
        mock_l2_repo.update_state.assert_called_once_with(
            service_name="svc",
            state=state.state,
            failure_count=state.failure_count,
            success_count=state.success_count,
            opened_at=state.opened_at,
        )
        assert repo._metrics["l2_sync_success_count"] == 1


class TestL2SyncErrorRouting:
    """D4 -- the fire-and-forget body routes every exception to _handle_l2_error.

    Without the body-wide catch-all the exception would be swallowed by the
    discarded Future: it would neither advance _l2_consecutive_failures nor
    log, so the D3 quarantine trigger would never arm.
    """

    def test_l2_get_or_create_exception_advances_consecutive_failures(
        self, repo, mock_l2_repo
    ):
        # Given: the first L2 call fails
        mock_l2_repo.get_or_create.side_effect = Exception("L2 down")
        inline = _InlineExecutor()

        # When
        with patch.object(repo, "_get_executor", return_value=inline):
            repo._sync_to_l2_async("svc", CircuitBreakerStateData(service_name="svc"))

        # Then: routed to _handle_l2_error (sync-failure path), not the timeout
        # path -- so the quarantine trigger is armed.
        assert repo._l2_consecutive_failures == 1
        assert repo._metrics["l2_sync_failure_count"] == 1
        assert repo._metrics["l2_timeout_count"] == 0

    def test_l2_update_state_exception_is_also_routed(self, repo, mock_l2_repo):
        # Given: get_or_create succeeds but the write fails -- proves the
        # catch-all wraps the *whole* body, not just the first call.
        mock_l2_repo.update_state.side_effect = Exception("write rejected")
        inline = _InlineExecutor()

        # When
        with patch.object(repo, "_get_executor", return_value=inline):
            repo._sync_to_l2_async("svc", CircuitBreakerStateData(service_name="svc"))

        # Then
        assert repo._l2_consecutive_failures == 1
        assert repo._metrics["l2_sync_failure_count"] == 1


class TestL2SyncQuarantineWiring:
    """D5 -- trigger->guard wiring end to end.

    Drives 3 real async-path failures (not the manual _l2_healthy=False
    shortcut) so the D4 error-routing advances the counter, the 3rd failure
    flips _l2_healthy, and the D3 guard observes the flip and stops submitting.
    """

    def test_three_async_failures_quarantine_then_guard_blocks_submit(
        self, repo, mock_l2_repo
    ):
        # Given: every async L2 write fails
        mock_l2_repo.get_or_create.side_effect = Exception("L2 down")
        inline = _InlineExecutor()
        state = CircuitBreakerStateData(service_name="svc")

        with patch.object(repo, "_get_executor", return_value=inline):
            # Failures 1 and 2: below threshold -- still healthy, still submitting
            repo._sync_to_l2_async("svc", state)
            assert repo._l2_healthy is True
            repo._sync_to_l2_async("svc", state)
            assert repo._l2_healthy is True

            # Failure 3: trips the quarantine
            repo._sync_to_l2_async("svc", state)
            assert repo._l2_healthy is False
            assert repo._l2_consecutive_failures == QUARANTINE_THRESHOLD
            assert inline.submit_count == QUARANTINE_THRESHOLD

            # When: a subsequent sync runs while quarantined
            repo._sync_to_l2_async("svc", state)

        # Then: the now-effective D3 guard blocks it -- no 4th submit
        assert inline.submit_count == QUARANTINE_THRESHOLD


class TestL2QuarantineEntryWarning:
    """D7 -- one-shot WARNING on the healthy->quarantined transition.

    Boundary: silent before the 3rd failure, fires exactly once on the 3rd,
    silent on the 4th. Verified for both the error and timeout handlers.
    """

    @staticmethod
    def _l1_only_repo(adapter_type):
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        # l2_repo=None: the handlers operate on counters/logging only, so no L2
        # store or executor is needed to exercise the quarantine-entry transition.
        return LayeredCircuitBreakerStateRepository(
            l2_repo=None, adapter_type=adapter_type
        )

    @staticmethod
    def _quarantine_warnings(cap):
        return [e for e in cap if e.get("event") == L2_QUARANTINED_EVENT]

    # --- _handle_l2_error -----------------------------------------------------

    def test_handle_l2_error_no_warning_before_threshold(self):
        repo = self._l1_only_repo("redis")

        with capture_logs() as cap:
            repo._handle_l2_error("sync", "svc", Exception("boom"))
            repo._handle_l2_error("sync", "svc", Exception("boom"))

        assert self._quarantine_warnings(cap) == []
        assert repo._l2_healthy is True

    def test_handle_l2_error_emits_one_warning_on_third_failure(self):
        repo = self._l1_only_repo("redis")

        with capture_logs() as cap:
            for _ in range(QUARANTINE_THRESHOLD):
                repo._handle_l2_error("sync", "svc", Exception("boom"))

        warnings = self._quarantine_warnings(cap)
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["adapter_type"] == "redis"
        assert warnings[0]["consecutive_failures"] == QUARANTINE_THRESHOLD
        assert repo._l2_healthy is False

    def test_handle_l2_error_does_not_re_emit_after_quarantine(self):
        repo = self._l1_only_repo("redis")
        for _ in range(QUARANTINE_THRESHOLD):
            repo._handle_l2_error("sync", "svc", Exception("boom"))
        assert repo._l2_healthy is False

        with capture_logs() as cap:
            repo._handle_l2_error("sync", "svc", Exception("boom"))  # 4th

        assert self._quarantine_warnings(cap) == []

    # --- _handle_l2_timeout ---------------------------------------------------

    def test_handle_l2_timeout_emits_one_warning_on_third_failure(self):
        repo = self._l1_only_repo("django")

        with capture_logs() as cap:
            for _ in range(QUARANTINE_THRESHOLD):
                repo._handle_l2_timeout("sync", "svc")

        warnings = self._quarantine_warnings(cap)
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["adapter_type"] == "django"
        assert warnings[0]["consecutive_failures"] == QUARANTINE_THRESHOLD
        assert repo._l2_healthy is False

    def test_handle_l2_timeout_does_not_re_emit_after_quarantine(self):
        repo = self._l1_only_repo("django")
        for _ in range(QUARANTINE_THRESHOLD):
            repo._handle_l2_timeout("sync", "svc")
        assert repo._l2_healthy is False

        with capture_logs() as cap:
            repo._handle_l2_timeout("sync", "svc")  # 4th

        assert self._quarantine_warnings(cap) == []
