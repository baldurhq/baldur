"""End-to-end DLQ activation tests for ``protect(..., dlq=True, retry=True)`` (#466).

Closes G3 of 466 — verifies that the metadata-propagation seam between
``RetryPolicy.execute`` (which sets ``should_dlq=True`` on FAILURE) and
``DLQSink.handle_failure`` (which gates on ``policy_result.metadata['should_dlq']``)
remains intact end-to-end through ``PolicyComposer``. Without the fix landed
in ``composer.py``, ``should_dlq`` was lost in the outer catch branch and
``store_to_dlq`` was never invoked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.protect_facade import protect, protect_with_meta, reset_protect_caches
from baldur.services.retry_handler.models import RetryPolicyConfig

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


@pytest.fixture(autouse=True)
def _reset_protect_state():
    """Reset protect-scope caches between tests so the #485 D1b/G4
    ``baldur_pro.services.dlq.store_to_dlq`` resolver re-resolves under the
    test's ``patch(...)`` context. Without this, the first test populates
    the cached function ref and subsequent patches do not intercept.
    """
    reset_protect_caches()
    yield
    reset_protect_caches()


# =============================================================================
# Behavior — DLQ activation through composer chain
# =============================================================================


class TestProtectDlqChainBehavior:
    """``protect(..., dlq=True, retry=True)`` invokes ``store_to_dlq`` on
    Retry exhaustion. Pre-fix the call never happened (G1 metadata loss).
    """

    def _retry_cfg(
        self, max_attempts: int = 2, *, enable_dlq: bool = True
    ) -> RetryPolicyConfig:
        return RetryPolicyConfig(
            max_attempts=max_attempts,
            backoff_base=0,
            backoff_max=0,
            jitter_percent=0,
            enable_dlq=enable_dlq,
            domain="test_protect_dlq",
        )

    def test_store_to_dlq_called_on_retry_exhaustion(self):
        """Always-failing fn under protect(dlq=True, retry=cfg) → store_to_dlq fires."""

        def always_fails() -> None:
            raise ValueError("always_fails")

        with patch(
            "baldur_pro.services.dlq.store_to_dlq",
            return_value=MagicMock(success=True, dlq_id="dlq-123", error=None),
        ) as mock_store:
            with pytest.raises(ValueError):
                protect(
                    "test.charge",
                    always_fails,
                    dlq=True,
                    retry=self._retry_cfg(),
                    circuit_breaker=False,
                    timeout=None,
                )

        # Pre-fix this would be 0 — the metadata-loss bug short-circuited DLQSink.
        assert mock_store.call_count == 1

    def test_store_to_dlq_skipped_when_should_dlq_false(self):
        """RetryPolicy with enable_dlq=False → metadata['should_dlq']=False → no DLQ write.

        Pass a pre-built RetryPolicy instance so protect()'s automatic
        ``enable_dlq=True`` override (in ``_resolve_retry_stage``) does not fire.
        """
        from baldur.services.retry_handler.policy import RetryPolicy

        def always_fails() -> None:
            raise ValueError("err")

        retry_policy = RetryPolicy(config=self._retry_cfg(enable_dlq=False))

        with patch(
            "baldur_pro.services.dlq.store_to_dlq",
            return_value=MagicMock(success=True, dlq_id="x", error=None),
        ) as mock_store:
            with pytest.raises(ValueError):
                protect(
                    "test.no_dlq",
                    always_fails,
                    dlq=True,  # sink wired
                    retry=retry_policy,
                    circuit_breaker=False,
                    timeout=None,
                )

        assert mock_store.call_count == 0

    def test_store_to_dlq_receives_domain_from_metadata(self):
        """RetryPolicy.metadata['domain'] reaches store_to_dlq via DLQSink."""

        def always_fails() -> None:
            raise RuntimeError("boom")

        with patch(
            "baldur_pro.services.dlq.store_to_dlq",
            return_value=MagicMock(success=True, dlq_id="dlq-1", error=None),
        ) as mock_store:
            with pytest.raises(RuntimeError):
                protect(
                    "test.charge",
                    always_fails,
                    dlq=True,
                    retry=self._retry_cfg(),
                    circuit_breaker=False,
                    timeout=None,
                )

        assert mock_store.call_args.kwargs["domain"] == "test_protect_dlq"

    def test_protect_with_meta_exposes_should_dlq_metadata(self):
        """protect_with_meta() ProtectResult.metadata also carries should_dlq."""

        def always_fails() -> None:
            raise ValueError("boom")

        with patch(
            "baldur_pro.services.dlq.store_to_dlq",
            return_value=MagicMock(success=True, dlq_id="dlq-7", error=None),
        ):
            outcome = protect_with_meta(
                "test.with_meta",
                always_fails,
                dlq=True,
                retry=self._retry_cfg(),
                circuit_breaker=False,
                timeout=None,
            )

        assert outcome.success is False
        assert outcome.metadata.get("should_dlq") is True
        # DLQSink writes its sink_id back onto the result's metadata too.
        assert outcome.metadata.get("sink_id") == "dlq-7"
