"""Unit tests for ``@protected`` / ``@dlq_protect`` context auto-capture (#504).

Verifies the helper ``_build_context_from_callsite`` and its decoration-time
cache ``_precompute_signature_cache`` together with the
``protected()`` / ``aprotected()`` wrappers and the ``@dlq_protect`` preset
forwarding.

Verification techniques (per the unit test guidelines §8):
- Behavior: dependency_interaction (helper output → protect() ``context=`` kwarg).
- Behavior: side_effects (DEBUG ``dlq_protect.context_capture_skipped`` log
  per non-primitive drop; WARNING ``dlq_protect.context_capture_failed`` on
  ``bind_partial`` TypeError).
- Contract: state_transition (``context_from is False`` → return None).
- Behavior: concurrency_isolation (two ``asyncio.gather`` invocations
  produce two distinct ``PolicyContext`` instances — fresh ``extra`` per call).
- Behavior: composition (``@dlq_protect`` + ``@idempotent`` decorator stack —
  ``__wrapped__`` keeps the raw signature introspectable).
"""

# NOTE: do NOT use ``from __future__ import annotations`` here. The helper's
# decoration-time annotation gate reads
# ``inspect.signature(...).parameters[name].annotation`` and only treats real
# ``type`` objects as primitives — PEP 563 string annotations would always
# fall through to the runtime ``isinstance`` gate, masking dual-gate behaviour.

import asyncio
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.decorators import dlq_protect
from baldur.interfaces.resilience_policy import PolicyContext
from baldur.protect_facade import (
    _CONTEXT_AUTO_EXTRACT_FIELDS,
    _build_context_from_callsite,
    _precompute_signature_cache,
    aprotected,
    protected,
)


def _cache_for(func):
    """Helper — return (sig, annotated_primitive) for a wrapped function."""
    return _precompute_signature_cache(func)


@pytest.fixture(autouse=True)
def _reset_protect_settings_singleton():
    """Each test starts with fresh ProtectSettings + composer caches."""
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()
    yield
    reset_protect_settings()


# =============================================================================
# Contract — _CONTEXT_AUTO_EXTRACT_FIELDS pinning (#504 D5)
# =============================================================================


class TestAutoExtractFieldsContract:
    """D5: only the named-field set ``(order_id, user_id)`` is written to the
    PolicyContext named fields — ``payment_id`` is dropped to extras only
    because it has zero downstream consumers."""

    def test_auto_extract_fields_is_order_id_and_user_id(self):
        assert _CONTEXT_AUTO_EXTRACT_FIELDS == ("order_id", "user_id")

    def test_payment_id_is_not_in_auto_extract_fields(self):
        assert "payment_id" not in _CONTEXT_AUTO_EXTRACT_FIELDS


# =============================================================================
# Behavior — auto-extract (context_from=None)
# =============================================================================


