"""Unit tests for ``baldur.runtime`` (#450 Phase 1 + Phase 2).

Source: ``src/baldur/runtime.py``

Covers:
- ``BaldurRuntime.get_settings`` / ``set_settings`` / ``reset_settings`` —
  per-class DCL caching, full vs per-class clear, override.
- ``BaldurRuntime.get_singleton`` / ``set_singleton`` / ``reset_singleton`` —
  Phase 2 surface; ``_UNSET`` sentinel allows ``None`` / ``False`` to be
  cached without re-invoking ``create_fn``; ``reset_singleton`` returns
  ``(was_present, old_value)`` so callers can decide whether to invoke
  cleanup.
- ``BaldurRuntime._lock`` is an ``RLock`` — a ``create_fn`` may transitively
  request another singleton from the same runtime without deadlocking.
- ``current_runtime`` / ``get_runtime`` / ``set_runtime`` / ``reset_runtime``
  — ContextVar slot semantics, lazy-create fallback to the process-global
  ``_default_runtime``, plain-Thread inheritance contract (PEP 567).
- ContextVar isolation across ``copy_context().run(...)`` — child Context
  installations do not leak to the parent.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.10 Singleton/lifecycle — get/set/reset DCL behavior.
- §8.3  Idempotency — repeated get returns same instance.
- §8.7  Concurrency/thread safety — plain-Thread fallback to default runtime.
- §8.4  Side effects — ContextVar slot / default slot mutation.
- §8.2  Exception/edge cases — ``reset_singleton`` for never-set name,
  ``None`` / ``False`` cached values.
"""

from __future__ import annotations

import contextvars
import threading
from typing import cast

import pytest
from pydantic_settings import BaseSettings

from baldur import runtime as runtime_module
from baldur.runtime import (
    BaldurRuntime,
    current_runtime,
    get_runtime,
    reset_runtime,
    set_runtime,
)

# ---------------------------------------------------------------------------
# Test settings classes (deliberately non-production to avoid env coupling)
# ---------------------------------------------------------------------------


class _AlphaSettings(BaseSettings):
    """Minimal Pydantic Settings class — distinct identity per cls key."""

    flag: bool = False


class _BetaSettings(BaseSettings):
    """Second Settings class to verify per-cls isolation."""

    n: int = 1


# ---------------------------------------------------------------------------
# Fixtures — restore process-global default runtime after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_default_runtime():
    """Snapshot and restore the process-global default runtime + ContextVar.

    Tests in this file mutate ``_default_runtime`` and the ``_runtime_var``
    ContextVar directly. Without restoration, the conftest auto-reset would
    fight with our manual swaps and pollute neighboring suites.
    """
    saved_default = runtime_module._default_runtime
    saved_var = runtime_module._runtime_var.get()
    yield
    runtime_module._default_runtime = saved_default
    runtime_module._runtime_var.set(saved_var)


@pytest.fixture
def fresh_runtime():
    """Install a fresh ``BaldurRuntime`` for the duration of the test."""
    runtime = BaldurRuntime()
    token = set_runtime(runtime)
    try:
        yield runtime
    finally:
        runtime_module._runtime_var.reset(token)


# ---------------------------------------------------------------------------
# BaldurRuntime — Settings surface (Phase 1, §8.10 + §8.3)
# ---------------------------------------------------------------------------


