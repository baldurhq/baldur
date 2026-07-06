"""RolloutWatchdog.detect_stalled_rollouts() unit tests (638 D5).

The extracted side-effect-free stall detector is the single source of truth for
the "stuck canary" definition, shared by the Celery ``scan_and_handle()``
maintenance path (which passes its already-fetched rollout list) and the
meta-watchdog ``canary_rollout`` semantic-stuck probe (which calls with no
arguments). It applies the zombie/stall check to each active rollout and returns
the stalled set with NO lock-renewal / notify / auto-rollback / metric side
effect (read-only).

These tests cover the CANARY / PROMOTING stall clauses (the PAUSED clause reads
``ZOMBIE_EXEMPT_TRIGGERS``, a PRO-side detail out of this OSS test's SUT) and
prove the read-only contract plus pre-fetched-vs-self-fetch parity.

Covers:
- state transition (stalled CANARY/PROMOTING → returned; fresh → not)
- boundary (empty input → empty output)
- side-effect-free verification (no renew/notify/rollback/promote on the service)
- pre-fetched-list vs self-fetch parity (same verdict either entry path)
"""

from __future__ import annotations

from types import SimpleNamespace

from baldur.models.canary import CanaryState
from baldur.tasks.canary_watchdog import RolloutWatchdog
from baldur.utils.time import utc_now

_MUTATING_METHODS = (
    "renew_config_lock",
    "rollback",
    "promote",
    "notify",
)


def _rollout(
    state: CanaryState,
    *,
    rollout_id: str = "r1",
    minutes_ago: float = 0.0,
    duration_minutes: int = 10,
):
    """A duck-typed CanaryRollout anchored ``minutes_ago`` into its current state."""
    now = utc_now()
    from datetime import timedelta

    anchor = now - timedelta(minutes=minutes_ago)
    return SimpleNamespace(
        id=rollout_id,
        state=state,
        config_type="feature_flags",
        created_by="alice",
        affected_clusters=["cluster-a"],
        created_at=anchor,
        stage_started_at=anchor,
        paused_at=None,
        current_stage=SimpleNamespace(duration_minutes=duration_minutes),
    )


def _watchdog_with_service(rollouts: list) -> tuple[RolloutWatchdog, object]:
    """A RolloutWatchdog whose lazy service is pre-bound to a recording fake."""
    service = SimpleNamespace(
        get_active_rollouts=lambda: rollouts,
        renew_config_lock=_Recorder(),
        rollback=_Recorder(),
        promote=_Recorder(),
        notify=_Recorder(),
    )
    watchdog = RolloutWatchdog()
    watchdog._service = service  # bypass the registry-backed lazy property
    return watchdog, service


class _Recorder:
    """A minimal callable that records whether it was invoked."""

    def __init__(self) -> None:
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        return None


class TestDetectStalledRolloutsBehavior:
    """Behavior verification for RolloutWatchdog.detect_stalled_rollouts()."""

    def test_stalled_canary_past_threshold_is_returned(self):
        """A CANARY rollout past 2x its stage duration is stalled."""
        # Given a CANARY rollout stuck 25 min with a 10-min stage (threshold 20)
        rollout = _rollout(CanaryState.CANARY, minutes_ago=25, duration_minutes=10)
        watchdog, _ = _watchdog_with_service([rollout])

        # When detection runs over the pre-fetched list
        stalled = watchdog.detect_stalled_rollouts([rollout], utc_now())

        # Then the rollout is reported with its id
        assert len(stalled) == 1
        assert stalled[0].rollout_id == "r1"
        assert stalled[0].state == CanaryState.CANARY.value

    def test_fresh_canary_is_not_returned(self):
        """A CANARY rollout within 2x its stage duration is not stalled."""
        rollout = _rollout(CanaryState.CANARY, minutes_ago=5, duration_minutes=10)
        watchdog, _ = _watchdog_with_service([rollout])

        stalled = watchdog.detect_stalled_rollouts([rollout], utc_now())

        assert stalled == []

    def test_stalled_promoting_past_five_minutes_is_returned(self):
        """A PROMOTING rollout stuck > 5 min (failed transition) is stalled."""
        rollout = _rollout(CanaryState.PROMOTING, minutes_ago=10)
        watchdog, _ = _watchdog_with_service([rollout])

        stalled = watchdog.detect_stalled_rollouts([rollout], utc_now())

        assert len(stalled) == 1
        assert stalled[0].state == CanaryState.PROMOTING.value

    def test_fresh_promoting_is_not_returned(self):
        """A PROMOTING rollout within the 5-min transition window is not stalled."""
        rollout = _rollout(CanaryState.PROMOTING, minutes_ago=2)
        watchdog, _ = _watchdog_with_service([rollout])

        stalled = watchdog.detect_stalled_rollouts([rollout], utc_now())

        assert stalled == []

    def test_empty_input_returns_empty(self):
        """No active rollouts → empty stalled set (boundary)."""
        watchdog, _ = _watchdog_with_service([])

        assert watchdog.detect_stalled_rollouts([], utc_now()) == []

    def test_no_mutating_side_effects_on_self_fetch(self):
        """Self-fetch path performs NO renew / rollback / promote / notify.

        The read-only contract is what lets the probe call this on every tick
        without disturbing rollout state.
        """
        # Given a stalled rollout reachable only through the service (self-fetch)
        rollout = _rollout(CanaryState.CANARY, minutes_ago=99, duration_minutes=10)
        watchdog, service = _watchdog_with_service([rollout])

        # When detection runs with no pre-fetched list
        stalled = watchdog.detect_stalled_rollouts()

        # Then it still finds the zombie but touches no mutating method
        assert len(stalled) == 1
        for method in _MUTATING_METHODS:
            assert getattr(service, method).called is False

    def test_prefetched_and_self_fetch_parity(self):
        """The same rollouts yield the same verdict via either entry path."""
        rollouts = [
            _rollout(CanaryState.CANARY, rollout_id="a", minutes_ago=25),
            _rollout(CanaryState.CANARY, rollout_id="b", minutes_ago=1),
        ]
        now = utc_now()
        watchdog, _ = _watchdog_with_service(rollouts)

        prefetched = watchdog.detect_stalled_rollouts(rollouts, now)
        self_fetched = watchdog.detect_stalled_rollouts(None, now)

        assert [z.rollout_id for z in prefetched] == [
            z.rollout_id for z in self_fetched
        ]
        assert [z.rollout_id for z in prefetched] == ["a"]
