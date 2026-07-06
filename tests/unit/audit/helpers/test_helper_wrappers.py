"""OSS-side audit helper wrappers — `baldur.audit.helpers` (518 batch a).

Scope:
- ``_get_pro()`` cached resolution: idempotent, ``_resolved`` flag transition.
- ``_safe_delegate()`` fail-open: exception swallowed, ``None`` returned, WARNING
  log emitted (per LOGGING_STANDARDS §3.2 — audit/compliance failures are
  GDPR/SOC2-essential; the 518 fix 12a13724 raised this from DEBUG to WARNING).
- Each public wrapper delegates verbatim args/kwargs to the matching PRO
  function when PRO is present, and silently returns ``None`` when absent.
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import baldur.audit.helpers as helpers

PRO_MODULE_NAME = "baldur_pro.services.audit"


# =============================================================================
# _get_pro — cached PRO module resolution
# =============================================================================


def _patched_import_factory(target_name: str, raises: bool):
    """Return an `__import__` replacement that raises ImportError for `target_name`.

    Used to simulate PRO-absent in an environment where baldur_pro IS installed.
    Other imports pass through to the real `__import__`.
    """
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if raises and name == target_name:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    return _fake_import


class TestAuditHelpersResolutionBehavior:
    """``_get_pro()`` caches the PRO module resolution under ``_resolved``."""

    def test_get_pro_returns_module_when_pro_present(self):
        pytest.importorskip("baldur_pro")
        # Real environment: baldur_pro is installed, so import succeeds.
        result = helpers._get_pro()

        assert result is not None
        assert helpers._resolved is True
        # Sanity: the cache holds the same object the call returned.
        assert helpers._pro is result

    def test_get_pro_returns_none_when_pro_absent(self):
        fake_import = _patched_import_factory(PRO_MODULE_NAME, raises=True)
        with patch.object(builtins, "__import__", side_effect=fake_import):
            result = helpers._get_pro()

        assert result is None
        assert helpers._resolved is True
        assert helpers._pro is None

    def test_get_pro_is_idempotent_after_first_resolution(self):
        """Second call returns the cached object without retrying the import.

        State transition: ``_resolved`` goes ``False -> True`` on the first
        call, then the cached ``_pro`` reference is returned without re-import.
        We verify this by raising ImportError on the second call — if the
        cache works, the second call never reaches the import line.
        """
        pytest.importorskip("baldur_pro")
        # First call resolves (PRO is installed → returns real module).
        first = helpers._get_pro()
        assert first is not None
        assert helpers._resolved is True

        # Now poison the import path; cached call must not touch it.
        fake_import = _patched_import_factory(PRO_MODULE_NAME, raises=True)
        with patch.object(builtins, "__import__", side_effect=fake_import):
            second = helpers._get_pro()

        assert second is first  # cached, not re-resolved


# =============================================================================
# _safe_delegate — fail-open with WARNING-level log
# =============================================================================


class TestAuditHelpersSafeDelegateBehavior:
    """``_safe_delegate()`` forwards on success and swallows exceptions to None."""

    def test_safe_delegate_returns_pro_value_on_success(self):
        fake_pro = SimpleNamespace(log_dlq_store_audit=lambda *a, **kw: 42)
        with patch.object(helpers, "_get_pro", return_value=fake_pro):
            result = helpers._safe_delegate(
                "log_dlq_store_audit", dlq_id=1, domain="x", failure_type="y"
            )

        assert result == 42

    def test_safe_delegate_returns_none_when_pro_absent(self):
        with patch.object(helpers, "_get_pro", return_value=None):
            result = helpers._safe_delegate("log_dlq_store_audit", dlq_id=1)

        assert result is None

    def test_safe_delegate_swallows_exception_and_returns_none(self):
        def _boom(*a, **kw):
            raise RuntimeError("WAL unreachable")

        fake_pro = SimpleNamespace(log_dlq_store_audit=_boom)
        with patch.object(helpers, "_get_pro", return_value=fake_pro):
            result = helpers._safe_delegate("log_dlq_store_audit")

        assert result is None  # fail-open contract

    def test_safe_delegate_logs_warning_when_pro_call_raises(self):
        """Fix 12a13724: failure log level is WARNING, not DEBUG.

        Audit/compliance writes are GDPR/SOC2-essential. Operators MUST be
        notified when an audit write is missed, even though the caller's
        primary operation continues fail-open.
        """

        def _boom(*a, **kw):
            raise RuntimeError("adapter misconfigured")

        fake_pro = SimpleNamespace(log_dlq_store_audit=_boom)
        with (
            patch.object(helpers, "_get_pro", return_value=fake_pro),
            patch.object(helpers, "logger") as mock_logger,
        ):
            helpers._safe_delegate("log_dlq_store_audit")

        mock_logger.warning.assert_called_once()
        event_name = mock_logger.warning.call_args.args[0]
        kwargs = mock_logger.warning.call_args.kwargs
        assert event_name == "audit.helper_failed"
        assert kwargs["function"] == "log_dlq_store_audit"
        assert kwargs["exc_info"] is True
        # The pre-fix code logged at DEBUG; assert that never happens now.
        mock_logger.debug.assert_not_called()

    def test_safe_delegate_forwards_args_and_kwargs_verbatim(self):
        """Mixed positional + keyword args reach PRO unchanged."""
        mock_pro_fn = MagicMock(return_value=7)
        fake_pro = SimpleNamespace(log_dlq_replay_audit=mock_pro_fn)

        with patch.object(helpers, "_get_pro", return_value=fake_pro):
            result = helpers._safe_delegate(
                "log_dlq_replay_audit",
                123,
                "payment",
                success=True,
                actor_id="user-1",
            )

        assert result == 7
        mock_pro_fn.assert_called_once_with(
            123, "payment", success=True, actor_id="user-1"
        )


# =============================================================================
# Public wrapper delegation — parametrized over every name in __all__
# =============================================================================


@pytest.fixture
def fake_pro_with_recorder(monkeypatch):
    """Install a fake PRO module that records the last call per attribute.

    Each ``getattr(fake_pro, func_name)`` returns a MagicMock — the test can
    assert the exact ``(args, kwargs)`` shape after calling the OSS wrapper.
    """
    recorder: dict[str, MagicMock] = {}

    class _FakePRO:
        def __getattr__(self, name: str) -> MagicMock:
            if name not in recorder:
                recorder[name] = MagicMock(return_value=("ok", name))
            return recorder[name]

    fake = _FakePRO()
    monkeypatch.setattr(helpers, "_get_pro", lambda: fake)
    # Keep cache state coherent so the test isn't surprised by post-hoc inspection.
    monkeypatch.setattr(helpers, "_pro", fake)
    monkeypatch.setattr(helpers, "_resolved", True)
    return fake, recorder


@pytest.fixture
def pro_absent(monkeypatch):
    """Force `_get_pro()` to return None — wrappers must silent-noop."""
    monkeypatch.setattr(helpers, "_get_pro", lambda: None)
    monkeypatch.setattr(helpers, "_pro", None)
    monkeypatch.setattr(helpers, "_resolved", True)


class TestAuditHelpersDelegationContract:
    """Every name in `__all__` matches the {present, absent} contract.

    Contract:
    - PRO present: wrapper calls ``getattr(pro, name)(*args, **kwargs)``
      and returns its result.
    - PRO absent: wrapper returns ``None`` and does not raise.

    The ``__all__`` list IS the surface promise — covering every name keeps
    new wrappers honest. A future contributor who adds a wrapper but skips
    ``_safe_delegate`` gets caught here.
    """

    @pytest.mark.parametrize("wrapper_name", helpers.__all__)
    def test_wrapper_delegates_to_pro_when_present(
        self, wrapper_name, fake_pro_with_recorder
    ):
        _, recorder = fake_pro_with_recorder
        wrapper = getattr(helpers, wrapper_name)

        result = wrapper("positional-1", kw_arg="kw-value")

        assert wrapper_name in recorder
        recorder[wrapper_name].assert_called_once_with(
            "positional-1", kw_arg="kw-value"
        )
        assert result == ("ok", wrapper_name)

    @pytest.mark.parametrize("wrapper_name", helpers.__all__)
    def test_wrapper_returns_none_when_pro_absent(self, wrapper_name, pro_absent):
        wrapper = getattr(helpers, wrapper_name)

        result = wrapper("anything", whatever=1)

        assert result is None

    def test_all_wrappers_listed_match_module_attributes(self):
        """``__all__`` only lists names that actually exist as callables."""
        for name in helpers.__all__:
            obj = getattr(helpers, name, None)
            assert callable(obj), f"{name} declared in __all__ but not callable"
