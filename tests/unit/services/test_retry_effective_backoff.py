"""The settings -> policy -> sleep chain for retry backoff.

Target: ``services/retry_handler/models.py``

This chain had no gate before: every other retry test constructs
``RetryPolicyConfig`` directly with explicit values, so the resolution that
turns ``BALDUR_RETRY_BASE_DELAY`` into an actual sleep was never exercised. A
4x backoff inflation shipped in a release under that blind spot -- the
PRO-absent branch read a legacy field documented as an *exponent* base and
passed it as a *first delay in seconds*, while the PRO branch read the real
field. Only a wall-clock scenario assertion caught it.

What is pinned here:

- the effective sleep ladder a settings tree produces, as bands (jitter is
  real, so exact values are not assertable);
- both resolution branches bottoming out in the same operator-facing fields,
  so moving between tiers cannot change retry timing;
- the per-domain base overlay, its two accepted spellings, their precedence,
  and its fail-open;
- ``build_backoff()``'s purity and its strategy dispatch, including the
  per-execution rebuild the one stateful strategy needs.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.backoff import (
    ConstantBackoff,
    DecorrelatedJitterBackoff,
    ExponentialBackoff,
    LinearBackoff,
)
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

#: Emitted when a per-domain override carries a value the base cannot be
#: resolved from.
COERCION_FAILED_EVENT = "retry.domain_override_coercion_failed"

#: Emitted when ``build_backoff()`` cannot honor the configured strategy name.
STRATEGY_FAILED_EVENT = "retry.backoff_strategy_resolution_failed"

#: Every field ``build_backoff()`` reads. Tier parity is asserted over exactly
#: this tuple: a future strategy parameter sourced at build time instead of at
#: resolution time would be invisible to a narrower one.
_LADDER_FIELDS = (
    "backoff_base",
    "backoff_max",
    "jitter_percent",
    "backoff_multiplier",
    "backoff_increment",
    "backoff_strategy",
)


def _settings_tree(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    max_elapsed: float | None = None,
    backoff_strategy: str = "exponential",
    jitter_factor: float = 0.2,
    multiplier: float = 2.0,
    linear_increment: float = 1.0,
    domain_configs: dict | None = None,
    **backoff_extra,
) -> SimpleNamespace:
    """A settings tree shaped exactly as the static branch reads it.

    A plain namespace rather than a mock: the branch reads attributes off four
    sub-trees and a spec-less mock would answer for any of them, including
    fields that no longer exist -- which is the failure this file exists to
    catch. ``backoff_extra`` exists so a test can *add* a field the production
    code must not read.
    """
    return SimpleNamespace(
        core=SimpleNamespace(
            retry=SimpleNamespace(
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                max_elapsed=max_elapsed,
                backoff_strategy=backoff_strategy,
            ),
            backoff=SimpleNamespace(
                exponential_jitter_factor=jitter_factor,
                exponential_multiplier=multiplier,
                linear_increment=linear_increment,
                **backoff_extra,
            ),
        ),
        services_group=SimpleNamespace(dlq=SimpleNamespace(enabled=True)),
        domain_configs={} if domain_configs is None else domain_configs,
    )


@contextmanager
def _static_branch(tree: SimpleNamespace):
    """Force the PRO-absent resolution branch to read ``tree``."""
    with patch(
        "baldur.factory.registry.ProviderRegistry.runtime_config_manager"
    ) as manager_slot:
        manager_slot.safe_get.return_value = None
        with patch(
            "baldur.services.retry_handler.models.get_config", return_value=tree
        ):
            yield


@contextmanager
def _runtime_branch(tree: SimpleNamespace, retry_config: dict):
    """Force the PRO runtime-store branch, with ``tree`` behind the shape dials.

    The runtime store carries only the ``RetrySettings`` family, so this branch
    still falls through to ``BackoffSettings`` for multiplier, jitter width and
    linear increment -- which is what makes the two branches comparable at all.
    """
    manager = SimpleNamespace(
        get_retry_config=lambda: retry_config,
        get_dlq_config=lambda: {"enabled": True},
    )
    with patch(
        "baldur.factory.registry.ProviderRegistry.runtime_config_manager"
    ) as manager_slot:
        manager_slot.safe_get.return_value = manager
        with patch(
            "baldur.services.retry_handler.models.get_config", return_value=tree
        ):
            yield


def _resolve_static(domain: str = "default", **tree_kwargs) -> RetryPolicyConfig:
    """Resolve one config off a static settings tree built from ``tree_kwargs``."""
    with _static_branch(_settings_tree(**tree_kwargs)):
        return RetryPolicyConfig.from_settings(domain)


def _record_ladder(config: RetryPolicyConfig) -> list[float]:
    """Run one always-failing execution and return the sleeps it asked for.

    The injected sleeper is the whole point: it makes the effective ladder
    observable without spending its wall-clock. ``domain`` stays the
    placeholder so no rate-limit coordinator is resolved into the loop.
    """
    sleeps: list[float] = []
    policy = RetryPolicy(config=config, sleeper=sleeps.append)
    policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))
    return sleeps


def _assert_within_jitter_band(
    sleeps: list[float], centers: list[float], jitter_percent: float
) -> None:
    """Assert each sleep sits inside the band its center and jitter width imply."""
    assert len(sleeps) == len(centers)
    width = jitter_percent / 100.0
    for index, (actual, center) in enumerate(zip(sleeps, centers, strict=True)):
        low, high = center * (1 - width), center * (1 + width)
        assert low <= actual <= high, (
            f"sleep {index + 1} was {actual}, outside [{low}, {high}]"
        )


def _ladder_of(config: RetryPolicyConfig) -> tuple:
    """Project a config onto every field the backoff builder reads."""
    return tuple(getattr(config, name) for name in _LADDER_FIELDS)


async def _always_fails_async():
    raise ConnectionError("fail")


# =============================================================================
# Behavior -- the effective sleep ladder
# =============================================================================


class TestEffectiveRetryDelayBehavior:
    """What an operator's settings actually cost in between-attempt sleep."""

    def test_default_settings_produce_the_ladder_the_config_describes(self):
        """Three sleeps follow base * multiplier ** n, within the jitter width.

        ``max_attempts=4`` rather than the default 3: the loop sleeps *between*
        attempts, so the shipped default produces only two sleeps and a
        three-element ladder assertion would silently test a two-element one.
        """
        # Given -- the shipped defaults, one extra attempt
        config = _resolve_static(max_attempts=4)

        # When
        sleeps = _record_ladder(config)

        # Then -- centers computed from the resolved config, not from literals
        centers = [
            config.backoff_base * config.backoff_multiplier**step for step in range(3)
        ]
        _assert_within_jitter_band(sleeps, centers, config.jitter_percent)

    def test_configured_base_delay_is_the_first_wait(self):
        """The operator-facing base delay lands on the wire, not a substitute.

        This is the regression itself: a legacy exponent-base field used to be
        substituted here, so the first wait was ~4x what the operator set. The
        band is deliberately narrow enough that the old value cannot pass.
        """
        base_delay = 2.5
        config = _resolve_static(max_attempts=2, base_delay=base_delay)

        sleeps = _record_ladder(config)

        assert config.backoff_base == base_delay
        _assert_within_jitter_band(sleeps, [base_delay], config.jitter_percent)

    def test_multiplier_override_moves_the_whole_ladder(self):
        """BALDUR_BACKOFF_EXPONENTIAL_MULTIPLIER reaches the retry ladder.

        Both construction sites used to take ``ExponentialBackoff``'s own
        default of 2.0, so the knob was inert with a coincidentally-matching
        value -- the same shape as the base-delay defect one field over.
        """
        config = _resolve_static(max_attempts=4, multiplier=3.0)

        sleeps = _record_ladder(config)

        assert config.backoff_multiplier == 3.0
        _assert_within_jitter_band(sleeps, [1.0, 3.0, 9.0], config.jitter_percent)

    @pytest.mark.parametrize(
        ("strategy", "centers"),
        [
            ("exponential", [1.0, 2.0, 4.0]),
            ("linear", [1.0, 1.5, 2.0]),
            ("constant", [1.0, 1.0, 1.0]),
        ],
        ids=["exponential", "linear", "constant"],
    )
    def test_configured_strategy_shapes_the_sleep_ladder(self, strategy, centers):
        """Each strategy name produces its own curve off one settings tree.

        ``linear_increment`` is moved off its default on purpose: at all-default
        values linear and exponential agree on the first two sleeps, so a
        dispatch that silently ignored the strategy name would still pass.
        """
        config = _resolve_static(
            max_attempts=4, backoff_strategy=strategy, linear_increment=0.5
        )

        sleeps = _record_ladder(config)

        assert config.backoff_strategy == strategy
        _assert_within_jitter_band(sleeps, centers, config.jitter_percent)

    def test_linear_third_sleep_is_disjoint_from_the_exponential_one(self):
        """The four advertised strategies are distinguishable, not just named.

        Band membership alone leaves overlap between neighboring curves; the
        third sleep is where linear and exponential separate completely, so it
        is the step that proves dispatch actually happened.
        """
        linear = _resolve_static(
            max_attempts=4, backoff_strategy="linear", linear_increment=0.5
        )
        exponential = _resolve_static(max_attempts=4, backoff_strategy="exponential")

        linear_sleeps = _record_ladder(linear)
        exponential_sleeps = _record_ladder(exponential)

        assert linear_sleeps[2] < exponential_sleeps[2]

    def test_decorrelated_strategy_stays_between_base_and_max_delay(self):
        """Decorrelated jitter is random by definition, so only its bounds hold.

        It is also the one strategy with no lower clamp at zero -- every delay
        is drawn at or above ``base_delay`` -- which is why the non-positive
        base guard elsewhere in this file is load-bearing.
        """
        config = _resolve_static(
            max_attempts=4, backoff_strategy="decorrelated_jitter", max_delay=30.0
        )

        sleeps = _record_ladder(config)

        assert len(sleeps) == 3
        assert all(
            config.backoff_base <= sleep <= config.backoff_max for sleep in sleeps
        )


