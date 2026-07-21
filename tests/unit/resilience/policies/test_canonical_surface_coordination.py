"""Outbound 429 coordination reaches every canonical synchronous retry surface.

Target: the five surfaces that compose a ``RetryPolicy`` from settings —
``protect(retry=True)``, ``@dlq_protect``, ``standard_pipeline``, ``ha_pipeline``
and the synchronous ``@retry`` branch.

The policy-level wiring is covered in ``tests/unit/services/
test_retry_coordination_wiring.py``. What this file adds is per-surface reach:
each of these builds its own ``RetryPolicyConfig`` internally, so a surface can
lose coordination on its own — by pinning a config the resolution declines, or by
routing around ``RetryPolicy.execute`` entirely — without any policy-level test
noticing.

The five split on identity. ``protect`` and ``@dlq_protect`` take ``name`` as a
required positional, so they always carry a caller-chosen domain. The other three
default ``domain`` to the shared placeholder, and coordinating on that would merge
unrelated downstreams into one cooldown record. Every positive case below is
therefore **paired** with its placeholder negative: a wiring test on those three
that forgets to pass ``domain`` passes vacuously, asserting "no coordinator" when
it meant to assert the opposite.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.decorators.dlq_protect import dlq_protect
from baldur.protect_facade import protect, reset_protect_caches
from baldur.resilience.policies.async_retry import retry
from baldur.resilience.policies.presets import ha_pipeline, standard_pipeline
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitResult

_IDENTIFIED_DOMAIN = "payments.charge"


@pytest.fixture
def singleton_coordinator():
    """Stand a spec'd mock in for the process-wide coordinator singleton.

    ``reset_protect_caches()`` runs on both sides because the facade caches the
    composer that embeds the ``RetryPolicy``: a composer built by an earlier test
    would otherwise decide this one's outcome. The cache is not the reason
    resolution is use-time, but it is the reason a stale one would go unnoticed.
    """
    coordinator = MagicMock(spec=RateLimitCoordinator)
    # A real result object: an auto-generated ``.deferred`` is truthy and would
    # defer every call before it ran.
    coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
    reset_protect_caches()
    with patch.object(
        RateLimitCoordinator,
        "get_instance",
        autospec=True,
        return_value=coordinator,
    ) as get_instance:
        yield coordinator, get_instance
    reset_protect_caches()


def _waited_keys(coordinator) -> list[str]:
    """Coordination keys the surface actually consulted a cooldown for."""
    return [call.args[0] for call in coordinator.wait_if_needed.call_args_list]


# =============================================================================
# Per-surface default-on coordination
# =============================================================================


class TestCanonicalSurfaceCoordinationBehavior:
    """Each canonical sync surface coordinates 429s once it carries an identity."""

    # --- protect() / @dlq_protect: identified by construction ------------

    def test_protect_with_a_retry_stage_coordinates_on_its_name(
        self, singleton_coordinator
    ):
        """``name`` is a required positional, so this surface is always identified."""
        coordinator, get_instance = singleton_coordinator

        result = protect(_IDENTIFIED_DOMAIN, lambda: "ok", retry=True)

        assert result == "ok"
        get_instance.assert_called()
        assert _waited_keys(coordinator) == [_IDENTIFIED_DOMAIN]

    def test_protect_without_a_retry_stage_coordinates_nothing(
        self, singleton_coordinator
    ):
        """The paired negative: no retry stage, no coordination.

        ``ProtectSettings.default_retry`` is False, so a bare ``protect()`` call
        composes no retry stage at all — the wiring lives on the stage, which is
        what keeps the bare-facade overhead baselines unaffected by this change.
        """
        _coordinator, get_instance = singleton_coordinator

        result = protect(_IDENTIFIED_DOMAIN, lambda: "ok", retry=False)

        assert result == "ok"
        get_instance.assert_not_called()

    def test_dlq_protect_coordinates_on_its_name(self, singleton_coordinator):
        """``@dlq_protect`` pins ``retry=True``, so every decorated call coordinates."""
        coordinator, get_instance = singleton_coordinator

        @dlq_protect(_IDENTIFIED_DOMAIN)
        def charge():
            return "ok"

        assert charge() == "ok"
        get_instance.assert_called()
        assert _waited_keys(coordinator) == [_IDENTIFIED_DOMAIN]

    # --- @retry: domain is its only in-signature identity ----------------

    def test_retry_decorator_with_a_domain_coordinates(self, singleton_coordinator):
        """``domain`` is already this decorator's retry / DLQ / metric key.

        It is also the only remedy the signature exposes — ``@retry`` accepts
        neither a ``rate_limit_key`` nor a coordinator, by design.
        """
        coordinator, get_instance = singleton_coordinator

        @retry(domain=_IDENTIFIED_DOMAIN, max_attempts=1)
        def call_provider():
            return "ok"

        assert call_provider() == "ok"
        get_instance.assert_called()
        assert _waited_keys(coordinator) == [_IDENTIFIED_DOMAIN]

    def test_retry_decorator_without_a_domain_does_not_coordinate(
        self, singleton_coordinator
    ):
        """Paired negative: the unnamed caller shares the placeholder with everyone."""
        _coordinator, get_instance = singleton_coordinator

        @retry(max_attempts=1)
        def call_provider():
            return "ok"

        assert call_provider() == "ok"
        get_instance.assert_not_called()

    # --- the pipeline presets --------------------------------------------

    @pytest.mark.parametrize(
        "preset", ["standard", "ha"], ids=["standard_pipeline", "ha_pipeline"]
    )
    def test_preset_with_a_domain_coordinates(self, singleton_coordinator, preset):
        """Both presets default ``domain``, and both take it as the identity remedy."""
        coordinator, get_instance = singleton_coordinator
        pipeline = _build_preset(preset, domain=_IDENTIFIED_DOMAIN)

        result = pipeline.execute(lambda: "ok")

        assert result.value == "ok"
        get_instance.assert_called()
        assert _waited_keys(coordinator) == [_IDENTIFIED_DOMAIN]

    @pytest.mark.parametrize(
        "preset", ["standard", "ha"], ids=["standard_pipeline", "ha_pipeline"]
    )
    def test_preset_without_a_domain_does_not_coordinate(
        self, singleton_coordinator, preset
    ):
        """Paired negative — and the case most existing preset tests fall into.

        Nearly every pre-existing preset test omits ``domain``, so they all sit
        in this branch. Without the paired positive above, "no coordinator" would
        be indistinguishable from "the wiring is broken".
        """
        _coordinator, get_instance = singleton_coordinator
        pipeline = _build_preset(preset, domain=None)

        result = pipeline.execute(lambda: "ok")

        assert result.value == "ok"
        get_instance.assert_not_called()

    def test_preset_retry_policy_route_carries_an_explicit_key(
        self, singleton_coordinator
    ):
        """The presets' second escape: a pre-built policy owning the whole config.

        ``retry_policy=`` is mutually exclusive with ``domain=``/``max_retries=``
        (both raise ``ValueError`` alongside it), so this route means the caller
        owns the key choice — here via ``rate_limit_key`` on an otherwise
        placeholder-domain config.
        """
        from baldur.services.retry_handler.models import RetryPolicyConfig
        from baldur.services.retry_handler.policy import RetryPolicy

        coordinator, get_instance = singleton_coordinator
        pipeline = standard_pipeline(
            "svc",
            retry_policy=RetryPolicy(
                config=RetryPolicyConfig(max_attempts=1, rate_limit_key="stripe-api"),
                sleeper=lambda _: None,
            ),
        )

        result = pipeline.execute(lambda: "ok")

        assert result.value == "ok"
        get_instance.assert_called()
        assert _waited_keys(coordinator) == ["stripe-api"]


def _build_preset(preset: str, *, domain: str | None):
    """Build a preset with or without an explicit domain, retries pinned to one.

    ``max_retries=1`` keeps a failing case from fanning the assertion out over
    several attempts; the identity question is decided before the first one.
    """
    kwargs = {"max_retries": 1}
    if domain is not None:
        kwargs["domain"] = domain
    if preset == "standard":
        return standard_pipeline("svc", **kwargs)
    # ha_pipeline is fail-closed PRO-absent (its Hedging stage is PRO-tier), so
    # it raises at construction before any coordination decision is reachable.
    # The surface is still worth covering where it can be built.
    pytest.importorskip("baldur_pro")
    return ha_pipeline("svc", candidates=[], **kwargs)
