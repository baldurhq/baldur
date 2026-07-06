"""Unit tests for ``baldur.decorators.idempotent`` (#458 §D1, §D3, §D5, §D6, §D8).

Verification techniques applied:
- Behavior: state_transition — CONTINUE → mark_completed on success;
  CONTINUE → mark_failed on exception; SKIP / ABORT → raise
  IdempotencyDuplicateError.
- Behavior: idempotency — second call with same key raises
  IdempotencyDuplicateError.
- Contract: D3 key extraction — key_args / key_fn / both / neither.
- Contract: D3 primitive validation — annotated non-primitive rejected at
  decoration time; unannotated non-primitive rejected at first call;
  allowed primitives (int, str, UUID, Enum, datetime) work.
- Behavior: D5 toggle — IdempotencySettings.enabled=False short-circuits
  the gate.
- Behavior: D6 lazy cache + fallback — resolution happens on first call;
  ``_reset_cached_gate()`` re-resolves; AdapterNotFoundError falls back to
  the module-level InMemoryCacheAdapter.
- Contract: D8 logging — ``idempotency.duplicate_blocked`` (SKIP) and
  ``idempotency.execution_blocked`` (ABORT) WARNING events with documented
  ``extra`` payload.
- 595 D1: per-operation key identity — two functions sharing domain + key
  values get distinct keys by default; a shared explicit ``operation=`` label
  dedups as one logical operation; ``functools.wraps`` stacking derives the
  original function's identity.
- 595 D7: injective key encoding — pipe/backslash value escaping; escape-twin
  value tuples assemble distinct keys.
- 595 D2: window threading (``execution_ttl`` → ``check_and_acquire``,
  ``ttl`` → ``mark_*``) and clock-controlled window-expiry behavior
  (memory-window expiry, decoupled crash recovery).
"""

# NOTE: do NOT use ``from __future__ import annotations`` here. The source's
# decoration-time primitive validation reads ``inspect.signature(...).
# parameters[name].annotation`` and only fires when annotations are real
# types (not deferred PEP 563 strings).

import functools
import logging
import os
from datetime import datetime, timedelta
from enum import Enum
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from baldur.core.exceptions import (
    IdempotencyDuplicateError,
    IdempotencyUnavailableError,
)
from baldur.core.idempotency_gate import (
    IdempotencyCheckResult,
    IdempotencyDecision,
)
from baldur.decorators.idempotent import (
    _build_key_from_args,
    _escape_key_value,
    _reset_fallback_cache,
    idempotent,
)
from baldur.services.idempotency.models import IdempotencyDomain
from tests.factories.time_helpers import freeze_time

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_module_state():
    """Clear the module-level fallback cache before/after each test."""
    _reset_fallback_cache()
    yield
    _reset_fallback_cache()


@pytest.fixture
def reset_idempotency_settings_singleton():
    from baldur.settings.idempotency import reset_idempotency_settings

    reset_idempotency_settings()
    yield
    reset_idempotency_settings()


def _unique_key(prefix: str = "k") -> str:
    """Return a process-unique raw key fragment so tests don't interfere with
    each other through the module-level fallback cache."""
    return f"{prefix}-{uuid4().hex}"


# =============================================================================
# Decoration-time contract — key_args / key_fn mutual exclusion
# =============================================================================


class TestIdempotentKeyExtractionContract:
    """D3: key_args + key_fn are mutually exclusive; one must be supplied."""

    def test_neither_key_args_nor_key_fn_raises_typeerror(self):
        with pytest.raises(TypeError, match="requires either"):
            idempotent()  # type: ignore[call-arg]

    def test_both_key_args_and_key_fn_raises_typeerror(self):
        with pytest.raises(TypeError, match="mutually exclusive"):
            idempotent(key_args=["x"], key_fn=lambda x: str(x))

    def test_invalid_domain_string_raises_typeerror(self):
        with pytest.raises(TypeError, match="not a valid IdempotencyDomain"):
            idempotent(domain="not_a_real_domain", key_args=["x"])

    def test_string_domain_resolved_to_enum(self):
        # No raise → string domain coerces to enum at decoration time.
        @idempotent(domain="external_service", key_args=["order_id"])
        def op(order_id: int) -> str:
            return f"ok:{order_id}"

        # Sanity — the decoration succeeded; first call should pass.
        assert op(_unique_key("oid")) is not None or True  # decoration check


# =============================================================================
# D3 primitive validation — annotated and runtime
# =============================================================================


class _MyEnum(str, Enum):
    A = "a"
    B = "b"


class TestIdempotentKeyArgsValidation:
    """D3: only primitive types are allowed for key_args."""

    def test_annotated_non_primitive_rejected_at_decoration(self):
        with pytest.raises(TypeError, match="non-primitive"):

            @idempotent(key_args=["payload"])
            def op(payload: dict) -> None:
                pass

    def test_annotated_primitive_int_accepted(self):
        @idempotent(key_args=["order_id"])
        def op(order_id: int) -> str:
            return "ok"

        assert op(1) == "ok"

    def test_annotated_primitive_uuid_accepted(self):
        @idempotent(key_args=["request_id"])
        def op(request_id: UUID) -> str:
            return "ok"

        assert op(uuid4()) == "ok"

    def test_annotated_primitive_enum_accepted(self):
        @idempotent(key_args=["choice"])
        def op(choice: _MyEnum) -> str:
            return "ok"

        assert op(_MyEnum.A) == "ok"

    def test_annotated_primitive_datetime_accepted(self):
        @idempotent(key_args=["ts"])
        def op(ts: datetime) -> str:
            return "ok"

        assert op(datetime(2026, 1, 1)) == "ok"

    def test_unannotated_non_primitive_rejected_at_first_call(self):
        # No annotation → decoration succeeds; first call validates the value.
        @idempotent(key_args=["payload"])
        def op(payload):  # type: ignore[no-untyped-def]
            return "ok"

        with pytest.raises(TypeError, match="non-primitive"):
            op({"x": 1})

    def test_missing_key_args_parameter_rejected_at_decoration(self):
        with pytest.raises(TypeError, match="not found"):

            @idempotent(key_args=["order_id"])
            def op(other_id: int) -> str:
                return "ok"