# =============================================================================
# Behavior -- tier parity between the two resolution branches
# =============================================================================


class TestRetryConfigTierParityBehavior:
    """The PRO and PRO-absent branches resolve the same ladder.

    Scoped to the no-override case on purpose: the runtime-store branch never
    applies per-domain overlays, so parity is an env-level guarantee. Without
    it, the same ``BALDUR_RETRY_BASE_DELAY`` meant two different things
    depending on entitlement, and an upgrade silently changed retry timing.
    """

    def _runtime_dict(self, tree: SimpleNamespace) -> dict:
        """The runtime store's retry family, sourced from the same settings tree."""
        retry = tree.core.retry
        return {
            "max_attempts": retry.max_attempts,
            "base_delay": retry.base_delay,
            "max_delay": retry.max_delay,
            "max_elapsed": retry.max_elapsed,
            "backoff_strategy": retry.backoff_strategy,
        }

    def test_both_branches_resolve_an_identical_ladder(self):
        """Every field the backoff builder reads matches across the two branches."""
        # Given -- one settings tree, projected into both resolution shapes
        tree = _settings_tree(base_delay=1.5, max_delay=45.0, multiplier=2.5)

        # When
        with _static_branch(tree):
            static = RetryPolicyConfig.from_settings("payment")
        with _runtime_branch(tree, self._runtime_dict(tree)):
            runtime = RetryPolicyConfig.from_settings("payment")

        # Then
        assert _ladder_of(static) == _ladder_of(runtime)

    def test_the_shared_ladder_is_the_operator_facing_one(self):
        """Parity is not parity on a wrong value: both land on ``base_delay``."""
        tree = _settings_tree(base_delay=1.5)

        with _static_branch(tree):
            static = RetryPolicyConfig.from_settings("payment")
        with _runtime_branch(tree, self._runtime_dict(tree)):
            runtime = RetryPolicyConfig.from_settings("payment")

        assert static.backoff_base == tree.core.retry.base_delay
        assert runtime.backoff_base == tree.core.retry.base_delay

    def test_each_branch_labels_which_one_resolved(self):
        """``config_source`` records the branch that ran, not the registry state.

        A registered manager that raises mid-resolution falls through to the
        static branch, so a label re-derived from the registry would call
        static values "runtime_config" -- and the whole point of the label is
        telling an operator which numbers they are looking at.
        """
        tree = _settings_tree()

        with _static_branch(tree):
            static = RetryPolicyConfig.from_settings("payment")
        with _runtime_branch(tree, self._runtime_dict(tree)):
            runtime = RetryPolicyConfig.from_settings("payment")

        assert static.config_source == "static"
        assert runtime.config_source == "runtime_config"

    def test_a_manager_that_raises_falls_through_and_says_so(self):
        """The silent fallback stays silent behaviorally, but not in the label."""
        tree = _settings_tree()
        manager = SimpleNamespace(
            get_retry_config=lambda: (_ for _ in ()).throw(RuntimeError("store down")),
            get_dlq_config=lambda: {"enabled": True},
        )

        with patch(
            "baldur.factory.registry.ProviderRegistry.runtime_config_manager"
        ) as manager_slot:
            manager_slot.safe_get.return_value = manager
            with patch(
                "baldur.services.retry_handler.models.get_config", return_value=tree
            ):
                config = RetryPolicyConfig.from_settings("payment")

        assert config.config_source == "static"
        assert config.backoff_base == tree.core.retry.base_delay

    def test_config_source_is_excluded_from_equality(self):
        """Two configs differing only by branch label still compare equal.

        The field was added for observability; letting it into ``__eq__`` would
        silently change every existing config comparison.
        """
        assert RetryPolicyConfig(config_source="static") == RetryPolicyConfig(
            config_source="runtime_config"
        )


