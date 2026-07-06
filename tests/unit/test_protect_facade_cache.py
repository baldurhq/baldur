"""Unit tests for ``baldur.protect_facade`` per-name CircuitBreakerPolicy cache (480 DEC-1/3).

Scope:
- Cache hit: same ``name`` → ``CircuitBreakerPolicy`` constructor invoked once
  across N ``protect()`` calls (subsumes G1/G2/G3 per-call work).
- Cache miss: distinct names → distinct cached instances.
- DCL race: concurrent first call from N threads → 1 instance returned to all,
  1 constructor invocation total (double-checked locking pattern from
  ``factory/base.py:127-148``).
- Reset chain: ``reset_protect_settings()`` invalidates the CB cache via lazy
  import of ``reset_protect_caches()``, which also forwards to
  ``reset_protect_recorder()``.
- ``reset_protect_caches()`` direct call clears both the CB cache and the
  recorder state.

Reference:
    480 — DEC-1, DEC-3
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

import baldur.protect_facade as protect_module
from baldur.protect_facade import protect, reset_protect_caches
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy


@pytest.fixture(autouse=True)
def _reset_protect_state():
    """Force a fresh cache + settings + recorder for every test."""
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()  # also clears CB cache + recorder via DEC-3 chain
    yield
    reset_protect_settings()


# =============================================================================
# Cache hit / miss — same name reuses, distinct names build distinct instances
# =============================================================================


class TestProtectCbPolicyCacheBehavior:
    """DEC-1 — protect() must construct ``CircuitBreakerPolicy`` at most once
    per ``name`` for the lifetime of the process (or until reset)."""

    def test_repeated_calls_with_same_name_invoke_cb_constructor_once(self):
        """Cache hit: 5 protect() calls on the same name → 1 CB construction."""
        from baldur.services.circuit_breaker import policy as cb_policy_module

        construct_count = 0
        original_init = CircuitBreakerPolicy.__init__

        def counting_init(self, *args, **kwargs):
            nonlocal construct_count
            construct_count += 1
            original_init(self, *args, **kwargs)

        with patch.object(
            cb_policy_module.CircuitBreakerPolicy, "__init__", counting_init
        ):
            for _ in range(5):
                protect(name="cache.same", fn=lambda: 1, circuit_breaker=True)

        assert construct_count == 1
        assert "cache.same" in protect_module._cb_policy_cache

    def test_distinct_names_produce_distinct_cached_instances(self):
        """Cache miss across names: each unique name gets its own CB Policy."""
        protect(name="cache.a", fn=lambda: 1, circuit_breaker=True)
        protect(name="cache.b", fn=lambda: 1, circuit_breaker=True)

        cache = protect_module._cb_policy_cache
        assert "cache.a" in cache
        assert "cache.b" in cache
        assert cache["cache.a"] is not cache["cache.b"]

    def test_circuit_breaker_false_does_not_populate_cache(self):
        """When circuit_breaker=False, _build_sync_composer skips the helper —
        the cache must remain empty for that name."""
        protect(name="cache.skip", fn=lambda: 1, circuit_breaker=False)

        assert "cache.skip" not in protect_module._cb_policy_cache

    def test_helper_returns_same_instance_on_repeat_call(self):
        """``_get_or_build_cb_policy`` is itself idempotent — the cache lookup
        returns the same object reference on every call after the first."""
        from baldur.protect_facade import _get_or_build_cb_policy

        first = _get_or_build_cb_policy("cache.idem")
        second = _get_or_build_cb_policy("cache.idem")

        assert first is second
        assert isinstance(first, CircuitBreakerPolicy)

    def test_default_profile_creates_none_keyed_composer_cache_entry(self):
        """482 D5 + 499 D2 — post-flip canonical ``protect("name", fn)``
        populates ``_composer_cache[("name", None, "default")]`` (the third
        tuple component is the profile id added by #499) and the second call
        hits the same composer instance. Locks the cache shape against future
        regressions that would re-introduce the dropped
        ``timeout_seconds is not None`` clause from ``_build_sync_composer``'s
        fast-path predicate, or that would drop the profile discriminator."""
        protect(name="cache.default_profile", fn=lambda: 1)

        assert (
            "cache.default_profile",
            None,
            "default",
        ) in protect_module._composer_cache
        first_composer = protect_module._composer_cache[
            ("cache.default_profile", None, "default")
        ]

        protect(name="cache.default_profile", fn=lambda: 1)

        second_composer = protect_module._composer_cache[
            ("cache.default_profile", None, "default")
        ]
        assert first_composer is second_composer


# =============================================================================
# 499 — @dlq_protect profile cache (PolicyComposer + RetryPolicy + DLQSink)
# =============================================================================


class TestDlqProtectComposerCacheBehavior:
    """499 D2+D4+D7 — the ``dlq_protect`` profile fast-path populates the
    composer cache under a distinct ``"dlq_protect"`` profile id so two
    profiles for the same name do not collide.
    """

    def test_dlq_protect_profile_creates_dedicated_cache_entry(self):
        """499 D2 — ``protect(..., dlq=True, retry=True, circuit_breaker=True)``
        populates ``_composer_cache[(name, None, "dlq_protect")]`` with a
        composer holding the canonical zero-message-loss chain shape.
        """
        protect(
            name="cache.dlq_profile",
            fn=lambda: 1,
            dlq=True,
            retry=True,
            circuit_breaker=True,
        )

        key = ("cache.dlq_profile", None, "dlq_protect")
        assert key in protect_module._composer_cache
        cached = protect_module._composer_cache[key]
        assert [p.name for p in cached._policies] == ["circuit_breaker", "retry"]

    def test_repeated_dlq_protect_calls_return_same_composer_via_is(self):
        """499 D7 — two ``@dlq_protect``-shaped calls share the same cached
        ``PolicyComposer`` instance."""
        protect(
            name="cache.dlq_identity",
            fn=lambda: 1,
            dlq=True,
            retry=True,
            circuit_breaker=True,
        )
        first = protect_module._composer_cache[
            ("cache.dlq_identity", None, "dlq_protect")
        ]

        protect(
            name="cache.dlq_identity",
            fn=lambda: 1,
            dlq=True,
            retry=True,
            circuit_breaker=True,
        )
        second = protect_module._composer_cache[
            ("cache.dlq_identity", None, "dlq_protect")
        ]

        assert first is second

    def test_dlq_protect_invokes_retry_policy_constructor_once(self):
        """499 D6/G2 — once the composer is cached, the embedded
        ``RetryPolicy`` reference is stable. N protect() calls → 1
        constructor invocation (mirror of the CB constructor-count test).
        """
        from baldur.services.retry_handler import policy as retry_policy_module
        from baldur.services.retry_handler.policy import RetryPolicy

        construct_count = 0
        original_init = RetryPolicy.__init__

        def counting_init(self, *args, **kwargs):
            nonlocal construct_count
            construct_count += 1
            original_init(self, *args, **kwargs)

        with patch.object(retry_policy_module.RetryPolicy, "__init__", counting_init):
            for _ in range(5):
                protect(
                    name="cache.dlq_retry_count",
                    fn=lambda: 1,
                    dlq=True,
                    retry=True,
                    circuit_breaker=True,
                )

        assert construct_count == 1

    def test_dlq_sink_identity_stable_across_calls(self):
        """499 D1 — the cached composer's DLQ sink is the module-level
        ``_DLQ_SINK`` singleton. Identity is observable across calls."""
        protect(
            name="cache.dlq_sink_id",
            fn=lambda: 1,
            dlq=True,
            retry=True,
            circuit_breaker=True,
        )
        cached = protect_module._composer_cache[
            ("cache.dlq_sink_id", None, "dlq_protect")
        ]

        assert cached._sinks[0] is protect_module._DLQ_SINK

    def test_reset_drains_dlq_protect_cache_entries(self):
        """499 D8 — ``reset_protect_caches()`` drains all profile entries
        (single dict clear)."""
        protect(
            name="cache.dlq_reset",
            fn=lambda: 1,
            dlq=True,
            retry=True,
            circuit_breaker=True,
        )
        assert (
            "cache.dlq_reset",
            None,
            "dlq_protect",
        ) in protect_module._composer_cache

        reset_protect_caches()

        assert protect_module._composer_cache == {}

    def test_explicit_retry_policy_config_does_not_populate_dlq_cache(self):
        """499 D4 — explicit ``RetryPolicyConfig`` callers stay on the slow
        path. The dlq_protect cache key must NOT be populated for them so
        ``@dlq_protect("X")`` and ``protect("X", retry=RetryPolicyConfig(...))``
        cannot collide on the same key."""
        from baldur.services.retry_handler.models import RetryPolicyConfig

        protect(
            name="cache.dlq_explicit",
            fn=lambda: 1,
            dlq=True,
            retry=RetryPolicyConfig(max_attempts=10, domain="cache.dlq_explicit"),
            circuit_breaker=True,
        )

        assert (
            "cache.dlq_explicit",
            None,
            "dlq_protect",
        ) not in protect_module._composer_cache


class TestDlqProtectComposerCacheConcurrency:
    """499 D7 — concurrent first-call on the dlq_protect helper produces
    exactly one composer instance via DCL. Mirror of
    ``TestProtectCbPolicyCacheConcurrency``."""

    def test_concurrent_first_call_yields_single_composer(self):
        from baldur.protect_facade import _get_or_build_dlq_protect_composer
        from baldur.services.retry_handler.models import RetryPolicyConfig

        thread_count = 8
        retry_cfg = RetryPolicyConfig.from_settings(domain="dlq.race")
        results: list = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def worker():
            barrier.wait()
            composer = _get_or_build_dlq_protect_composer("dlq.race", None, retry_cfg)
            with results_lock:
                results.append(composer)

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == thread_count
        first = results[0]
        assert all(r is first for r in results)


# =============================================================================
# 499 D1 — _DLQ_SINK module singleton identity on the slow path
# =============================================================================


class TestProtectDlqSlowPathSingletonIdentity:
    """499 D1 — even when the call falls through to the slow path (e.g. a
    ``fallback`` is supplied so the dlq_protect fast-path is bypassed), the
    DLQ sink added to the composer MUST be the module-level ``_DLQ_SINK``
    singleton — not a freshly allocated ``DLQSink()``. Locks the D1 promise
    that the per-call ``DLQSink()`` allocation has been fully eliminated
    across BOTH fast-path and slow-path branches.
    """

    def test_slow_path_composer_uses_dlq_sink_singleton(self):
        """Behavior — supplying ``fallback`` forces the call to bypass the
        dlq_protect fast-path, exercising the slow-path branch in
        ``_build_sync_composer``. The sink attached to the composer must
        still be ``protect_module._DLQ_SINK``."""
        from baldur.protect_facade import _build_sync_composer
        from baldur.services.retry_handler.models import RetryPolicyConfig

        cfg = RetryPolicyConfig.from_settings(domain="slow.dlq_singleton")
        composer = _build_sync_composer(
            name="slow.dlq_singleton",
            fallback=lambda: "fb",
            dlq=True,
            retry_cfg=cfg,
            circuit_breaker=True,
            timeout_seconds=None,
            retry_settings_derived=True,
        )

        assert composer._sinks
        assert composer._sinks[0] is protect_module._DLQ_SINK

    def test_slow_and_fast_path_composers_share_dlq_sink_instance(self):
        """Dependency interaction — a fast-path composer (no fallback) and a
        slow-path composer (with fallback) for the SAME name must reference
        the same ``_DLQ_SINK`` instance. Idempotency guarantee: no path
        constructs a private DLQSink that would diverge from the shared
        singleton."""
        from baldur.protect_facade import _build_sync_composer
        from baldur.services.retry_handler.models import RetryPolicyConfig

        cfg = RetryPolicyConfig.from_settings(domain="dlq.shared_sink")
        fast = _build_sync_composer(
            name="dlq.shared_sink",
            fallback=None,
            dlq=True,
            retry_cfg=cfg,
            circuit_breaker=True,
            timeout_seconds=None,
            retry_settings_derived=True,
        )
        slow = _build_sync_composer(
            name="dlq.shared_sink",
            fallback=lambda: "fb",
            dlq=True,
            retry_cfg=cfg,
            circuit_breaker=True,
            timeout_seconds=None,
            retry_settings_derived=True,
        )

        assert fast._sinks[0] is slow._sinks[0]
        assert fast._sinks[0] is protect_module._DLQ_SINK


# =============================================================================
# 499 D1 — DLQSink zero-state invariant (regression lock)
# =============================================================================


class TestDLQSinkSingletonSafetyContract:
    """499 D1 — ``DLQSink`` is documented stateless and shared as a
    module-level singleton by ``baldur.protect_facade._DLQ_SINK``. These tests
    structurally inspect the class to guard against a future maintainer
    silently introducing instance state (``__init__`` override, ``__slots__``,
    runtime-bound attributes) which would invalidate the singleton sharing
    and re-introduce the per-call allocation cost #499 set out to remove.
    """

    def test_dlq_sink_has_no_custom_init(self):
        """Structural — ``DLQSink.__init__`` is inherited from ``object`` (no
        custom override). The default object initializer takes no parameters
        beyond ``self`` and binds no state."""
        from baldur.services.retry_handler.sinks import DLQSink

        assert DLQSink.__init__ is object.__init__

    def test_dlq_sink_declares_no_slots(self):
        """Structural — ``DLQSink`` does not declare ``__slots__``. Declaring
        instance slots would imply per-instance storage, which contradicts
        the documented "no instance attributes" contract."""
        from baldur.services.retry_handler.sinks import DLQSink

        assert "__slots__" not in vars(DLQSink)

    def test_dlq_sink_instance_dict_is_empty_after_construction(self):
        """Idempotency — a freshly constructed ``DLQSink`` carries an empty
        ``__dict__``. Any instance state would surface here as a non-empty
        mapping, breaking the singleton-safe sharing contract."""
        from baldur.services.retry_handler.sinks import DLQSink

        instance = DLQSink()

        assert instance.__dict__ == {}


# =============================================================================
# 499 D2 — _composer_cache 3-tuple key shape (parametrized matrix)
# =============================================================================


class TestComposerCacheKeyShapeContract:
    """499 D2 — the ``_composer_cache`` key is the 3-tuple
    ``(name, timeout_seconds, profile_id)`` where ``profile_id`` is one of
    ``"default"`` or ``"dlq_protect"``. Parametrized across the timeout
    axis (None / explicit float) and profile axis to lock the key shape
    against accidental drop of the profile discriminator (which would
    collide ``@dlq_protect("X")`` and the default ``protect("X", fn)`` on
    the same key)."""

    @pytest.mark.parametrize(
        ("timeout", "expected_timeout_in_key"),
        [
            (None, None),
            (5.0, 5.0),
        ],
        ids=["timeout_none", "timeout_5s"],
    )
    def test_default_profile_cache_key_matches_3_tuple_shape(
        self, timeout, expected_timeout_in_key
    ):
        """Contract: default-profile callers populate
        ``(name, timeout, "default")``."""
        name = f"key_shape.default.{expected_timeout_in_key}"
        protect(name=name, fn=lambda: 1, timeout=timeout)

        assert (
            name,
            expected_timeout_in_key,
            "default",
        ) in protect_module._composer_cache

    @pytest.mark.parametrize(
        ("timeout", "expected_timeout_in_key"),
        [
            (None, None),
            (5.0, 5.0),
        ],
        ids=["timeout_none", "timeout_5s"],
    )
    def test_dlq_protect_profile_cache_key_matches_3_tuple_shape(
        self, timeout, expected_timeout_in_key
    ):
        """Contract: dlq_protect-profile callers populate
        ``(name, timeout, "dlq_protect")``."""
        name = f"key_shape.dlq.{expected_timeout_in_key}"
        protect(
            name=name,
            fn=lambda: 1,
            dlq=True,
            retry=True,
            circuit_breaker=True,
            timeout=timeout,
        )

        assert (
            name,
            expected_timeout_in_key,
            "dlq_protect",
        ) in protect_module._composer_cache

    def test_default_and_dlq_protect_profiles_for_same_name_do_not_collide(self):
        """Behavior: a single ``name`` may legitimately populate BOTH profile
        slots (a service that is sometimes called via ``protect(name, fn)``
        and sometimes via ``@dlq_protect(name)``). The 3-tuple key ensures
        the two entries coexist without overwriting each other."""
        protect(name="key_shape.both", fn=lambda: 1)
        protect(
            name="key_shape.both",
            fn=lambda: 1,
            dlq=True,
            retry=True,
            circuit_breaker=True,
        )

        cache = protect_module._composer_cache
        assert ("key_shape.both", None, "default") in cache
        assert ("key_shape.both", None, "dlq_protect") in cache
        assert (
            cache[("key_shape.both", None, "default")]
            is not cache[("key_shape.both", None, "dlq_protect")]
        )


# =============================================================================
# DCL race — concurrent first-call from multiple threads
# =============================================================================


class TestProtectCbPolicyCacheConcurrency:
    """DEC-1 — concurrent first-call must produce exactly one instance.

    The double-checked locking pattern in ``_get_or_build_cb_policy`` mirrors
    ``factory/base.py:127-148`` (the same precedent used 13× across
    ``GenericProviderRegistry``).
    """

    def test_concurrent_first_call_yields_single_instance(self):
        """Behavior — N threads racing on the same name → 1 constructor call,
        all threads see the same cached instance."""
        from baldur.services.circuit_breaker import policy as cb_policy_module

        thread_count = 8
        construct_count = 0
        construct_lock = threading.Lock()
        original_init = CircuitBreakerPolicy.__init__

        def counting_init(self, *args, **kwargs):
            nonlocal construct_count
            with construct_lock:
                construct_count += 1
            original_init(self, *args, **kwargs)

        results: list[CircuitBreakerPolicy] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def worker():
            from baldur.protect_facade import _get_or_build_cb_policy

            barrier.wait()
            policy = _get_or_build_cb_policy("race.shared")
            with results_lock:
                results.append(policy)

        with patch.object(
            cb_policy_module.CircuitBreakerPolicy, "__init__", counting_init
        ):
            threads = [threading.Thread(target=worker) for _ in range(thread_count)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert construct_count == 1
        assert len(results) == thread_count
        first = results[0]
        assert all(r is first for r in results)


# =============================================================================
# Reset chain — DEC-3
# =============================================================================


class TestProtectCacheReset:
    """DEC-3 — ``reset_protect_caches()`` clears the CB cache and forwards to
    ``reset_protect_recorder()``."""

    def test_reset_clears_populated_cb_cache(self):
        """State transition: populated cache → empty after reset."""
        protect(name="reset.x", fn=lambda: 1, circuit_breaker=True)
        assert "reset.x" in protect_module._cb_policy_cache

        reset_protect_caches()

        assert protect_module._cb_policy_cache == {}

    def test_reset_forwards_to_reset_protect_recorder(self):
        """Side effect: ``reset_protect_caches`` calls
        ``reset_protect_recorder`` so a single reset surface flushes both."""
        with patch(
            "baldur.metrics.recorders.protect.reset_protect_recorder",
            autospec=True,
        ) as mock_reset_recorder:
            reset_protect_caches()

        mock_reset_recorder.assert_called_once()

    def test_reset_on_empty_cache_is_noop(self):
        """Idempotent: reset called on an empty cache does not raise."""
        assert protect_module._cb_policy_cache == {}

        reset_protect_caches()  # must not raise

        assert protect_module._cb_policy_cache == {}


class TestProtectSettingsResetChain:
    """DEC-3 — ``reset_protect_settings()`` must invalidate the CB cache so a
    settings reset between tests does not leak a stale ``CircuitBreakerService``
    config snapshot through the cached policy."""

    def test_settings_reset_clears_cb_policy_cache(self):
        """State transition: populating the cache then resetting settings
        empties the cache (the lazy-import chain in
        ``settings/protect.py:reset_protect_settings``)."""
        from baldur.settings.protect import reset_protect_settings

        protect(name="chain.x", fn=lambda: 1, circuit_breaker=True)
        assert "chain.x" in protect_module._cb_policy_cache

        reset_protect_settings()

        assert protect_module._cb_policy_cache == {}

    def test_settings_reset_followed_by_protect_call_rebuilds_policy(self):
        """After reset, the next protect() call re-populates the cache with a
        FRESH instance — confirms the post-reset path actually rebuilds rather
        than reusing a leaked reference."""
        from baldur.settings.protect import reset_protect_settings

        protect(name="chain.rebuild", fn=lambda: 1, circuit_breaker=True)
        first = protect_module._cb_policy_cache["chain.rebuild"]

        reset_protect_settings()
        protect(name="chain.rebuild", fn=lambda: 1, circuit_breaker=True)
        second = protect_module._cb_policy_cache["chain.rebuild"]

        assert first is not second