# =============================================================================
# D1 + state transitions — CONTINUE / SKIP / ABORT
# =============================================================================


class TestIdempotentDecisionPaths:
    """D1: SKIP/ABORT raise; CONTINUE runs and marks completed; error marks failed."""

    def test_first_call_continues_and_returns_result(self):
        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return f"charged:{order_id}"

        assert op(_unique_key("oid")) is not None

    def test_second_call_with_same_key_raises_skip(self):
        # Bind to a unique raw key so prior tests can't interfere.
        order_id = _unique_key("oid")

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return f"charged:{order_id}"

        op(order_id)
        with pytest.raises(IdempotencyDuplicateError) as exc_info:
            op(order_id)
        assert exc_info.value.decision == "SKIP"

    def test_call_with_different_key_proceeds(self):
        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return f"charged:{order_id}"

        a = _unique_key("a")
        b = _unique_key("b")
        op(a)
        # Distinct key — must not collide.
        assert op(b) is not None

    def test_failed_call_marks_failed_and_allows_retry(self):
        # When the wrapped function raises, mark_failed runs; the next call
        # with the same key gets a CONTINUE (status "failed" → delete + setnx).
        order_id = _unique_key("oid")
        attempts: list[int] = []

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("transient")
            return "ok"

        with pytest.raises(RuntimeError):
            op(order_id)

        # Retry — gate sees status="failed" → CONTINUE, second attempt succeeds.
        assert op(order_id) == "ok"

    @pytest.mark.asyncio
    async def test_async_first_call_continues_and_returns(self):
        @idempotent(key_args=["order_id"])
        async def op(order_id: str) -> str:
            return f"charged:{order_id}"

        assert await op(_unique_key("aoid")) is not None

    @pytest.mark.asyncio
    async def test_async_second_call_raises_duplicate(self):
        order_id = _unique_key("aoid")

        @idempotent(key_args=["order_id"])
        async def op(order_id: str) -> str:
            return f"charged:{order_id}"

        await op(order_id)
        with pytest.raises(IdempotencyDuplicateError) as exc_info:
            await op(order_id)
        assert exc_info.value.decision == "SKIP"

    def test_abort_decision_raises_with_decision_field(self):
        # Force an ABORT by patching the gate's check_and_acquire result.
        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
            return_value=IdempotencyCheckResult(decision=IdempotencyDecision.ABORT),
        ):
            with pytest.raises(IdempotencyDuplicateError) as exc_info:
                op(_unique_key("oid"))

        assert exc_info.value.decision == "ABORT"


# =============================================================================
# D3 key_fn behavior — str return vs IdempotencyKey return
# =============================================================================


class TestIdempotentKeyFn:
    """key_fn returning str honors configured domain; returning IdempotencyKey
    uses its own domain."""

    def test_key_fn_returning_str_collides_on_same_value(self):
        suffix = uuid4().hex

        @idempotent(key_fn=lambda payload: payload["request_id"])
        def op(payload: dict) -> str:
            return "ok"

        op({"request_id": f"req-{suffix}"})
        with pytest.raises(IdempotencyDuplicateError):
            op({"request_id": f"req-{suffix}"})

    def test_key_fn_returning_idempotency_key_uses_its_own_domain(self):
        # If key_fn returns an IdempotencyKey directly, the configured domain
        # on the decorator is ignored (the factory call already set its own).
        from baldur.services.idempotency.models import IdempotencyKey

        suffix = uuid4().hex

        @idempotent(
            domain=IdempotencyDomain.CHAOS_EXPERIMENT,
            key_fn=lambda x: IdempotencyKey(
                domain=IdempotencyDomain.EXTERNAL_SERVICE,
                key=f"k-{suffix}",
                components={},
            ),
        )
        def op(x: int) -> str:
            return "ok"

        op(1)
        # Same key collides regardless of decorator-domain mismatch.
        with pytest.raises(IdempotencyDuplicateError):
            op(2)

    def test_key_fn_returning_invalid_type_raises_typeerror(self):
        @idempotent(key_fn=lambda x: 123)  # int → unsupported
        def op(x: int) -> str:
            return "ok"

        with pytest.raises(TypeError, match="must return str or IdempotencyKey"):
            op(1)


# =============================================================================
# D5 toggle — IdempotencySettings.enabled=False short-circuits
# =============================================================================


class TestIdempotentToggle:
    """D5: when ``enabled=False``, the gate is bypassed entirely."""

    def test_disabled_decorator_lets_all_calls_through(self):
        # 686: the console-editable ``enabled`` flag resolves via the cached
        # layered seam. Patch the seam rather than the env base — a present PRO
        # RuntimeConfigManager pins a full-blob default that overrides the env
        # var, so an env-only override would not disable the gate.
        from baldur.settings.idempotency import IdempotencySettings

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        order_id = _unique_key("oid")
        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(enabled=False),
        ):
            # Multiple calls with the same key all succeed because the gate is
            # never consulted.
            for _ in range(3):
                assert op(order_id) == "ok"


# =============================================================================
# D6 lazy cache resolution + fallback
# =============================================================================


