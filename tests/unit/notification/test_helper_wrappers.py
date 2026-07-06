"""OSS-side notification helper wrappers — `baldur.notification.helpers` (518 batch a).

Scope:
- ``_get_pro()`` cached resolution: idempotent, ``_resolved`` flag transition.
- Each public wrapper delegates verbatim args/kwargs to the matching PRO
  function when PRO is present, and silently returns ``None`` when absent.

Difference from `baldur.audit.helpers`: notification wrappers use the
inline ``if (p := _get_pro()) is None: return None`` pattern rather than a
``_safe_delegate`` helper, so there is no ``_safe_delegate`` test class
here. PRO-side exceptions propagate raw to the caller — the notification
path is **not** wrapped fail-open (notification delivery failures are the
caller's responsibility, distinct from audit-write fail-open semantics).
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import baldur.notification.helpers as helpers

PRO_MODULE_NAME = "baldur_pro.services.unified_notification"


# =============================================================================
# _get_pro — cached PRO module resolution
# =============================================================================


def _patched_import_factory(target_name: str, raises: bool):
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if raises and name == target_name:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    return _fake_import


class TestNotificationHelpersResolutionBehavior:
    """``_get_pro()`` caches the PRO module resolution under ``_resolved``."""

    def test_get_pro_returns_module_when_pro_present(self):
        pytest.importorskip("baldur_pro")
        result = helpers._get_pro()

        assert result is not None
        assert helpers._resolved is True
        assert helpers._pro is result

    def test_get_pro_returns_none_when_pro_absent(self):
        fake_import = _patched_import_factory(PRO_MODULE_NAME, raises=True)
        with patch.object(builtins, "__import__", side_effect=fake_import):
            result = helpers._get_pro()

        assert result is None
        assert helpers._resolved is True
        assert helpers._pro is None

    def test_get_pro_is_idempotent_after_first_resolution(self):
        pytest.importorskip("baldur_pro")
        first = helpers._get_pro()
        assert first is not None

        fake_import = _patched_import_factory(PRO_MODULE_NAME, raises=True)
        with patch.object(builtins, "__import__", side_effect=fake_import):
            second = helpers._get_pro()

        assert second is first  # cached, not re-resolved


# =============================================================================
# Public wrapper delegation
# =============================================================================


@pytest.fixture
def fake_pro_with_recorder(monkeypatch):
    recorder: dict[str, MagicMock] = {}

    class _FakePRO:
        def __getattr__(self, name: str) -> MagicMock:
            if name not in recorder:
                recorder[name] = MagicMock(return_value=("ok", name))
            return recorder[name]

    fake = _FakePRO()
    monkeypatch.setattr(helpers, "_get_pro", lambda: fake)
    monkeypatch.setattr(helpers, "_pro", fake)
    monkeypatch.setattr(helpers, "_resolved", True)
    return fake, recorder


@pytest.fixture
def pro_absent(monkeypatch):
    monkeypatch.setattr(helpers, "_get_pro", lambda: None)
    monkeypatch.setattr(helpers, "_pro", None)
    monkeypatch.setattr(helpers, "_resolved", True)


class TestNotificationHelpersDelegationContract:
    """Every name in `__all__` matches the {present, absent} contract.

    Notification helpers do NOT wrap fail-open: a present PRO that raises
    will propagate raw to the caller. The absent-PRO path returns ``None``,
    matching the OSS no-op promise.
    """

    @pytest.mark.parametrize("wrapper_name", helpers.__all__)
    def test_wrapper_delegates_to_pro_when_present(
        self, wrapper_name, fake_pro_with_recorder
    ):
        _, recorder = fake_pro_with_recorder
        wrapper = getattr(helpers, wrapper_name)

        result = wrapper("title-1", "body", priority="high")

        assert wrapper_name in recorder
        recorder[wrapper_name].assert_called_once_with(
            "title-1", "body", priority="high"
        )
        assert result == ("ok", wrapper_name)

    @pytest.mark.parametrize("wrapper_name", helpers.__all__)
    def test_wrapper_returns_none_when_pro_absent(self, wrapper_name, pro_absent):
        wrapper = getattr(helpers, wrapper_name)

        result = wrapper("title", "body")

        assert result is None

    def test_pro_exception_propagates_to_caller(self):
        """Notification helpers are NOT fail-open — exceptions surface."""

        def _boom(*a, **kw):
            raise RuntimeError("slack webhook 500")

        fake_pro = SimpleNamespace(notify=_boom)
        with patch.object(helpers, "_get_pro", return_value=fake_pro):
            with pytest.raises(RuntimeError, match="slack webhook 500"):
                helpers.notify("alert", "msg")

    def test_all_wrappers_listed_match_module_attributes(self):
        for name in helpers.__all__:
            obj = getattr(helpers, name, None)
            assert callable(obj), f"{name} declared in __all__ but not callable"
