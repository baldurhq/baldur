"""Unit tests for ``baldur.api.middleware.deadline`` (592).

Scope:
    - ``check_deadline``: inbound ``X-Deadline-Remaining`` fast-fail — the
      header-absent / above-threshold / below-threshold / malformed-fail-open
      branches, the ``DEADLINE_ENABLED`` gate, the deadline-set-before-reject
      side effect, and the 503 reject response shape (``DEADLINE_FAST_FAIL`` +
      ``X-Baldur-Deadline-Rejected``).
    - ``record_rtt_sample``: the triple-filter (2xx-only / above-min-threshold /
      probabilistic) post-response RTT sampler, its ``tier_id=None`` short
      circuit, and the PRO-gated fail-open no-op.
    - ``TestDeadlineOrderingInvariant``: the correctness consequence of running
      ``check_deadline`` BEFORE ``check_admission`` — a tighter inbound deadline
      survives admission's degraded-tier forced 1 s cap (the pipeline-order tests
      in the Flask / FastAPI middleware suites prove the ordering itself).

``check_deadline`` resolves all of ``baldur.scaling.deadline_context`` via a
lazy import, so the ``DEADLINE_ENABLED`` gate is toggled by patching the
attribute on that module. ``record_rtt_sample``'s PRO gradient calculator is
exercised WITHOUT a real ``baldur_pro`` install by injecting a fake (or ``None``)
``baldur_pro.services.throttle.gradient`` into ``sys.modules`` — so this file
stays a pure-OSS test (no module-level ``baldur_pro`` import, no ``requires_pro``
marker) yet still asserts the sampling-fires path and the import-failure no-op.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from baldur.api.middleware import deadline as deadline_mod
from baldur.api.middleware.deadline import check_deadline, record_rtt_sample
from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
    ResponseContext,
)
from baldur.scaling.deadline_context import (
    _RTT_MIN_SAMPLE_MS,
    _RTT_SAMPLE_RATE,
    DEADLINE_HEADER,
    DEFAULT_MINIMUM_USEFUL_TIME_MS,
    _request_deadline,
)

# =============================================================================
# Fixtures / builders
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_deadline():
    """Reset the deadline ContextVar around each test (check_deadline writes it)."""
    _request_deadline.set(None)
    yield
    _request_deadline.set(None)


def _make_request(
    deadline_header: str | None = None,
    *,
    path: str = "/api/test",
) -> RequestContext:
    headers: dict[str, str] = {}
    if deadline_header is not None:
        headers[DEADLINE_HEADER] = deadline_header
    return RequestContext(method=HttpMethod.GET, path=path, headers=headers)


@contextmanager
def _fake_pro_gradient(*, side_effect=None):
    """Inject a fake ``baldur_pro...gradient`` so record_rtt_sample's PRO path
    is exercisable without a real ``baldur_pro`` install.

    Yields a namespace exposing the ``get_gradient_calculator`` mock and the
    per-calculator ``add_sample`` mock for call assertions.
    """
    from unittest.mock import MagicMock

    calc = MagicMock()
    get_calc = MagicMock(return_value=calc)
    if side_effect is not None:
        get_calc.side_effect = side_effect

    fake_leaf = types.ModuleType("baldur_pro.services.throttle.gradient")
    fake_leaf.get_gradient_calculator = get_calc
    injected = {
        "baldur_pro": types.ModuleType("baldur_pro"),
        "baldur_pro.services": types.ModuleType("baldur_pro.services"),
        "baldur_pro.services.throttle": types.ModuleType(
            "baldur_pro.services.throttle"
        ),
        "baldur_pro.services.throttle.gradient": fake_leaf,
    }
    with patch.dict(sys.modules, injected):
        yield SimpleNamespace(get_calc=get_calc, calc=calc)


# =============================================================================
# check_deadline — Behavior
# =============================================================================


class TestCheckDeadlineBehavior:
    """Allow/reject decision driven by the inbound X-Deadline-Remaining header."""

    def test_no_header_returns_none(self):
        """Absent header -> pass-through, no deadline set."""
        assert check_deadline(_make_request()) is None
        assert _request_deadline.get() is None

    def test_header_above_min_returns_none_and_sets_deadline(self):
        """A header above the minimum useful time passes AND sets the deadline."""
        result = check_deadline(_make_request("100ms"))
        assert result is None
        # set_deadline ran: 100ms - 50ms network buffer ≈ 50ms remaining.
        assert _request_deadline.get() is not None

    def test_header_at_min_returns_none(self):
        """Boundary: remaining == DEFAULT_MINIMUM_USEFUL_TIME_MS is NOT below it."""
        result = check_deadline(_make_request(f"{DEFAULT_MINIMUM_USEFUL_TIME_MS}ms"))
        assert result is None

    def test_header_just_below_min_returns_503(self):
        """Boundary: just below the minimum useful time fast-fails with 503."""
        below = DEFAULT_MINIMUM_USEFUL_TIME_MS - 1
        result = check_deadline(_make_request(f"{below}ms"))
        assert isinstance(result, ResponseContext)
        assert result.status_code == 503

    def test_deadline_set_before_reject(self):
        """State: set_deadline runs BEFORE the < min reject (deadline written)."""
        result = check_deadline(_make_request("10ms"))
        assert result is not None  # rejected
        # The deadline was written on the fast-fail path even though we reject.
        assert _request_deadline.get() is not None

    @pytest.mark.parametrize("bad_value", ["abc", "-100ms", "1000s", "ms", "12.3.4"])
    def test_malformed_header_fails_open(self, bad_value):
        """Malformed header -> pass-through (None), no deadline set (fail-open)."""
        result = check_deadline(_make_request(bad_value))
        assert result is None
        assert _request_deadline.get() is None

    def test_disabled_bypasses_check(self, monkeypatch):
        """DEADLINE_ENABLED=False -> no fast-fail even with a tight header."""
        monkeypatch.setattr("baldur.scaling.deadline_context.DEADLINE_ENABLED", False)
        result = check_deadline(_make_request("10ms"))
        assert result is None
        assert _request_deadline.get() is None

    def test_enabled_with_tight_header_rejects(self, monkeypatch):
        """DEADLINE_ENABLED=True (default) + tight header -> reject (gate paired)."""
        monkeypatch.setattr("baldur.scaling.deadline_context.DEADLINE_ENABLED", True)
        result = check_deadline(_make_request("10ms"))
        assert result is not None
        assert result.status_code == 503

    def test_path_prefix_passed_to_metric(self):
        """The first path segment is forwarded to the fast-fail metric."""
        with patch(
            "baldur.scaling.deadline_context.record_fast_fail"
        ) as mock_fast_fail:
            check_deadline(_make_request("10ms", path="/payments/charge"))
        mock_fast_fail.assert_called_once()
        assert mock_fast_fail.call_args.kwargs["path_prefix"] == "payments"

    def test_module_unavailable_fails_open(self):
        """An ImportError resolving deadline_context -> pass-through (fail-open)."""
        with patch.dict(sys.modules, {"baldur.scaling.deadline_context": None}):
            assert check_deadline(_make_request("10ms")) is None


# =============================================================================
# check_deadline — Contract
# =============================================================================


class TestCheckDeadlineContract:
    """The 503 fast-fail reject response shape is a wire contract."""

    def test_reject_response_shape(self):
        result = check_deadline(_make_request("10ms"))
        assert isinstance(result, ResponseContext)
        assert result.status_code == 503
        assert result.body["error"] == "Deadline Exceeded"
        assert result.body["code"] == "DEADLINE_FAST_FAIL"
        assert result.body["remaining_ms"] == 10.0
        assert result.body["retry_after"] == 0

    def test_reject_response_headers(self):
        """L7 proxies distinguish a deadline fast-fail via the dedicated header."""
        result = check_deadline(_make_request("10ms"))
        assert result.headers["Retry-After"] == "0"
        assert result.headers["X-Baldur-Deadline-Rejected"] == "true"


# =============================================================================
# record_rtt_sample — Behavior
# =============================================================================


class TestRecordRttSampleBehavior:
    """Triple-filtered (2xx / above-min / probabilistic) PRO-gated RTT sampler."""

    def test_tier_id_none_is_noop(self):
        """OSS no-op: tier_id None returns before touching random / PRO import."""
        with patch.object(deadline_mod, "random") as mock_random:
            record_rtt_sample(None, 200, 100.0)
        mock_random.random.assert_not_called()

    def test_2xx_above_threshold_samples(self):
        """Happy path: 2xx + above-min + probability pass -> add_sample fires."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = 0.0
            record_rtt_sample("standard", 200, 42.0)
        pro.get_calc.assert_called_once_with("admission_control:standard")
        pro.calc.add_sample.assert_called_once_with(42.0)

    @pytest.mark.parametrize("status", [100, 199, 300, 404, 500, 503])
    def test_non_2xx_not_sampled(self, status):
        """Only 2xx responses (real business logic) feed the gradient."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = 0.0
            record_rtt_sample("standard", status, 100.0)
        pro.get_calc.assert_not_called()

    @pytest.mark.parametrize("status", [200, 204, 299])
    def test_2xx_boundary_sampled(self, status):
        """The full 2xx band (200..299) is sampled."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = 0.0
            record_rtt_sample("standard", status, 100.0)
        pro.get_calc.assert_called_once()

    def test_below_min_threshold_not_sampled(self):
        """elapsed_ms below _RTT_MIN_SAMPLE_MS (health-check noise) is dropped."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = 0.0
            record_rtt_sample("standard", 200, _RTT_MIN_SAMPLE_MS - 0.1)
        pro.get_calc.assert_not_called()

    def test_at_min_threshold_sampled(self):
        """Boundary: elapsed_ms == _RTT_MIN_SAMPLE_MS is NOT below it -> sampled."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = 0.0
            record_rtt_sample("standard", 200, _RTT_MIN_SAMPLE_MS)
        pro.get_calc.assert_called_once()

    def test_probability_skip(self):
        """random() >= _RTT_SAMPLE_RATE drops the sample (lock-contention guard)."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = 0.5  # >= 0.1 -> skip
            record_rtt_sample("standard", 200, 100.0)
        pro.get_calc.assert_not_called()

    def test_probability_boundary_at_rate_skips(self):
        """Boundary: random() == _RTT_SAMPLE_RATE is NOT below it -> skip."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = _RTT_SAMPLE_RATE
            record_rtt_sample("standard", 200, 100.0)
        pro.get_calc.assert_not_called()

    def test_tier_separated_calculator_name(self):
        """The calculator name is keyed by the classified tier."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient() as pro,
        ):
            mock_random.random.return_value = 0.0
            record_rtt_sample("critical", 200, 100.0)
        pro.get_calc.assert_called_once_with("admission_control:critical")

    def test_pro_absent_is_clean_noop(self):
        """ImportError on the gradient import -> swallowed (no raise), fail-open."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            patch.dict(
                sys.modules,
                {"baldur_pro.services.throttle.gradient": None},
            ),
        ):
            mock_random.random.return_value = 0.0
            # Passes all filters, then the PRO import raises ImportError.
            record_rtt_sample("standard", 200, 100.0)  # must not raise

    def test_collection_failure_is_fail_open(self):
        """An unexpected calculator error must not propagate to the request."""
        with (
            patch.object(deadline_mod, "random") as mock_random,
            _fake_pro_gradient(side_effect=RuntimeError("boom")),
        ):
            mock_random.random.return_value = 0.0
            record_rtt_sample("standard", 200, 100.0)  # must not raise