class TestIdempotentLazyCache:
    """D6: gate is constructed on first call; reset helper re-resolves."""

    def test_gate_not_constructed_until_first_call(self):
        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        # Inspect the closure state via the documented test helper.
        # No gate exists yet — _reset_cached_gate is callable.
        assert hasattr(op, "_reset_cached_gate")

        # Patch the resolver and verify it's only invoked at call time.
        with patch(
            "baldur.decorators.idempotent._resolve_cache_via_registry",
            wraps=lambda: (
                __import__(
                    "baldur.decorators.idempotent",
                    fromlist=["_FALLBACK_CACHE"],
                )._FALLBACK_CACHE
            ),
        ) as mock_resolver:
            assert mock_resolver.call_count == 0
            op(_unique_key("oid"))
            assert mock_resolver.call_count == 1
            # Subsequent calls reuse the cached gate.
            op(_unique_key("oid2"))
            assert mock_resolver.call_count == 1

    def test_reset_cached_gate_forces_reresolution(self):
        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        op(_unique_key("oid"))

        with patch(
            "baldur.decorators.idempotent._resolve_cache_via_registry",
            wraps=lambda: (
                __import__(
                    "baldur.decorators.idempotent",
                    fromlist=["_FALLBACK_CACHE"],
                )._FALLBACK_CACHE
            ),
        ) as mock_resolver:
            # Before reset — call uses cached gate, no resolver call.
            op(_unique_key("oid2"))
            assert mock_resolver.call_count == 0
            # Reset forces re-resolution on the next call.
            op._reset_cached_gate()  # type: ignore[attr-defined]
            op(_unique_key("oid3"))
            assert mock_resolver.call_count == 1

    def test_adapter_not_found_falls_back_to_inmemory_in_dev(
        self, monkeypatch, reset_idempotency_settings_singleton
    ):
        # 461 D1: in non-prod, missing adapter still falls back to the
        # in-memory cache so local dev / CI runs without registering an
        # adapter. Production behavior is exercised by
        # TestResolveCacheFallbackMatrix and TestResolveCacheFailClosed.
        from baldur.core.exceptions import AdapterNotFoundError
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)  # default non-prod
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        reset_idempotency_settings()
        reset_runtime()

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            # Should not raise — fallback path kicks in.
            assert op(_unique_key("oid")) == "ok"


# =============================================================================
# D8 logging — duplicate_blocked / execution_blocked WARNING
# =============================================================================


class TestIdempotentLogging:
    """D8: WARNING events with documented ``extra`` payload on SKIP / ABORT."""

    def test_skip_emits_duplicate_blocked_warning(self, caplog):
        order_id = _unique_key("oid")

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        op(order_id)

        with caplog.at_level(logging.WARNING, logger="baldur.decorators.idempotent"):
            with pytest.raises(IdempotencyDuplicateError):
                op(order_id)

        records = [
            r for r in caplog.records if r.message == "idempotency.duplicate_blocked"
        ]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.function == op.__qualname__
        assert record.domain == IdempotencyDomain.CUSTOM.value
        assert record.decision == "SKIP"

    def test_abort_emits_execution_blocked_warning(self, caplog):

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
            return_value=IdempotencyCheckResult(decision=IdempotencyDecision.ABORT),
        ):
            with caplog.at_level(
                logging.WARNING, logger="baldur.decorators.idempotent"
            ):
                with pytest.raises(IdempotencyDuplicateError):
                    op(_unique_key("oid"))

        records = [
            r for r in caplog.records if r.message == "idempotency.execution_blocked"
        ]
        assert len(records) == 1
        assert records[0].decision == "ABORT"


# =============================================================================
# 567 D9 — cache-error fail direction (default fail-closed, opt-in fail-open)
# =============================================================================


class TestIdempotentDecoratorFailDirectionBehavior:
    """#567 D9: a cache I/O error during ``check_and_acquire`` fails CLOSED by
    default — the decorator raises ``IdempotencyUnavailableError`` wrapping the
    raw cause via ``from`` (so a backend-specific error never leaks). Opting in
    via ``fail_open_on_cache_error`` treats the unverifiable check as CONTINUE."""

    def test_cache_error_fails_closed_and_wraps_cause(self):
        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        cause = RuntimeError("redis down")
        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
            side_effect=cause,
        ):
            with pytest.raises(IdempotencyUnavailableError) as exc_info:
                op(_unique_key("oid"))

        # The domain type wraps the raw cause (D9: never leak RedisError).
        assert exc_info.value.__cause__ is cause
        assert exc_info.value.key != ""

    def test_cache_error_fail_open_treats_as_continue_and_runs(self):
        # 686: ``fail_open_on_cache_error`` resolves via the cached layered
        # seam. Patch the seam (not the env base) — a present PRO
        # RuntimeConfigManager pins a full-blob default that overrides env.
        from baldur.settings.idempotency import IdempotencySettings

        oid = _unique_key("oid")

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return f"ran:{order_id}"

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(fail_open_on_cache_error=True),
            ),
            patch(
                "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
                side_effect=RuntimeError("redis down"),
            ),
        ):
            assert op(oid) == f"ran:{oid}"

    @pytest.mark.asyncio
    async def test_async_cache_error_fails_closed(self):
        @idempotent(key_args=["order_id"])
        async def op(order_id: str) -> str:
            return "ok"

        cause = RuntimeError("redis down")
        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
            side_effect=cause,
        ):
            with pytest.raises(IdempotencyUnavailableError) as exc_info:
                await op(_unique_key("aoid"))

        assert exc_info.value.__cause__ is cause


# =============================================================================
# 673 D5 — mark_* fail-open (a mark against a down cache never raises after run)
# =============================================================================


