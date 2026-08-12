"""The error-budget compute answers tier absence instead of failing it.

An install without the PRO distribution had no error-budget service to ask,
and the compute called a name its own ``ImportError`` guard had just set to
``None``: a ``TypeError`` caught one frame later, logged with a full
traceback, and cached as ``{"status": "error"}`` — repeated on the refresh
cadence, forever, on every install that ran ``init()``.

Two halves have to hold together, and each covers a path the other cannot:

- the refresh job stops registering a key this install can never answer, so
  the cadence stops paying a compute plus an L1/L2 write for a constant;
- the compute itself answers *before* the import, because the read path
  re-executes it on every L1+L2 miss — a registration-only fix would leave
  readers receiving the error dict.

The "broken PRO install" case is the discriminator for both: the guard must
cover designed absence only, so an importable-but-failing service keeps its
loud ERROR. It is driven by blocking the submodule in ``sys.modules`` rather
than by the ambient tier, so the case measures the same thing whether or not
a PRO distribution happens to be installed alongside this tree.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.services.precomputed_cache.compute_functions import (
    compute_error_budget_status,
    get_cached_error_budget,
    register_default_compute_functions,
)
from baldur.services.precomputed_cache.constants import (
    CACHE_KEY_ERROR_BUDGET,
    CACHE_KEY_HEALTH,
    CACHE_KEY_POOL_STATUS,
)
from baldur.services.precomputed_cache.multi_tier import get_cached_response
from baldur.services.precomputed_cache.worker import (
    get_precomputed_cache_worker,
    reset_precomputed_cache_worker,
)

COMPUTE_FAILED = "precomputed_cache.error_budget_compute_failed"
KEY_SKIPPED = "precomputed_cache.pro_gated_key_skipped"

_PRO_ERROR_BUDGET_MODULE = "baldur_pro.services.error_budget"


def _count_event(logs: list[dict], event: str, level: str | None = None) -> int:
    """Count captured structlog records matching ``event`` (and ``level``)."""
    return sum(
        1
        for entry in logs
        if entry.get("event") == event
        and (level is None or entry.get("log_level") == level)
    )


@contextmanager
def _tier(installed: bool):
    """Pin the PRO-wheel presence probe (a module-level singleton function)."""
    with patch("baldur.utils.tier.is_pro_installed", return_value=installed):
        yield


@contextmanager
def _pro_present_but_broken():
    """PRO probes present, but its error-budget module refuses to import.

    Blocking the submodule reproduces the broken-install posture identically
    in a PRO-present and a PRO-absent tree: ``from ... import`` raises
    ``ImportError``, the inner guard binds ``None``, and the call one line
    later raises ``TypeError`` into the module's own ``except``.
    """
    with _tier(True), patch.dict("sys.modules", {_PRO_ERROR_BUDGET_MODULE: None}):
        yield


class TestComputeErrorBudgetStatusBehavior:
    """Designed absence answers quietly; a real failure still shouts."""

    def test_tier_absent_returns_the_designed_absence_payload(self):
        with _tier(False):
            payload = compute_error_budget_status()

        assert payload["status"] == "unavailable"
        assert payload["reason"] == "pro_not_installed"
        # A parseable stamp, not a literal — the payload is a cached answer and
        # readers date it.
        assert datetime.fromisoformat(payload["timestamp"])

    def test_tier_absent_emits_no_compute_failed_record(self):
        """The line this whole change exists to delete, at its source."""
        with _tier(False), capture_logs() as logs:
            compute_error_budget_status()

        assert _count_event(logs, COMPUTE_FAILED) == 0

    def test_tier_absent_never_reports_an_error_status(self):
        """Absence is not failure: the worker counts a returned dict as
        success either way, so an ``error`` status here would be cached and
        served as one."""
        with _tier(False):
            payload = compute_error_budget_status()

        assert payload["status"] != "error"
        assert "error" not in payload

    def test_a_broken_pro_install_still_reports_the_failure_loudly(self):
        """The guard covers absence only — an install that *has* the
        capability and cannot answer keeps its ERROR record and its error
        payload."""
        with _pro_present_but_broken(), capture_logs() as logs:
            payload = compute_error_budget_status()

        assert payload["status"] == "error"
        assert _count_event(logs, COMPUTE_FAILED, "error") == 1

    def test_the_read_path_recomputes_the_absence_payload_on_a_miss(self):
        """The read path re-executes the compute on every L1+L2 miss, so the
        guard — not the registration skip — is what covers readers.

        Both tiers are switched off through the production function's own
        parameters, which is exactly the L3 branch a cold cache takes.
        """
        with _tier(False), capture_logs() as logs:
            payload = get_cached_response(
                CACHE_KEY_ERROR_BUDGET,
                compute_error_budget_status,
                use_l1=False,
                use_l2=False,
            )

        assert payload["status"] == "unavailable"
        assert payload["reason"] == "pro_not_installed"
        assert _count_event(logs, COMPUTE_FAILED) == 0

    def test_the_public_read_helper_routes_through_the_guarded_compute(self):
        """``get_cached_error_budget`` hands the cache the guarded function —
        the seam that makes the case above true of the real reader."""
        with patch(
            "baldur.services.precomputed_cache.compute_functions.get_cached_response",
            return_value={"status": "unavailable"},
        ) as cached_response:
            get_cached_error_budget()

        cached_response.assert_called_once_with(
            CACHE_KEY_ERROR_BUDGET, compute_error_budget_status
        )


class TestRegisterDefaultComputeFunctionsBehavior:
    """The refresh job's key list stops overstating what this install answers."""

    @pytest.fixture(autouse=True)
    def fresh_worker(self):
        """Registration accumulates on a process-global worker."""
        reset_precomputed_cache_worker()
        yield
        reset_precomputed_cache_worker()

    def test_tier_absent_registers_every_key_except_the_error_budget_one(self):
        """The skip removes exactly one of three: an empty registry would let
        the worker's first tick stamp a completed pass without computing."""
        with _tier(False):
            register_default_compute_functions()

        registered = get_precomputed_cache_worker().get_stats()["registered_keys"]

        assert CACHE_KEY_ERROR_BUDGET not in registered
        assert CACHE_KEY_HEALTH in registered
        assert CACHE_KEY_POOL_STATUS in registered

    def test_tier_present_registers_the_error_budget_key(self):
        with _tier(True):
            register_default_compute_functions()

        registered = get_precomputed_cache_worker().get_stats()["registered_keys"]

        assert CACHE_KEY_ERROR_BUDGET in registered

    def test_the_skip_leaves_a_debug_breadcrumb_naming_the_key(self):
        """Silence would make the missing key indistinguishable from a wiring
        bug; DEBUG matches how the scheduler reports its own tier filter."""
        with _tier(False), capture_logs() as logs:
            register_default_compute_functions()

        skipped = [entry for entry in logs if entry.get("event") == KEY_SKIPPED]

        assert len(skipped) == 1
        assert skipped[0]["log_level"] == "debug"
        assert skipped[0]["cache_key"] == CACHE_KEY_ERROR_BUDGET

    def test_tier_present_leaves_no_skip_breadcrumb(self):
        with _tier(True), capture_logs() as logs:
            register_default_compute_functions()

        assert _count_event(logs, KEY_SKIPPED) == 0
