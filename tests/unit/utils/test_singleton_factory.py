"""
Unit tests for make_singleton_factory() helper.

Source: src/baldur/utils/singleton.py

The make_singleton_factory() helper replaces 67 hand-rolled DCL singleton
patterns with a generic, lock-safe factory generator. These tests verify
the helper's core behavior, thread safety, cleanup variations, and registry.

Verification techniques:
  - §8.10 Singleton/lifecycle — get/configure/reset caching behavior
  - §8.7 Concurrency/thread safety — DCL under multi-thread access
  - §8.3 Idempotency — reset and configure called multiple times
  - §8.5 Dependency interaction — create_fn/cleanup_fn call counts
  - §8.2 Exception/edge cases — cleanup failure, no-instance reset
  - §8.4 Side effects — cleanup failure logging
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.utils.singleton import (
    _REGISTRY,
    CLEANUP_CLOSE,
    CLEANUP_STOP,
    make_singleton_factory,
)

# ---------------------------------------------------------------------------
# Contract Tests
# ---------------------------------------------------------------------------


class TestMakeSingletonFactoryContract:
    """Design contract verification for make_singleton_factory."""

    def test_returns_three_element_tuple(self):
        """make_singleton_factory returns a 3-tuple (get, configure, reset)."""
        result = make_singleton_factory("_test_contract_tuple", lambda: object())
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_all_elements_are_callable(self):
        """All three returned elements must be callable."""
        get_fn, configure_fn, reset_fn = make_singleton_factory(
            "_test_contract_callable", lambda: object()
        )
        assert callable(get_fn)
        assert callable(configure_fn)
        assert callable(reset_fn)

    def test_registry_key_matches_name_parameter(self):
        """The name parameter becomes the _REGISTRY key."""
        name = "_test_contract_registry_key"
        triple = make_singleton_factory(name, lambda: object())
        assert name in _REGISTRY
        assert _REGISTRY[name] is triple

    def test_cleanup_close_calls_close_method(self):
        """CLEANUP_CLOSE invokes .close() on the instance."""
        mock_obj = MagicMock()
        CLEANUP_CLOSE(mock_obj)
        mock_obj.close.assert_called_once()

    def test_cleanup_stop_calls_stop_method(self):
        """CLEANUP_STOP invokes .stop() on the instance."""
        mock_obj = MagicMock()
        CLEANUP_STOP(mock_obj)
        mock_obj.stop.assert_called_once()

    def test_reset_fn_default_cleanup_parameter_is_true(self):
        """reset_fn defaults to cleanup=True."""
        cleanup_fn = MagicMock()
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_contract_default_cleanup",
            lambda: object(),
            cleanup_fn=cleanup_fn,
        )
        get_fn()
        reset_fn()
        cleanup_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Behavior Tests — Singleton Lifecycle (§8.10)
# ---------------------------------------------------------------------------


class TestSingletonLifecycleBehavior:
    """Singleton get/configure/reset lifecycle behavior."""

    @pytest.fixture(autouse=True)
    def _factory(self):
        self.create_fn = MagicMock(side_effect=lambda: object())
        self.cleanup_fn = MagicMock()
        self.get_fn, self.configure_fn, self.reset_fn = make_singleton_factory(
            "_test_lifecycle",
            self.create_fn,
            cleanup_fn=self.cleanup_fn,
        )
        yield
        self.reset_fn(cleanup=False)

    def test_get_returns_same_instance_on_repeated_calls(self):
        """get_fn returns the same cached instance."""
        first = self.get_fn()
        second = self.get_fn()
        assert first is second

    def test_get_invokes_create_fn_exactly_once(self):
        """create_fn is called once regardless of how many times get_fn is called."""
        self.get_fn()
        self.get_fn()
        self.get_fn()
        self.create_fn.assert_called_once()

    def test_configure_replaces_cached_instance(self):
        """configure_fn replaces the singleton with the provided value."""
        # Given
        original = self.get_fn()
        replacement = object()

        # When
        self.configure_fn(replacement)

        # Then
        assert self.get_fn() is replacement
        assert self.get_fn() is not original

    def test_configure_does_not_invoke_create_fn(self):
        """configure_fn sets the instance without calling create_fn."""
        replacement = object()
        self.configure_fn(replacement)
        assert self.get_fn() is replacement
        self.create_fn.assert_not_called()

    def test_reset_clears_instance_and_creates_new_on_next_get(self):
        """After reset, get_fn creates a new instance."""
        first = self.get_fn()
        self.reset_fn()
        second = self.get_fn()
        assert first is not second

    def test_reset_then_get_invokes_create_fn_again(self):
        """create_fn is called again after reset."""
        self.get_fn()
        self.reset_fn()
        self.get_fn()
        assert self.create_fn.call_count == 2


# ---------------------------------------------------------------------------
# Behavior Tests — Cleanup Variations (§8.5 Dependency Interaction)
# ---------------------------------------------------------------------------


class TestCleanupVariationsBehavior:
    """Cleanup fn present/absent × cleanup=True/False (4 combinations)."""

    def test_cleanup_fn_present_and_cleanup_true_calls_cleanup(self):
        """cleanup_fn is called when present and cleanup=True (default)."""
        cleanup_fn = MagicMock()
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_present_true",
            lambda: object(),
            cleanup_fn=cleanup_fn,
        )
        instance = get_fn()
        reset_fn(cleanup=True)
        cleanup_fn.assert_called_once_with(instance)

    def test_cleanup_fn_present_and_cleanup_false_skips_cleanup(self):
        """cleanup_fn is NOT called when cleanup=False (fork-safe reset)."""
        cleanup_fn = MagicMock()
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_present_false",
            lambda: object(),
            cleanup_fn=cleanup_fn,
        )
        get_fn()
        reset_fn(cleanup=False)
        cleanup_fn.assert_not_called()

    def test_cleanup_fn_absent_and_cleanup_true_does_not_raise(self):
        """No error when cleanup_fn is None and cleanup=True."""
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_absent_true",
            lambda: object(),
        )
        get_fn()
        reset_fn(cleanup=True)

    def test_cleanup_fn_absent_and_cleanup_false_does_not_raise(self):
        """No error when cleanup_fn is None and cleanup=False."""
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_absent_false",
            lambda: object(),
        )
        get_fn()
        reset_fn(cleanup=False)

    def test_cleanup_fn_receives_old_instance(self):
        """cleanup_fn receives the exact instance that was cached."""
        cleanup_fn = MagicMock()
        sentinel = object()
        get_fn, configure_fn, reset_fn = make_singleton_factory(
            "_test_cleanup_receives_old",
            lambda: object(),
            cleanup_fn=cleanup_fn,
        )
        configure_fn(sentinel)
        reset_fn()
        cleanup_fn.assert_called_once_with(sentinel)


# ---------------------------------------------------------------------------
# Behavior Tests — Cleanup Failure (§8.2 Exception/Edge Case)
# ---------------------------------------------------------------------------


class TestCleanupFailureBehavior:
    """cleanup_fn raises → instance still cleared (try/finally guarantee)."""

    def test_cleanup_failure_still_clears_instance(self):
        """Instance is cleared even if cleanup_fn raises."""

        def failing_cleanup(x):
            raise RuntimeError("cleanup exploded")

        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_failure_clears",
            lambda: object(),
            cleanup_fn=failing_cleanup,
        )

        # Given
        first = get_fn()

        # When — reset with failing cleanup (should not propagate)
        reset_fn()

        # Then — new instance created
        second = get_fn()
        assert first is not second

    def test_cleanup_failure_logs_warning(self):
        """Cleanup failure logs 'singleton.cleanup_failed' at WARNING level."""

        def failing_cleanup(x):
            raise RuntimeError("boom")

        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_failure_logs",
            lambda: object(),
            cleanup_fn=failing_cleanup,
        )
        get_fn()

        with patch("baldur.utils.singleton.logger") as mock_logger:
            reset_fn()
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert call_args[0][0] == "singleton.cleanup_failed"
            assert call_args[1]["name"] == "_test_cleanup_failure_logs"

    def test_cleanup_failure_does_not_propagate(self):
        """RuntimeError from cleanup_fn does not propagate to the caller."""

        def failing_cleanup(x):
            raise RuntimeError("should be swallowed")

        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_no_propagate",
            lambda: object(),
            cleanup_fn=failing_cleanup,
        )
        get_fn()
        reset_fn()


# ---------------------------------------------------------------------------
# Behavior Tests — Idempotency (§8.3)
# ---------------------------------------------------------------------------


class TestIdempotencyBehavior:
    """Reset and configure idempotency verification."""

    def test_reset_idempotent_when_no_instance_exists(self):
        """Calling reset_fn without prior get_fn does not raise."""
        _, _, reset_fn = make_singleton_factory(
            "_test_idempotent_reset_no_instance",
            lambda: object(),
        )
        reset_fn()
        reset_fn()

    def test_reset_idempotent_consecutive_calls(self):
        """Consecutive reset calls do not raise even after instance was created."""
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_idempotent_reset_consecutive",
            lambda: object(),
        )
        get_fn()
        reset_fn()
        reset_fn()

    def test_cleanup_fn_not_called_on_second_reset(self):
        """cleanup_fn is only called once across consecutive resets."""
        cleanup_fn = MagicMock()
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_idempotent_cleanup_once",
            lambda: object(),
            cleanup_fn=cleanup_fn,
        )
        get_fn()
        reset_fn()
        reset_fn()
        cleanup_fn.assert_called_once()

    def test_configure_multiple_times_keeps_last_value(self):
        """Multiple configure calls keep only the last value."""
        get_fn, configure_fn, reset_fn = make_singleton_factory(
            "_test_idempotent_configure_multi",
            lambda: object(),
        )
        try:
            val_a = object()
            val_b = object()
            val_c = object()
            configure_fn(val_a)
            configure_fn(val_b)
            configure_fn(val_c)
            assert get_fn() is val_c
        finally:
            reset_fn(cleanup=False)


# ---------------------------------------------------------------------------
# Behavior Tests — Thread Safety (§8.7)
# ---------------------------------------------------------------------------


class TestThreadSafetyBehavior:
    """Thread-safe DCL under multi-threaded access."""

    def test_concurrent_get_invokes_create_fn_exactly_once(self):
        """N threads calling get_fn concurrently create exactly one instance."""
        call_count = {"n": 0}
        lock = threading.Lock()
        sentinel = object()

        def counting_create():
            with lock:
                call_count["n"] += 1
            return sentinel

        get_fn, _, reset_fn = make_singleton_factory(
            "_test_concurrent_create_once",
            counting_create,
        )

        try:
            results = []
            barrier = threading.Barrier(8)

            def worker():
                barrier.wait()
                results.append(get_fn())

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            # Then
            assert call_count["n"] == 1
            assert len(results) == 8
            assert all(r is sentinel for r in results)
        finally:
            reset_fn(cleanup=False)

    def test_concurrent_get_returns_same_instance(self):
        """All threads receive the same singleton instance."""
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_concurrent_same_instance",
            lambda: object(),
        )

        try:
            results = []
            barrier = threading.Barrier(8)

            def worker():
                barrier.wait()
                results.append(get_fn())

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            assert len(results) == 8
            assert all(r is results[0] for r in results)
        finally:
            reset_fn(cleanup=False)

    def test_concurrent_configure_does_not_corrupt(self):
        """Concurrent configure_fn calls do not corrupt state."""
        get_fn, configure_fn, reset_fn = make_singleton_factory(
            "_test_concurrent_configure",
            lambda: object(),
        )

        try:
            values = [object() for _ in range(10)]
            barrier = threading.Barrier(10)
            errors = []

            def worker(val):
                try:
                    barrier.wait()
                    configure_fn(val)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(v,)) for v in values]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            assert len(errors) == 0
            final = get_fn()
            assert final in values
        finally:
            reset_fn(cleanup=False)


# ---------------------------------------------------------------------------
# Behavior Tests — Registry (§8.4 Side Effects)
# ---------------------------------------------------------------------------


class TestRegistryBehavior:
    """_REGISTRY population and correctness."""

    def test_make_singleton_factory_registers_triple(self):
        """Each call registers the (get, configure, reset) triple in _REGISTRY."""
        name = "_test_registry_triple"
        triple = make_singleton_factory(name, lambda: object())
        assert _REGISTRY[name] is triple
        get_fn, configure_fn, reset_fn = triple
        assert _REGISTRY[name][0] is get_fn
        assert _REGISTRY[name][1] is configure_fn
        assert _REGISTRY[name][2] is reset_fn

    def test_duplicate_name_overwrites_registry_entry(self):
        """A second call with the same name overwrites the registry entry."""
        name = "_test_registry_overwrite"
        triple_a = make_singleton_factory(name, lambda: "a")
        triple_b = make_singleton_factory(name, lambda: "b")
        assert _REGISTRY[name] is triple_b
        assert _REGISTRY[name] is not triple_a

    def test_registry_contains_known_production_singletons(self):
        """Spot-check that production singletons appear in _REGISTRY after import."""
        from baldur.core.action_executor import get_action_executor  # noqa: F401
        from baldur.core.tls import get_tls_config  # noqa: F401

        assert "tls_config" in _REGISTRY
        assert "action_executor" in _REGISTRY


# ---------------------------------------------------------------------------
# Behavior Tests — _UNSET Sentinel
# ---------------------------------------------------------------------------


class TestUnsetSentinelBehavior:
    """Verify _UNSET sentinel guarantees correct initial state."""

    def test_get_fn_creates_instance_on_first_call(self):
        """First get_fn call triggers create_fn."""
        create_fn = MagicMock(return_value=object())
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_unset_first_call",
            create_fn,
        )
        try:
            get_fn()
            create_fn.assert_called_once()
        finally:
            reset_fn(cleanup=False)

    def test_none_is_valid_singleton_value(self):
        """create_fn returning None is cached correctly (not confused with unset)."""
        create_fn = MagicMock(return_value=None)
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_unset_none_valid",
            create_fn,
        )
        try:
            result = get_fn()
            assert result is None
            get_fn()
            create_fn.assert_called_once()
        finally:
            reset_fn(cleanup=False)

    def test_false_is_valid_singleton_value(self):
        """create_fn returning False is cached correctly."""
        create_fn = MagicMock(return_value=False)
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_unset_false_valid",
            create_fn,
        )
        try:
            result = get_fn()
            assert result is False
            get_fn()
            create_fn.assert_called_once()
        finally:
            reset_fn(cleanup=False)


# ---------------------------------------------------------------------------
# Behavior Tests — Runtime Delegation (#450 Phase 2, D2)
# ---------------------------------------------------------------------------


class TestMakeSingletonFactoryDelegationBehavior:
    """Phase 2 contract: each function delegates to ``BaldurRuntime``.

    Per 450 D2, ``make_singleton_factory`` is now a thin wrapper around the
    active runtime's singleton store. These tests verify that
    ``get_fn`` / ``configure_fn`` / ``reset_fn`` actually go through the
    runtime — i.e. swapping in a fresh runtime drops every prior instance
    and a runtime override is observable via the same ``get_fn`` call site.
    """

    def test_get_fn_reads_from_active_runtime_singleton_store(self):
        """``get_fn()`` and ``runtime.get_singleton(name)`` share identity."""
        from baldur.runtime import get_runtime

        get_fn, _, reset_fn = make_singleton_factory(
            "_test_delegation_runtime_read",
            lambda: object(),
        )
        try:
            instance = get_fn()
            # The runtime should now hold the same instance under the same name.
            assert get_runtime().has_singleton("_test_delegation_runtime_read")
            cached_via_runtime = get_runtime().get_singleton(
                "_test_delegation_runtime_read",
                lambda: object(),  # MUST NOT run — cached value short-circuits
            )
            assert cached_via_runtime is instance
        finally:
            reset_fn(cleanup=False)

    def test_configure_fn_writes_through_runtime_set_singleton(self):
        """``configure_fn(value)`` injects via ``runtime.set_singleton``."""
        from baldur.runtime import get_runtime

        get_fn, configure_fn, reset_fn = make_singleton_factory(
            "_test_delegation_configure_writes",
            lambda: object(),
        )
        try:
            sentinel = object()
            configure_fn(sentinel)
            # Reading via the runtime returns the injected value.
            assert (
                get_runtime().get_singleton(
                    "_test_delegation_configure_writes", lambda: object()
                )
                is sentinel
            )
            # And the wrapper also sees it (no separate cache).
            assert get_fn() is sentinel
        finally:
            reset_fn(cleanup=False)

    def test_reset_fn_drops_runtime_entry(self):
        """``reset_fn`` calls ``runtime.reset_singleton`` so the entry is gone."""
        from baldur.runtime import get_runtime

        get_fn, _, reset_fn = make_singleton_factory(
            "_test_delegation_reset_drops",
            lambda: object(),
        )
        get_fn()
        assert get_runtime().has_singleton("_test_delegation_reset_drops")

        reset_fn()

        assert not get_runtime().has_singleton("_test_delegation_reset_drops")

    def test_runtime_swap_isolates_singleton_from_prior_runtime(self):
        """Swapping in a fresh runtime invalidates every wrapper's cache."""
        from baldur import runtime as runtime_module
        from baldur.runtime import BaldurRuntime, set_runtime

        get_fn, _, reset_fn = make_singleton_factory(
            "_test_delegation_swap_isolation",
            lambda: object(),
        )
        try:
            first = get_fn()

            new_runtime = BaldurRuntime()
            token = set_runtime(new_runtime)
            try:
                second = get_fn()
                assert second is not first
            finally:
                runtime_module._runtime_var.reset(token)
        finally:
            reset_fn(cleanup=False)

    def test_get_runtime_re_resolved_not_captured_resists_patch_poison(self):
        """A factory built while ``baldur.runtime.get_runtime`` is patched must
        not stay bound to the mock after the patch exits.

        Regression: ``get_runtime`` is looked up per call, never captured in the
        closure. Previously a singleton-defining module that was first imported
        inside a test's ``patch("baldur.runtime.get_runtime")`` window baked the
        mock into the ``cluster_identity`` / ``quarantine_state`` factories
        permanently — every later test on that xdist worker then saw
        ``get_runtime()`` return a ``MagicMock`` (``reset_singleton`` 0-unpack
        and over-long trace ids), an order-dependent cross-test polluter.
        """
        import baldur.runtime as runtime_module

        real_get_runtime = runtime_module.get_runtime
        with patch("baldur.runtime.get_runtime", MagicMock(name="get_runtime")):
            # Factory created while the source is patched (the poison window).
            get_fn, _, reset_fn = make_singleton_factory(
                "_test_poison_guard", lambda: object()
            )
        try:
            # Patch has exited — the wrapper must use the restored real function.
            assert runtime_module.get_runtime is real_get_runtime
            # reset_fn unpacks (was_present, old); a captured mock would 0-unpack.
            reset_fn(cleanup=False)
            # get_fn returns a real object, not a child of the leaked mock.
            value = get_fn()
            assert not hasattr(value, "_mock_name")
        finally:
            reset_fn(cleanup=False)
            _REGISTRY.pop("_test_poison_guard", None)