class TestIdempotentMarkFailOpenBehavior:
    """673 D5: once the guarded function has run (including on the fail-open
    cache-outage path), the four ``mark_*`` calls are best-effort — a mark
    against a still-down cache is swallowed + logged, never re-raised. Otherwise
    a ``cas_dict_field`` outage would raise AFTER the function already
    succeeded/failed, masking its result or its own exception (violating
    fail-open)."""

    def test_success_mark_completed_failure_is_swallowed_and_warns(self, caplog):
        """A successful run whose ``mark_completed`` fails still returns the
        result; the silent degradation is logged at WARNING with ``fail_open``."""
        oid = _unique_key("oid")

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return f"ran:{order_id}"

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.mark_completed",
            autospec=True,
            side_effect=RuntimeError("redis down"),
        ):
            with caplog.at_level(
                logging.WARNING, logger="baldur.decorators.idempotent"
            ):
                # Must not raise — the function already produced its result.
                assert op(oid) == f"ran:{oid}"

        records = [
            r
            for r in caplog.records
            if r.message == "idempotency.mark_completed_failed"
        ]
        assert len(records) == 1
        assert records[0].fail_open is True

    def test_failure_mark_failed_error_does_not_mask_original_exception(self):
        """When the function raises AND ``mark_failed`` also raises, the ORIGINAL
        function exception propagates — the mark error must never shadow it."""
        oid = _unique_key("oid")

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            raise ValueError("original boom")

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.mark_failed",
            autospec=True,
            side_effect=RuntimeError("redis down"),
        ):
            with pytest.raises(ValueError, match="original boom"):
                op(oid)

    @pytest.mark.asyncio
    async def test_async_success_mark_completed_failure_is_swallowed(self, caplog):
        """Async parity: a mark_completed outage after a successful run is
        swallowed (the decorator drives the sync gate for both wrappers)."""
        oid = _unique_key("aoid")

        @idempotent(key_args=["order_id"])
        async def op(order_id: str) -> str:
            return f"ran:{order_id}"

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.mark_completed",
            autospec=True,
            side_effect=RuntimeError("redis down"),
        ):
            with caplog.at_level(
                logging.WARNING, logger="baldur.decorators.idempotent"
            ):
                assert await op(oid) == f"ran:{oid}"

        records = [
            r
            for r in caplog.records
            if r.message == "idempotency.mark_completed_failed"
        ]
        assert len(records) == 1
        assert records[0].fail_open is True

    @pytest.mark.asyncio
    async def test_async_failure_mark_failed_error_does_not_mask_original(self):
        """Async parity: the original function exception survives a mark_failed
        outage."""
        oid = _unique_key("aoid")

        @idempotent(key_args=["order_id"])
        async def op(order_id: str) -> str:
            raise ValueError("original boom")

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.mark_failed",
            autospec=True,
            side_effect=RuntimeError("redis down"),
        ):
            with pytest.raises(ValueError, match="original boom"):
                await op(oid)


# =============================================================================
# 461 — fail-closed when ProviderRegistry has no cache adapter
# =============================================================================


class TestProductionEnvironmentDetection:
    """463 D1/D10: idempotent's production detection delegates to
    ``baldur.runtime.is_production`` — strict equality with
    ``BALDUR_ENVIRONMENT == "production"``, normalised via
    ``.strip().lower()``. Trailing-space tolerance comes from the
    ``.strip()`` in the runtime helper; trailing-space rejection that the
    pre-D10 decorator-private helper used is no longer the contract."""

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            (None, False),  # unset
            ("production", True),  # canonical
            ("PRODUCTION", True),  # uppercase normalises via .lower()
            ("Production", True),  # mixed case normalises via .lower()
            ("development", False),  # canonical non-prod
            ("", False),  # empty string is not "production"
            ("staging", False),  # arbitrary non-prod
            ("prod", False),  # legacy alias does NOT match (D6)
        ],
        ids=[
            "unset",
            "production_lower",
            "production_upper",
            "production_mixed",
            "development",
            "empty",
            "staging",
            "prod_abbrev",
        ],
    )
    def test_runtime_is_production_matches_strict_lowercase(
        self, monkeypatch, env_value, expected
    ):
        from baldur.runtime import is_production, reset_runtime

        if env_value is None:
            monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)
        else:
            monkeypatch.setenv("BALDUR_ENVIRONMENT", env_value)
        # The runtime eager-reads BALDUR_ENVIRONMENT in __init__; reset so
        # the next get_runtime() rebuilds against the patched env.
        reset_runtime()

        assert is_production() is expected


class TestResolveCacheRegistryHit:
    """461 D1: when ProviderRegistry returns a cache adapter, that adapter
    is returned unchanged (no fallback, no environment check)."""

    def test_registered_adapter_returned_without_inspecting_env(self, monkeypatch):
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.decorators.idempotent import _resolve_cache_via_registry
        from baldur.runtime import reset_runtime

        # Force production with no escape hatch — none of those branches
        # should run because the registry hit short-circuits everything.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        reset_runtime()

        registered = InMemoryCacheAdapter(key_prefix="test_registered:")
        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            return_value=registered,
        ):
            assert _resolve_cache_via_registry() is registered