class TestBaldurRuntimeSettingsBehavior:
    """``get_settings`` / ``reset_settings`` / ``set_settings`` lifecycle."""

    def test_get_settings_returns_same_instance_for_same_cls(self, fresh_runtime):
        """DCL: a second call with the same cls returns the cached instance."""
        first = fresh_runtime.get_settings(_AlphaSettings)
        second = fresh_runtime.get_settings(_AlphaSettings)
        assert first is second

    def test_get_settings_returns_distinct_instances_for_different_classes(
        self, fresh_runtime
    ):
        """Per-class cache: different cls keys never share an instance."""
        alpha = fresh_runtime.get_settings(_AlphaSettings)
        beta = fresh_runtime.get_settings(_BetaSettings)
        assert isinstance(alpha, _AlphaSettings)
        assert isinstance(beta, _BetaSettings)
        assert alpha is not beta

    def test_reset_settings_with_cls_drops_only_that_entry(self, fresh_runtime):
        """``reset_settings(cls=X)`` clears X but leaves Y cached."""
        # Given
        alpha = fresh_runtime.get_settings(_AlphaSettings)
        beta = fresh_runtime.get_settings(_BetaSettings)

        # When
        fresh_runtime.reset_settings(_AlphaSettings)

        # Then — Alpha rebuilt (new identity), Beta preserved
        assert fresh_runtime.get_settings(_AlphaSettings) is not alpha
        assert fresh_runtime.get_settings(_BetaSettings) is beta

    def test_reset_settings_none_clears_all_entries(self, fresh_runtime):
        """``reset_settings(None)`` empties the per-cls cache entirely."""
        alpha = fresh_runtime.get_settings(_AlphaSettings)
        beta = fresh_runtime.get_settings(_BetaSettings)

        fresh_runtime.reset_settings(None)

        assert fresh_runtime.get_settings(_AlphaSettings) is not alpha
        assert fresh_runtime.get_settings(_BetaSettings) is not beta

    def test_reset_settings_for_unknown_cls_is_noop(self, fresh_runtime):
        """``reset_settings`` on a never-cached cls does not raise."""
        # No prior get_settings call — pop must use default.
        fresh_runtime.reset_settings(_AlphaSettings)

    def test_set_settings_overrides_cached_instance(self, fresh_runtime):
        """A subsequent ``get_settings`` returns the injected override."""
        override = _AlphaSettings(flag=True)
        fresh_runtime.set_settings(_AlphaSettings, override)
        assert fresh_runtime.get_settings(_AlphaSettings) is override

    def test_set_settings_replaces_existing_entry(self, fresh_runtime):
        """``set_settings`` overwrites any previously-cached instance."""
        first = fresh_runtime.get_settings(_AlphaSettings)
        replacement = _AlphaSettings(flag=True)

        fresh_runtime.set_settings(_AlphaSettings, replacement)

        assert fresh_runtime.get_settings(_AlphaSettings) is replacement
        assert fresh_runtime.get_settings(_AlphaSettings) is not first


# ---------------------------------------------------------------------------
# BaldurRuntime — Singleton surface (Phase 2, §8.10 + §8.2 boundary)
# ---------------------------------------------------------------------------


