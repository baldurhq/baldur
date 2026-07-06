"""DrainAwareMiddleware unit tests (impl 471 D1, D3, D4).

Coverage:
- ``__call__(request)`` matrix: phase=RUNNING/DRAINING/TERMINATING/TERMINATED
  × (liveness path, normal path)
- ``_is_liveness_path(path)``: canonical defaults, settings overrides,
  prefix-match edge case, unmatched paths
- ``_retry_after_seconds()``: clamp >= 1 boundary, fallback when
  ``remaining_drain_time`` is None
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.django.middleware.drain_aware import (
    _CANONICAL_LIVENESS_PATHS,
    DrainAwareMiddleware,
)
from baldur.core.shutdown_coordinator import (
    ShutdownPhase,
    ShutdownStats,
)

# =============================================================================
# Test helpers
# =============================================================================


class _FakeRequest:
    """Minimal HttpRequest stub."""

    def __init__(self, path: str = "/api/test/"):
        self.path = path
        self.method = "GET"
        self.META: dict = {}


def _make_coordinator(
    *,
    phase: ShutdownPhase = ShutdownPhase.RUNNING,
    remaining_drain_time: float | None = None,
    in_flight_count: int = 0,
):
    """Build a coordinator stub with controllable phase/stats."""
    coordinator = MagicMock()
    coordinator.phase = phase
    coordinator.is_accepting_requests.return_value = phase == ShutdownPhase.RUNNING
    coordinator.get_stats.return_value = ShutdownStats(
        phase=phase,
        shutdown_started_at=None,
        in_flight_count=in_flight_count,
        completed_during_drain=0,
        aborted_count=0,
        drain_timeout_seconds=30.0,
        remaining_drain_time=remaining_drain_time,
    )
    return coordinator


def _make_middleware(
    coordinator,
    get_response_return=None,
):
    """Build a DrainAwareMiddleware with the coordinator pre-injected.

    Bypasses the ``__init__`` lookup of ``get_shutdown_coordinator()``
    so tests stay independent of the singleton.
    """
    response = get_response_return if get_response_return is not None else MagicMock()
    response_callable = MagicMock(return_value=response)
    with patch(
        "baldur.api.django.middleware.drain_aware.get_shutdown_coordinator",
        return_value=coordinator,
        create=True,
    ):
        # The import inside __init__ pulls get_shutdown_coordinator from
        # baldur.core.shutdown_coordinator — patch there.
        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            return_value=coordinator,
        ):
            mw = DrainAwareMiddleware(response_callable)
    return mw, response_callable, response


# =============================================================================
# __call__ — phase × path matrix
# =============================================================================


class TestDrainAwareMiddlewareBehavior:
    """``__call__`` routing across coordinator phases and request paths."""

    def test_running_phase_passes_request_through(self):
        """RUNNING phase delegates to ``get_response`` for a normal path."""
        coordinator = _make_coordinator(phase=ShutdownPhase.RUNNING)
        mw, get_response, fake_response = _make_middleware(coordinator)
        request = _FakeRequest("/api/orders/")

        result = mw(request)

        get_response.assert_called_once_with(request)
        assert result is fake_response

    @pytest.mark.parametrize(
        "phase",
        [
            ShutdownPhase.DRAINING,
            ShutdownPhase.TERMINATING,
            ShutdownPhase.TERMINATED,
        ],
    )
    def test_non_running_phase_returns_503_for_normal_path(self, phase):
        """All non-RUNNING phases return 503 for non-liveness paths."""
        coordinator = _make_coordinator(phase=phase, remaining_drain_time=12.0)
        mw, get_response, _ = _make_middleware(coordinator)
        request = _FakeRequest("/api/orders/")

        response = mw(request)

        get_response.assert_not_called()
        assert response.status_code == 503
        assert response["Connection"] == "close"
        assert int(response["Retry-After"]) >= 1

    @pytest.mark.parametrize("liveness_path", list(_CANONICAL_LIVENESS_PATHS))
    def test_canonical_liveness_paths_passthrough_during_draining(self, liveness_path):
        """Canonical k8s liveness paths bypass the 503 even during DRAINING."""
        coordinator = _make_coordinator(
            phase=ShutdownPhase.DRAINING, remaining_drain_time=10.0
        )
        mw, get_response, fake_response = _make_middleware(coordinator)
        request = _FakeRequest(liveness_path)

        result = mw(request)

        get_response.assert_called_once_with(request)
        assert result is fake_response

    def test_drain_503_response_carries_plain_text_body(self):
        """503 response body is plain-text per HttpResponse default semantics."""
        coordinator = _make_coordinator(
            phase=ShutdownPhase.DRAINING, remaining_drain_time=5.0
        )
        mw, _, _ = _make_middleware(coordinator)

        response = mw(_FakeRequest("/api/x/"))

        assert response.status_code == 503
        # Body content describes draining state for the LB-side log.
        assert b"draining" in response.content.lower()


# =============================================================================
# _is_liveness_path — matrix
# =============================================================================


class TestDrainAwareMiddlewareLivenessExemption:
    """``_is_liveness_path`` matrix coverage."""

    @pytest.mark.parametrize("path", list(_CANONICAL_LIVENESS_PATHS))
    def test_canonical_default_paths_match(self, path):
        """The canonical default tuple is recognized without operator config."""
        coordinator = _make_coordinator()
        mw, _, _ = _make_middleware(coordinator)

        with patch(
            "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings"
        ) as get_settings:
            get_settings.return_value.drain_liveness_paths = []
            assert mw._is_liveness_path(path) is True

    def test_settings_override_path_matches(self):
        """Operator-supplied path is unioned with the canonical defaults."""
        coordinator = _make_coordinator()
        mw, _, _ = _make_middleware(coordinator)

        with patch(
            "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings"
        ) as get_settings:
            get_settings.return_value.drain_liveness_paths = ["/livez", "/healthz/live"]
            assert mw._is_liveness_path("/livez") is True
            assert mw._is_liveness_path("/healthz/live") is True

    def test_prefix_only_match_does_not_match(self):
        """Membership is exact-match — substring/prefix paths are rejected.

        ``/api/baldur/health/live`` (no trailing slash) must NOT match the
        canonical ``/api/baldur/health/live/`` because the runtime URL is the
        normalized form. Prevents accidental exemption of subtly-different
        operator paths.
        """
        coordinator = _make_coordinator()
        mw, _, _ = _make_middleware(coordinator)

        with patch(
            "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings"
        ) as get_settings:
            get_settings.return_value.drain_liveness_paths = []
            assert mw._is_liveness_path("/api/baldur/health/live") is False
            assert mw._is_liveness_path("/api/baldur/health/live/extra") is False

    def test_unmatched_path_returns_false(self):
        """Random non-liveness path returns False."""
        coordinator = _make_coordinator()
        mw, _, _ = _make_middleware(coordinator)

        with patch(
            "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings"
        ) as get_settings:
            get_settings.return_value.drain_liveness_paths = []
            assert mw._is_liveness_path("/api/orders/") is False


# =============================================================================
# _retry_after_seconds — clamp + fallback
# =============================================================================


class TestDrainAwareMiddlewareRetryAfter:
    """``_retry_after_seconds`` boundary and fallback behavior."""

    @pytest.mark.parametrize(
        ("remaining", "expected"),
        [
            (0.0, 1),  # clamp at lower bound — must never be 0
            (0.4, 1),  # int(0.4) == 0 → clamp to 1
            (1.0, 1),  # exact 1.0 → 1
            (1.9, 1),  # int(1.9) == 1
            (2.0, 2),  # int(2.0) == 2
            (12.7, 12),  # int(12.7) == 12
        ],
    )
    def test_clamps_remaining_drain_time_to_minimum_one(self, remaining, expected):
        """Retry-After must never report 0; ``int()`` floors and clamp >= 1."""
        coordinator = _make_coordinator(
            phase=ShutdownPhase.DRAINING, remaining_drain_time=remaining
        )
        mw, _, _ = _make_middleware(coordinator)

        assert mw._retry_after_seconds() == expected

    def test_falls_back_to_settings_default_when_remaining_is_none(self):
        """When ``remaining_drain_time`` is None (TERMINATING+), fallback applies."""
        coordinator = _make_coordinator(
            phase=ShutdownPhase.TERMINATING, remaining_drain_time=None
        )
        mw, _, _ = _make_middleware(coordinator)

        with patch(
            "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings"
        ) as get_settings:
            get_settings.return_value.drain_default_retry_after_seconds = 7.0
            assert mw._retry_after_seconds() == 7

    def test_fallback_also_clamps_to_minimum_one(self):
        """Fallback path is clamped >= 1 just like the live path."""
        coordinator = _make_coordinator(
            phase=ShutdownPhase.TERMINATING, remaining_drain_time=None
        )
        mw, _, _ = _make_middleware(coordinator)

        with patch(
            "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings"
        ) as get_settings:
            # Settings min is ge=1.0 but clamp is the last line of defense
            get_settings.return_value.drain_default_retry_after_seconds = 0.5
            assert mw._retry_after_seconds() == 1
