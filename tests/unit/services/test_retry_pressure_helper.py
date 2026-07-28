"""The shared attempt-start helper — one 1-based convention, five call sites.

``record_retry_attempt_started`` is the single point where an attempt number
becomes the ``is_retry`` flag the pressure ratio is built from. Both retry
policies and the tenacity bridge route through it precisely so the sync,
async and bridge surfaces cannot drift on that derivation — which is also why
an off-by-one here would not fail anywhere loudly: it would just move one
attempt per sequence from the denominator-only child into the numerator,
skewing every shipped retry-pressure panel and alert by a fixed factor.

The helper is also the fail-open envelope between the loop and the metric
backend: it is called on the business call's own thread, at the top of every
attempt, so a recorder fault must be swallowed rather than turned into a
failed request.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.services.retry_handler.observability import record_retry_attempt_started

# The helper imports the facade inside its own body, so the source module is
# the patch seam — the same one the terminal helper documents and the policy
# tests use.
_FACADE = "baldur.services.metrics.recorders.record_retry_attempt_started"


class TestRetryAttemptStartedHelperBehavior:
    """The 1-based attempt number becomes ``is_retry``; faults never propagate."""

    @pytest.mark.parametrize(
        ("attempt", "expected_is_retry"),
        [(1, False), (2, True), (5, True)],
        ids=["first_attempt", "first_retry", "deep_in_the_ladder"],
    )
    def test_attempt_start_derives_is_retry_from_the_one_based_attempt_number(
        self, attempt, expected_is_retry
    ):
        """Attempt 1 is demand, everything past it is a retry.

        The boundary is between 1 and 2: attempt 1 is the call the caller asked
        for and belongs to the denominator only, while attempt 2 onward is
        pressure the retry layer itself created.
        """
        with patch(_FACADE, autospec=True) as mock_facade:
            record_retry_attempt_started("payment", attempt)

        mock_facade.assert_called_once_with("payment", is_retry=expected_is_retry)

    def test_attempt_start_forwards_the_raw_domain_without_resolving_it(self):
        """Label resolution belongs to the facade, not to a second copy here.

        A domain needing sanitization arrives unchanged: resolving on both
        sides would put two copies of the cardinality policy on the hot path,
        and the second one would silently win.
        """
        with patch(_FACADE, autospec=True) as mock_facade:
            record_retry_attempt_started("Payment-API/v2", 3)

        assert mock_facade.call_args.args[0] == "Payment-API/v2"

    def test_attempt_start_recording_is_fail_open_when_the_facade_raises(self):
        """A recorder fault is swallowed and logged, not raised into the loop."""
        with (
            patch(_FACADE, side_effect=RuntimeError("recorder down")),
            capture_logs() as logs,
        ):
            record_retry_attempt_started("payment", 2)

        record = next(
            log
            for log in logs
            if log["event"] == "retry.attempt_start_recording_failed"
        )
        assert record["log_level"] == "warning"