class TestBaldurRuntimeSingletonBehavior:
    """``get_singleton`` / ``set_singleton`` / ``reset_singleton`` lifecycle."""

    def test_get_singleton_caches_return_value(self, fresh_runtime):
        """A second call with the same name returns the cached instance."""
        first = fresh_runtime.get_singleton("svc", lambda: object())
        second = fresh_runtime.get_singleton("svc", lambda: object())
        assert first is second

    def test_get_singleton_invokes_create_fn_exactly_once(self, fresh_runtime):
        """``create_fn`` runs once even when ``get_singleton`` is called N times."""
        calls = {"n": 0}

        def create():
            calls["n"] += 1
            return object()

        fresh_runtime.get_singleton("svc", create)
        fresh_runtime.get_singleton("svc", create)
        fresh_runtime.get_singleton("svc", create)

        assert calls["n"] == 1

    def test_get_singleton_caches_none_via_unset_sentinel(self, fresh_runtime):
        """``create_fn`` returning ``None`` is cached (not re-invoked)."""
        calls = {"n": 0}

        def create_none():
            calls["n"] += 1
            return None

        first = fresh_runtime.get_singleton("nullable", create_none)
        second = fresh_runtime.get_singleton("nullable", create_none)

        assert first is None
        assert second is None
        assert calls["n"] == 1

    def test_get_singleton_caches_false_via_unset_sentinel(self, fresh_runtime):
        """``create_fn`` returning ``False`` is cached (not re-invoked)."""
        calls = {"n": 0}

        def create_false():
            calls["n"] += 1
            return False

        first = fresh_runtime.get_singleton("falsy", create_false)
        second = fresh_runtime.get_singleton("falsy", create_false)

        assert first is False
        assert second is False
        assert calls["n"] == 1

    def test_set_singleton_injects_value(self, fresh_runtime):
        """``set_singleton`` makes ``get_singleton`` return the injected value."""
        sentinel = object()
        fresh_runtime.set_singleton("svc", sentinel)
        # create_fn must NOT run when a value is already cached.
        result = fresh_runtime.get_singleton("svc", lambda: object())
        assert result is sentinel

    def test_set_singleton_overwrites_existing_entry(self, fresh_runtime):
        """A second ``set_singleton`` replaces the prior value."""
        first = object()
        second = object()

        fresh_runtime.set_singleton("svc", first)
        fresh_runtime.set_singleton("svc", second)

        assert fresh_runtime.get_singleton("svc", lambda: object()) is second

    def test_reset_singleton_returns_was_present_true_when_cached(self, fresh_runtime):
        """``reset_singleton`` returns ``(True, old_value)`` for cached names."""
        sentinel = object()
        fresh_runtime.set_singleton("svc", sentinel)

        was_present, old = fresh_runtime.reset_singleton("svc")

        assert was_present is True
        assert old is sentinel

    def test_reset_singleton_returns_was_present_false_when_never_set(
        self, fresh_runtime
    ):
        """``reset_singleton`` returns ``(False, None)`` for unknown names."""
        was_present, old = fresh_runtime.reset_singleton("never_set")
        assert was_present is False
        assert old is None

    def test_reset_singleton_returns_was_present_true_for_cached_none(
        self, fresh_runtime
    ):
        """A legitimately-cached ``None`` is reported as present (not as unset)."""
        fresh_runtime.get_singleton("nullable", lambda: None)

        was_present, old = fresh_runtime.reset_singleton("nullable")

        assert was_present is True
        assert old is None

    def test_reset_singleton_drops_cache_so_create_fn_runs_again(self, fresh_runtime):
        """After reset, the next ``get_singleton`` invokes ``create_fn`` afresh."""
        calls = {"n": 0}

        def create():
            calls["n"] += 1
            return object()

        fresh_runtime.get_singleton("svc", create)
        fresh_runtime.reset_singleton("svc")
        fresh_runtime.get_singleton("svc", create)

        assert calls["n"] == 2

    def test_has_singleton_reflects_presence(self, fresh_runtime):
        """``has_singleton`` returns True only after the name is cached."""
        assert fresh_runtime.has_singleton("svc") is False
        fresh_runtime.get_singleton("svc", lambda: object())
        assert fresh_runtime.has_singleton("svc") is True
        fresh_runtime.reset_singleton("svc")
        assert fresh_runtime.has_singleton("svc") is False


# ---------------------------------------------------------------------------
# BaldurRuntime — Reentrancy (Phase 2, RLock contract)
# ---------------------------------------------------------------------------


class TestBaldurRuntimeReentrancyBehavior:
    """``_lock`` is an ``RLock`` — nested ``get_singleton`` does not deadlock."""

    def test_create_fn_for_a_can_request_singleton_b(self, fresh_runtime):
        """A's ``create_fn`` calling ``get_singleton('b')`` must not deadlock.

        Mirrors the production case where ``constraint_engine``'s create_fn
        invokes ``get_dependency_graph()`` which itself goes through
        ``get_singleton``. The same thread re-entering the runtime lock is
        safe only if ``_lock`` is an ``RLock``.
        """
        b_sentinel = object()

        def create_b():
            return b_sentinel

        def create_a():
            # Re-enter the runtime lock from the same thread.
            return ("a-wraps", fresh_runtime.get_singleton("b", create_b))

        result = fresh_runtime.get_singleton("a", create_a)

        assert result == ("a-wraps", b_sentinel)
        assert fresh_runtime.get_singleton("b", create_b) is b_sentinel


# ---------------------------------------------------------------------------
# Module-level accessors — Contract (Phase 1)
# ---------------------------------------------------------------------------


