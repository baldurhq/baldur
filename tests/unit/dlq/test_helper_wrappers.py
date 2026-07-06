"""OSS-side DLQ + postmortem store helper wrappers — `baldur.dlq.helpers` (518 batch a).

Scope:
- Three independent module-level caches: ``_pro_dlq`` / ``_pro_dlq_compression``
  / ``_pro_postmortem_store``. Each resolves a different PRO submodule and
  caches under its own ``_resolved_*`` flag.
- Wrappers delegate verbatim args/kwargs to the matching PRO function when
  the relevant cache resolves; otherwise return the type-appropriate empty
  sentinel: ``None`` for writers (``store_to_dlq`` / ``compress_entries`` /
  ``add_healing_incident``), ``[]`` for ``get_healing_incidents``, ``0`` for
  ``get_healing_incidents_count``.
"""

from __future__ import annotations

import builtins
from unittest.mock import MagicMock, patch

import pytest

import baldur.dlq.helpers as helpers


def _patched_import_factory(target_name: str):
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == target_name:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    return _fake_import


# =============================================================================
# Per-submodule cache resolution
# =============================================================================


class TestDlqHelpersResolutionBehavior:
    """Each ``_get_pro_*()`` caches its submodule independently."""

    @pytest.mark.parametrize(
        ("resolver_name", "cache_attr", "resolved_attr", "target_module"),
        [
            ("_get_pro_dlq", "_pro_dlq", "_resolved_dlq", "baldur_pro.services.dlq"),
            (
                "_get_pro_dlq_compression",
                "_pro_dlq_compression",
                "_resolved_dlq_compression",
                "baldur_pro.services.dlq.compression",
            ),
            (
                "_get_pro_postmortem_store",
                "_pro_postmortem_store",
                "_resolved_postmortem_store",
                "baldur_pro.services.postmortem.store",
            ),
        ],
    )
    def test_resolver_caches_module_on_success(
        self, resolver_name, cache_attr, resolved_attr, target_module
    ):
        # Success path requires the PRO submodule to actually resolve.
        pytest.importorskip("baldur_pro")
        resolver = getattr(helpers, resolver_name)

        first = resolver()
        second = resolver()

        assert first is not None
        assert second is first
        assert getattr(helpers, resolved_attr) is True
        assert getattr(helpers, cache_attr) is first

    @pytest.mark.parametrize(
        ("resolver_name", "cache_attr", "resolved_attr", "target_module"),
        [
            ("_get_pro_dlq", "_pro_dlq", "_resolved_dlq", "baldur_pro.services.dlq"),
            (
                "_get_pro_dlq_compression",
                "_pro_dlq_compression",
                "_resolved_dlq_compression",
                "baldur_pro.services.dlq.compression",
            ),
            (
                "_get_pro_postmortem_store",
                "_pro_postmortem_store",
                "_resolved_postmortem_store",
                "baldur_pro.services.postmortem.store",
            ),
        ],
    )
    def test_resolver_returns_none_when_submodule_absent(
        self, resolver_name, cache_attr, resolved_attr, target_module
    ):
        resolver = getattr(helpers, resolver_name)
        fake_import = _patched_import_factory(target_module)
        with patch.object(builtins, "__import__", side_effect=fake_import):
            result = resolver()

        assert result is None
        assert getattr(helpers, resolved_attr) is True
        assert getattr(helpers, cache_attr) is None

    def test_caches_are_independent_per_submodule(self):
        """A failure resolving one submodule must not poison the others."""
        # The non-failing submodules must actually resolve, which needs PRO.
        pytest.importorskip("baldur_pro")
        fake_import = _patched_import_factory("baldur_pro.services.dlq.compression")
        with patch.object(builtins, "__import__", side_effect=fake_import):
            # Compression resolution fails.
            assert helpers._get_pro_dlq_compression() is None
            # DLQ + postmortem store still resolve cleanly.
            assert helpers._get_pro_dlq() is not None
            assert helpers._get_pro_postmortem_store() is not None


# =============================================================================
# Public wrapper delegation
# =============================================================================


# Map each wrapper → (which resolver feeds it, expected absent-sentinel).
# The resolver attribute is what we monkeypatch to inject the fake module.
WRAPPER_TABLE = [
    # (wrapper_name, resolver_attr, absent_sentinel)
    ("store_to_dlq", "_get_pro_dlq", None),
    ("compress_entries", "_get_pro_dlq_compression", None),
    ("add_healing_incident", "_get_pro_postmortem_store", None),
    ("get_healing_incidents", "_get_pro_postmortem_store", []),
    ("get_healing_incidents_count", "_get_pro_postmortem_store", 0),
]

