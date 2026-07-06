"""Bootstrap RequestTracker initialization tests (impl 471 D12).

Coverage:
- ``_register_shutdown_handlers()`` populates ``coordinator._tracker`` once
- A second invocation does NOT overwrite the existing tracker (idempotent)
- Gunicorn ``post_worker_init`` running after bootstrap is a no-op
  (singleton-getter only sets ``_tracker`` when currently None)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import baldur.bootstrap as bootstrap_module
from baldur.core.shutdown_coordinator import (
    RequestTracker,
    get_shutdown_coordinator,
    reset_shutdown_coordinator,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Clear coordinator singleton before/after each test for isolation."""
    reset_shutdown_coordinator()
    yield
    reset_shutdown_coordinator()


# =============================================================================
# Tracker initialization through bootstrap
# =============================================================================


class TestBootstrapTrackerInitializationBehavior:
    """471 D12: bootstrap unconditionally seeds coordinator._tracker."""

    def test_first_call_populates_tracker(self):
        """After ``_register_shutdown_handlers()``, coordinator._tracker is set."""
        # Stub out signal registration and the deferred-Timer to keep the
        # test focused on D12. The chained register_signals call would
        # mutate process-wide signal handlers in a non-gunicorn test env.
        with (
            patch(
                "baldur.core.shutdown_coordinator."
                "GracefulShutdownCoordinator.register_signals"
            ),
            patch.object(bootstrap_module, "_schedule_gunicorn_hooks_check"),
        ):
            bootstrap_module._register_shutdown_handlers()

        coordinator = get_shutdown_coordinator()
        assert coordinator._tracker is not None
        assert isinstance(coordinator._tracker, RequestTracker)

    def test_second_call_does_not_overwrite_tracker(self):
        """471 D12: ``get_shutdown_coordinator(request_tracker=...)`` only
        sets ``_tracker`` when currently None — second bootstrap is a no-op."""
        with (
            patch(
                "baldur.core.shutdown_coordinator."
                "GracefulShutdownCoordinator.register_signals"
            ),
            patch.object(bootstrap_module, "_schedule_gunicorn_hooks_check"),
        ):
            bootstrap_module._register_shutdown_handlers()
            first_tracker = get_shutdown_coordinator()._tracker

            bootstrap_module._register_shutdown_handlers()
            second_tracker = get_shutdown_coordinator()._tracker

        assert first_tracker is second_tracker

    def test_gunicorn_post_worker_init_after_bootstrap_is_noop(self):
        """If bootstrap runs first, the post_worker_init tracker-set is a no-op.

        Mirrors the gunicorn hook code path: after bootstrap pre-populates
        the tracker, calling ``get_shutdown_coordinator(request_tracker=other)``
        again must NOT swap the tracker — the original instance survives.
        """
        with (
            patch(
                "baldur.core.shutdown_coordinator."
                "GracefulShutdownCoordinator.register_signals"
            ),
            patch.object(bootstrap_module, "_schedule_gunicorn_hooks_check"),
        ):
            bootstrap_module._register_shutdown_handlers()

        bootstrap_tracker = get_shutdown_coordinator()._tracker

        # Simulate gunicorn post_worker_init's call.
        new_tracker = RequestTracker()
        coord = get_shutdown_coordinator(request_tracker=new_tracker)

        assert coord._tracker is bootstrap_tracker
        assert coord._tracker is not new_tracker

    def test_pre_existing_tracker_survives_bootstrap(self):
        """If gunicorn post_worker_init ran FIRST and seeded the tracker,
        bootstrap must not overwrite it (reverse order of D12 invariant)."""
        first_tracker = RequestTracker()
        # Pre-seed (simulates post_worker_init winning the race).
        get_shutdown_coordinator(request_tracker=first_tracker)

        with (
            patch(
                "baldur.core.shutdown_coordinator."
                "GracefulShutdownCoordinator.register_signals"
            ),
            patch.object(bootstrap_module, "_schedule_gunicorn_hooks_check"),
        ):
            bootstrap_module._register_shutdown_handlers()

        assert get_shutdown_coordinator()._tracker is first_tracker
