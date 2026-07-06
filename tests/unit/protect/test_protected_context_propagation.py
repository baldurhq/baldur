"""Regression guard for #504 D2 — explicit ``protect(..., context=X)`` direct
callers must continue to thread ``X`` end-to-end through the composer to the
DLQ sink, even after the decorator path gained the auto-extract helper.

The risk D2 hedges against: a future refactor that wires
``_build_context_from_callsite`` into the ``protect()`` facade itself would
overwrite a caller-supplied ``context=`` with an empty auto-extract (the
facade has no wrapped-function args to extract from). Auto-extract MUST live
at the decorator entry only.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.protect_facade import aprotect, protect


@pytest.fixture(autouse=True)
def _reset_protect_settings_singleton():
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()
    yield
    reset_protect_settings()


# =============================================================================
# Behavior — protect() threads explicit context= to composer.execute()
# =============================================================================


class TestProtectExplicitContextPropagationBehavior:
    """``protect(name, fn, context=ctx)`` must call
    ``composer.execute(fn, context=ctx)`` with the exact same instance."""

    def test_explicit_context_reaches_composer_execute(self):
        explicit_ctx = PolicyContext(order_id="o-explicit", user_id="u-explicit")

        captured: dict = {}

        def fake_execute(fn, context):
            captured["context"] = context
            return PolicyResult(
                value="ok",
                outcome=PolicyOutcome.SUCCESS,
                total_attempts=1,
            )

        with patch(
            "baldur.protect_facade._build_sync_composer",
            return_value=MagicMock(execute=fake_execute),
        ):
            result = protect(
                name="svc.regression",
                fn=lambda: "ok",
                dlq=False,
                retry=False,
                circuit_breaker=False,
                context=explicit_ctx,
            )

        assert result == "ok"
        # IDENTITY check — the helper must not have constructed a new
        # PolicyContext around the explicit one.
        assert captured["context"] is explicit_ctx

    def test_protect_with_none_context_reaches_composer_as_none(self):
        """Sanity inverse: when no context is passed, the facade forwards
        ``None`` (auto-extract lives at the decorator layer, not here)."""
        captured: dict = {}

        def fake_execute(fn, context):
            captured["context"] = context
            return PolicyResult(
                value="ok",
                outcome=PolicyOutcome.SUCCESS,
                total_attempts=1,
            )

        with patch(
            "baldur.protect_facade._build_sync_composer",
            return_value=MagicMock(execute=fake_execute),
        ):
            protect(
                name="svc.regression",
                fn=lambda: "ok",
                dlq=False,
                retry=False,
                circuit_breaker=False,
            )

        assert captured["context"] is None


class TestAprotectExplicitContextPropagationBehavior:
    """Async parity for D2 — explicit ``context=`` propagates through
    ``aprotect`` → ``AsyncPolicyComposer.execute``."""

    def test_aprotect_explicit_context_reaches_async_composer_execute(self):
        import asyncio

        explicit_ctx = PolicyContext(order_id="o-async", user_id="u-async")
        captured: dict = {}

        async def fake_async_execute(fn, context):
            captured["context"] = context
            return PolicyResult(
                value="ok",
                outcome=PolicyOutcome.SUCCESS,
                total_attempts=1,
            )

        with patch(
            "baldur.protect_facade._build_async_composer",
            return_value=MagicMock(execute=fake_async_execute),
        ):

            async def afn():
                return "ok"

            result = asyncio.run(
                aprotect(
                    name="svc.async",
                    fn=afn,
                    dlq=False,
                    context=explicit_ctx,
                )
            )

        assert result == "ok"
        assert captured["context"] is explicit_ctx


# =============================================================================
# Behavior — explicit context reaches the DLQ sink (end-to-end)
# =============================================================================


class TestProtectExplicitContextReachesDlqSinkBehavior:
    """Stricter end-to-end check: when ``protect()`` is called with
    ``dlq=True, retry=True, context=ctx`` and ``fn`` exhausts retries, the
    captured ``ctx.order_id`` lands in ``store_to_dlq(entity_id=...)``."""

    def test_dlq_store_receives_entity_id_from_explicit_context(self):
        explicit_ctx = PolicyContext(
            order_id="o-end2end",
            user_id="7",
        )

        with patch("baldur.services.retry_handler.sinks.store_to_dlq") as mock_store:
            mock_store.return_value = MagicMock(success=True, dlq_id="dlq-1")

            with pytest.raises(RuntimeError):
                protect(
                    name="svc.end2end",
                    fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                    dlq=True,
                    retry=True,
                    circuit_breaker=False,
                    context=explicit_ctx,
                )

        assert mock_store.called
        kwargs = mock_store.call_args.kwargs
        # entity_id ← context.order_id
        assert kwargs["entity_id"] == "o-end2end"
        # user_id ← named PolicyContext.user_id (D10 — int-coerced for the
        # int-typed DLQ column).
        assert kwargs["user_id"] == 7
