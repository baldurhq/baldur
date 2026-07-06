"""``protect(dlq=True)`` warns once when no DLQ store backs the request.

On OSS (``baldur_pro`` absent) ``store_to_dlq`` silently no-ops, so a
``dlq=True`` request is accepted yet final failures are never durably captured.
The facade emits a single WARNING (``protect.dlq_requested_without_backing``)
the first time such a composer is wired, then stays quiet — the failure itself
still propagates to the caller regardless.

The warning is observed by replacing the module logger rather than capturing
log output: the facade routes structlog through stdlib logging with a cached
module-level logger, which ``structlog.testing.capture_logs()`` cannot
intercept after first use. Counting ``logger.warning`` calls is routing-
independent and exact.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import baldur.dlq.helpers as helpers
import baldur.protect_facade as pf
from baldur.protect_facade import protect, reset_protect_caches

_EVENT = "protect.dlq_requested_without_backing"


def _boom() -> None:
    raise ValueError("fail")


def _warn_count(logger: MagicMock) -> int:
    return sum(
        1
        for call in logger.warning.call_args_list
        if call.args and call.args[0] == _EVENT
    )


class TestDlqBackingWarning:
    def test_warns_once_across_callsites_when_backing_absent(self, monkeypatch):
        """Two distinct dlq=True callsites emit the warning exactly once."""
        monkeypatch.setattr(helpers, "_get_pro_dlq", lambda: None)
        monkeypatch.setattr(pf, "logger", MagicMock())
        reset_protect_caches()  # re-arm the once-per-process flag

        for name in ("svc_a", "svc_b"):
            try:
                protect(name, _boom, retry=False, circuit_breaker=False, dlq=True)
            except ValueError:
                pass

        assert _warn_count(pf.logger) == 1

    def test_no_warning_when_backing_available(self, monkeypatch):
        """A resolvable PRO store keeps the path silent."""
        monkeypatch.setattr(helpers, "_get_pro_dlq", lambda: object())
        monkeypatch.setattr(pf, "logger", MagicMock())
        reset_protect_caches()

        try:
            protect("svc_ok", _boom, retry=False, circuit_breaker=False, dlq=True)
        except ValueError:
            pass

        assert _warn_count(pf.logger) == 0

    def test_no_warning_when_dlq_not_requested(self, monkeypatch):
        """dlq defaults off — an unrequested DLQ never warns even on OSS."""
        monkeypatch.setattr(helpers, "_get_pro_dlq", lambda: None)
        monkeypatch.setattr(pf, "logger", MagicMock())
        reset_protect_caches()

        try:
            protect("svc_nodlq", _boom, retry=False, circuit_breaker=False, dlq=False)
        except ValueError:
            pass

        assert _warn_count(pf.logger) == 0

    def test_failure_still_propagates_to_caller(self, monkeypatch):
        """The warning is advisory: the original exception still raises, so the
        caller is never left believing a silently-dropped failure succeeded.
        """
        monkeypatch.setattr(helpers, "_get_pro_dlq", lambda: None)
        reset_protect_caches()

        raised = False
        try:
            protect("svc_raise", _boom, retry=False, circuit_breaker=False, dlq=True)
        except ValueError:
            raised = True
        assert raised