class TestBuildContextAutoExtractBehavior:
    """Default ``context_from=None`` → bind_partial + apply_defaults, populate
    named fields (order_id, user_id) and the full primitive snapshot in
    ``extra["request_data"]``."""

    def test_auto_extract_populates_named_fields_for_known_consumers(self):
        def fn(order_id: str, user_id: str, amount: int) -> None: ...

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, ("o-1", "u-7", 100), {}, None, ann)

        assert ctx is not None
        assert ctx.order_id == "o-1"
        assert ctx.user_id == "u-7"

    def test_auto_extract_writes_full_primitive_snapshot_to_request_data(self):
        def fn(order_id: str, user_id: str, amount: int) -> None: ...

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, ("o-1", "u-7", 100), {}, None, ann)

        assert ctx is not None
        # request_data carries every primitive bound arg — including ones
        # also reflected in named fields (operator search uses JSON path).
        assert ctx.extra["request_data"] == {
            "order_id": "o-1",
            "user_id": "u-7",
            "amount": 100,
        }

    def test_auto_extract_payment_id_flows_to_extras_only(self):
        """D5: payment_id has zero downstream named-field consumers; the
        auto-extract must NOT promote it to ``PolicyContext.payment_id``."""

        def fn(payment_id: str, amount: int) -> None: ...

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, ("pay-1", 250), {}, None, ann)

        assert ctx is not None
        # payment_id is dropped from the named field (no consumer)
        assert ctx.payment_id is None
        # but still present in request_data for operator JSON-path search
        assert ctx.extra["request_data"] == {
            "payment_id": "pay-1",
            "amount": 250,
        }

    def test_auto_extract_applies_defaults_for_omitted_args(self):
        """``apply_defaults()`` populates parameters with default values even
        when the caller omits them — required so DLQ entries see e.g.
        ``user_id=0`` rather than a missing key."""

        def fn(order_id: str = "o-default", user_id: int = 0) -> None: ...

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, (), {}, None, ann)

        assert ctx is not None
        assert ctx.order_id == "o-default"
        assert ctx.extra["request_data"] == {
            "order_id": "o-default",
            "user_id": 0,
        }

    def test_auto_extract_returns_fresh_extra_dict_per_invocation(self):
        """D5: ``PolicyContext`` is frozen but ``extra`` is a mutable dict —
        each call must produce a fresh dict so cross-request leak is impossible."""

        def fn(order_id: str) -> None: ...

        sig, ann = _cache_for(fn)
        ctx1 = _build_context_from_callsite(sig, ("o-1",), {}, None, ann)
        ctx2 = _build_context_from_callsite(sig, ("o-2",), {}, None, ann)

        assert ctx1 is not None
        assert ctx2 is not None
        assert ctx1.extra is not ctx2.extra
        assert ctx1.extra["request_data"] is not ctx2.extra["request_data"]

    def test_auto_extract_with_no_primitive_args_yields_empty_request_data(self):
        """A function whose only argument is a non-primitive (e.g. DRF Request)
        still produces ``extra["request_data"] == {}`` — never a missing key."""

        class _NotPrimitive:
            pass

        def fn(req) -> None: ...  # no annotation → runtime gate

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, (_NotPrimitive(),), {}, None, ann)

        assert ctx is not None
        assert ctx.extra["request_data"] == {}


# =============================================================================
# Contract — opt-out via False sentinel (#504 D6)
# =============================================================================


class TestBuildContextOptOutContract:
    """``context_from=False`` short-circuits to ``None`` so privacy-sensitive
    callsites preserve today's empty-context behaviour."""

    def test_returns_none_when_context_from_is_false(self):
        def fn(secret: str) -> None: ...

        sig, ann = _cache_for(fn)
        assert _build_context_from_callsite(sig, ("p@ss",), {}, False, ann) is None

    def test_false_sentinel_skips_bind_partial(self):
        """Even when bind would fail, the opt-out path never invokes it —
        the helper returns None before any introspection happens."""

        def fn(only_one_arg: str) -> None: ...

        sig, ann = _cache_for(fn)
        # Passing too many positional args would normally TypeError out of
        # bind_partial, but the False sentinel short-circuits before then.
        assert (
            _build_context_from_callsite(sig, ("a", "b", "c"), {}, False, ann) is None
        )


# =============================================================================
# Contract — custom callable (#504 D1)
# =============================================================================


class TestBuildContextCallableContract:
    """``context_from`` as a ``Callable[..., PolicyContext]`` is invoked with
    the wrapped function's call args; non-PolicyContext returns raise TypeError."""

    def test_callable_invoked_with_wrapped_call_args(self):
        seen = {}

        def custom(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return PolicyContext(order_id="custom-o", user_id="custom-u")

        def fn(a: int, b: int, *, c: str) -> None: ...

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, (1, 2), {"c": "x"}, custom, ann)

        assert seen["args"] == (1, 2)
        assert seen["kwargs"] == {"c": "x"}
        assert ctx is not None
        assert ctx.order_id == "custom-o"
        assert ctx.user_id == "custom-u"

    def test_callable_returning_non_policy_context_raises_typeerror(self):
        def bad_custom(*args, **kwargs) -> dict:
            return {"order_id": "o-bad"}

        def fn(x: int) -> None: ...

        sig, ann = _cache_for(fn)
        with pytest.raises(TypeError, match="must return PolicyContext"):
            _build_context_from_callsite(sig, (1,), {}, bad_custom, ann)


