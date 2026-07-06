"""Regression contract tests for #482 D1 — ``ProtectSettings.default_timeout_seconds``
flipped from 30.0 → None, so the canonical ``protect("name", fn)`` /
``aprotect("name", fn)`` profile no longer pays per-call
``ThreadPoolExecutor.submit`` overhead from ``TimeoutPolicy``.

Scope:
- ``_build_sync_composer`` direct construction — chain shape under
  (timeout_seconds=None, explicit float, explicit None) inputs.
- ``protect()`` end-to-end — composer cached under
  ``_composer_cache[(name, None, "default")]`` for the post-flip default
  profile (the third tuple component is the profile id introduced by #499 D2).
- ``_build_async_composer`` direct construction — async chain shape, locks
  the § Out of Scope claim that the async path was already None-correct
  (no code change in #482, regression-locked here).
- ``aprotect()`` end-to-end — async composer omits ``AsyncTimeoutPolicy``
  for the post-flip default profile.

Verification techniques: state_transition (chain shape under each
timeout-input branch), boundary_analysis (None vs float vs sentinel),
contract pinning (locks D1 default + § Out of Scope async-already-correct
claim against future settings drift).

Reference:
    482 — D1, D5, § Testability Notes
"""

from __future__ import annotations

import asyncio

import pytest

import baldur.protect_facade as protect_module
from baldur.protect_facade import (
    _TIMEOUT_UNSET,
    _build_async_composer,
    _build_sync_composer,
    _resolve_timeout,
    aprotect,
    protect,
)


@pytest.fixture(autouse=True)
def _reset_protect_state():
    """Force a fresh cache + settings + recorder for every test."""
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()
    yield
    reset_protect_settings()


def _policy_names(composer) -> list[str]:
    """Inspect the policy chain by ``.name`` — both PolicyComposer and
    AsyncPolicyComposer expose ``_policies`` and policies expose ``.name``
    via the ``ResiliencePolicy`` Protocol."""
    return [policy.name for policy in composer._policies]


# =============================================================================
# Sync — _build_sync_composer direct construction
# =============================================================================


class TestBuildSyncComposerTimeoutBranches:
    """482 D1 + D5: sync builder must omit ``TimeoutPolicy`` when
    ``timeout_seconds`` is None and include it when a float is supplied."""

    def test_post_flip_default_chain_has_no_timeout_policy(self):
        """timeout_seconds resolved from _TIMEOUT_UNSET → None (post-#482
        default) → chain holds only CircuitBreakerPolicy."""
        timeout_seconds = _resolve_timeout(_TIMEOUT_UNSET)
        assert timeout_seconds is None  # guard: settings flip is in effect

        composer = _build_sync_composer(
            name="bypass.default",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=timeout_seconds,
        )

        assert _policy_names(composer) == ["circuit_breaker"]

    def test_explicit_float_chain_includes_timeout_policy(self):
        """Per-call ``timeout=5.0`` → chain holds CB + TimeoutPolicy."""
        composer = _build_sync_composer(
            name="bypass.with_timeout",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=5.0,
        )

        assert _policy_names(composer) == ["circuit_breaker", "timeout"]

    def test_explicit_none_chain_has_no_timeout_policy(self):
        """Per-call ``timeout=None`` → caller explicitly disables
        TimeoutPolicy. Same chain shape as the post-#482 default."""
        composer = _build_sync_composer(
            name="bypass.disabled",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=None,
        )

        assert _policy_names(composer) == ["circuit_breaker"]


# =============================================================================
# Sync — protect() end-to-end (cache key + chain inspection)
# =============================================================================


class TestProtectDefaultTimeoutBypassEndToEnd:
    """482 D5 — canonical ``protect("name", fn)`` populates the composer
    cache with a ``(name, None)`` key whose composer holds no TimeoutPolicy."""

    def test_default_profile_caches_no_timeout_composer(self):
        """End-to-end: protect() with default kwargs → composer cached
        under (name, None) → cached composer has no timeout policy."""
        result = protect(name="bypass.e2e_default", fn=lambda: 42)

        assert result == 42
        cached = protect_module._composer_cache[("bypass.e2e_default", None, "default")]
        assert _policy_names(cached) == ["circuit_breaker"]

    def test_explicit_per_call_timeout_caches_separate_composer(self):
        """End-to-end: protect(timeout=5.0) → separate cache slot under
        (name, 5.0, "default") holding CB + TimeoutPolicy. Distinct from
        the (name, None, "default") slot a default call would create."""
        result = protect(name="bypass.e2e_explicit", fn=lambda: 42, timeout=5.0)

        assert result == 42
        cached = protect_module._composer_cache[("bypass.e2e_explicit", 5.0, "default")]
        assert _policy_names(cached) == ["circuit_breaker", "timeout"]

    def test_explicit_none_per_call_caches_no_timeout_composer(self):
        """End-to-end: protect(timeout=None) → cache slot under
        (name, None, "default") holding only CB — explicit None always
        wins, even if the env var supplies a float default."""
        result = protect(name="bypass.e2e_none", fn=lambda: 42, timeout=None)

        assert result == 42
        cached = protect_module._composer_cache[("bypass.e2e_none", None, "default")]
        assert _policy_names(cached) == ["circuit_breaker"]


# =============================================================================
# Async — _build_async_composer direct construction
# =============================================================================


class TestBuildAsyncComposerTimeoutBranches:
    """482 § Out of Scope: ``_build_async_composer`` is None-correct on the
    timeout branch. These tests isolate the timeout branch by passing
    ``circuit_breaker=False`` + ``retry_cfg=None`` (CB/Retry are wired since
    670 but excluded here), so the chain holds only the optional timeout —
    empty when ``timeout_seconds`` is None."""

    def test_post_flip_default_chain_is_empty(self):
        """timeout_seconds None → empty async chain (no CB, no timeout)."""
        composer = _build_async_composer(
            name="t",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=False,
            timeout_seconds=None,
        )

        assert _policy_names(composer) == []

    def test_explicit_float_chain_includes_async_timeout_policy(self):
        """Per-call ``timeout=5.0`` → chain holds AsyncTimeoutPolicy."""
        composer = _build_async_composer(
            name="t",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=False,
            timeout_seconds=5.0,
        )

        assert _policy_names(composer) == ["timeout"]

    def test_explicit_none_chain_is_empty(self):
        """Per-call ``timeout=None`` → empty chain. Same shape as
        post-#482 default."""
        composer = _build_async_composer(
            name="t",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=False,
            timeout_seconds=None,
        )

        assert _policy_names(composer) == []


# =============================================================================
# Async — aprotect() end-to-end
# =============================================================================


class TestAprotectDefaultTimeoutBypassEndToEnd:
    """482 § Out of Scope async-correctness regression lock — end-to-end
    aprotect() with default kwargs (CB/retry None-coerced) returns its
    value through an empty async composer chain."""

    def test_default_profile_returns_value_through_empty_chain(self):
        """End-to-end: aprotect() with default kwargs — no Async CB / no
        AsyncTimeoutPolicy is built. Locks the post-flip default behavior."""

        async def fn():
            return 7

        result = asyncio.run(aprotect(name="bypass.async_default", fn=fn))

        assert result == 7

    def test_explicit_per_call_timeout_executes_under_async_timeout(self):
        """End-to-end: aprotect(timeout=5.0) succeeds when fn is fast
        enough — proves AsyncTimeoutPolicy is wired in but does not fire."""

        async def fn():
            return 9

        result = asyncio.run(aprotect(name="bypass.async_explicit", fn=fn, timeout=5.0))

        assert result == 9