class TestRuntimeAccessorContract:
    """Public accessor contract: lazy-create + None-default semantics."""

    def test_current_runtime_returns_none_when_var_unset(self):
        """``current_runtime`` reports an empty slot as ``None`` (no fallback)."""
        # Force both slots empty.
        runtime_module._default_runtime = None
        runtime_module._runtime_var.set(None)
        assert current_runtime() is None

    def test_get_runtime_lazy_creates_default_when_both_empty(self):
        """First ``get_runtime`` call instantiates the process-global default."""
        runtime_module._default_runtime = None
        runtime_module._runtime_var.set(None)

        rt = get_runtime()

        assert isinstance(rt, BaldurRuntime)
        assert runtime_module._default_runtime is rt

    def test_get_runtime_returns_same_instance_on_subsequent_calls(self):
        """Lazy create is idempotent — repeated ``get_runtime`` calls share identity."""
        runtime_module._default_runtime = None
        runtime_module._runtime_var.set(None)

        first = get_runtime()
        second = get_runtime()

        assert first is second

    def test_current_runtime_does_not_see_default_runtime(self):
        """``current_runtime`` only reads the ContextVar slot — default is invisible.

        Diagnostic guarantee from the docstring: ``current_runtime`` exists
        precisely to detect "no runtime in this Context yet" without paying
        the lazy-create cost.
        """
        runtime_module._runtime_var.set(None)
        runtime_module._default_runtime = BaldurRuntime()

        assert current_runtime() is None
        # Sanity check: get_runtime DOES see it.
        assert get_runtime() is runtime_module._default_runtime


# ---------------------------------------------------------------------------
# Module-level accessors — Behavior (Phase 1 + Phase 2)
# ---------------------------------------------------------------------------


class TestRuntimeAccessorBehavior:
    """``set_runtime`` / ``reset_runtime`` mutate slots correctly."""

    def test_set_runtime_returns_token_for_restoration(self):
        """``set_runtime`` returns a ``contextvars.Token``."""
        rt = BaldurRuntime()
        token = set_runtime(rt)
        try:
            assert isinstance(token, contextvars.Token)
        finally:
            runtime_module._runtime_var.reset(token)

    def test_set_runtime_makes_get_runtime_return_the_new_runtime(self):
        """ContextVar override takes precedence over lazy default."""
        rt = BaldurRuntime()
        token = set_runtime(rt)
        try:
            assert get_runtime() is rt
            assert current_runtime() is rt
        finally:
            runtime_module._runtime_var.reset(token)

    def test_token_reset_restores_previous_value(self):
        """``token.reset()`` reverts the ContextVar to the prior value."""
        original = BaldurRuntime()
        runtime_module._runtime_var.set(original)

        new_runtime = BaldurRuntime()
        token = set_runtime(new_runtime)
        runtime_module._runtime_var.reset(token)

        assert current_runtime() is original

    def test_reset_runtime_clears_context_var_slot(self):
        """``reset_runtime`` empties the ContextVar slot."""
        rt = BaldurRuntime()
        runtime_module._runtime_var.set(rt)

        reset_runtime()

        assert current_runtime() is None

    def test_reset_runtime_clears_process_global_default(self):
        """``reset_runtime`` also wipes ``_default_runtime`` so init can rebuild."""
        runtime_module._default_runtime = BaldurRuntime()

        reset_runtime()

        assert runtime_module._default_runtime is None

    def test_reset_runtime_then_get_runtime_rebuilds_default(self):
        """A fresh default is created on the next ``get_runtime`` call."""
        runtime_module._default_runtime = BaldurRuntime()
        original_default = runtime_module._default_runtime

        reset_runtime()
        rebuilt = get_runtime()

        assert rebuilt is not original_default
        assert isinstance(rebuilt, BaldurRuntime)


# ---------------------------------------------------------------------------
# ContextVar isolation across copy_context().run(...) (Phase 1)
# ---------------------------------------------------------------------------


