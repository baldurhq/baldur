"""
retry_handler package re-export unit tests.

Test targets: services/retry_handler/__init__.py
- Package-level re-export check (RetryPolicy, Guards, Sinks)

Note: the ``@with_retry`` decorator was removed in 670 (superseded by the
unified ``@retry`` in resilience/policies/async_retry.py). Its behavior is now
covered by test_retry_decorator.py.
"""

from __future__ import annotations

import pytest

from baldur.services import retry_handler as pkg

# =============================================================================
# Package re-export — contract
# =============================================================================


class TestRetryHandlerPackageExportsContract:
    """Verify the new classes are re-exported correctly from the retry_handler package."""

    @pytest.mark.parametrize(
        "name",
        [
            "RetryPolicy",
            "RetryPolicyConfig",
            "ErrorBudgetGuard",
            "DLQSink",
            "detect_rate_limit",
        ],
    )
    def test_new_symbol_importable(self, name: str):
        """Newly added symbols are importable from the package."""
        assert hasattr(pkg, name), f"{name} is not exported from retry_handler"

    def test_all_new_symbols_in_dunder_all(self):
        """__all__ carries the five new symbols."""
        expected = {
            "RetryPolicy",
            "RetryPolicyConfig",
            "ErrorBudgetGuard",
            "DLQSink",
            "detect_rate_limit",
        }
        assert expected.issubset(set(pkg.__all__))

    def test_kill_switch_guard_removed(self):
        """The kill-switch guard is gone: the switch rides the resolver."""
        assert not hasattr(pkg, "KillSwitchGuard")
        assert "KillSwitchGuard" not in pkg.__all__

    def test_with_retry_removed(self):
        """``with_retry`` was removed in 670 (superseded by unified ``@retry``)."""
        assert not hasattr(pkg, "with_retry")
        assert "with_retry" not in pkg.__all__