# =============================================================================
# Deadline-before-admission ordering invariant (D1 correctness)
# =============================================================================


class TestDeadlineOrderingInvariant:
    """A tighter inbound deadline survives admission's degraded-tier forced cap.

    ``check_deadline`` runs BEFORE ``check_admission`` (proved by the pipeline
    call-order tests in the Flask / FastAPI middleware suites). This class proves
    the *consequence*: with the inbound deadline already set, admission's
    ``_maybe_force_degraded_deadline`` only caps when no tighter deadline exists,
    so a ``non_essential`` request under heavy load keeps its tighter inbound
    budget instead of being re-based to the 1 s cap.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        _request_deadline.set(None)
        yield
        _request_deadline.set(None)

    @staticmethod
    def _gate_at(level):
        from unittest.mock import MagicMock

        gate = MagicMock()
        gate.get_level.return_value = level
        return gate

    @pytest.mark.parametrize("level_name", ["HIGH", "CRITICAL"])
    def test_tighter_inbound_deadline_survives_forced_cap(self, level_name):
        from baldur.api.middleware.admission import (
            _DEGRADED_TIER_DEADLINE_MS,
            _maybe_force_degraded_deadline,
        )
        from baldur.scaling.deadline_context import get_remaining_ms, set_deadline
        from baldur.settings.backpressure import BackpressureLevel

        # check_deadline already set a tighter inbound deadline (~550ms remaining).
        set_deadline(600.0)
        before = get_remaining_ms()
        assert before is not None
        assert before < _DEGRADED_TIER_DEADLINE_MS

        _maybe_force_degraded_deadline(
            self._gate_at(getattr(BackpressureLevel, level_name)),
            "non_essential",
        )

        after = get_remaining_ms()
        assert after is not None
        # The 1 s forced cap must NOT overwrite the tighter inbound deadline.
        assert after <= before + 1.0
        assert after < _DEGRADED_TIER_DEADLINE_MS

    @pytest.mark.parametrize("level_name", ["HIGH", "CRITICAL"])
    def test_absent_inbound_deadline_gets_forced_cap(self, level_name):
        from baldur.api.middleware.admission import _maybe_force_degraded_deadline
        from baldur.scaling.deadline_context import get_remaining_ms
        from baldur.settings.backpressure import BackpressureLevel

        assert get_remaining_ms() is None
        _maybe_force_degraded_deadline(
            self._gate_at(getattr(BackpressureLevel, level_name)),
            "non_essential",
        )
        remaining = get_remaining_ms()
        assert remaining is not None
        # Forced to ~1000ms (minus the 50ms network buffer).
        assert 900 < remaining <= 1000

    def test_looser_inbound_deadline_is_capped(self):
        from baldur.api.middleware.admission import _maybe_force_degraded_deadline
        from baldur.scaling.deadline_context import get_remaining_ms, set_deadline
        from baldur.settings.backpressure import BackpressureLevel

        set_deadline(5000.0)  # looser than the 1 s degraded cap
        _maybe_force_degraded_deadline(
            self._gate_at(BackpressureLevel.HIGH), "non_essential"
        )
        remaining = get_remaining_ms()
        assert remaining is not None
        assert remaining <= 1000

    def test_no_force_when_backpressure_not_high(self):
        from baldur.api.middleware.admission import _maybe_force_degraded_deadline
        from baldur.scaling.deadline_context import get_remaining_ms, set_deadline
        from baldur.settings.backpressure import BackpressureLevel

        set_deadline(5000.0)
        _maybe_force_degraded_deadline(
            self._gate_at(BackpressureLevel.LOW), "non_essential"
        )
        remaining = get_remaining_ms()
        # Untouched — only HIGH/CRITICAL trigger the forced cap.
        assert remaining is not None
        assert remaining > 1000

    def test_no_force_for_non_degraded_tier(self):
        from baldur.api.middleware.admission import _maybe_force_degraded_deadline
        from baldur.scaling.deadline_context import get_remaining_ms
        from baldur.settings.backpressure import BackpressureLevel

        _maybe_force_degraded_deadline(
            self._gate_at(BackpressureLevel.CRITICAL), "critical"
        )
        # Only non_essential is degraded; critical/standard are never capped here.
        assert get_remaining_ms() is None