class TestRuntimeContextVarIsolationBehavior:
    """``copy_context().run(...)`` isolates ``_runtime_var`` mutations."""

    def test_set_inside_copy_context_does_not_leak_to_parent(self):
        """Installing a runtime in a child Context leaves the parent untouched."""
        # Given — parent Context has a known runtime.
        parent_rt = BaldurRuntime()
        runtime_module._runtime_var.set(parent_rt)

        child_observed: list = []

        def child_workload():
            child_rt = BaldurRuntime()
            set_runtime(child_rt)
            child_observed.append(current_runtime())

        # When — run the workload in a copied Context.
        ctx = contextvars.copy_context()
        ctx.run(child_workload)

        # Then
        assert child_observed[0] is not parent_rt
        assert current_runtime() is parent_rt  # parent unchanged

    def test_parent_runtime_visible_inside_copy_context_until_overridden(self):
        """A copied Context inherits the parent's ``_runtime_var`` value."""
        parent_rt = BaldurRuntime()
        runtime_module._runtime_var.set(parent_rt)

        observed: list = []

        def child_observer():
            observed.append(current_runtime())

        ctx = contextvars.copy_context()
        ctx.run(child_observer)

        assert observed[0] is parent_rt


# ---------------------------------------------------------------------------
# Process-global default runtime — plain Thread inheritance (Phase 2, §8.7)
# ---------------------------------------------------------------------------


class TestRuntimeProcessGlobalFallbackBehavior:
    """Plain ``threading.Thread`` workers fall back to ``_default_runtime``.

    PEP 567: ``threading.Thread`` does not inherit the parent's ContextVar
    values. Without the process-global default fallback, a Phase 2 singleton
    accessed from a worker thread would lazy-create a *separate* runtime per
    thread and break the "exactly one instance" guarantee.
    """

    def test_main_and_worker_thread_resolve_to_same_runtime(self):
        """Main and worker threads both resolve ``get_runtime`` to the default."""
        # Given — no ContextVar override, default not yet built.
        runtime_module._runtime_var.set(None)
        runtime_module._default_runtime = None

        main_rt = get_runtime()

        worker_observed: list = []

        def worker():
            worker_observed.append(get_runtime())

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        assert len(worker_observed) == 1
        assert worker_observed[0] is main_rt

    def test_context_var_override_does_not_propagate_to_plain_thread(self):
        """A ``set_runtime`` in main is invisible to a plain Thread (PEP 567)."""
        # Given — the default and a separate ContextVar override.
        runtime_module._default_runtime = BaldurRuntime()
        default_rt = runtime_module._default_runtime

        override_rt = BaldurRuntime()
        token = set_runtime(override_rt)
        try:
            worker_observed: list = []

            def worker():
                # Plain Thread: ContextVar slot is empty, falls back to default.
                worker_observed.append(current_runtime())
                worker_observed.append(get_runtime())

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=5)
        finally:
            runtime_module._runtime_var.reset(token)

        assert worker_observed[0] is None  # ContextVar not inherited
        assert worker_observed[1] is default_rt  # falls back to default

    def test_concurrent_lazy_create_yields_single_default(self):
        """N threads racing on the first ``get_runtime`` end up with one default."""
        runtime_module._runtime_var.set(None)
        runtime_module._default_runtime = None

        results: list[BaldurRuntime] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(get_runtime())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 8
        assert all(r is results[0] for r in results)
        # Sanity: the slot now holds that single instance.
        assert runtime_module._default_runtime is results[0]


# ---------------------------------------------------------------------------
# Singleton surface — concurrency contract (Phase 2, §8.7)
# ---------------------------------------------------------------------------


class TestBaldurRuntimeSingletonConcurrencyBehavior:
    """``get_singleton`` invokes ``create_fn`` exactly once under thread race."""

    def test_concurrent_get_singleton_invokes_create_fn_exactly_once(
        self, fresh_runtime
    ):
        """8 threads racing on a fresh name → ``create_fn`` runs once total."""
        calls = {"n": 0}
        sentinel = object()
        cnt_lock = threading.Lock()

        def create():
            with cnt_lock:
                calls["n"] += 1
            return sentinel

        results: list = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(fresh_runtime.get_singleton("svc", create))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert calls["n"] == 1
        assert len(results) == 8
        assert all(r is sentinel for r in results)