# =============================================================================
# Behavior -- the static branch's sourcing, stated negatively
# =============================================================================


class TestStaticBranchSourcingBehavior:
    """Which settings field the static branch reads -- and which it must not."""

    def test_a_legacy_base_field_on_the_tree_is_ignored(self):
        """An unrelated field named like the old one changes nothing.

        The legacy trio is deleted from ``BackoffSettings``, so this fixture
        carries a field production code no longer has. That is the assertion:
        re-introducing the read would resolve 999 instead of the operator's
        value, which is precisely the shape of the shipped defect.
        """
        config = _resolve_static(base_delay=1.5, legacy_base=999)

        assert config.backoff_base == 1.5

    def test_jitter_percent_is_the_backoff_factor_scaled_to_a_percent(self):
        """The 0..1 factor becomes a percent -- dropping the scale is invisible.

        ``jitter = jitter_percent > 0`` keeps looking healthy either way, so a
        missing ``* 100`` would silently collapse the width from 20% to 0.2%
        with every flag still reporting jitter as on.
        """
        config = _resolve_static(jitter_factor=0.35)

        assert config.jitter_percent == pytest.approx(35.0)

    def test_max_delay_and_attempts_come_from_the_retry_family(self):
        """The rest of the ladder's bounds are sourced where they are documented."""
        config = _resolve_static(max_attempts=7, max_delay=99.0)

        assert config.max_attempts == 7
        assert config.backoff_max == 99.0


