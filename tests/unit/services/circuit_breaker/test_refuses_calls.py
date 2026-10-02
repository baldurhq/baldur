"""``CircuitBreakerService.refuses_calls``: the admission rule, read without a call.

The recovery trial and a trial-dispatched recovery chain decide from the shared
store's rows whether a job's breaker would turn a replay away right now
(807 D3, D7). The answer is the breaker's own admission rule over one row:

- a CLOSED row never refuses;
- an OPEN row refuses under an operator's Block, inside its recovery timeout,
  and with no ``opened_at`` — unless the operator's own pin is due to lift;
- an OPEN row past its recovery timeout and a HALF_OPEN row admit a call (the
  admission moves the row and takes a half-open slot), except while automatic
  transitions are frozen;
- a row it cannot read refuses. It never raises.

The service runs for real with a pinned config; the freeze is the one seam
patched (its own module owns the decision).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

RECOVERY_TIMEOUT_SECONDS = 600
NAME = "payment-api"
_FREEZE = "baldur.services.circuit_breaker.service.should_allow_cb_state_change"


def _service() -> CircuitBreakerService:
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True, recovery_timeout=RECOVERY_TIMEOUT_SECONDS
        )
    )


def _ago(seconds: float):
    return utc_now() - timedelta(seconds=seconds)


def _row(state: str, **fields) -> CircuitBreakerStateData:
    return CircuitBreakerStateData(service_name=NAME, state=state, **fields)


class TestRefusesCallsBehavior:
    """Decision table over one row."""

    @pytest.mark.parametrize(
        ("make_row", "refuses"),
        [
            (lambda: _row("closed"), False),
            (lambda: _row("open", opened_at=_ago(5)), True),
            (lambda: _row("open", opened_at=_ago(RECOVERY_TIMEOUT_SECONDS - 5)), True),
            (lambda: _row("open", opened_at=_ago(RECOVERY_TIMEOUT_SECONDS + 5)), False),
            (lambda: _row("open", opened_at=None), True),
            (
                lambda: _row(
                    "open",
                    opened_at=_ago(RECOVERY_TIMEOUT_SECONDS + 60),
                    manually_controlled=True,
                    manual_override_expires_at=utc_now() + timedelta(minutes=30),
                ),
                True,
            ),
            (
                lambda: _row(
                    "open",
                    opened_at=_ago(120),
                    manually_controlled=True,
                    manual_override_expires_at=_ago(60),
                ),
                False,
            ),
            (
                lambda: _row(
                    "open",
                    opened_at=_ago(30),
                    manually_controlled=True,
                    manual_override_expires_at=_ago(60),
                ),
                True,
            ),
            (lambda: _row("half_open"), False),
            (
                lambda: _row(
                    "closed",
                    manually_controlled=True,
                    manual_override_expires_at=utc_now() + timedelta(minutes=30),
                ),
                False,
            ),
        ],
        ids=[
            "closed",
            "open_inside_timeout",
            "open_just_inside_timeout",
            "open_past_timeout",
            "open_without_opened_at",
            "operator_block_past_timeout",
            "operator_pin_due_to_lift",
            "automatic_open_after_a_lapsed_pin",
            "half_open",
            "closed_under_a_force_close_pin",
        ],
    )
    def test_refuses_calls_by_the_admission_rule(self, make_row, refuses):
        assert _service().refuses_calls(make_row()) is refuses

    @pytest.mark.parametrize(
        ("make_row", "refuses"),
        [
            (lambda: _row("half_open"), True),
            (lambda: _row("open", opened_at=_ago(RECOVERY_TIMEOUT_SECONDS + 5)), True),
            (lambda: _row("closed"), False),
        ],
        ids=["half_open", "open_past_timeout", "closed"],
    )
    def test_refuses_calls_while_frozen(self, make_row, refuses):
        """The admission would make the automatic OPEN -> HALF_OPEN move the
        freeze withholds."""
        with patch(_FREEZE, return_value=False):
            assert _service().refuses_calls(make_row()) is refuses

    def test_refuses_calls_reads_the_row_s_own_effective_config(self):
        service = _service()
        row = _row("open", opened_at=_ago(30))

        with patch.object(
            CircuitBreakerService,
            "get_effective_config",
            autospec=True,
            return_value=CircuitBreakerConfig(enabled=True, recovery_timeout=10),
        ) as effective:
            refuses = service.refuses_calls(row)

        assert refuses is False
        effective.assert_called_once_with(service, NAME)

    def test_refuses_calls_on_a_row_it_cannot_read_refuses_without_raising(self):
        service = _service()

        with (
            patch.object(
                CircuitBreakerService,
                "get_effective_config",
                autospec=True,
                side_effect=RuntimeError("config store down"),
            ),
            capture_logs() as logs,
        ):
            refuses = service.refuses_calls(_row("open", opened_at=_ago(5)))

        assert refuses is True
        failed = [
            e for e in logs if e["event"] == "circuit_breaker.refusal_read_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["service_name"] == NAME

    def test_refuses_calls_is_a_pure_read(self):
        """Judging a row moves no state: the row the trial reads stays as read."""
        row = _row("open", opened_at=_ago(RECOVERY_TIMEOUT_SECONDS + 5))
        before = (row.state, row.half_open_request_count, row.opened_at)

        _service().refuses_calls(row)

        assert (row.state, row.half_open_request_count, row.opened_at) == before
