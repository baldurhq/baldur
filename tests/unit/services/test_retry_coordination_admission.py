"""The key-identity and admission rules both retry stages share.

Target: services/retry_handler/coordination.py
- ``coordination_key()``: the ``or`` identity rule
- ``coordination_admitted()``: opt-out -> kill switch -> identity gate, in
  that order, with the once-per-key WARNING
- ``resolve_coordinator_sync()``: injection precedence and the fail-open wrap

The rules used to live on the synchronous ``RetryPolicy``. They were lifted
out so the asynchronous stage could not disagree with it — and so the WARNING
dedup set is one record for the whole process rather than one per module.
Every test here must be able to fail because the two stages stopped sharing a
rule, not because either loop stopped calling a collaborator it was given.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.backoff import ConstantBackoff
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitResult
from baldur.services.retry_handler.coordination import (
    coordination_admitted,
    coordination_key,
    resolve_coordinator_sync,
)
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.services.retry_handler.rate_limit_detection import (
    UNIDENTIFIED_COORDINATION_KEY,
)
from baldur.settings.rate_limit_backoff import reset_rate_limit_backoff_settings

_SKIPPED_EVENT = "retry.rate_limit_coordination_skipped"
_RESOLUTION_FAILED_EVENT = "retry.rate_limit_coordinator_resolution_failed"
_COORDINATION_SWITCH_ENV = "BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED"


@pytest.fixture
def coordination_switch(monkeypatch):
    """Set the deployment kill switch and drop the cached settings node."""

    def _set(enabled: bool) -> None:
        monkeypatch.setenv(_COORDINATION_SWITCH_ENV, "true" if enabled else "false")
        reset_rate_limit_backoff_settings()

    yield _set
    reset_rate_limit_backoff_settings()


def _skipped_lines(logs) -> list:
    return [entry for entry in logs if entry["event"] == _SKIPPED_EVENT]


# =============================================================================
# coordination_key — the identity rule
# =============================================================================


class TestCoordinationKeyBehavior:
    """One expression decides what counts as an override."""

    @pytest.mark.parametrize(
        ("rate_limit_key", "domain", "expected"),
        [
            (None, "payment", "payment"),
            ("stripe-api", "payment", "stripe-api"),
            ("", "payment", "payment"),
            ("", UNIDENTIFIED_COORDINATION_KEY, UNIDENTIFIED_COORDINATION_KEY),
        ],
        ids=[
            "unset_falls_back",
            "override_wins",
            "empty_falls_back",
            "empty_is_not_a_rescue",
        ],
    )
    def test_coordination_key_reads_set_ness_as_truthiness(
        self, rate_limit_key, domain, expected
    ):
        """An empty override is not an identity — it falls back to the domain."""
        assert coordination_key(rate_limit_key, domain) == expected


# =============================================================================
# coordination_admitted — the lever order
# =============================================================================


class TestCoordinationAdmissionBehavior:
    """Opt-out, then the kill switch, then the identity gate — the gate alone logs."""

    def test_an_identified_key_with_both_levers_on_is_admitted(self):
        with capture_logs() as logs:
            admitted = coordination_admitted(
                rate_limit_aware=True, rate_limit_key=None, domain="payment"
            )

        assert admitted is True
        assert _skipped_lines(logs) == []

    def test_the_per_policy_opt_out_refuses_first(self, coordination_switch):
        """``rate_limit_aware=False`` refuses before the settings are even read."""
        coordination_switch(True)

        with patch(
            "baldur.settings.rate_limit_backoff.get_rate_limit_backoff_settings",
            autospec=True,
        ) as settings_read:
            admitted = coordination_admitted(
                rate_limit_aware=False, rate_limit_key=None, domain="payment"
            )

        assert admitted is False
        settings_read.assert_not_called()

    def test_the_kill_switch_refuses_second(self, coordination_switch):
        coordination_switch(False)

        admitted = coordination_admitted(
            rate_limit_aware=True, rate_limit_key=None, domain="payment"
        )

        assert admitted is False

    def test_the_identity_gate_refuses_last_and_warns(self):
        """The gate is the only conjunct that logs, so it runs after both levers."""
        with capture_logs() as logs:
            admitted = coordination_admitted(
                rate_limit_aware=True,
                rate_limit_key=None,
                domain=UNIDENTIFIED_COORDINATION_KEY,
            )

        assert admitted is False
        skipped = _skipped_lines(logs)
        assert len(skipped) == 1
        assert skipped[0]["log_level"] == "warning"
        assert skipped[0]["reason"] == "unidentified_domain"
        assert skipped[0]["domain"] == UNIDENTIFIED_COORDINATION_KEY

    @pytest.mark.parametrize(
        ("rate_limit_aware", "switch_enabled"),
        [(False, True), (True, False)],
        ids=["opt_out", "switch_off"],
    )
    def test_no_warning_when_a_lever_is_off(
        self, coordination_switch, rate_limit_aware, switch_enabled
    ):
        """An operator who turned coordination off is not told to configure it."""
        coordination_switch(switch_enabled)

        with capture_logs() as logs:
            admitted = coordination_admitted(
                rate_limit_aware=rate_limit_aware,
                rate_limit_key=None,
                domain=UNIDENTIFIED_COORDINATION_KEY,
            )

        assert admitted is False
        assert _skipped_lines(logs) == []

    def test_an_empty_key_is_not_an_identity_at_the_gate(self):
        """The gate and the key read set-ness the same way: ``""`` falls back."""
        with capture_logs() as logs:
            admitted = coordination_admitted(
                rate_limit_aware=True,
                rate_limit_key="",
                domain=UNIDENTIFIED_COORDINATION_KEY,
            )

        assert admitted is False
        assert len(_skipped_lines(logs)) == 1

    def test_a_key_rescues_the_placeholder_domain(self):
        assert (
            coordination_admitted(
                rate_limit_aware=True,
                rate_limit_key="stripe-api",
                domain=UNIDENTIFIED_COORDINATION_KEY,
            )
            is True
        )

    def test_the_warning_fires_once_per_key_per_process(self):
        """Idempotency: five refusals on one key are one WARNING line."""
        with capture_logs() as logs:
            for _ in range(5):
                coordination_admitted(
                    rate_limit_aware=True,
                    rate_limit_key=None,
                    domain=UNIDENTIFIED_COORDINATION_KEY,
                )

        assert len(_skipped_lines(logs)) == 1

    def test_the_dedup_record_is_shared_by_both_retry_stages(self):
        """Sync then async on one unidentified key -> one line, not one per module.

        The set lives in the shared module for exactly this reason: a second
        resolver in the async stage would have warned once per key *per
        module*, breaking the "one WARNING line per key per process" contract.
        """
        sync_policy = RetryPolicy(
            config=RetryPolicyConfig(
                max_attempts=1, domain=UNIDENTIFIED_COORDINATION_KEY
            ),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        async_policy = AsyncRetryPolicy(
            max_retries=0, domain=UNIDENTIFIED_COORDINATION_KEY
        )

        async def _ok():
            return "ok"

        with capture_logs() as logs:
            sync_policy.execute(lambda: "ok")
            asyncio.run(async_policy.execute(_ok))

        assert len(_skipped_lines(logs)) == 1

    def test_the_async_stage_alone_warns_through_the_same_record(self):
        """Discriminator for the row above: the async stage does reach the gate."""
        async_policy = AsyncRetryPolicy(
            max_retries=0, domain=UNIDENTIFIED_COORDINATION_KEY
        )

        async def _ok():
            return "ok"

        with capture_logs() as logs:
            asyncio.run(async_policy.execute(_ok))

        assert len(_skipped_lines(logs)) == 1


# =============================================================================
# resolve_coordinator_sync — injection precedence and the fail-open wrap
# =============================================================================


class TestResolveCoordinatorSyncBehavior:
    """Injection wins over every lever; every fault degrades to ``None``."""

    @pytest.fixture
    def singleton(self):
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=coordinator,
        ) as get_instance:
            yield coordinator, get_instance

    def test_an_injected_coordinator_bypasses_both_levers(
        self, coordination_switch, singleton
    ):
        """A caller who constructed a coordinator asked for it."""
        coordination_switch(False)
        injected = MagicMock(spec=RateLimitCoordinator)
        _singleton, get_instance = singleton

        resolved = resolve_coordinator_sync(
            injected=injected,
            rate_limit_aware=False,
            rate_limit_key=None,
            domain=UNIDENTIFIED_COORDINATION_KEY,
        )

        assert resolved is injected
        get_instance.assert_not_called()

    def test_an_admitted_call_resolves_the_singleton(self, singleton):
        coordinator, get_instance = singleton

        resolved = resolve_coordinator_sync(
            injected=None, rate_limit_aware=True, rate_limit_key=None, domain="payment"
        )

        assert resolved is coordinator
        get_instance.assert_called_once()

    def test_a_refused_call_resolves_nothing(self, singleton):
        """Negative at the resolution seam: the singleton is not even built."""
        _coordinator, get_instance = singleton

        resolved = resolve_coordinator_sync(
            injected=None,
            rate_limit_aware=True,
            rate_limit_key=None,
            domain=UNIDENTIFIED_COORDINATION_KEY,
        )

        assert resolved is None
        get_instance.assert_not_called()

    def test_a_resolution_fault_degrades_to_none_with_a_warning(self):
        """Storage auto-detect and a Redis connect sit on the business call's path."""
        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            side_effect=RuntimeError("storage auto-detect exploded"),
        ):
            with capture_logs() as logs:
                resolved = resolve_coordinator_sync(
                    injected=None,
                    rate_limit_aware=True,
                    rate_limit_key=None,
                    domain="payment",
                )

        assert resolved is None
        failed = [entry for entry in logs if entry["event"] == _RESOLUTION_FAILED_EVENT]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"
        assert failed[0]["domain"] == "payment"

    def test_a_settings_read_fault_is_inside_the_same_wrap(self, singleton):
        _coordinator, get_instance = singleton

        with patch(
            "baldur.settings.rate_limit_backoff.get_rate_limit_backoff_settings",
            autospec=True,
            side_effect=RuntimeError("settings backend down"),
        ):
            resolved = resolve_coordinator_sync(
                injected=None,
                rate_limit_aware=True,
                rate_limit_key=None,
                domain="payment",
            )

        assert resolved is None
        get_instance.assert_not_called()