# =============================================================================
# Behavior -- the per-domain base overlay
# =============================================================================


class TestDomainBackoffBaseBehavior:
    """Resolving one domain's first-retry delay off an unvalidated overlay."""

    def _resolve(self, retry_overlay: dict, **tree_kwargs) -> RetryPolicyConfig:
        return _resolve_static(
            "payment",
            domain_configs={"payment": {"retry": retry_overlay}},
            **tree_kwargs,
        )

    @pytest.mark.parametrize(
        ("overlay", "expected"),
        [
            ({"backoff_base": 3.0}, 3.0),
            ({"base_delay": 2.0}, 2.0),
            ({"backoff_base": 3.0, "base_delay": 2.0}, 3.0),
            ({}, 1.0),
        ],
        ids=["backoff_base_only", "base_delay_only", "both_keys", "neither_key"],
    )
    def test_either_spelling_resolves_with_backoff_base_taking_precedence(
        self, overlay, expected
    ):
        """Both keys mean the same quantity; the order between them is pinned.

        ``base_delay`` is the spelling the validated settings-side merge route
        uses, ``backoff_base`` the one the resolved config uses -- an operator
        writing either into an overlay means the same thing. The both-keys case
        exists so a later editor cannot flip the precedence unnoticed.
        """
        config = self._resolve(overlay, base_delay=1.0)

        assert config.backoff_base == expected

    def test_a_domain_without_an_overlay_keeps_the_settings_value(self):
        """An unrelated domain's overlay does not leak into this one."""
        config = _resolve_static(
            "checkout",
            base_delay=1.0,
            domain_configs={"payment": {"retry": {"base_delay": 9.0}}},
        )

        assert config.backoff_base == 1.0

    @pytest.mark.parametrize(
        "bad_value",
        ["fast", None, 0, -1, [2.0]],
        ids=["non_numeric_string", "none", "zero", "negative", "list"],
    )
    def test_an_unusable_override_falls_open_to_the_settings_value(self, bad_value):
        """A config typo degrades to the default instead of reaching the loop.

        ``domain_configs`` is an unvalidated mapping, so an uncoercible value
        would otherwise surface as a TypeError *inside* a business call,
        replacing its outcome. A non-positive value is rejected for a different
        reason: the retry loop skips any sleep at or below zero, so the ladder
        would degenerate into a hot loop against the failing upstream.
        """
        with capture_logs() as logs:
            config = self._resolve({"backoff_base": bad_value}, base_delay=1.0)

        assert config.backoff_base == 1.0
        warnings = [e for e in logs if e.get("event") == COERCION_FAILED_EVENT]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["domain"] == "payment"
        assert warnings[0]["key"] == "backoff_base"

    def test_a_numeric_string_override_is_coerced_rather_than_rejected(self):
        """JSON-sourced config routinely carries numbers as strings."""
        with capture_logs() as logs:
            config = self._resolve({"base_delay": "1.5"})

        assert config.backoff_base == 1.5
        assert not [e for e in logs if e.get("event") == COERCION_FAILED_EVENT]

    def test_an_unusable_first_key_does_not_fall_through_to_the_second(self):
        """Precedence is decided by presence, not by usability.

        A present-but-broken ``backoff_base`` resolves to the settings value
        even when a perfectly good ``base_delay`` sits beside it. Pinned
        because the alternative is defensible and silent: an editor "fixing"
        this would change which value a live deployment retries on.
        """
        config = self._resolve(
            {"backoff_base": "oops", "base_delay": 2.0}, base_delay=1.0
        )

        assert config.backoff_base == 1.0

    def test_a_valid_override_logs_nothing(self):
        """The warning is reserved for values that were actually rejected."""
        with capture_logs() as logs:
            self._resolve({"backoff_base": 3.0})

        assert not [e for e in logs if e.get("event") == COERCION_FAILED_EVENT]


