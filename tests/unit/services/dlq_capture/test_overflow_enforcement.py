"""Unit tests for OSS DLQ overflow — check scope + strategy-faithful enforcement.

Three surfaces:
- ``OverflowResult.overflow_scope`` reported by ``handle_overflow`` (Contract).
- ``enforce_overflow_eviction`` — batch sizing, scope→bucket routing, the
  compress→drop degrade and all-protected soft-cap warn-once flags (Behavior).
- ``DLQCaptureService._enforce_overflow`` — the store-path seam: reject / evict
  -then-accept / under-cap / fail-open (Behavior).

Overflow state (periodic-N counter, last-ratio, warn-once flags) is process
-global, so an autouse ``reset_overflow_state()`` isolates each test.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationRepository
from baldur.models.dlq import DLQConfig
from baldur.services.dlq_capture import DLQCaptureService
from baldur.services.dlq_capture import overflow as overflow_module
from baldur.services.dlq_capture.overflow import (
    OverflowResult,
    enforce_overflow_eviction,
    handle_overflow,
    reset_overflow_state,
)

# Patch target for the settings read inside the enforcement seam.
_SERVICE_SETTINGS = "baldur.services.dlq_capture.service.get_dlq_settings"


@pytest.fixture(autouse=True)
def _reset_overflow_state():
    reset_overflow_state()
    yield
    reset_overflow_state()


@pytest.fixture
def repo():
    return InMemoryFailedOperationRepository()


def make_settings(
    *,
    strategy="drop_oldest",
    max_size=100,
    max_size_per_domain=100,
    interval=1,
    emergency=0.8,
):
    """Deterministic DLQ settings stub (settings are a sanctioned mock boundary).

    ``interval=1`` makes every ``handle_overflow`` call perform the full check.
    """
    return SimpleNamespace(
        overflow_strategy=strategy,
        max_size=max_size,
        max_size_per_domain=max_size_per_domain,
        overflow_check_interval=interval,
        emergency_purge_threshold=emergency,
    )


# =============================================================================
# handle_overflow — overflow_scope contract
# =============================================================================


class TestOverflowResultContract:
    """``handle_overflow`` reports which cap triggered so enforcement evicts
    from the right bucket. Scope strings are design-contract values."""

    def test_under_both_caps_accepts_no_overflow(self, repo):
        result = handle_overflow(repo, make_settings(), "payment")

        assert result.accepted is True
        assert result.overflow_detected is False
        assert result.overflow_scope == ""

    def test_domain_count_just_under_cap_accepts(self, repo):
        """Boundary: domain_count < max_size_per_domain accepts."""
        repo.create(domain="payment", failure_type="X")  # 1 < cap 2

        result = handle_overflow(repo, make_settings(max_size_per_domain=2), "payment")

        assert result.overflow_detected is False

    def test_domain_cap_hit_drop_oldest_reports_domain_scope(self, repo):
        """Boundary: domain_count == max_size_per_domain overflows (domain)."""
        repo.create(domain="payment", failure_type="X")
        repo.create(domain="payment", failure_type="X")  # 2 == cap 2

        result = handle_overflow(repo, make_settings(max_size_per_domain=2), "payment")

        assert result.accepted is True
        assert result.overflow_detected is True
        assert result.overflow_scope == "domain"

    def test_global_cap_hit_drop_oldest_reports_global_scope(self, repo):
        repo.create(domain="a", failure_type="X")
        repo.create(domain="b", failure_type="X")  # total 2 == max_size 2

        result = handle_overflow(
            repo, make_settings(max_size=2, max_size_per_domain=100), "payment"
        )

        assert result.overflow_detected is True
        assert result.overflow_scope == "global"

    def test_domain_cap_reject_strategy_rejects(self, repo):
        repo.create(domain="payment", failure_type="X")
        repo.create(domain="payment", failure_type="X")

        result = handle_overflow(
            repo,
            make_settings(strategy="reject", max_size_per_domain=2),
            "payment",
        )

        assert result.accepted is False
        assert result.reason == "domain_capacity_exceeded"

    def test_global_cap_reject_strategy_rejects(self, repo):
        repo.create(domain="a", failure_type="X")
        repo.create(domain="b", failure_type="X")

        result = handle_overflow(
            repo,
            make_settings(strategy="reject", max_size=2, max_size_per_domain=100),
            "payment",
        )

        assert result.accepted is False
        assert result.reason == "dlq_capacity_exceeded"


# =============================================================================
# enforce_overflow_eviction — eviction behavior
# =============================================================================


class TestEnforceOverflowEvictionBehavior:
    """Synchronous eviction: batch sizing, scope routing, degrade / soft-cap."""

    def _detected(self, scope):
        return OverflowResult(
            accepted=True, overflow_detected=True, overflow_scope=scope
        )

    def test_drop_oldest_domain_scope_evicts_from_domain_bucket(self, repo):
        """Real eviction outcome: the domain bucket shrinks by the batch size."""
        for _ in range(5):
            repo.create(domain="payment", failure_type="X")

        enforce_overflow_eviction(
            repo,
            make_settings(strategy="drop_oldest", interval=3),
            "payment",
            self._detected("domain"),
        )

        assert repo.count_by_domain("payment") == 2  # 5 - batch(3)

    def test_evict_batch_equals_interval_for_domain_scope(self):
        """Batch = max(1, overflow_check_interval); domain scope passes the domain."""
        mock_repo = MagicMock(spec=FailedOperationRepository)
        mock_repo.evict_oldest.return_value = 4

        enforce_overflow_eviction(
            mock_repo,
            make_settings(strategy="drop_oldest", interval=4),
            "payment",
            self._detected("domain"),
        )

        mock_repo.evict_oldest.assert_called_once_with(4, domain="payment")

    def test_global_scope_evicts_across_all_domains(self):
        """Global scope evicts with domain=None (whole store)."""
        mock_repo = MagicMock(spec=FailedOperationRepository)
        mock_repo.evict_oldest.return_value = 2

        enforce_overflow_eviction(
            mock_repo,
            make_settings(strategy="drop_oldest", interval=2),
            "payment",
            self._detected("global"),
        )

        mock_repo.evict_oldest.assert_called_once_with(2, domain=None)

    def test_compress_oldest_degrades_to_drop_with_warn_once(self):
        """compress_oldest (PRO-only) degrades to drop_oldest and still evicts."""
        mock_repo = MagicMock(spec=FailedOperationRepository)
        mock_repo.evict_oldest.return_value = 1

        assert overflow_module._compress_degrade_warned is False
        enforce_overflow_eviction(
            mock_repo,
            make_settings(strategy="compress_oldest"),
            "payment",
            self._detected("global"),
        )

        assert overflow_module._compress_degrade_warned is True
        mock_repo.evict_oldest.assert_called_once()  # drop semantics applied

    def test_all_candidates_protected_sets_soft_cap_warn_once(self):
        """evicted == 0 (all REPLAYING/REVIEWING) accepts over the soft cap."""
        mock_repo = MagicMock(spec=FailedOperationRepository)
        mock_repo.evict_oldest.return_value = 0

        assert overflow_module._soft_cap_warned is False
        enforce_overflow_eviction(
            mock_repo,
            make_settings(strategy="drop_oldest"),
            "payment",
            self._detected("global"),
        )

        assert overflow_module._soft_cap_warned is True


# =============================================================================
# DLQCaptureService._enforce_overflow — store-path seam (OSS)
# =============================================================================


class TestOSSOverflowEnforcementBehavior:
    """The OSS seam: reject / evict-then-accept / under-cap / fail-open."""

    @pytest.fixture
    def service(self, repo):
        return DLQCaptureService(config=DLQConfig(enabled=True), repository=repo)

    def test_reject_strategy_at_cap_returns_failed(self, service, repo):
        repo.create(domain="payment", failure_type="X")
        repo.create(domain="payment", failure_type="X")
        settings = make_settings(strategy="reject", max_size_per_domain=2)

        with patch(_SERVICE_SETTINGS, return_value=settings):
            result = service._enforce_overflow("payment", "X", "err")

        assert result is not None
        assert result.success is False
        assert result.error == "domain_capacity_exceeded"

    def test_drop_oldest_overflow_evicts_then_accepts(self, service, repo):
        for _ in range(3):
            repo.create(domain="payment", failure_type="X")
        settings = make_settings(strategy="drop_oldest", max_size_per_domain=2)

        with patch(_SERVICE_SETTINGS, return_value=settings):
            result = service._enforce_overflow("payment", "X", "err")

        assert result is None  # proceed
        assert repo.count_by_domain("payment") < 3  # synchronous eviction ran

    def test_under_cap_proceeds_without_eviction(self, service, repo):
        settings = make_settings(max_size=100, max_size_per_domain=100)

        with (
            patch.object(repo, "evict_oldest", wraps=repo.evict_oldest) as evict_spy,
            patch(_SERVICE_SETTINGS, return_value=settings),
        ):
            result = service._enforce_overflow("payment", "X", "err")

        assert result is None
        evict_spy.assert_not_called()

    def test_handle_overflow_exception_fails_open_to_accept(self, service):
        with patch(
            "baldur.services.dlq_capture.service.handle_overflow",
            side_effect=RuntimeError("boom"),
        ):
            result = service._enforce_overflow("payment", "X", "err")

        assert result is None  # fail-open accept
