"""Unit tests for 545 chokepoint 5 — Celery context restore funnels through
``set_domain_context()`` so OTel-baggage / legacy-header injected ``domain``
values inherit validation + fallback.

545 D5:
    ``context/celery_context_utils.py:603`` was previously
    ``_current_domain.set(domain)`` (bypassing validation). The chokepoint
    rewires this to ``set_domain_context(domain)``, delegating to chokepoint 3.

Reference:
    545 D5 (item 5), D6
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from baldur.context.celery_context_utils import restore_all_task_context
from baldur.decorators.domain_tag import (
    _current_domain,
    clear_domain_context,
)
from baldur.utils.domain_validation import FALLBACK_DOMAIN, DomainRejectReason


@pytest.fixture(autouse=True)
def _clear_context_each_test():
    clear_domain_context()
    yield
    clear_domain_context()


def _make_celery_task(domain_header_value: object | None) -> MagicMock:
    """Build a minimal Celery task stub that returns ``domain_header_value``
    from ``task.request.get("domain")``."""
    task = MagicMock()
    request = MagicMock()
    request.headers = {}
    request.retries = 0

    def _get(key, default=None):
        if key == "domain":
            return domain_header_value
        return default

    request.get = _get
    task.request = request
    return task


class TestCeleryDomainRestoreBehavior:
    """Bad ``domain`` in legacy header → fallback via chokepoint 3 funnel."""

    def test_valid_domain_passes_through(self):
        task = _make_celery_task("payment")
        restore_all_task_context(task, task_id="t-1", task_name="t")
        assert _current_domain.get() == "payment"

    def test_uuid_in_legacy_header_falls_back(self):
        """The canonical buggy upstream caller — Celery header carries a UUID."""
        task = _make_celery_task(str(uuid4()))
        restore_all_task_context(task, task_id="t-1", task_name="t")
        assert _current_domain.get() == FALLBACK_DOMAIN

    @pytest.mark.parametrize(
        "bad_domain",
        [
            "1payment",
            "invalid name!",
            "payment-charge",
            "a" * 200,
        ],
    )
    def test_invalid_legacy_header_domains_fall_back(self, bad_domain):
        task = _make_celery_task(bad_domain)
        restore_all_task_context(task, task_id="t-1", task_name="t")
        assert _current_domain.get() == FALLBACK_DOMAIN

    def test_none_domain_leaves_context_unset(self):
        """No domain in baggage or header → ``_current_domain`` stays ``None``.

        The restore branch is skipped when ``_resolve_domain`` returns
        ``(None, "none")`` — ``set_domain_context`` is not invoked.
        """
        task = _make_celery_task(None)
        restore_all_task_context(task, task_id="t-1", task_name="t")
        assert _current_domain.get() is None

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_metric_uses_set_domain_context_site(self, mock_get_metrics):
        """Chokepoint 5 funnels through chokepoint 3 — site must be
        ``"set_domain_context"`` (NOT a new ``"celery_restore"`` label) so the
        Counter cardinality stays bounded to 3 sites."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        task = _make_celery_task(str(uuid4()))
        restore_all_task_context(task, task_id="t-1", task_name="t")

        mock_metrics.record_dlq_domain_input_rejected.assert_called_once_with(
            "set_domain_context"
        )

    @pytest.mark.flaky_quarantine(
        issue="545", first_seen="2026-05-28", category="mock_leak"
    )
    def test_emits_warning_log_on_rejection(self):
        task = _make_celery_task(str(uuid4()))
        with capture_logs() as logs:
            restore_all_task_context(task, task_id="t-1", task_name="t")

        rejected = [e for e in logs if e.get("event") == "domain.input_rejected"]
        assert len(rejected) == 1
        entry = rejected[0]
        assert entry["site"] == "set_domain_context"
        assert entry["reason"] == DomainRejectReason.INVALID_CHARSET.value