class TestSingletonResetCleanupBehavior:
    """``reset_fn(cleanup=True)`` invokes ``cleanup_fn`` only on present values.

    Boundary: ``runtime.reset_singleton`` returns ``(was_present, old_value)``.
    The factory must use ``was_present`` to decide whether to invoke
    ``cleanup_fn`` — so a ``reset_fn`` on a never-set name MUST NOT run
    cleanup, and a reset on a legitimately-cached ``None`` MUST run it.
    """

    def test_cleanup_not_called_when_singleton_was_never_set(self):
        """``reset_fn`` on a never-set name skips cleanup_fn (was_present=False)."""
        cleanup_fn = MagicMock()
        _, _, reset_fn = make_singleton_factory(
            "_test_cleanup_skipped_never_set",
            lambda: object(),
            cleanup_fn=cleanup_fn,
        )
        # No get_fn() call — name has never been cached.
        reset_fn(cleanup=True)
        cleanup_fn.assert_not_called()

    def test_cleanup_called_when_cached_value_is_none(self):
        """``reset_fn`` on a cached ``None`` invokes cleanup_fn (was_present=True)."""
        cleanup_fn = MagicMock()
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_called_for_cached_none",
            lambda: None,
            cleanup_fn=cleanup_fn,
        )
        get_fn()  # caches None
        reset_fn(cleanup=True)
        cleanup_fn.assert_called_once_with(None)

    def test_cleanup_called_when_cached_value_is_false(self):
        """``reset_fn`` on a cached ``False`` invokes cleanup_fn (was_present=True)."""
        cleanup_fn = MagicMock()
        get_fn, _, reset_fn = make_singleton_factory(
            "_test_cleanup_called_for_cached_false",
            lambda: False,
            cleanup_fn=cleanup_fn,
        )
        get_fn()  # caches False
        reset_fn(cleanup=True)
        cleanup_fn.assert_called_once_with(False)
