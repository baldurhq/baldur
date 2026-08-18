"""ConfigApplyService's PRO-unavailable short-circuit (759 D6).

``apply_pending_changes()`` is the tick body behind both the celery beat lane
and the in-process scheduler job. When the PRO runtime-config manager cannot be
imported the tick returns a ``blocked`` dict, and the celery lane feeds that
dict's ``reason`` straight into the audit trail — so the value is a contract,
not an internal detail.

The PRO import is failed deterministically by pinning ``None`` into
``sys.modules`` for the target module rather than by relying on the tier of the
checkout: this file runs in both a PRO-present and a PRO-absent tree, and only
the explicit pin gives the same arm in each.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.execution_services.config_apply_service import (
    get_config_apply_service,
    reset_config_apply_service,
)
from baldur.services.pending_config import PendingConfigService

_PRO_RUNTIME_CONFIG_MODULE = "baldur_pro.services.runtime_config"


@pytest.fixture
def service():
    """A ConfigApplyService with no cached instance carried in or out."""
    reset_config_apply_service()
    yield get_config_apply_service()
    reset_config_apply_service()


@pytest.fixture
def pro_runtime_config_unimportable():
    """Make the PRO runtime-config import raise, whatever the checkout's tier.

    ``None`` in ``sys.modules`` is the import system's own "halted" marker, so
    the ``from ... import ...`` inside the service raises ImportError exactly as
    it does on an install without the PRO distribution.
    """
    with patch.dict(sys.modules, {_PRO_RUNTIME_CONFIG_MODULE: None}):
        yield


class TestConfigApplyUnavailableContract:
    """The PRO-unavailable return is the value the audit trail records."""

    def test_missing_runtime_config_manager_returns_the_blocked_contract(
        self, service, pro_runtime_config_unimportable
    ):
        """status=blocked, reason=runtime_config_manager_unavailable.

        Hardcoded: the reason string is what the celery lane writes to the audit
        trail, so a rename is a contract change no matter how internal it looks.
        """
        result = service.apply_pending_changes()

        assert result["status"] == "blocked"
        assert result["reason"] == "runtime_config_manager_unavailable"


class TestConfigApplyBrokenProBehavior:
    """An idle tick must not report success just because nothing was due."""

    def test_idle_tick_still_reports_blocked_when_the_pro_import_fails(
        self, service, pro_runtime_config_unimportable
    ):
        """No due changes AND no runtime-config manager → blocked, not success.

        A negative assertion on purpose. Moving the due-check ahead of the PRO
        import would make an idle tick answer ``success``/``applied 0`` on an
        install that can never apply anything — turning a diagnostic into a
        false all-clear. This fails the moment that reorder is made.
        """
        pending_service = MagicMock(spec=PendingConfigService)
        pending_service.get_due_changes.return_value = []

        with patch(
            "baldur.services.pending_config.get_pending_config_service",
            return_value=pending_service,
        ):
            result = service.apply_pending_changes()

        assert result["status"] == "blocked"
        assert "applied" not in result