# =============================================================================
# Behavior — primitive dual-gate (#504 D8)
# =============================================================================


class TestBuildContextPrimitiveDualGateBehavior:
    """Annotation-True is trusted (skip isinstance); missing / generic
    annotations fall back to runtime ``isinstance(value, ALLOWED_PRIMITIVE_TYPES)``."""

    def test_unannotated_primitive_value_passes_runtime_gate(self):
        def fn(x, y) -> None: ...  # no annotations

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, (1, "hello"), {}, None, ann)

        assert ctx is not None
        assert ctx.extra["request_data"] == {"x": 1, "y": "hello"}

    def test_unannotated_non_primitive_value_dropped_by_runtime_gate(self):
        def fn(x) -> None: ...  # no annotation

        sig, ann = _cache_for(fn)
        ctx = _build_context_from_callsite(sig, ({"a": 1},), {}, None, ann)

        assert ctx is not None
        assert ctx.extra["request_data"] == {}

    def test_annotated_primitive_short_circuits_isinstance(self):
        """When the annotation says ``int``, the helper trusts it and skips
        the runtime ``isinstance`` check. Even an exotic value (here a real
        ``int``) confirms the fast path is taken by leaving ``ann[x]=True``."""

        def fn(x: int) -> None: ...

        sig, ann = _cache_for(fn)
        assert ann["x"] is True
        ctx = _build_context_from_callsite(sig, (42,), {}, None, ann)

        assert ctx is not None
        assert ctx.extra["request_data"] == {"x": 42}

    def test_generic_alias_annotation_falls_back_to_runtime_gate(self):
        """``dict[str, int]`` is not a concrete primitive type — the
        annotation gate returns False, so the runtime ``isinstance`` decides.
        A real dict value is rejected."""

        def fn(payload: dict[str, int]) -> None: ...

        sig, ann = _cache_for(fn)
        assert ann["payload"] is False  # annotation gate false → runtime gate
        ctx = _build_context_from_callsite(sig, ({"a": 1},), {}, None, ann)

        assert ctx is not None
        assert ctx.extra["request_data"] == {}


# =============================================================================
# Behavior — logging side effects (#504 D8)
# =============================================================================