# =============================================================================
# Behavior -- build_backoff()
# =============================================================================


class TestBuildBackoffBehavior:
    """Turning a resolved config into the strategy the retry loop sleeps on."""

    def test_the_builder_reads_no_settings(self):
        """Every parameter is resolved before the builder runs.

        Asserted rather than left to convention: a strategy parameter sourced
        at build time would escape both the tier-parity tuple and the startup
        report, so the config would stop describing the ladder it produces.
        """
        config = _resolve_static(max_attempts=4, multiplier=2.5)

        with patch(
            "baldur.services.retry_handler.models.get_config",
            side_effect=AssertionError("build_backoff read the settings tree"),
        ):
            strategy = config.build_backoff()

        assert isinstance(strategy, ExponentialBackoff)
        assert strategy.multiplier == 2.5

    @pytest.mark.parametrize(
        ("strategy_name", "expected_type"),
        [
            ("exponential", ExponentialBackoff),
            ("linear", LinearBackoff),
            ("constant", ConstantBackoff),
            ("decorrelated_jitter", DecorrelatedJitterBackoff),
        ],
        ids=["exponential", "linear", "constant", "decorrelated_jitter"],
    )
    def test_each_validated_strategy_name_builds_its_own_class(
        self, strategy_name, expected_type
    ):
        """The settings vocabulary maps one-to-one onto the strategy classes."""
        config = RetryPolicyConfig(backoff_strategy=strategy_name)

        assert isinstance(config.build_backoff(), expected_type)

    def test_exponential_receives_every_resolved_parameter(self):
        """Base, cap, multiplier and jitter width all cross into the strategy."""
        config = RetryPolicyConfig(
            backoff_base=2.0,
            backoff_max=45.0,
            backoff_multiplier=3.0,
            jitter_percent=25.0,
        )

        strategy = config.build_backoff()

        assert strategy.base_delay == 2.0
        assert strategy.max_delay == 45.0
        assert strategy.multiplier == 3.0
        assert strategy.jitter_factor == pytest.approx(0.25)

    def test_linear_receives_the_resolved_increment(self):
        """The linear increment comes off the config, not off settings."""
        config = RetryPolicyConfig(
            backoff_strategy="linear",
            backoff_base=2.0,
            backoff_increment=0.5,
            backoff_max=45.0,
        )

        strategy = config.build_backoff()

        assert strategy.base_delay == 2.0
        assert strategy.increment == 0.5
        assert strategy.max_delay == 45.0

    def test_constant_is_built_with_the_configured_cap(self):
        """A settings-derived constant ladder honors ``max_delay``.

        Constant backoff is uncapped when built directly, so without this the
        one strategy whose delay never grows would be the one that could
        exceed the configured maximum.
        """
        config = RetryPolicyConfig(
            backoff_strategy="constant", backoff_base=30.0, backoff_max=10.0
        )

        strategy = config.build_backoff()

        assert strategy.delay == 30.0
        assert strategy.max_delay == 10.0
        assert all(strategy.calculate(attempt) <= 10.0 for attempt in range(1, 6))

    def test_decorrelated_receives_only_its_two_bounds(self):
        """Its randomization is its definition -- no jitter parameters apply."""
        config = RetryPolicyConfig(
            backoff_strategy="decorrelated_jitter", backoff_base=2.0, backoff_max=45.0
        )

        strategy = config.build_backoff()

        assert strategy.base_delay == 2.0
        assert strategy.max_delay == 45.0

    def test_an_unknown_strategy_falls_open_to_exponential_with_a_warning(self):
        """A config-shaped side input must never fail a business call.

        An unknown name is reachable only through the unvalidated domain
        overlay -- the settings validator rejects it everywhere else -- so the
        builder degrades instead of raising, and says so.
        """
        config = RetryPolicyConfig(backoff_strategy="fibonacci", domain="payment")

        with capture_logs() as logs:
            strategy = config.build_backoff()

        assert isinstance(strategy, ExponentialBackoff)
        warnings = [e for e in logs if e.get("event") == STRATEGY_FAILED_EVENT]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["strategy"] == "fibonacci"
        assert warnings[0]["fallback"] == "exponential"
        assert warnings[0]["domain"] == "payment"

    @pytest.mark.parametrize(
        "strategy_name",
        ["exponential", "linear", "constant"],
        ids=["exponential", "linear", "constant"],
    )
    def test_jitter_false_builds_the_jitterless_skeleton(self, strategy_name):
        """The deterministic form of the same ladder, for callers that need it."""
        config = RetryPolicyConfig(backoff_strategy=strategy_name, jitter_percent=20.0)

        strategy = config.build_backoff(jitter=False)

        assert strategy.jitter is False

    def test_zero_jitter_percent_disables_jitter_on_the_flagged_strategies(self):
        """Width and flag agree: a zero width is not jitter that draws nothing."""
        config = RetryPolicyConfig(backoff_strategy="linear", jitter_percent=0.0)

        assert config.build_backoff().jitter is False