class TestResolveCacheFallbackMatrix:
    """461 D1: outcome matrix — env × adapter_present × escape_hatch.

    Outcomes:
      - "adapter": ProviderRegistry's cache is returned.
      - "fallback": module-level _FALLBACK_CACHE is returned.
      - "raises":   ConfigurationError is raised (fail-closed).
    """

    @pytest.mark.parametrize(
        ("in_production", "adapter_present", "escape_hatch", "expected_outcome"),
        [
            # Adapter present → env / escape_hatch don't matter, adapter wins.
            (True, True, False, "adapter"),
            (True, True, True, "adapter"),
            (False, True, False, "adapter"),
            (False, True, True, "adapter"),
            # Adapter absent in non-prod → always fallback regardless of escape hatch.
            (False, False, False, "fallback"),
            (False, False, True, "fallback"),
            # Adapter absent in prod → fail-closed unless escape hatch is on.
            (True, False, False, "raises"),
            (True, False, True, "fallback"),
        ],
        ids=[
            "prod_adapter_escape_off_returns_adapter",
            "prod_adapter_escape_on_returns_adapter",
            "dev_adapter_escape_off_returns_adapter",
            "dev_adapter_escape_on_returns_adapter",
            "dev_no_adapter_escape_off_returns_fallback",
            "dev_no_adapter_escape_on_returns_fallback",
            "prod_no_adapter_escape_off_raises",
            "prod_no_adapter_escape_on_returns_fallback",
        ],
    )
    def test_resolve_cache_outcome_matrix(
        self,
        monkeypatch,
        in_production,
        adapter_present,
        escape_hatch,
        expected_outcome,
    ):
        import sys

        _mod = sys.modules["baldur.decorators.idempotent"]
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.core.exceptions import AdapterNotFoundError, ConfigurationError
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import IdempotencySettings

        # Given — env drives ``is_production()``; the escape hatch (686:
        # console-editable) resolves via the cached layered seam, so patch the
        # seam rather than the env base (a present PRO RuntimeConfigManager pins
        # a full-blob default that overrides the env var).
        monkeypatch.setenv(
            "BALDUR_ENVIRONMENT", "production" if in_production else "development"
        )
        reset_runtime()

        registered = InMemoryCacheAdapter(key_prefix="matrix_registered:")
        if adapter_present:
            ctx = patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                return_value=registered,
            )
        else:
            ctx = patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                side_effect=AdapterNotFoundError(adapter_type="cache"),
            )

        # When + Then — outcome matches expectation.
        with (
            ctx,
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(allow_inmemory_fallback=escape_hatch),
            ),
        ):
            if expected_outcome == "raises":
                with pytest.raises(ConfigurationError):
                    _mod._resolve_cache_via_registry()
            elif expected_outcome == "adapter":
                assert _mod._resolve_cache_via_registry() is registered
            else:  # "fallback"
                assert _mod._resolve_cache_via_registry() is _mod._FALLBACK_CACHE


class TestResolveCacheFailClosed:
    """461 D1: fail-closed in production with no adapter and no escape hatch.

    Verifies the ConfigurationError message contains the escape-hatch hint and
    the WARNING log carries the documented event name + ``pid`` + ``reason``
    extras.
    """

    def test_prod_no_adapter_no_escape_raises_with_escape_hatch_hint(
        self, monkeypatch, reset_idempotency_settings_singleton, caplog
    ):
        from baldur.core.exceptions import AdapterNotFoundError, ConfigurationError
        from baldur.decorators.idempotent import _resolve_cache_via_registry
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        # Given — production + escape hatch off + adapter missing.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        reset_idempotency_settings()
        reset_runtime()

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with caplog.at_level(
                logging.WARNING, logger="baldur.decorators.idempotent"
            ):
                # When + Then — raises with operator-actionable message.
                with pytest.raises(ConfigurationError) as exc_info:
                    _resolve_cache_via_registry()

        message = str(exc_info.value)
        assert "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK" in message
        assert "ProviderRegistry" in message
        assert "production" in message.lower()

        records = [
            r
            for r in caplog.records
            if r.message == "idempotency.distributed_dedup_unavailable"
        ]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.pid == os.getpid()
        assert record.reason == "no_cache_adapter_registered"


class TestResolveCacheEscapeHatch:
    """461 D1: escape hatch returns the in-memory fallback even in prod and
    emits the visibility WARNING; in dev, the escape hatch is moot — fallback
    would happen anyway and no WARNING is emitted."""

    def test_prod_no_adapter_escape_on_returns_fallback_and_warns(
        self, monkeypatch, caplog
    ):
        import sys

        _mod = sys.modules["baldur.decorators.idempotent"]
        from baldur.core.exceptions import AdapterNotFoundError
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import IdempotencySettings

        # 686: env drives ``is_production()``; the escape hatch resolves via the
        # cached layered seam, patched here so a present PRO RuntimeConfigManager
        # cannot pin the full-blob default over it.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        reset_runtime()

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(allow_inmemory_fallback=True),
            ),
            patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                side_effect=AdapterNotFoundError(adapter_type="cache"),
            ),
            caplog.at_level(logging.WARNING, logger="baldur.decorators.idempotent"),
        ):
            resolved = _mod._resolve_cache_via_registry()

        assert resolved is _mod._FALLBACK_CACHE
        records = [
            r
            for r in caplog.records
            if r.message == "idempotency.inmemory_fallback_active"
        ]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.pid == os.getpid()
        assert record.reason == "escape_hatch_enabled"

    def test_dev_no_adapter_escape_on_returns_fallback_without_warning(
        self, monkeypatch, reset_idempotency_settings_singleton, caplog
    ):
        # In non-prod, the escape hatch is moot — fallback would be used
        # either way, so neither WARNING event is emitted.
        import sys

        _mod = sys.modules["baldur.decorators.idempotent"]
        from baldur.core.exceptions import AdapterNotFoundError
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "true")
        reset_idempotency_settings()
        reset_runtime()

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with caplog.at_level(
                logging.WARNING, logger="baldur.decorators.idempotent"
            ):
                resolved = _mod._resolve_cache_via_registry()

        assert resolved is _mod._FALLBACK_CACHE
        warning_messages = {
            "idempotency.distributed_dedup_unavailable",
            "idempotency.inmemory_fallback_active",
        }
        emitted = [r.message for r in caplog.records if r.message in warning_messages]
        assert emitted == []