# Public names that are NOT verbatim arg-forwarding wrappers and so are absent
# from WRAPPER_TABLE by design. ``dlq_backing_available`` is a bool predicate
# over the same ``_get_pro_dlq`` resolution, not a delegating wrapper.
NON_WRAPPER_PUBLIC_NAMES = {"dlq_backing_available"}


@pytest.fixture
def fake_submodule_factory(monkeypatch):
    """Install a recording fake PRO submodule under the requested resolver."""

    def _install(resolver_attr: str):
        recorder: dict[str, MagicMock] = {}

        class _FakePRO:
            def __getattr__(self, name: str) -> MagicMock:
                if name not in recorder:
                    recorder[name] = MagicMock(return_value=("ok", name))
                return recorder[name]

        fake = _FakePRO()
        monkeypatch.setattr(helpers, resolver_attr, lambda: fake)
        return fake, recorder

    return _install


@pytest.fixture
def absent_submodule(monkeypatch):
    def _install(resolver_attr: str):
        monkeypatch.setattr(helpers, resolver_attr, lambda: None)

    return _install


class TestDlqHelpersDelegationContract:
    """Each wrapper hits its resolver and falls back to the right empty sentinel."""

    @pytest.mark.parametrize(
        ("wrapper_name", "resolver_attr", "_sentinel"),
        WRAPPER_TABLE,
        ids=[row[0] for row in WRAPPER_TABLE],
    )
    def test_wrapper_delegates_to_pro_when_submodule_present(
        self, wrapper_name, resolver_attr, _sentinel, fake_submodule_factory
    ):
        _, recorder = fake_submodule_factory(resolver_attr)
        wrapper = getattr(helpers, wrapper_name)

        result = wrapper("arg1", kw="value")

        assert wrapper_name in recorder
        recorder[wrapper_name].assert_called_once_with("arg1", kw="value")
        assert result == ("ok", wrapper_name)

    @pytest.mark.parametrize(
        ("wrapper_name", "resolver_attr", "sentinel"),
        WRAPPER_TABLE,
        ids=[row[0] for row in WRAPPER_TABLE],
    )
    def test_wrapper_returns_type_specific_sentinel_when_submodule_absent(
        self, wrapper_name, resolver_attr, sentinel, absent_submodule
    ):
        """Read helpers fall back to ``[]`` / ``0`` so OSS callers can iterate
        or compare without ``None``-checking.
        """
        absent_submodule(resolver_attr)
        wrapper = getattr(helpers, wrapper_name)

        result = wrapper("ignored")

        assert result == sentinel
        # Exact-type check so a `[]` doesn't accidentally satisfy a `0` slot.
        assert type(result) is type(sentinel)

    def test_all_wrappers_listed_match_module_attributes(self):
        for name in helpers.__all__:
            obj = getattr(helpers, name, None)
            assert callable(obj), f"{name} declared in __all__ but not callable"

    def test_wrapper_table_covers_every_public_name(self):
        """Belt-and-braces: the parametrized table must match the wrapper
        subset of ``__all__`` (every public name except non-wrapper predicates).
        """
        covered = {row[0] for row in WRAPPER_TABLE}
        assert covered == set(helpers.__all__) - NON_WRAPPER_PUBLIC_NAMES


class TestDlqBackingAvailablePredicate:
    """``dlq_backing_available`` mirrors the ``_get_pro_dlq`` store resolution."""

    def test_true_when_store_resolves(self, fake_submodule_factory):
        fake_submodule_factory("_get_pro_dlq")
        assert helpers.dlq_backing_available() is True

    def test_false_when_store_absent(self, absent_submodule):
        absent_submodule("_get_pro_dlq")
        assert helpers.dlq_backing_available() is False

    def test_verdict_matches_store_to_dlq_noop(self, absent_submodule):
        """The predicate's ``False`` must coincide with the store no-op: when it
        reports unavailable, ``store_to_dlq`` returns ``None`` (nothing stored).
        """
        absent_submodule("_get_pro_dlq")
        assert helpers.dlq_backing_available() is False
        assert helpers.store_to_dlq(domain="d", failure_type="t") is None
