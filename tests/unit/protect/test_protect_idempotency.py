"""Unit tests for the ``protect(idempotency_key=…)`` opt-in dedup facade (#564).

Covers, per the impl-doc Test Assessment:
- ``_read_context_field`` lookup precedence (named field → ``request_data`` →
  ``extra``; ``extra`` excluded; unresolved → ``None``).
- ``_field_key_generator`` str-form: namespacing, fail-fast on missing / ``None``
  / non-primitive value (and the ``None``-before-primitive ordering, since
  ``type(None)`` is itself an allowed primitive).
- ``_build_idempotency_stage`` state mapping (``None`` → None, str → pair,
  Callable → verbatim), the ``context is None`` and bad-field ``ValueError``
  fail-fasts, and the prod-no-adapter ``ConfigurationError`` (D5 fail-closed).
- ``_build_sync_composer`` / ``_build_async_composer`` wiring: guard+hook
  appended when supplied, fast-path cache non-collision, chain unchanged when
  omitted (composer introspection).
- ``protect`` / ``protect_with_meta`` / ``aprotect_with_meta`` and the
  ``@protected`` / ``@aprotected`` decorators: a duplicate key blocks
  re-execution (``PolicyOutcome.REJECTED``) against a real in-process cache;
  raw ``protect`` without a context fail-fasts; the decorator reads the wrapped
  function's arg and is incompatible with ``context_from=False``.
- The shared resolver's ``ConfigurationError`` message is feature-neutral (no
  ``@idempotent`` literal) now that the facade is a second raising caller.
- 595 D4: ``idempotency_ttl`` / ``idempotency_execution_ttl`` kwargs — guard
  threading, wiring-time ``ValueError`` on non-positive values, ignored
  without ``idempotency_key``, and end-to-end guard→gate/hook window delivery
  on the sync and async facades.
- 799 D1: a fallback that answered a failure releases the key (a genuine
  repeat runs the function again), a fallback that answered a timeout keeps
  refusing the repeat; 799 D2: in production the registry's in-process default
  is refused before the function runs, the escape hatch accepts it, and a call
  that refused runs on the shared cache once one is wired.

The guard/hook resolve a cache-backed gate via ``resolve_cache_via_registry``.
``ProviderRegistry.get_cache`` is patched to raise ``AdapterNotFoundError`` for
the whole module so resolution deterministically lands on the in-process
``_POLICY_FALLBACK_CACHE`` fallback, mirroring the @idempotent decorator tests.
The production-refusal class overrides that isolation to resolve the
registry's real in-process default instead.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.core.exceptions import (
    AdapterNotFoundError,
    BaldurError,
    ConfigurationError,
    IdempotencyDuplicateError,
    IdempotencyUnavailableError,
)
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.protect_facade import (
    _build_async_composer,
    _build_async_idempotency_stage,
    _build_idempotency_stage,
    _build_sync_composer,
    _field_key_generator,
    _finalize_value,
    _read_context_field,
    aprotect,
    aprotect_with_meta,
    aprotected,
    protect,
    protect_with_meta,
    protected,
    reset_protect_caches,
)
from baldur.resilience.policies.idempotency import (
    IdempotencyGuard,
    IdempotencyHook,
    _ensure_policy_gate,
)

# =============================================================================
# Isolation — reset the memoized policy gate + force the in-process fallback
# =============================================================================


@pytest.fixture(autouse=True)
def _isolate_protect_idempotency():
    """Reset protect/idempotency singletons and force the in-process fallback
    cache so each test starts from a clean dedup state."""
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings
    from baldur.settings.protect import reset_protect_settings

    def _reset() -> None:
        reset_protect_settings()
        reset_idempotency_settings()
        reset_runtime()
        reset_protect_caches()

    _reset()
    with patch(
        "baldur.factory.registry.ProviderRegistry.get_cache",
        side_effect=AdapterNotFoundError(adapter_type="cache"),
    ):
        yield
    _reset()


# =============================================================================
# _read_context_field — Behavior (lookup precedence)
# =============================================================================


class TestReadContextFieldBehavior:
    """``_read_context_field`` resolves a field name against a PolicyContext."""

    def test_named_field_present_is_returned(self):
        ctx = PolicyContext(order_id="o-1")
        assert _read_context_field(ctx, "order_id") == "o-1"

    def test_named_field_none_falls_through_to_request_data(self):
        # Given — order_id unset on the named field but present in request_data.
        ctx = PolicyContext(order_id=None, extra={"request_data": {"order_id": "r-9"}})
        # Then — request_data supplies the value.
        assert _read_context_field(ctx, "order_id") == "r-9"

    def test_request_data_wins_over_top_level_extra(self):
        ctx = PolicyContext(extra={"request_data": {"foo": "rd"}, "foo": "ex"})
        assert _read_context_field(ctx, "foo") == "rd"

    def test_non_named_field_resolved_from_top_level_extra(self):
        ctx = PolicyContext(extra={"tenant": "t-1"})
        assert _read_context_field(ctx, "tenant") == "t-1"

    def test_extra_itself_is_excluded_as_a_field_name(self):
        # ``extra`` is the open-ended container, never a key source.
        ctx = PolicyContext(extra={"k": 1})
        assert _read_context_field(ctx, "extra") is None

    def test_unresolved_field_returns_none(self):
        assert _read_context_field(PolicyContext(), "missing") is None


# =============================================================================
# _field_key_generator — Behavior (str field-name form)
# =============================================================================


class TestFieldKeyGeneratorBehavior:
    """``_field_key_generator`` builds the namespaced, fail-fast str-form key."""

    def test_present_field_returns_service_namespaced_key(self):
        gen = _field_key_generator("payment.charge", "order_id")
        assert gen(PolicyContext(order_id="o-42")) == "payment.charge:o-42"

    def test_same_value_different_service_yields_different_key(self):
        ctx = PolicyContext(order_id="o-42")
        key_a = _field_key_generator("svc.a", "order_id")(ctx)
        key_b = _field_key_generator("svc.b", "order_id")(ctx)
        assert key_a != key_b

    def test_missing_field_raises_value_error(self):
        gen = _field_key_generator("svc", "order_id")
        with pytest.raises(ValueError, match="no value"):
            gen(PolicyContext())

    def test_none_value_raises_before_primitive_check(self):
        # ``type(None)`` is in ALLOWED_PRIMITIVE_TYPES, so a None field must hit
        # the "no value" branch (None-check first), not produce ``svc:None``.
        gen = _field_key_generator("svc", "order_id")
        with pytest.raises(ValueError, match="no value"):
            gen(PolicyContext(order_id=None))

    def test_non_primitive_value_raises_value_error(self):
        gen = _field_key_generator("svc", "payload")
        ctx = PolicyContext(extra={"request_data": {"payload": {"nested": 1}}})
        with pytest.raises(ValueError, match="non-primitive"):
            gen(ctx)

    def test_primitive_int_value_is_accepted(self):
        gen = _field_key_generator("svc", "amount")
        ctx = PolicyContext(extra={"request_data": {"amount": 100}})
        assert gen(ctx) == "svc:100"


# =============================================================================
# _build_idempotency_stage — Behavior + Contract (resolution & fail-fast)
# =============================================================================


class TestBuildIdempotencyStageBehavior:
    """``_build_idempotency_stage`` maps the key spec to a guard+hook pair."""

    def test_none_key_returns_none_stage(self):
        assert _build_idempotency_stage("svc", None, None) is None

    def test_key_without_context_raises_value_error(self):
        with pytest.raises(ValueError, match="requires a PolicyContext"):
            _build_idempotency_stage("svc", "order_id", None)

    def test_str_form_returns_guard_and_hook_pair(self):
        stage = _build_idempotency_stage("svc", "order_id", PolicyContext(order_id="o"))
        assert stage is not None
        guard, hook = stage
        assert isinstance(guard, IdempotencyGuard)
        assert guard.name == "idempotency"
        assert isinstance(hook, IdempotencyHook)

    def test_str_form_bad_field_fails_fast_with_value_error(self):
        # Eager validation against the context — missing field raises here, not
        # swallowed into a silent no-op by the composer's fail-open guard loop.
        with pytest.raises(ValueError, match="no value"):
            _build_idempotency_stage("svc", "order_id", PolicyContext())

    def test_callable_form_used_verbatim_without_namespacing(self):
        def my_key(ctx: PolicyContext) -> str:
            return "custom-key"

        stage = _build_idempotency_stage("svc", my_key, PolicyContext())
        assert stage is not None
        guard, _hook = stage
        # The callable is the guard's key generator verbatim (no ``svc:`` prefix).
        assert guard._key_fn is my_key

    def test_callable_form_not_eagerly_invoked(self):
        # A callable may carry side effects; it must not be invoked at build time
        # (a runtime fault stays fail-open inside guard.check()).
        calls = {"n": 0}

        def my_key(ctx: PolicyContext) -> str:
            calls["n"] += 1
            return "k"

        _build_idempotency_stage("svc", my_key, PolicyContext())
        assert calls["n"] == 0

    def test_prod_no_adapter_raises_configuration_error_at_build(self, monkeypatch):
        # D5: the cache-backed gate is resolved at guard construction so a prod
        # misconfiguration fails closed out of the facade's composer build.
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        reset_idempotency_settings()
        reset_runtime()

        with pytest.raises(ConfigurationError):
            _build_idempotency_stage("svc", lambda c: "k", PolicyContext())


# =============================================================================
# _build_sync_composer — Behavior (wiring + fast-path bypass)
# =============================================================================


class TestSyncComposerIdempotencyBehavior:
    """The sync composer appends the guard+hook and bypasses the fast-path
    cache when an idempotency stage is supplied."""

    def test_guard_and_hook_appended_when_stage_supplied(self):
        stage = _build_idempotency_stage(
            "svc.wire", "order_id", PolicyContext(order_id="o")
        )
        guard, hook = stage
        composer = _build_sync_composer(
            name="svc.wire",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=None,
            idempotency_stage=stage,
        )
        assert guard in composer._guards
        assert hook in composer._hooks
        assert composer._guards[0].name == "idempotency"

    def test_no_idempotency_guard_or_hook_when_stage_omitted(self):
        composer = _build_sync_composer(
            name="svc.plain",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=None,
            idempotency_stage=None,
        )
        assert not any(g.name == "idempotency" for g in composer._guards)
        assert not any(isinstance(h, IdempotencyHook) for h in composer._hooks)

    def test_sync_composer_cache_non_collision_with_idempotency(self):
        # Default-kwargs calls share one cached composer ...
        c1 = _build_sync_composer(
            name="svc.cnc",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=None,
        )
        c2 = _build_sync_composer(
            name="svc.cnc",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=None,
        )
        assert c1 is c2  # cached fast-path

        # ... but an idempotency-enabled call never receives the cached
        # idempotency-less composer — it builds per-call.
        stage = _build_idempotency_stage(
            "svc.cnc", "order_id", PolicyContext(order_id="o")
        )
        c3 = _build_sync_composer(
            name="svc.cnc",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=True,
            timeout_seconds=None,
            idempotency_stage=stage,
        )
        assert c3 is not c1
        assert any(g.name == "idempotency" for g in c3._guards)
        assert not any(g.name == "idempotency" for g in c1._guards)


# =============================================================================
# _build_async_composer — Behavior (async parity)
# =============================================================================


class TestAsyncComposerIdempotencyBehavior:
    """The async composer wires the native async guard+hook pair (672 D4).

    The async facade builds the stage via ``_build_async_idempotency_stage``
    (``AsyncIdempotencyGuard`` / ``AsyncIdempotencyHook``); those are native
    async, so add-time normalization passes them through unchanged (identity
    preserved, zero thread hop)."""

    def test_guard_and_hook_appended_when_stage_supplied(self):
        stage = _build_async_idempotency_stage(
            "svc.async", lambda c: "k", PolicyContext()
        )
        guard, hook = stage
        composer = _build_async_composer(
            name="svc.async",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=False,
            timeout_seconds=None,
            idempotency_stage=stage,
        )
        # native async guard/hook → pass-through (not wrapped)
        assert guard in composer._guards
        assert hook in composer._hooks

    def test_no_idempotency_guard_or_hook_when_stage_omitted(self):
        composer = _build_async_composer(
            name="svc.async",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=False,
            timeout_seconds=None,
            idempotency_stage=None,
        )
        assert not any(g.name == "idempotency" for g in composer._guards)
        assert not any(isinstance(h, IdempotencyHook) for h in composer._hooks)


# =============================================================================
# protect / protect_with_meta — Behavior (real-cache dedup, fail-fast)
# =============================================================================


class TestProtectIdempotencyBehavior:
    """End-to-end dedup through the sync facade against a real in-process gate."""

    @pytest.mark.parametrize(
        "key_spec",
        ["order_id", lambda c: f"custom:{c.order_id}"],
        ids=["str_field", "callable"],
    )
    def test_protect_duplicate_blocked_returns_rejected(self, key_spec):
        # Given — a fn that records each execution.
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        # When — first call runs, second carries the same key.
        r1 = protect_with_meta(
            "svc.pdup",
            fn,
            idempotency_key=key_spec,
            context=PolicyContext(order_id="o-dup"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        r2 = protect_with_meta(
            "svc.pdup",
            fn,
            idempotency_key=key_spec,
            context=PolicyContext(order_id="o-dup"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        # Then — first succeeds, the duplicate is REJECTED without re-running fn.
        assert r1.success is True
        assert r2.outcome == PolicyOutcome.REJECTED
        assert calls["n"] == 1

    def test_protect_chain_unchanged_when_key_omitted(self):
        # Composer introspection: no idempotency guard/hook is added when the
        # key is omitted (chain identical to pre-#564).
        composer = _build_sync_composer(
            name="svc.cu",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=False,
            timeout_seconds=None,
            idempotency_stage=None,
        )
        assert not any(g.name == "idempotency" for g in composer._guards)
        assert not any(isinstance(h, IdempotencyHook) for h in composer._hooks)

    def test_protect_raw_without_context_raises_value_error(self):
        with pytest.raises(ValueError, match="requires a PolicyContext"):
            protect("svc.noctx", lambda: "x", idempotency_key="order_id")

    def test_protect_missing_field_raises_value_error(self):
        with pytest.raises(ValueError, match="no value"):
            protect(
                "svc.badfield",
                lambda: "x",
                idempotency_key="order_id",
                context=PolicyContext(),
            )

    def test_protect_raising_variant_duplicate_raises_idempotency_error(self):
        # The raising protect() surfaces a dedup-blocked REJECTED as the domain
        # IdempotencyDuplicateError (not a bare RuntimeError); fn runs once.
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        assert (
            protect(
                "svc.praise",
                fn,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                circuit_breaker=False,
            )
            == "ok"
        )
        with pytest.raises(IdempotencyDuplicateError) as exc_info:
            protect(
                "svc.praise",
                fn,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                circuit_breaker=False,
            )
        assert exc_info.value.decision == "SKIP"
        assert calls["n"] == 1

    def test_protect_inflight_duplicate_raises_abort(self):
        # #567 D1: a concurrent in-flight duplicate (gate state ``executing``)
        # is blocked. Reproduced deterministically without threads by
        # pre-acquiring the key on the shared in-process gate (Testability
        # Notes), so the facade's check_and_acquire sees ``executing`` → ABORT.
        from baldur.core.idempotency_gate import IdempotencyDecision

        key = "svc.pabort:o-abort"
        pre = _ensure_policy_gate().check_and_acquire(key)
        assert pre.decision == IdempotencyDecision.CONTINUE  # we now hold the key

        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        with pytest.raises(IdempotencyDuplicateError) as exc_info:
            protect(
                "svc.pabort",
                fn,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-abort"),
                circuit_breaker=False,
            )

        assert exc_info.value.decision == "ABORT"
        # The loser never runs the side effect.
        assert calls["n"] == 0


# =============================================================================
# aprotect_with_meta / @aprotected — Behavior (async parity)
# =============================================================================


class TestAprotectIdempotencyBehavior:
    """Dedup is meaningful on the async facade even without async retry."""

    @pytest.mark.asyncio
    async def test_aprotect_duplicate_blocked_returns_rejected(self):
        calls = {"n": 0}

        async def afn():
            calls["n"] += 1
            return "ok"

        r1 = await aprotect_with_meta(
            "svc.adup",
            afn,
            idempotency_key="order_id",
            context=PolicyContext(order_id="o-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        r2 = await aprotect_with_meta(
            "svc.adup",
            afn,
            idempotency_key="order_id",
            context=PolicyContext(order_id="o-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert r1.success is True
        assert r2.outcome == PolicyOutcome.REJECTED
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_aprotected_decorator_dedups_on_wrapped_arg(self):
        calls = {"n": 0}

        @aprotected("svc.adec", idempotency_key="order_id")
        async def op(order_id):
            calls["n"] += 1
            return f"ok:{order_id}"

        assert await op(order_id="o-1") == "ok:o-1"
        assert calls["n"] == 1
        # Duplicate → REJECTED → raising variant surfaces IdempotencyDuplicateError.
        with pytest.raises(IdempotencyDuplicateError):
            await op(order_id="o-1")
        assert calls["n"] == 1


# =============================================================================
# @protected — Behavior (reads wrapped-fn arg, context_from=False guard)
# =============================================================================


class TestProtectedDecoratorIdempotencyBehavior:
    """The decorator reads ``idempotency_key`` from the wrapped function's
    bound arguments and is incompatible with ``context_from=False``."""

    def test_decorator_keys_on_wrapped_order_id_arg(self):
        calls = {"n": 0}

        @protected("svc.dec", idempotency_key="order_id", circuit_breaker=False)
        def op(order_id):
            calls["n"] += 1
            return f"ok:{order_id}"

        # Same order_id → second blocked (REJECTED → IdempotencyDuplicateError);
        # fn ran once.
        assert op(order_id="o-1") == "ok:o-1"
        assert calls["n"] == 1
        with pytest.raises(IdempotencyDuplicateError):
            op(order_id="o-1")
        assert calls["n"] == 1
        # Different order_id → runs (proves the key is derived from the arg).
        assert op(order_id="o-2") == "ok:o-2"
        assert calls["n"] == 2

    def test_context_from_false_is_incompatible_with_idempotency_key(self):
        @protected("svc.dec2", idempotency_key="order_id", context_from=False)
        def op(order_id):
            return "x"

        with pytest.raises(ValueError, match="requires a PolicyContext"):
            op(order_id="o-1")


# =============================================================================
# resolve_cache_via_registry — Contract (feature-neutral message)
# =============================================================================


class TestResolverMessageFeatureNeutralContract:
    """The prod-no-adapter ConfigurationError must not name ``@idempotent`` now
    that the facade (layer="policy") is a second raising caller."""

    def test_policy_layer_message_is_feature_neutral(self, monkeypatch):
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.runtime import reset_runtime
        from baldur.services.idempotency._cache_resolver import (
            resolve_cache_via_registry,
        )
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        reset_idempotency_settings()
        reset_runtime()

        with pytest.raises(ConfigurationError) as exc_info:
            resolve_cache_via_registry(
                layer="policy",
                fallback_cache=InMemoryCacheAdapter(key_prefix="x:"),
                raise_on_prod_no_toggle=True,
            )

        message = str(exc_info.value)
        assert "@idempotent" not in message
        # Operator-actionable substrings preserved.
        assert "ProviderRegistry" in message
        assert "production" in message.lower()
        assert "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK" in message

    @pytest.mark.parametrize(
        "registry_state",
        ["no_adapter", "in_process_default", "non_redis_on_async_path"],
        ids=["no_adapter", "in_process_default", "non_redis_on_async_path"],
    )
    def test_refusal_message_names_every_fix(self, monkeypatch, registry_state):
        """799 D2: whichever registry state is refused — including a non-Redis
        adapter on the async path (verify V1) — the message opens with the text
        the troubleshooting entry is keyed on, names each fix
        (``baldur.init()`` with ``BALDUR_REDIS_URL``, an adapter via
        ``ProviderRegistry``, the escape hatch) and stays feature-neutral."""
        from baldur.adapters.cache.async_memory_adapter import (
            AsyncInMemoryCacheAdapter,
        )
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.runtime import reset_runtime
        from baldur.services.idempotency._cache_resolver import (
            resolve_async_cache,
            resolve_cache_via_registry,
        )
        from baldur.settings.idempotency import reset_idempotency_settings
        from tests.factories.cache_doubles import NonRedisCacheStandIn

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        reset_idempotency_settings()
        reset_runtime()
        if registry_state == "no_adapter":
            registry = patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                side_effect=AdapterNotFoundError(adapter_type="cache"),
            )
        elif registry_state == "in_process_default":
            registry = patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                return_value=InMemoryCacheAdapter(key_prefix="default:"),
            )
        else:
            registry = patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                return_value=NonRedisCacheStandIn(key_prefix="shared:"),
            )

        with registry, pytest.raises(ConfigurationError) as exc_info:
            if registry_state == "non_redis_on_async_path":
                resolve_async_cache(
                    layer="policy",
                    sync_fallback_cache=InMemoryCacheAdapter(key_prefix="x:"),
                    async_fallback_cache=AsyncInMemoryCacheAdapter(key_prefix="x:"),
                    raise_on_prod_no_toggle=True,
                )
            else:
                resolve_cache_via_registry(
                    layer="policy",
                    fallback_cache=InMemoryCacheAdapter(key_prefix="x:"),
                    raise_on_prod_no_toggle=True,
                )

        message = str(exc_info.value)
        assert message.startswith(
            "Baldur idempotency requires a distributed cache adapter in production"
        )
        assert "baldur.init()" in message
        assert "BALDUR_REDIS_URL" in message
        assert "ProviderRegistry" in message
        assert "production" in message.lower()
        assert "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK" in message
        assert "@idempotent" not in message


# =============================================================================
# protect / aprotect — D9 cache-error fail direction (Behavior, §8.1 Boundary)
# =============================================================================


class TestProtectIdempotencyFailDirectionBehavior:
    """#567 D9: a cache I/O error during dedup fails CLOSED by default — the
    raising facade surfaces ``IdempotencyUnavailableError`` (distinct from a
    deduped ``IdempotencyDuplicateError``). ``idempotency_fail_open=True``
    restores fail-open (the unverifiable check proceeds)."""

    def test_cache_error_fails_closed_raises_unavailable(self):
        from baldur.core.idempotency_gate import IdempotencyGate

        def fn():
            return "ok"

        with patch.object(
            IdempotencyGate,
            "check_and_acquire",
            side_effect=RuntimeError("redis down"),
        ):
            with pytest.raises(IdempotencyUnavailableError) as exc_info:
                protect(
                    "svc.fc",
                    fn,
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="o-1"),
                    circuit_breaker=False,
                )

        assert exc_info.value.key == "svc.fc:o-1"

    def test_per_call_fail_open_allows_through_on_cache_error(self):
        from baldur.core.idempotency_gate import IdempotencyGate

        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        with patch.object(
            IdempotencyGate,
            "check_and_acquire",
            side_effect=RuntimeError("redis down"),
        ):
            result = protect(
                "svc.fo",
                fn,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                idempotency_fail_open=True,
                circuit_breaker=False,
            )

        assert result == "ok"
        assert calls["n"] == 1


# =============================================================================
# @idempotent ↔ protect parity (Contract)
# =============================================================================


class TestIdempotencyDuplicateParityContract:
    """#567 D3/D8: the ``@idempotent`` decorator and ``protect(idempotency_key=)``
    raise the SAME exception type (``IdempotencyDuplicateError``) with the same
    ``.decision`` on an in-flight (ABORT) duplicate."""

    def test_decorator_and_facade_raise_same_type_on_inflight_duplicate(self):
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )
        from baldur.decorators.idempotent import idempotent

        @idempotent(key_args=["order_id"])
        def dec_op(order_id: str) -> str:
            return "ok"

        def fac_fn():
            return "ok"

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
            return_value=IdempotencyCheckResult(decision=IdempotencyDecision.ABORT),
        ):
            with pytest.raises(IdempotencyDuplicateError) as dec_exc:
                dec_op("o-1")
            with pytest.raises(IdempotencyDuplicateError) as fac_exc:
                protect(
                    "svc.parity",
                    fac_fn,
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="o-1"),
                    circuit_breaker=False,
                )

        assert type(dec_exc.value) is type(fac_exc.value) is IdempotencyDuplicateError
        assert dec_exc.value.decision == "ABORT"
        assert fac_exc.value.decision == "ABORT"


# =============================================================================
# _finalize_value — REJECTED reject-mapping (Behavior, §8 state mapping)
# =============================================================================


class TestFinalizeValueMappingBehavior:
    """#567 D3: ``_finalize_value`` maps a guard short-circuit (REJECTED, no
    captured error) to a precise domain exception instead of a bare
    ``RuntimeError``."""

    def test_success_returns_value(self):
        result = PolicyResult(value="v", outcome=PolicyOutcome.SUCCESS)
        assert _finalize_value(result) == "v"

    def test_captured_error_is_reraised(self):
        err = ValueError("boom")
        result = PolicyResult(value=None, outcome=PolicyOutcome.FAILURE, error=err)
        with pytest.raises(ValueError, match="boom"):
            _finalize_value(result)

    @pytest.mark.parametrize("decision", ["SKIP", "ABORT"], ids=["skip", "abort"])
    def test_idempotency_reject_maps_to_duplicate_error(self, decision):
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            metadata={
                "rejected_by": "idempotency",
                "idempotency_decision": decision,
                "idempotency_key": "svc:k",
            },
        )
        with pytest.raises(IdempotencyDuplicateError) as exc_info:
            _finalize_value(result)
        assert exc_info.value.decision == decision
        assert exc_info.value.key == "svc:k"

    def test_unavailable_marker_maps_to_unavailable_error(self):
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            metadata={
                "rejected_by": "idempotency",
                "idempotency_unavailable": True,
                "idempotency_key": "svc:k",
                "error": "redis down",
            },
        )
        with pytest.raises(IdempotencyUnavailableError) as exc_info:
            _finalize_value(result)
        assert exc_info.value.key == "svc:k"
        assert "redis down" in exc_info.value.error

    def test_non_idempotency_reject_maps_to_baldur_error_not_runtime(self):
        # G3 fix: the defensive fallback raises a domain BaldurError (base
        # type), not a bare RuntimeError.
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            metadata={"rejected_by": "some_other_guard"},
        )
        with pytest.raises(BaldurError) as exc_info:
            _finalize_value(result)
        assert type(exc_info.value) is BaldurError
        assert not isinstance(exc_info.value, IdempotencyDuplicateError)


# =============================================================================
# protect_with_meta — D4 caller-signal markers (Behavior, §8.4 metadata)
# =============================================================================


class TestProtectWithMetaIdempotencyBehavior:
    """#567 D4: ``protect_with_meta`` surfaces SKIP / ABORT / cache-unavailable
    via ``ProtectResult.metadata`` so a non-raising caller can distinguish
    "already completed" from "in progress, retry later" from "couldn't verify"."""

    def test_skip_decision_marker_reaches_metadata(self):
        def fn():
            return "ok"

        protect_with_meta(
            "svc.m",
            fn,
            idempotency_key="order_id",
            context=PolicyContext(order_id="o-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        r2 = protect_with_meta(
            "svc.m",
            fn,
            idempotency_key="order_id",
            context=PolicyContext(order_id="o-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert r2.outcome == PolicyOutcome.REJECTED
        assert r2.metadata["idempotency_decision"] == "SKIP"
        assert r2.metadata["idempotency_key"] == "svc.m:o-1"

    def test_abort_decision_marker_reaches_metadata(self):
        from baldur.core.idempotency_gate import IdempotencyDecision

        key = "svc.mabort:o-1"
        assert (
            _ensure_policy_gate().check_and_acquire(key).decision
            == IdempotencyDecision.CONTINUE
        )

        def fn():
            return "ok"

        result = protect_with_meta(
            "svc.mabort",
            fn,
            idempotency_key="order_id",
            context=PolicyContext(order_id="o-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata["idempotency_decision"] == "ABORT"

    def test_cache_unavailable_marker_reaches_metadata(self):
        from baldur.core.idempotency_gate import IdempotencyGate

        def fn():
            return "ok"

        with patch.object(
            IdempotencyGate,
            "check_and_acquire",
            side_effect=RuntimeError("redis down"),
        ):
            result = protect_with_meta(
                "svc.munavail",
                fn,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata.get("idempotency_unavailable") is True


# =============================================================================
# 595 D4 — idempotency_ttl / idempotency_execution_ttl kwargs
# =============================================================================


class TestProtectIdempotencyTtlContract:
    """595 D4 wiring-time fail-fast: a non-positive / non-timedelta window
    raises ``ValueError`` in ``_build_idempotency_stage``; both TTL kwargs are
    ignored without ``idempotency_key`` (the ``idempotency_fail_open``
    precedent — the stage returns ``None`` before any of them is read)."""

    @pytest.mark.parametrize(
        "kwarg",
        ["idempotency_ttl", "idempotency_execution_ttl"],
        ids=["ttl", "execution_ttl"],
    )
    @pytest.mark.parametrize(
        "value",
        [timedelta(0), timedelta(seconds=-1), 300],
        ids=["zero", "negative", "non_timedelta"],
    )
    def test_non_positive_window_raises_value_error_at_build(self, kwarg, value):
        with pytest.raises(ValueError, match="positive timedelta"):
            _build_idempotency_stage(
                "svc",
                "order_id",
                PolicyContext(order_id="o"),
                **{kwarg: value},
            )

    def test_ttl_kwargs_ignored_without_idempotency_key(self):
        """Even an invalid window does not raise when no key is requested."""
        stage = _build_idempotency_stage(
            "svc",
            None,
            None,
            idempotency_ttl=timedelta(0),
            idempotency_execution_ttl=timedelta(0),
        )
        assert stage is None


class TestProtectIdempotencyTtlBehavior:
    """595 D4: the TTL kwargs thread facade → ``IdempotencyGuard`` and reach
    the gate — ``execution_ttl`` at ``check_and_acquire``, the memory ``ttl``
    at ``mark_completed`` via ``context.extra`` (guard is the single source)."""

    _MEM_TTL = timedelta(hours=2)
    _EXEC_TTL = timedelta(minutes=5)

    def test_ttl_kwargs_thread_to_guard(self):
        stage = _build_idempotency_stage(
            "svc.ttl",
            "order_id",
            PolicyContext(order_id="o"),
            idempotency_ttl=self._MEM_TTL,
            idempotency_execution_ttl=self._EXEC_TTL,
        )

        assert stage is not None
        guard, _hook = stage
        assert guard._ttl is self._MEM_TTL
        assert guard._execution_ttl is self._EXEC_TTL

    def test_protect_with_meta_delivers_windows_to_gate(self):
        """End-to-end sync facade: execution window reaches the acquire, the
        memory window reaches the completion mark."""
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
            IdempotencyGate,
        )

        with (
            patch.object(
                IdempotencyGate,
                "check_and_acquire",
                autospec=True,
                return_value=IdempotencyCheckResult(
                    decision=IdempotencyDecision.CONTINUE
                ),
            ) as mock_check,
            patch.object(IdempotencyGate, "mark_completed", autospec=True) as mock_mark,
        ):
            result = protect_with_meta(
                "svc.ttl.sync",
                lambda: "ok",
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                idempotency_ttl=self._MEM_TTL,
                idempotency_execution_ttl=self._EXEC_TTL,
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )

        assert result.success is True
        assert mock_check.call_args.kwargs["ttl"] is self._EXEC_TTL
        assert mock_mark.call_args.kwargs["ttl"] is self._MEM_TTL

    @pytest.mark.asyncio
    async def test_aprotect_with_meta_delivers_windows_to_gate(self):
        """End-to-end async facade parity for both windows.

        672 D4/D5: the async facade drives the native ``AsyncIdempotencyGate``
        (awaited), not the sync gate — so the windows are asserted against the
        async gate's ``check_and_acquire`` / ``mark_completed``.
        """
        from baldur.core.idempotency_gate import (
            AsyncIdempotencyGate,
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        async def afn():
            return "ok"

        with (
            patch.object(
                AsyncIdempotencyGate,
                "check_and_acquire",
                autospec=True,
                return_value=IdempotencyCheckResult(
                    decision=IdempotencyDecision.CONTINUE
                ),
            ) as mock_check,
            patch.object(
                AsyncIdempotencyGate, "mark_completed", autospec=True
            ) as mock_mark,
        ):
            result = await aprotect_with_meta(
                "svc.ttl.async",
                afn,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                idempotency_ttl=self._MEM_TTL,
                idempotency_execution_ttl=self._EXEC_TTL,
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )

        assert result.success is True
        assert mock_check.call_args.kwargs["ttl"] is self._EXEC_TTL
        assert mock_mark.call_args.kwargs["ttl"] is self._MEM_TTL

    def test_failure_path_delivers_memory_window_to_mark_failed(self):
        """The same threaded memory window reaches mark_failed when the
        protected call raises."""
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
            IdempotencyGate,
        )

        def boom():
            raise RuntimeError("boom")

        with (
            patch.object(
                IdempotencyGate,
                "check_and_acquire",
                autospec=True,
                return_value=IdempotencyCheckResult(
                    decision=IdempotencyDecision.CONTINUE
                ),
            ),
            patch.object(IdempotencyGate, "mark_failed", autospec=True) as mock_mark,
        ):
            result = protect_with_meta(
                "svc.ttl.fail",
                boom,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                idempotency_ttl=self._MEM_TTL,
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )

        assert result.success is False
        assert mock_mark.call_args.kwargs["ttl"] is self._MEM_TTL


# =============================================================================
# 799 D1 — a fallback's answer: released after a failure, held after a timeout
# =============================================================================

# A facade timeout that fires long before a held function would finish.
_TIMEOUT_FIRES_S = 0.05
# The sync facade's timeout when the held function must already be running as
# it fires: a future the executor has not started yet is cancelled and never
# runs, so the timeout leaves a scheduling margin for the worker thread to pick
# the call up. The async facade starts the coroutine before its timer can fire.
_SYNC_TIMEOUT_FIRES_S = 1.0
# Upper bound on any wait for a held function to enter or be released.
_HOLD_S = 5.0

# The facade's own retry / breaker / DLQ stay off so the only stages are the
# ones under test.
_BARE = {"circuit_breaker": False, "retry": False, "dlq": False}


class TestProtectFallbackAnswerBehavior:
    """799 D1 end to end: guard → composer → hook → gate over the real
    in-process cache. When the function raised and the fallback answered, the
    key is released, so a genuine repeat runs the function again."""

    def test_protect_fallback_answer_releases_key_repeat_runs_function(self):
        # Given — a charge that fails every time, answered by a fallback.
        calls = {"n": 0}

        def charge():
            calls["n"] += 1
            raise ConnectionError("payment gateway refused")

        def call():
            return protect(
                "svc.fa",
                charge,
                fallback=lambda: {"status": "unavailable"},
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        # When — the customer retries with the same key.
        first = call()
        repeat = call()

        # Then — both got the fallback's answer and the charge ran twice.
        assert first == repeat == {"status": "unavailable"}
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_aprotect_fallback_answer_releases_key_repeat_runs_function(self):
        calls = {"n": 0}

        async def charge():
            calls["n"] += 1
            raise ConnectionError("payment gateway refused")

        async def unavailable():
            return {"status": "unavailable"}

        async def call():
            return await aprotect(
                "svc.afa",
                charge,
                fallback=unavailable,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        first = await call()
        repeat = await call()

        assert first == repeat == {"status": "unavailable"}
        assert calls["n"] == 2

    def test_protect_fallback_answer_after_rejected_results_releases_key(self):
        """A value the retry's ``retry_on_result`` still rejects when the
        attempts run out counts as a failure: the fallback's answer releases
        the key like a raise, although the function never raised."""
        from baldur.services.retry_handler.models import RetryPolicyConfig

        # Given — a charge that keeps returning a soft error the predicate
        # rejects, answered by a fallback once the retries run out.
        calls = {"n": 0}
        max_attempts = 2

        def charge():
            calls["n"] += 1
            return {"status": "pending"}

        retry = RetryPolicyConfig(
            domain="svc.farr",
            max_attempts=max_attempts,
            backoff_base=0,
            retry_on_result=lambda r: r["status"] == "pending",
        )

        def call():
            return protect(
                "svc.farr",
                charge,
                retry=retry,
                fallback=lambda: {"status": "unavailable"},
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                circuit_breaker=False,
                dlq=False,
            )

        # When
        first = call()
        repeat = call()

        # Then — the repeat was not refused: every attempt ran again.
        assert first == repeat == {"status": "unavailable"}
        assert calls["n"] == 2 * max_attempts


class TestProtectTimeoutFallbackBehavior:
    """805 D10: a fallback that answered a timeout holds the key while the
    timed-out work still runs — a same-key repeat is refused with ``ABORT``
    instead of running beside it — and an async timeout, which cancels the
    coroutine, leaves nothing running, so the key is released at once."""

    def test_protect_sync_timeout_fallback_holds_key_while_function_runs(self):
        # Given — a charge that keeps running past the facade's timeout.
        calls = {"n": 0}
        entered = threading.Event()
        release = threading.Event()

        def charge():
            calls["n"] += 1
            entered.set()
            release.wait(timeout=_HOLD_S)
            return "charged"

        def call():
            return protect(
                "svc.tf",
                charge,
                fallback=lambda: "pending",
                timeout=_SYNC_TIMEOUT_FIRES_S,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        try:
            # When — the timeout's fallback answers; the repeat arrives while
            # the abandoned charge is still held.
            first = call()
            assert entered.wait(timeout=_HOLD_S)
            with pytest.raises(IdempotencyDuplicateError) as exc_info:
                call()
        finally:
            release.set()

        # Then — the repeat was refused as in flight and the charge body ran
        # once.
        assert first == "pending"
        assert exc_info.value.decision == "ABORT"
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_aprotect_timeout_fallback_releases_key(self):
        # Given — a charge whose cancellation propagates unchanged.
        calls = {"n": 0}

        async def charge():
            calls["n"] += 1
            await asyncio.Event().wait()  # only the timeout's cancel ends it
            return "charged"

        async def pending():
            return "pending"

        async def call():
            return await aprotect(
                "svc.atf",
                charge,
                fallback=pending,
                timeout=_TIMEOUT_FIRES_S,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        # When
        first = await call()
        repeat = await call()

        # Then — the cancelled charge left nothing running: the repeat ran.
        assert first == repeat == "pending"
        assert calls["n"] == 2


# =============================================================================
# 799 D2 — production refuses the registry's in-process default
# =============================================================================


def _enter_production(monkeypatch) -> None:
    """Production runtime with the escape hatch off (its default)."""
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings

    monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
    monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
    reset_idempotency_settings()
    reset_runtime()


def _escape_hatch_on():
    """Accept in-process dedup in production. Patches the cached layered seam
    rather than the env base, so a present runtime-config manager cannot pin
    its default over the flag."""
    from baldur.settings.idempotency import IdempotencySettings

    return patch(
        "baldur.settings.layered_provider.get_layered_settings_cached",
        return_value=IdempotencySettings(allow_inmemory_fallback=True),
    )


class TestProtectIdempotencyInProcessDefaultBehavior:
    """799 D2 end to end: a production process whose registry still holds the
    in-process default (``init()`` skipped, or failed before wiring the cache)
    refuses the first guarded call with ``ConfigurationError`` before the
    function runs, on the sync and the async facade. The escape hatch accepts
    it. The refusal memoizes nothing, so once a shared cache is wired the same
    call runs on it."""

    @pytest.fixture
    def _isolate_protect_idempotency(self):
        """Overrides the module isolation: ``get_cache`` is NOT patched, so
        resolution sees the registry's real in-process default."""
        from baldur.factory.registry import ProviderRegistry
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings
        from baldur.settings.protect import reset_protect_settings

        def _reset() -> None:
            reset_protect_settings()
            reset_idempotency_settings()
            reset_runtime()
            reset_protect_caches()

        _reset()
        with ProviderRegistry.cache.snapshot():
            ProviderRegistry.cache.set_default("memory")
            yield
        _reset()

    @staticmethod
    def _counting_charge(calls):
        def charge():
            calls["n"] += 1
            return "charged"

        return charge

    @staticmethod
    def _counting_acharge(calls):
        async def charge():
            calls["n"] += 1
            return "charged"

        return charge

    def test_protect_in_process_default_in_production_raises_before_function_runs(
        self, monkeypatch
    ):
        _enter_production(monkeypatch)
        calls = {"n": 0}

        with pytest.raises(ConfigurationError):
            protect(
                "svc.ipd",
                self._counting_charge(calls),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_aprotect_in_process_default_in_production_raises_before_function_runs(
        self, monkeypatch
    ):
        _enter_production(monkeypatch)
        calls = {"n": 0}

        with pytest.raises(ConfigurationError):
            await aprotect(
                "svc.aipd",
                self._counting_acharge(calls),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        assert calls["n"] == 0

    def test_protect_in_process_default_escape_hatch_on_runs_function(
        self, monkeypatch
    ):
        _enter_production(monkeypatch)
        calls = {"n": 0}

        with _escape_hatch_on():
            result = protect(
                "svc.ipde",
                self._counting_charge(calls),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        assert result == "charged"
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_aprotect_in_process_default_escape_hatch_on_runs_function(
        self, monkeypatch
    ):
        _enter_production(monkeypatch)
        calls = {"n": 0}

        with _escape_hatch_on():
            result = await aprotect(
                "svc.aipde",
                self._counting_acharge(calls),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        assert result == "charged"
        assert calls["n"] == 1

    def test_protect_after_init_refused_call_runs_on_wired_shared_cache(
        self, monkeypatch
    ):
        from baldur.factory.registry import ProviderRegistry
        from tests.factories.cache_doubles import DistributedCacheStandIn

        # Given — production, the registry still on its in-process default.
        _enter_production(monkeypatch)
        calls = {"n": 0}
        shared = DistributedCacheStandIn(key_prefix="shared:")

        def call():
            return protect(
                "svc.ai",
                self._counting_charge(calls),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        with pytest.raises(ConfigurationError):
            call()

        # When — a shared cache is wired, then the same call is made again.
        with ProviderRegistry.cache.override(shared):
            result = call()

        # Then — it ran once, and its dedup record lives on the shared cache.
        assert result == "charged"
        assert calls["n"] == 1
        assert shared.get("svc.ai:o-1")["status"] == "completed"

    @pytest.mark.asyncio
    async def test_aprotect_after_init_refused_call_runs_on_wired_shared_cache(
        self, monkeypatch
    ):
        """The stand-in reports a Redis provider, so the async resolver builds
        an async Redis adapter for it; that constructor is stubbed with an
        async in-process store standing in for the same shared server."""
        from baldur.adapters.cache.async_memory_adapter import (
            AsyncInMemoryCacheAdapter,
        )
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.idempotency import _cache_resolver as resolver_module
        from tests.factories.cache_doubles import DistributedCacheStandIn

        # Given
        _enter_production(monkeypatch)
        calls = {"n": 0}
        async_shared = AsyncInMemoryCacheAdapter(key_prefix="shared:")

        async def call():
            return await aprotect(
                "svc.aai",
                self._counting_acharge(calls),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        with pytest.raises(ConfigurationError):
            await call()

        # When
        with (
            ProviderRegistry.cache.override(
                DistributedCacheStandIn(key_prefix="shared:")
            ),
            patch(
                "baldur.adapters.cache.async_redis_adapter.AsyncRedisCacheAdapter",
                new=lambda: async_shared,
            ),
            patch.object(resolver_module, "_register_async_pool_drain", autospec=True),
        ):
            result = await call()

        # Then
        assert result == "charged"
        assert calls["n"] == 1
        record = await async_shared.aget("svc.aai:o-1")
        assert record["status"] == "completed"

    @pytest.mark.asyncio
    async def test_aprotect_non_redis_shared_cache_in_production_raises_before_function_runs(
        self, monkeypatch
    ):
        """799 verify V1: a distributed adapter that is not Redis serves the
        sync facade, but the async path shares its ledger through Redis only —
        in production the async call refuses instead of deduping per worker,
        while the sync call on the same key runs on the shared adapter."""
        from baldur.factory.registry import ProviderRegistry
        from tests.factories.cache_doubles import NonRedisCacheStandIn

        # Given — production, a non-Redis distributed adapter wired.
        _enter_production(monkeypatch)
        calls = {"n": 0}
        shared = NonRedisCacheStandIn(key_prefix="shared:")

        with ProviderRegistry.cache.override(shared):
            # When — the async call first, then the sync call.
            with pytest.raises(ConfigurationError):
                await aprotect(
                    "svc.anr",
                    self._counting_acharge(calls),
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="o-1"),
                    **_BARE,
                )
            async_runs = calls["n"]
            result = protect(
                "svc.nr",
                self._counting_charge(calls),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        # Then — the async function never ran; the sync call ran on the
        # shared adapter.
        assert async_runs == 0
        assert result == "charged"
        assert calls["n"] == 1
        assert shared.get("svc.nr:o-1")["status"] == "completed"
