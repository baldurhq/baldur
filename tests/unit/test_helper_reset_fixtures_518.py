"""Meta-tests for the OSS->PRO helper cache reset fixtures (518 batch a).

The autouse fixtures ``reset_audit_helpers`` / ``reset_notification_helpers``
/ ``reset_dlq_helpers`` in ``tests/conftest.py`` clear the module-level PRO
module caches in ``baldur.{audit,notification,dlq}.helpers`` between tests.
They exist because ``reset_audit_modules`` (the broader reset fixture) pops
``baldur_pro.*`` from ``sys.modules`` between tests, which would leave the
helpers caching a stale module object pointer.

We drive each fixture manually as a generator so the assertions cover both
the pre-yield body (state at test-entry) and the post-yield body (cleanup),
without depending on inter-test ordering.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import baldur.audit.helpers as audit_helpers
import baldur.dlq.helpers as dlq_helpers
import baldur.notification.helpers as notification_helpers


def _drive_fixture(fixture_callable):
    """Manually drive a pytest fixture defined as a generator function.

    The ``@pytest.fixture`` decorator wraps the underlying generator; we
    unwrap it so we can step through ``pre-yield -> yield -> post-yield``.
    Returns a tuple of (pre-yield-checkpoint-callable, run-cleanup-callable).
    """
    # Pytest 9 exposes the undecorated function as `_fixture_function`.
    # Earlier versions used `__pytest_wrapped__.obj`. Support both.
    raw = (
        getattr(fixture_callable, "_fixture_function", None)
        or fixture_callable.__pytest_wrapped__.obj
    )
    gen = raw()
    # First next() runs the pre-yield body and stops at `yield`.
    next(gen)

    def run_cleanup() -> None:
        try:
            next(gen)
        except StopIteration:
            pass

    return run_cleanup


# =============================================================================
# reset_audit_helpers
# =============================================================================


class TestResetAuditHelpersFixtureBehavior:
    """The fixture clears `_pro` and `_resolved` on entry AND on cleanup."""

    def test_pre_yield_clears_dirty_cache(self):
        # Pre-populate the cache with sentinel values.
        audit_helpers._pro = SimpleNamespace(marker="dirty")
        audit_helpers._resolved = True

        from tests.conftest import reset_audit_helpers

        run_cleanup = _drive_fixture(reset_audit_helpers)

        # After the pre-yield body runs, cache must be reset.
        assert audit_helpers._pro is None
        assert audit_helpers._resolved is False
        run_cleanup()

    def test_post_yield_clears_dirty_cache(self):
        from tests.conftest import reset_audit_helpers

        run_cleanup = _drive_fixture(reset_audit_helpers)

        # Pollute the cache during the "test body" window.
        audit_helpers._pro = SimpleNamespace(marker="dirty")
        audit_helpers._resolved = True

        run_cleanup()

        # Post-yield body must have re-cleaned the cache.
        assert audit_helpers._pro is None
        assert audit_helpers._resolved is False

    def test_reset_is_idempotent_on_already_clean_state(self):
        audit_helpers._pro = None
        audit_helpers._resolved = False

        from tests.conftest import reset_audit_helpers

        run_cleanup = _drive_fixture(reset_audit_helpers)
        assert audit_helpers._pro is None
        run_cleanup()
        assert audit_helpers._pro is None


# =============================================================================
# reset_notification_helpers
# =============================================================================


class TestResetNotificationHelpersFixtureBehavior:
    """Same shape as the audit fixture, for ``baldur.notification.helpers``."""

    def test_pre_yield_clears_dirty_cache(self):
        notification_helpers._pro = SimpleNamespace(marker="dirty")
        notification_helpers._resolved = True

        from tests.conftest import reset_notification_helpers

        run_cleanup = _drive_fixture(reset_notification_helpers)

        assert notification_helpers._pro is None
        assert notification_helpers._resolved is False
        run_cleanup()

    def test_post_yield_clears_dirty_cache(self):
        from tests.conftest import reset_notification_helpers

        run_cleanup = _drive_fixture(reset_notification_helpers)

        notification_helpers._pro = SimpleNamespace(marker="dirty")
        notification_helpers._resolved = True

        run_cleanup()

        assert notification_helpers._pro is None
        assert notification_helpers._resolved is False


# =============================================================================
# reset_dlq_helpers — three independent sub-caches
# =============================================================================


# The fixture clears six attributes; this table makes the per-cache assertions
# data-driven instead of duplicating six near-identical method bodies.
DLQ_CACHE_ATTRS = [
    ("_pro_dlq", "_resolved_dlq"),
    ("_pro_dlq_compression", "_resolved_dlq_compression"),
    ("_pro_postmortem_store", "_resolved_postmortem_store"),
]


class TestResetDlqHelpersFixtureBehavior:
    """The fixture clears all three sub-module caches on entry AND on cleanup."""

    @pytest.mark.parametrize(("pro_attr", "resolved_attr"), DLQ_CACHE_ATTRS)
    def test_pre_yield_clears_each_subcache(self, pro_attr, resolved_attr):
        setattr(dlq_helpers, pro_attr, SimpleNamespace(marker="dirty"))
        setattr(dlq_helpers, resolved_attr, True)

        from tests.conftest import reset_dlq_helpers

        run_cleanup = _drive_fixture(reset_dlq_helpers)

        assert getattr(dlq_helpers, pro_attr) is None
        assert getattr(dlq_helpers, resolved_attr) is False
        run_cleanup()

    @pytest.mark.parametrize(("pro_attr", "resolved_attr"), DLQ_CACHE_ATTRS)
    def test_post_yield_clears_each_subcache(self, pro_attr, resolved_attr):
        from tests.conftest import reset_dlq_helpers

        run_cleanup = _drive_fixture(reset_dlq_helpers)

        setattr(dlq_helpers, pro_attr, SimpleNamespace(marker="dirty"))
        setattr(dlq_helpers, resolved_attr, True)

        run_cleanup()

        assert getattr(dlq_helpers, pro_attr) is None
        assert getattr(dlq_helpers, resolved_attr) is False

    def test_pre_yield_clears_all_three_sub_caches_at_once(self):
        """Single pollution wave across all three sub-caches resets together."""
        for pro_attr, resolved_attr in DLQ_CACHE_ATTRS:
            setattr(dlq_helpers, pro_attr, SimpleNamespace(marker="dirty"))
            setattr(dlq_helpers, resolved_attr, True)

        from tests.conftest import reset_dlq_helpers

        run_cleanup = _drive_fixture(reset_dlq_helpers)

        for pro_attr, resolved_attr in DLQ_CACHE_ATTRS:
            assert getattr(dlq_helpers, pro_attr) is None
            assert getattr(dlq_helpers, resolved_attr) is False
        run_cleanup()