class TestBuildContextSkippedLogBehavior:
    """Per-arg non-primitive drop emits ``dlq_protect.context_capture_skipped``
    at DEBUG (per LOGGING_STANDARDS suffix table)."""

    def test_non_primitive_arg_emits_skipped_debug_event(self):
        class _Payload:
            pass

        def fn(req) -> None: ...

        sig, ann = _cache_for(fn)
        with capture_logs() as logs:
            _build_context_from_callsite(sig, (_Payload(),), {}, None, ann)

        skipped = [
            e for e in logs if e.get("event") == "dlq_protect.context_capture_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["log_level"] == "debug"
        assert skipped[0]["arg"] == "req"
        assert skipped[0]["type"] == "_Payload"

    def test_one_event_per_dropped_arg(self):
        def fn(a, b) -> None: ...

        sig, ann = _cache_for(fn)
        with capture_logs() as logs:
            _build_context_from_callsite(sig, ({"x": 1}, {"y": 2}), {}, None, ann)

        skipped = [
            e for e in logs if e.get("event") == "dlq_protect.context_capture_skipped"
        ]
        assert len(skipped) == 2
        assert {e["arg"] for e in skipped} == {"a", "b"}


class TestBuildContextFailedLogBehavior:
    """``bind_partial`` ``TypeError`` is caught, emits a single WARNING
    ``dlq_protect.context_capture_failed`` event, and the helper returns an
    empty ``PolicyContext()`` so the pipeline continues fail-open."""

    def test_bind_partial_typeerror_returns_empty_policy_context(self):
        def fn(only_one_arg: int) -> None: ...

        sig, ann = _cache_for(fn)
        # Three positional args against a one-arg signature → TypeError from
        # bind_partial. The helper must return an empty PolicyContext (not None).
        ctx = _build_context_from_callsite(sig, (1, 2, 3), {}, None, ann)

        assert ctx is not None
        # Empty PolicyContext — no extras populated either.
        assert ctx.order_id is None
        assert ctx.user_id is None
        assert ctx.extra == {}

    def test_bind_partial_typeerror_emits_failed_warning_log(self):
        def fn(only_one_arg: int) -> None: ...

        sig, ann = _cache_for(fn)
        with capture_logs() as logs:
            _build_context_from_callsite(sig, (1, 2, 3), {}, None, ann)

        failed = [
            e for e in logs if e.get("event") == "dlq_protect.context_capture_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"
        assert "reason" in failed[0]


# =============================================================================
# Behavior — protected() decorator wiring (#504 D1, D3)
# =============================================================================


class TestProtectedContextPassthroughBehavior:
    """``@protected`` calls ``_build_context_from_callsite`` and forwards the
    result via ``protect(..., context=ctx)``."""

    def test_protected_passes_auto_extracted_context_to_protect(self):
        with patch("baldur.protect_facade.protect", return_value=None) as mock_protect:

            @protected("svc.charge")
            def charge(order_id: str, user_id: str, amount: int) -> None: ...

            charge("o-9", "u-3", 250)

        assert mock_protect.call_count == 1
        ctx = mock_protect.call_args.kwargs["context"]
        assert isinstance(ctx, PolicyContext)
        assert ctx.order_id == "o-9"
        assert ctx.user_id == "u-3"
        assert ctx.extra["request_data"] == {
            "order_id": "o-9",
            "user_id": "u-3",
            "amount": 250,
        }

    def test_protected_with_context_from_false_passes_none_context(self):
        """The opt-out sentinel must reach protect() as ``context=None``,
        preserving today's empty-context behaviour for privacy-sensitive code."""

        with patch("baldur.protect_facade.protect", return_value=None) as mock_protect:

            @protected("auth.verify_password", context_from=False)
            def verify(user_id: str, password: str) -> bool: ...

            verify("u-1", "secret")

        assert mock_protect.call_args.kwargs["context"] is None


class TestProtectedNameCollisionBehavior:
    """A wrapped function whose parameter is named ``context`` or ``extra``
    must NOT clash with the framework's ``context=`` kwarg to ``protect()``."""

    def test_wrapped_param_named_context_or_extra_flows_into_request_data(self):
        with patch("baldur.protect_facade.protect", return_value=None) as mock_protect:

            @protected("svc.collision")
            def fn(context: str, extra: int) -> None: ...

            fn("ctx-value", 99)

        ctx = mock_protect.call_args.kwargs["context"]
        assert isinstance(ctx, PolicyContext)
        # Wrapped-fn args land in extra["request_data"] — no clash with the
        # PolicyContext.extra field or with the protect() framework kwarg.
        assert ctx.extra["request_data"] == {
            "context": "ctx-value",
            "extra": 99,
        }


# =============================================================================
# Behavior — aprotected() async parity + asyncio.gather isolation (#504 D3)
# =============================================================================


class TestAprotectedContextPassthroughBehavior:
    """``@aprotected`` mirrors the sync path and threads the captured context
    through ``aprotect()``."""

    def test_aprotected_passes_auto_extracted_context_to_aprotect(self):
        async def fake_aprotect(*args, **kwargs):
            fake_aprotect.captured_ctx = kwargs.get("context")
            return None

        fake_aprotect.captured_ctx = None

        with patch("baldur.protect_facade.aprotect", side_effect=fake_aprotect):

            @aprotected("svc.acharge")
            async def acharge(order_id: str, user_id: str) -> None: ...

            asyncio.run(acharge("o-1", "u-1"))

        ctx = fake_aprotect.captured_ctx
        assert isinstance(ctx, PolicyContext)
        assert ctx.order_id == "o-1"
        assert ctx.user_id == "u-1"

    def test_aprotected_gather_isolation_produces_distinct_contexts(self):
        """Two concurrent invocations under ``asyncio.gather`` must each get
        their own ``PolicyContext`` — fresh ``extra`` per call by construction
        (no ContextVar; the helper rebuilds the dict every invocation)."""
        captured: list[PolicyContext] = []

        async def fake_aprotect(*args, **kwargs):
            captured.append(kwargs.get("context"))
            return None

        with patch("baldur.protect_facade.aprotect", side_effect=fake_aprotect):

            @aprotected("svc.acharge")
            async def acharge(order_id: str, user_id: str) -> None: ...

            async def driver():
                await asyncio.gather(
                    acharge("o-A", "u-A"),
                    acharge("o-B", "u-B"),
                )

            asyncio.run(driver())

        assert len(captured) == 2
        # Distinct instances (Python identity), distinct extras dicts
        assert captured[0] is not captured[1]
        assert captured[0].extra is not captured[1].extra
        # Each captures its own args — no cross-contamination
        order_ids = {c.order_id for c in captured}
        assert order_ids == {"o-A", "o-B"}


# =============================================================================
# Behavior — @dlq_protect + @idempotent decorator stack (#504 D2 test row)
# =============================================================================


class TestDlqProtectIdempotentStackBehavior:
    """``@dlq_protect`` wrapping ``@idempotent`` — the inner ``__wrapped__``
    chain keeps the raw signature introspectable so auto-extract still
    captures the wrapped function's args, not the ``*args, **kwargs`` of
    ``@idempotent``'s wrapper."""

    def test_dlq_protect_over_idempotent_introspects_raw_signature(self):
        # ``inspect.signature`` follows ``__wrapped__`` by default. The
        # decoration-time cache built by @dlq_protect → @protected must
        # therefore reflect the inner function's parameters, not
        # @idempotent's generic *args/**kwargs.
        from baldur.decorators.idempotent import idempotent
        from baldur.services.idempotency.models import IdempotencyDomain

        with patch("baldur.protect_facade.protect", return_value=None) as mock_protect:

            @dlq_protect("orders.charge")
            @idempotent(
                domain=IdempotencyDomain.EXTERNAL_SERVICE, key_args=["order_id"]
            )
            def charge(order_id: str, amount: int) -> None: ...

            charge("o-7", 100)

        ctx = mock_protect.call_args.kwargs["context"]
        assert isinstance(ctx, PolicyContext)
        # Inner signature reachable → both args captured as primitives
        assert ctx.order_id == "o-7"
        assert ctx.extra["request_data"] == {"order_id": "o-7", "amount": 100}


# =============================================================================
# Contract — @dlq_protect forwards context_from= (#504 D7)
# =============================================================================


class TestDlqProtectContextFromForwardingContract:
    """D7: ``@dlq_protect`` lifts ``context_from=`` to ``@protected``."""

    def test_omitted_context_from_is_not_forwarded(self):
        """When ``context_from`` is omitted on ``@dlq_protect``, the kwarg
        must NOT be injected into ``protected()`` — preserving its default
        (``None`` → auto-extract)."""

        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("orders.charge")
            def charge(order_id: int) -> None: ...

            assert "context_from" not in mock_protected.call_args.kwargs

    def test_context_from_false_forwarded_to_protected(self):
        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("auth.verify_password", context_from=False)
            def verify(user_id: str, password: str) -> bool: ...

            assert mock_protected.call_args.kwargs["context_from"] is False

    def test_context_from_callable_forwarded_to_protected(self):
        def custom(*args, **kwargs) -> PolicyContext:
            return PolicyContext()

        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("svc.op", context_from=custom)
            def op(x: int) -> None: ...

            assert mock_protected.call_args.kwargs["context_from"] is custom
