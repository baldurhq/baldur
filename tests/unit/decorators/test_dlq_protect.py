"""Unit tests for ``baldur.decorators.dlq_protect`` (#458 §D4).

Verification techniques applied:
- Behavior: dependency_interaction — verify the underlying ``protected()``
  facade receives the PRO defaults pinned (``dlq=True``, ``retry=True``,
  ``circuit_breaker=True``) plus per-decoration kwargs forwarded unchanged.
- Contract: kwarg passthrough — ``fallback`` and ``timeout`` reach the
  facade as supplied; the ``timeout`` sentinel distinguishes "omitted"
  from "explicit None".
- Behavior: end-to-end sync dispatch through real ``protect()`` to confirm
  the wrapper preserves return values. Async end-to-end is not tested
  because ``aprotect()`` does not yet support ``circuit_breaker=True``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.decorators import dlq_protect

# =============================================================================
# Sync delegation — pinned PRO defaults
# =============================================================================


class TestDlqProtectDelegation:
    """``@dlq_protect`` pins ``dlq=True`` / ``retry=True`` / ``circuit_breaker=True``."""

    def test_sync_decoration_pins_pro_defaults_on_protected(self):
        # Given the underlying protected() is mocked at dlq_protect's import site
        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("orders.charge")
            def charge(order_id: int) -> str:
                return f"charged:{order_id}"

            # Then protected() was invoked with PRO defaults pinned
            mock_protected.assert_called_once()
            args, kwargs = mock_protected.call_args
            assert args == ("orders.charge",)
            assert kwargs["dlq"] is True
            assert kwargs["retry"] is True
            assert kwargs["circuit_breaker"] is True

    def test_sync_end_to_end_call_returns_function_result(self):
        # End-to-end sanity — value is preserved through the real protect() chain.
        @dlq_protect("orders.charge")
        def charge(order_id: int) -> str:
            return f"charged:{order_id}"

        assert charge(42) == "charged:42"

    def test_async_decoration_pins_pro_defaults_on_protected(self):
        # Async end-to-end is not exercised here because aprotect() does not
        # yet support circuit_breaker=True (NotImplementedError at call time);
        # @dlq_protect's value is the decoration-time wiring it establishes.
        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("orders.charge_async")
            async def charge(order_id: int) -> str:
                return f"charged:{order_id}"

            kwargs = mock_protected.call_args.kwargs
            assert kwargs["dlq"] is True
            assert kwargs["retry"] is True
            assert kwargs["circuit_breaker"] is True


# =============================================================================
# Kwarg passthrough — fallback, timeout sentinel
# =============================================================================


class TestDlqProtectPassthrough:
    """Optional kwargs pass through ``protected()`` unchanged."""

    def test_fallback_forwarded_to_protected(self):
        def my_fallback() -> str:
            return "fallback"

        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("svc.op", fallback=my_fallback)
            def op() -> str:
                return "ok"

        assert mock_protected.call_args.kwargs["fallback"] is my_fallback

    def test_timeout_omitted_is_not_forwarded_as_kwarg(self):
        # When timeout is omitted, dlq_protect must NOT inject a ``timeout`` key
        # so ProtectSettings defaults apply downstream.
        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("svc.op")
            def op() -> str:
                return "ok"

        assert "timeout" not in mock_protected.call_args.kwargs

    def test_timeout_explicit_value_forwarded(self):
        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("svc.op", timeout=2.5)
            def op() -> str:
                return "ok"

        assert mock_protected.call_args.kwargs["timeout"] == 2.5

    def test_timeout_explicit_none_forwarded(self):
        # Explicit None means "disable timeout" — must reach the facade.
        with patch(
            "baldur.decorators.dlq_protect.protected",
            return_value=lambda fn: fn,
        ) as mock_protected:

            @dlq_protect("svc.op", timeout=None)
            def op() -> str:
                return "ok"

        assert mock_protected.call_args.kwargs["timeout"] is None

    def test_decorator_preserves_function_metadata(self):
        @dlq_protect("svc.op")
        def my_op(a: int, b: int) -> int:
            """Docstring preserved."""
            return a + b

        assert my_op.__name__ == "my_op"
        assert my_op.__doc__ == "Docstring preserved."


# =============================================================================
# 499 — Composer cache identity check via @dlq_protect
# =============================================================================


class TestDlqProtectComposerCacheIdentity:
    """499 — two ``@dlq_protect("name")`` invocations share the same
    underlying ``PolicyComposer`` instance via ``_composer_cache``
    (looked up by the 3-tuple ``(name, timeout, "dlq_protect")``)."""

    # 525 D4: xdist state_leak — _composer_cache shared module-level dict
    # races with sibling tests under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="state_leak"
    )
    def test_repeated_invocations_share_cached_composer(self):
        import baldur.protect_facade as protect_module
        from baldur.settings.protect import reset_protect_settings

        reset_protect_settings()
        try:

            @dlq_protect("orders.cache_identity")
            def charge(order_id: int) -> str:
                return f"charged:{order_id}"

            charge(1)
            key = ("orders.cache_identity", None, "dlq_protect")
            assert key in protect_module._composer_cache
            first = protect_module._composer_cache[key]

            charge(2)
            second = protect_module._composer_cache[key]

            assert first is second
        finally:
            reset_protect_settings()