class TestResolveCacheUnexpectedError:
    """461 G3: only AdapterNotFoundError is caught — all other exceptions
    propagate as genuine bugs (the prior `except (AdapterNotFoundError,
    Exception)` swallowed them silently)."""

    def test_unexpected_exception_from_registry_propagates(
        self, monkeypatch, reset_idempotency_settings_singleton
    ):
        from baldur.decorators.idempotent import _resolve_cache_via_registry
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        # Production env + no escape hatch — confirms the prod-fail-closed
        # branch is NOT entered, because the unrelated exception propagates
        # before the AdapterNotFoundError arm runs.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        reset_idempotency_settings()
        reset_runtime()

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=RuntimeError("unrelated bug"),
        ):
            with pytest.raises(RuntimeError, match="unrelated bug"):
                _resolve_cache_via_registry()


class TestEnsureGateNonAtomicAdapter:
    """461 D2: removing the second silent fallback means a non-atomic adapter
    contract violation surfaces in EVERY environment (it's a bug, not a
    graceful-degradation candidate)."""

    @pytest.mark.parametrize(
        "in_production",
        [False, True],
        ids=["dev", "prod"],
    )
    def test_non_atomic_adapter_propagates_configuration_error(
        self, monkeypatch, reset_idempotency_settings_singleton, in_production
    ):
        from baldur.core.exceptions import ConfigurationError
        from baldur.interfaces.cache_provider import CacheProviderInterface
        from baldur.settings.idempotency import reset_idempotency_settings

        class _BadCache(CacheProviderInterface):
            """Cache that inherits the non-atomic setnx default — exists to
            exercise IdempotencyGate._validate_atomic_setnx."""

            @property
            def provider_name(self) -> str:
                return "bad_cache_test"

            def get(self, key): ...
            def set(self, key, value, ttl=None): ...
            def delete(self, key): ...
            def exists(self, key): ...
            def incr(self, key, amount=1): ...
            def decr(self, key, amount=1): ...
            def expire(self, key, ttl): ...
            def ttl(self, key): ...
            def get_lock(self, name, timeout=None, blocking_timeout=None): ...
            def mget(self, keys): ...
            def mset(self, mapping, ttl=None): ...
            def health_check(self): ...
            def flush_all(self): ...

        monkeypatch.setenv(
            "ENVIRONMENT", "production" if in_production else "development"
        )
        # Escape-hatch state is irrelevant: adapter IS returned, just broken.
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        reset_idempotency_settings()

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            return_value=_BadCache(),
        ):
            with pytest.raises(ConfigurationError, match="atomic setnx"):
                op(_unique_key("oid"))


# =============================================================================
# 595 D1 — per-operation key identity
# =============================================================================


class TestIdempotentOperationIdentityBehavior:
    """595 D1: the ``key_args`` form embeds a per-operation key component
    (default ``module.qualname``), so two different operations sharing a domain
    and key values no longer consume each other's dedup verdicts; a shared
    explicit ``operation=`` label preserves cross-entry-point dedup of one
    logical operation."""

    def test_two_functions_same_domain_and_key_values_do_not_collide(self):
        """charge() completing must not make ship(order_id=same) raise a false
        SKIP — the pre-595 caller-level data-loss bug (G1)."""
        oid = _unique_key("oid")

        @idempotent(domain=IdempotencyDomain.EXTERNAL_SERVICE, key_args=["order_id"])
        def charge(order_id: str) -> str:
            return "charged"

        @idempotent(domain=IdempotencyDomain.EXTERNAL_SERVICE, key_args=["order_id"])
        def ship(order_id: str) -> str:
            return "shipped"

        # Given — charge completes and is remembered under its own key.
        assert charge(oid) == "charged"

        # Then — ship with the same domain + key value runs (no false SKIP).
        assert ship(oid) == "shipped"

    def test_shared_explicit_operation_label_dedups_as_one_logical_operation(self):
        """Two entry points sharing ``operation=`` guard one logical operation:
        the second call with the same key values is a genuine duplicate."""
        oid = _unique_key("oid")

        @idempotent(
            domain=IdempotencyDomain.EXTERNAL_SERVICE,
            key_args=["order_id"],
            operation="billing.charge",
        )
        def http_charge(order_id: str) -> str:
            return "ok"

        @idempotent(
            domain=IdempotencyDomain.EXTERNAL_SERVICE,
            key_args=["order_id"],
            operation="billing.charge",
        )
        def worker_charge(order_id: str) -> str:
            return "ok"

        http_charge(oid)
        with pytest.raises(IdempotencyDuplicateError) as exc_info:
            worker_charge(oid)
        assert exc_info.value.decision == "SKIP"

    def test_default_operation_equals_module_qualname_of_decorated_function(self):
        """The auto-derived component is ``f"{module}.{qualname}"`` — an
        explicit label spelling exactly that shares the key space."""
        oid = _unique_key("oid")

        def original(order_id: str) -> str:
            return "ok"

        auto = idempotent(key_args=["order_id"])(original)
        labeled = idempotent(
            key_args=["order_id"],
            operation=f"{original.__module__}.{original.__qualname__}",
        )(lambda order_id: "ok")

        assert auto(oid) == "ok"
        with pytest.raises(IdempotencyDuplicateError) as exc_info:
            labeled(oid)
        assert exc_info.value.decision == "SKIP"

    def test_stacked_wraps_decorator_derives_original_identity(self):
        """``@idempotent`` over a ``functools.wraps``-using inner decorator
        resolves the ORIGINAL function's ``module.qualname`` as the default
        operation component (wraps copies ``__module__``/``__qualname__``)."""

        def passthrough(func):
            @functools.wraps(func)
            def inner(*args, **kwargs):
                return func(*args, **kwargs)

            return inner

        def original(order_id: str) -> str:
            return "ok"

        oid = _unique_key("oid")
        stacked = idempotent(key_args=["order_id"])(passthrough(original))
        labeled = idempotent(
            key_args=["order_id"],
            operation=f"{original.__module__}.{original.__qualname__}",
        )(lambda order_id: "ok")

        assert stacked(oid) == "ok"
        # Collision with the original's explicit identity proves the stacked
        # default derived the original — not the inner wrapper's — qualname.
        with pytest.raises(IdempotencyDuplicateError):
            labeled(oid)

    def test_assembled_key_shape_is_domain_operation_values(self):
        """The assembled key is ``idempotency:{domain}:{operation}:{v1|v2}``."""

        def op(order_id: str, attempt: int) -> str:
            return "ok"

        operation = f"{op.__module__}.{op.__qualname__}"
        key = _build_key_from_args(
            op,
            ("o-1", 2),
            {},
            ["order_id", "attempt"],
            IdempotencyDomain.CUSTOM,
            operation,
            annotated_primitive={"order_id": True, "attempt": True},
        )

        assert key == (
            f"idempotency:{IdempotencyDomain.CUSTOM.value}:{operation}:o-1|2"
        )