# ---------------------------------------------------------------------------
# __all__ contract
# ---------------------------------------------------------------------------


class TestRuntimeModuleContract:
    """Public surface (``__all__``) matches the documented accessors."""

    def test_module_all_lists_documented_symbols(self):
        """``__all__`` exports the runtime accessors and the class only."""
        assert set(runtime_module.__all__) == {
            "BaldurRuntime",
            "current_runtime",
            "get_runtime",
            "is_production",
            "reset_runtime",
            "set_runtime",
        }

    def test_baldur_runtime_uses_slots(self):
        """``BaldurRuntime`` must be ``__slots__``-backed (no per-instance dict)."""
        rt = BaldurRuntime()
        # Touching __dict__ on a __slots__ class raises AttributeError.
        with pytest.raises(AttributeError):
            cast(object, rt).__dict__  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# BaldurRuntime — is_test_mode eager-read (453 D5a, §8.2 + §8.10)
# ---------------------------------------------------------------------------


class TestBaldurRuntimeTestModeBehavior:
    """``is_test_mode`` is read once from ``BALDUR_TEST_MODE`` at ``__init__``.

    The eager-read design eliminates the timing window where
    ``_create_cluster_identity`` previously read the env at factory-invocation
    time and flipped quarantine globally. ``BaldurRuntime.is_test_mode`` is
    the only sanctioned getter for the framework's "in-test" signal — see
    ``UNIT_TEST_GUIDELINES.md §6.5.8``.
    """

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("TrUe", True),
            ("false", False),
            ("False", False),
            ("0", False),
            ("1", False),
            ("", False),
            ("yes", False),
            ("anything-else", False),
        ],
    )
    def test_is_test_mode_resolves_lowercase_true_only(
        self, monkeypatch, env_value, expected
    ):
        """Only ``BALDUR_TEST_MODE.lower() == "true"`` yields True; case-insensitive."""
        monkeypatch.setenv("BALDUR_TEST_MODE", env_value)
        assert BaldurRuntime().is_test_mode is expected

    def test_is_test_mode_false_when_env_unset(self, monkeypatch):
        """Missing BALDUR_TEST_MODE defaults to ``False`` (production)."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        assert BaldurRuntime().is_test_mode is False

    def test_is_test_mode_eager_read_immune_to_subsequent_env_change(self, monkeypatch):
        """Mutating the env after construction does NOT change the attribute.

        This is the categorical fix for the 453 G1 leak: a single eager read
        means production-init code paths cannot observe a transient env value
        that drifts under xdist worker scheduling.
        """
        # Given — runtime constructed under test_mode=true.
        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        rt = BaldurRuntime()
        assert rt.is_test_mode is True

        # When — env flipped after construction.
        monkeypatch.setenv("BALDUR_TEST_MODE", "false")

        # Then — attribute does not change.
        assert rt.is_test_mode is True

    def test_is_test_mode_independent_per_instance(self, monkeypatch):
        """Two runtimes constructed at different env states hold independent flags."""
        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        first = BaldurRuntime()

        monkeypatch.setenv("BALDUR_TEST_MODE", "false")
        second = BaldurRuntime()

        assert first.is_test_mode is True
        assert second.is_test_mode is False

    def test_is_test_mode_attribute_in_slots(self):
        """``is_test_mode`` is one of the documented slots — no per-instance dict."""
        assert "is_test_mode" in BaldurRuntime.__slots__


# ---------------------------------------------------------------------------
# BaldurRuntime — is_production eager-read (463 D1, §8.2 + §8.10)
# ---------------------------------------------------------------------------


class TestIsProductionContract:
    """``runtime.is_production()`` — strict equality with ``BALDUR_ENVIRONMENT``.

    The framework-agnostic runtime mandates a single canonical production signal:
    ``BALDUR_ENVIRONMENT == "production"`` after ``.strip().lower()``. No
    aliases (``prod``/``live``/``release``/``stable``), no
    ``DJANGO_SETTINGS_MODULE`` substring fallback. Eager-read at runtime
    construction time so test fixtures that swap the runtime via
    :func:`set_runtime` get a fresh per-test signal without re-reading
    ``os.environ`` on every call.
    """

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            (None, False),
            ("", False),
            ("production", True),
            ("PRODUCTION", True),
            ("Production", True),
            ("production ", True),  # trailing space stripped
            (" production", True),  # leading space stripped
            ("prod", False),  # legacy alias does NOT match (D6)
            ("live", False),
            ("release", False),
            ("stable", False),
            ("staging", False),
            ("development", False),
            ("dev", False),
            ("test", False),
            ("productionx", False),  # superset must not match
        ],
        ids=[
            "unset",
            "empty",
            "production_lower",
            "production_upper",
            "production_mixed",
            "production_trailing_space",
            "production_leading_space",
            "legacy_prod",
            "legacy_live",
            "legacy_release",
            "legacy_stable",
            "staging",
            "development",
            "dev",
            "test",
            "production_superset",
        ],
    )
    def test_is_production_strict_equality_after_strip_lower(
        self, monkeypatch, env_value, expected
    ):
        """Only ``BALDUR_ENVIRONMENT.strip().lower() == "production"`` returns True."""
        if env_value is None:
            monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)
        else:
            monkeypatch.setenv("BALDUR_ENVIRONMENT", env_value)

        assert BaldurRuntime().is_production is expected

    def test_is_production_false_when_env_unset(self, monkeypatch):
        """Missing BALDUR_ENVIRONMENT defaults to ``False``."""
        monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)
        assert BaldurRuntime().is_production is False


class TestBaldurRuntimeProductionModeBehavior:
    """``is_production`` slot lifecycle — symmetric to ``is_test_mode``."""

    def test_is_production_eager_read_immune_to_subsequent_env_change(
        self, monkeypatch
    ):
        """Mutating the env after construction does NOT change the slot.

        Single eager read in ``__init__`` is the categorical anti-pattern fix
        for env-read + global-mutation race documented in
        UNIT_TEST_GUIDELINES §6.5.8.
        """
        # Given — runtime constructed under production.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        rt = BaldurRuntime()
        assert rt.is_production is True

        # When — env flipped after construction.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")

        # Then — slot does not change.
        assert rt.is_production is True

    def test_is_production_independent_per_instance(self, monkeypatch):
        """Two runtimes constructed at different env states hold independent flags."""
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        first = BaldurRuntime()

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        second = BaldurRuntime()

        assert first.is_production is True
        assert second.is_production is False

    def test_is_production_attribute_in_slots(self):
        """``is_production`` is one of the documented slots — no per-instance dict."""
        assert "is_production" in BaldurRuntime.__slots__

    def test_module_level_is_production_delegates_to_active_runtime(self, monkeypatch):
        """``runtime.is_production()`` reads ``get_runtime().is_production``."""
        from baldur.runtime import is_production

        # Given — install a runtime that claims production.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        prod_rt = BaldurRuntime()
        token = set_runtime(prod_rt)
        try:
            assert is_production() is True

            # Swap to a non-production runtime → helper reflects the swap.
            monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
            dev_rt = BaldurRuntime()
            runtime_module._runtime_var.reset(token)
            token = set_runtime(dev_rt)
            assert is_production() is False
        finally:
            runtime_module._runtime_var.reset(token)

    def test_is_production_helper_picks_up_runtime_swap(self, monkeypatch):
        """A ``set_runtime`` swap is observable through the module-level helper.

        Per the eager-read contract, the helper reads the slot of whatever
        runtime is currently active, not the env var. Swapping runtimes mid-
        session is the test-isolation primitive.
        """
        from baldur.runtime import is_production

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        dev_rt = BaldurRuntime()
        token = set_runtime(dev_rt)
        try:
            assert is_production() is False
        finally:
            runtime_module._runtime_var.reset(token)