# =============================================================================
# Behavior -- per-execution freshness of the stateful strategy
# =============================================================================


class TestBackoffFreshnessBehavior:
    """Who owns the strategy instance: the policy, or each execution.

    ``protect()``'s composer cache hands one policy object to every caller of a
    name, so a shared decorrelated instance would let two concurrent ladders
    consume each other's running previous delay. The stateless strategies have
    no such state and keep one instance.
    """

    @contextmanager
    def _counting_builder(self):
        """Count ``build_backoff`` calls while still returning real strategies."""
        original = RetryPolicyConfig.build_backoff
        with patch.object(
            RetryPolicyConfig, "build_backoff", autospec=True, side_effect=original
        ) as builder:
            yield builder

    def test_a_stateless_policy_builds_its_strategy_once(self):
        """Construction builds it; executions reuse it."""
        config = _resolve_static(max_attempts=3)

        with self._counting_builder() as builder:
            policy = RetryPolicy(config=config, sleeper=lambda _: None)
            assert builder.call_count == 1
            policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))
            policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))

        assert builder.call_count == 1
        assert policy._backoff is not None

    def test_a_stateful_policy_builds_a_strategy_per_execution(self):
        """Nothing is built at construction; each ladder gets its own."""
        config = _resolve_static(max_attempts=3, backoff_strategy="decorrelated_jitter")

        with self._counting_builder() as builder:
            policy = RetryPolicy(config=config, sleeper=lambda _: None)
            assert builder.call_count == 0
            policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))
            policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))

        assert builder.call_count == 2
        assert policy._backoff is None

    def test_an_injected_strategy_wins_over_the_stateful_rebuild(self):
        """A caller who built a strategy asked for it -- on every strategy name."""
        config = _resolve_static(max_attempts=3, backoff_strategy="decorrelated_jitter")
        injected = ConstantBackoff(delay=0.0)

        with self._counting_builder() as builder:
            policy = RetryPolicy(
                config=config, backoff=injected, sleeper=lambda _: None
            )
            policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))

        assert builder.call_count == 0
        assert policy._backoff is injected

    def test_two_stateful_ladders_start_from_the_base_delay(self):
        """The observable consequence: one ladder cannot inherit another's state.

        A shared instance carries ``_previous_delay`` forward, so the second
        execution's first sleep would be drawn against the first execution's
        last delay instead of resetting to the base.
        """
        config = _resolve_static(
            max_attempts=4, backoff_strategy="decorrelated_jitter", base_delay=1.0
        )

        first = _record_ladder(config)
        second = _record_ladder(config)

        assert first[0] == config.backoff_base
        assert second[0] == config.backoff_base

    @pytest.mark.asyncio
    async def test_the_async_policy_rebuilds_the_stateful_strategy_too(self):
        """The two stages consume one builder, so they cannot drift apart."""
        config = _resolve_static(max_attempts=3, backoff_strategy="decorrelated_jitter")

        with self._counting_builder() as builder:
            policy = AsyncRetryPolicy.from_policy_config(config)
            assert builder.call_count == 0
            with patch(
                "baldur.resilience.policies.async_retry.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                await policy.execute(_always_fails_async)
                await policy.execute(_always_fails_async)

        assert builder.call_count == 2
        assert policy._backoff is None

    @pytest.mark.asyncio
    async def test_the_async_policy_keeps_one_stateless_strategy(self):
        """Only the stateful name pays the per-execution rebuild."""
        config = _resolve_static(max_attempts=3)

        with self._counting_builder() as builder:
            policy = AsyncRetryPolicy.from_policy_config(config)
            assert builder.call_count == 1
            with patch(
                "baldur.resilience.policies.async_retry.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                await policy.execute(_always_fails_async)

        assert builder.call_count == 1
        assert policy._backoff is not None