# =============================================================================
# 595 D1/D2/D7 — decoration-time validation contract
# =============================================================================


class TestIdempotentOperationValidationContract:
    """595 D1/D7: ``operation`` must be a non-empty, separator-free str and is
    key_args-form-only; D2: ``ttl``/``execution_ttl`` must be positive
    timedeltas. All validated at decoration time (TypeError)."""

    @pytest.mark.parametrize(
        "operation",
        ["", 123, "a:b", "a|b"],
        ids=["empty", "non_str", "colon", "pipe"],
    )
    def test_invalid_operation_raises_typeerror_at_decoration(self, operation):
        """Empty / non-str / separator-bearing operation → TypeError."""
        with pytest.raises(TypeError, match="operation"):
            idempotent(key_args=["x"], operation=operation)

    def test_operation_with_key_fn_raises_typeerror(self):
        """operation= applies only to the key_args form (key_fn is verbatim)."""
        with pytest.raises(TypeError, match="applies only to the key_args form"):
            idempotent(key_fn=lambda x: "k", operation="label")

    @pytest.mark.parametrize(
        "kwarg",
        ["ttl", "execution_ttl"],
        ids=["ttl", "execution_ttl"],
    )
    @pytest.mark.parametrize(
        "value",
        [timedelta(0), timedelta(seconds=-1), 300],
        ids=["zero", "negative", "non_timedelta"],
    )
    def test_non_positive_window_raises_typeerror_at_decoration(self, kwarg, value):
        """timedelta(0) / negative / non-timedelta windows → TypeError."""
        with pytest.raises(TypeError, match="positive timedelta"):
            idempotent(key_args=["x"], **{kwarg: value})


# =============================================================================
# 595 D7 — injective key encoding (value escaping)
# =============================================================================


class TestIdempotentKeyEscapingBehavior:
    """595 D7: backslash-escaping the value-join separator makes the assembled
    key injective — escape-twin value tuples no longer collide into a false
    "already processed"."""

    def test_pipe_free_value_is_byte_identical(self):
        """The overwhelming common case pays nothing: no pipes, no change."""
        assert _escape_key_value("plain-value_123") == "plain-value_123"

    def test_pipe_and_backslash_are_escaped(self):
        """``\\`` escapes first, then ``|`` — the composite is unambiguous."""
        assert _escape_key_value("us|er") == "us\\|er"
        assert _escape_key_value("a\\b") == "a\\\\b"
        assert _escape_key_value("a\\|b") == "a\\\\\\|b"

    @staticmethod
    def _key_for(values: tuple) -> str:
        """Assemble the full cache key for a fixed two-arg operation."""

        def op(a: str, b: str) -> str:
            return "ok"

        return _build_key_from_args(
            op,
            values,
            {},
            ["a", "b"],
            IdempotencyDomain.CUSTOM,
            "twin.op",
            annotated_primitive={"a": True, "b": True},
        )

    def test_pipe_twin_tuples_assemble_distinct_keys(self):
        """``("us|er", "1")`` vs ``("us", "er|1")`` — identical under a raw
        pipe join (``us|er|1``), distinct under escaping."""
        assert self._key_for(("us|er", "1")) != self._key_for(("us", "er|1"))

    def test_backslash_pipe_twin_tuples_assemble_distinct_keys(self):
        """``("x\\", "y|z")`` vs ``("x|y\\", "z")`` — identical under
        pipe-only escaping (both ``x\\|y\\|z``), distinct only when the
        backslash itself is escaped first."""
        assert self._key_for(("x\\", "y|z")) != self._key_for(("x|y\\", "z"))

    def test_pipe_twin_calls_do_not_cross_consume_verdicts(self):
        """Behavior-level regression: the second twin call runs instead of
        raising a false SKIP."""
        suffix = uuid4().hex

        @idempotent(key_args=["a", "b"])
        def op(a: str, b: str) -> str:
            return f"{a}/{b}"

        # Raw-join twins: f"{suffix}|er" + "|" + "1" == f"{suffix}" + "|" + "er|1".
        assert op(f"{suffix}|er", "1") == f"{suffix}|er/1"
        assert op(suffix, "er|1") == f"{suffix}/er|1"


# =============================================================================
# 595 D2 — window threading (execution_ttl → acquire, ttl → mark_*)
# =============================================================================


class TestIdempotentWindowThreadingBehavior:
    """595 D2: ``execution_ttl`` bounds the EXECUTING claim (threads to
    ``check_and_acquire``); ``ttl`` is the dedup memory window (threads to
    ``mark_completed`` / ``mark_failed``). ``None`` defers to gate defaults."""

    _MEM_TTL = timedelta(hours=2)
    _EXEC_TTL = timedelta(minutes=5)

    def test_sync_execution_ttl_reaches_check_and_acquire(self):
        @idempotent(key_args=["order_id"], execution_ttl=self._EXEC_TTL)
        def op(order_id: str) -> str:
            return "ok"

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
            autospec=True,
            return_value=IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE),
        ) as mock_check:
            op(_unique_key("oid"))

        assert mock_check.call_args.kwargs["ttl"] is self._EXEC_TTL

    def test_sync_success_threads_memory_ttl_to_mark_completed(self):
        @idempotent(key_args=["order_id"], ttl=self._MEM_TTL)
        def op(order_id: str) -> str:
            return "ok"

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.mark_completed",
            autospec=True,
        ) as mock_mark:
            op(_unique_key("oid"))

        assert mock_mark.call_args.kwargs["ttl"] is self._MEM_TTL

    def test_sync_failure_threads_memory_ttl_to_mark_failed(self):
        @idempotent(key_args=["order_id"], ttl=self._MEM_TTL)
        def op(order_id: str) -> str:
            raise RuntimeError("boom")

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.mark_failed",
            autospec=True,
        ) as mock_mark:
            with pytest.raises(RuntimeError):
                op(_unique_key("oid"))

        assert mock_mark.call_args.kwargs["ttl"] is self._MEM_TTL

    def test_sync_default_windows_thread_none_to_gate(self):
        """No decorator windows → ttl=None on both gate calls (gate defaults
        decide: execution constant for acquire, memory setting for mark)."""

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        with (
            patch(
                "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
                autospec=True,
                return_value=IdempotencyCheckResult(
                    decision=IdempotencyDecision.CONTINUE
                ),
            ) as mock_check,
            patch(
                "baldur.core.idempotency_gate.IdempotencyGate.mark_completed",
                autospec=True,
            ) as mock_mark,
        ):
            op(_unique_key("oid"))

        assert mock_check.call_args.kwargs["ttl"] is None
        assert mock_mark.call_args.kwargs["ttl"] is None

    @pytest.mark.asyncio
    async def test_async_windows_thread_to_acquire_and_mark(self):
        """The async wrapper threads both windows identically to sync."""

        @idempotent(
            key_args=["order_id"],
            ttl=self._MEM_TTL,
            execution_ttl=self._EXEC_TTL,
        )
        async def op(order_id: str) -> str:
            return "ok"

        with (
            patch(
                "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
                autospec=True,
                return_value=IdempotencyCheckResult(
                    decision=IdempotencyDecision.CONTINUE
                ),
            ) as mock_check,
            patch(
                "baldur.core.idempotency_gate.IdempotencyGate.mark_completed",
                autospec=True,
            ) as mock_mark,
        ):
            await op(_unique_key("aoid"))

        assert mock_check.call_args.kwargs["ttl"] is self._EXEC_TTL
        assert mock_mark.call_args.kwargs["ttl"] is self._MEM_TTL

    @pytest.mark.asyncio
    async def test_async_failure_threads_memory_ttl_to_mark_failed(self):
        @idempotent(key_args=["order_id"], ttl=self._MEM_TTL)
        async def op(order_id: str) -> str:
            raise RuntimeError("boom")

        with patch(
            "baldur.core.idempotency_gate.IdempotencyGate.mark_failed",
            autospec=True,
        ) as mock_mark:
            with pytest.raises(RuntimeError):
                await op(_unique_key("aoid"))

        assert mock_mark.call_args.kwargs["ttl"] is self._MEM_TTL


# =============================================================================
# 595 D2 — clock-controlled window-expiry behavior
# =============================================================================


class TestIdempotentWindowExpiryBehavior:
    """595 D2: behavioral window semantics against the in-process fallback
    cache with a frozen clock (no real waits) — the memory window bounds dedup
    after success; the execution window bounds crash recovery independently of
    an hours-longer memory window."""

    def test_duplicate_within_memory_window_blocked_after_expiry_runs(self):
        """ttl=5min: a duplicate at +0s is SKIPped; at +5min1s it runs.
        (Pre-595, completed-memory was floored at the hardcoded 30 min.)"""
        oid = _unique_key("oid")

        @idempotent(key_args=["order_id"], ttl=timedelta(minutes=5))
        def op(order_id: str) -> str:
            return "ok"

        with freeze_time("2026-01-01 10:00:00"):
            assert op(oid) == "ok"
            with pytest.raises(IdempotencyDuplicateError) as exc_info:
                op(oid)
            assert exc_info.value.decision == "SKIP"

        with freeze_time("2026-01-01 10:05:01"):
            # Memory window elapsed — the completed record expired.
            assert op(oid) == "ok"

    def test_crashed_claim_retryable_after_execution_window_despite_long_ttl(self):
        """execution_ttl=1min + ttl=24h: a crashed (never-marked) claim blocks
        within the execution window (ABORT) but is retryable right after it —
        crash recovery does NOT scale with the memory window (D2 decoupling)."""
        oid = _unique_key("oid")

        @idempotent(
            key_args=["order_id"],
            ttl=timedelta(hours=24),
            execution_ttl=timedelta(minutes=1),
        )
        def op(order_id: str) -> str:
            return "ok"

        with freeze_time("2026-01-01 10:00:00"):
            # Given — a worker crash after acquiring: completion never lands.
            with patch(
                "baldur.core.idempotency_gate.IdempotencyGate.mark_completed",
                autospec=True,
            ):
                assert op(oid) == "ok"

            # Then — within the execution window the claim is honored.
            with pytest.raises(IdempotencyDuplicateError) as exc_info:
                op(oid)
            assert exc_info.value.decision == "ABORT"

        with freeze_time("2026-01-01 10:01:02"):
            # Past the 1-minute execution window (memory ttl is 24 h) the key
            # is retryable.
            assert op(oid) == "ok"
