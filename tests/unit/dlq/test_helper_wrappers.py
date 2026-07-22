"""OSS-side DLQ + postmortem store helper wrappers — ``baldur.dlq.helpers``.

Scope:
- ``store_to_dlq`` / ``dlq_backing_available`` resolve the DLQ capture backing
  through one chain: the PRO ``DLQService`` (registered under ACTIVE
  entitlement) when present, else the OSS ``DLQCaptureService``. So a pure OSS
  install captures failures (no ``baldur_pro`` required) and the backing always
  resolves on a functional install.
- ``compress_entries`` / the ``postmortem.store`` helpers stay PRO-only: each
  caches its PRO submodule under its own ``_resolved_*`` flag and no-ops
  (``None`` / ``[]`` / ``0``) when the submodule is absent.
"""

from __future__ import annotations

import builtins
from unittest.mock import MagicMock, patch

import pytest

import baldur.dlq.helpers as helpers
from baldur.factory.registry import ProviderRegistry


def _patched_import_factory(target_name: str):
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == target_name:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    return _fake_import


# =============================================================================
# Per-submodule cache resolution (compression + postmortem — PRO-only)
# =============================================================================


class TestDlqHelpersResolutionBehavior:
    """Each PRO-only ``_get_pro_*()`` caches its submodule independently."""

    @pytest.mark.parametrize(
        ("resolver_name", "cache_attr", "resolved_attr", "target_module"),
        [
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
        """A failure resolving one submodule must not poison the other."""
        pytest.importorskip("baldur_pro")
        fake_import = _patched_import_factory("baldur_pro.services.dlq.compression")
        with patch.object(builtins, "__import__", side_effect=fake_import):
            # Compression resolution fails.
            assert helpers._get_pro_dlq_compression() is None
            # Postmortem store still resolves cleanly.
            assert helpers._get_pro_postmortem_store() is not None


# =============================================================================
# PRO-only wrapper delegation (compression + postmortem)
# =============================================================================


# Map each PRO-only wrapper → (which resolver feeds it, expected absent-sentinel).
WRAPPER_TABLE = [
    # (wrapper_name, resolver_attr, absent_sentinel)
    ("compress_entries", "_get_pro_dlq_compression", None),
    ("add_healing_incident", "_get_pro_postmortem_store", None),
    ("get_healing_incidents", "_get_pro_postmortem_store", []),
    ("get_healing_incidents_count", "_get_pro_postmortem_store", 0),
]

# Public names that are NOT verbatim PRO-only arg-forwarding wrappers.
# ``store_to_dlq`` / ``dlq_backing_available`` resolve the OSS-or-PRO backing
# chain rather than delegating to a cached PRO submodule.
# ``compressed_lifecycle_lock`` crosses the same boundary in a different shape:
# a context manager that yields around a caller's block instead of forwarding
# arguments and returning a value, so the table's call-and-compare form does
# not apply to it.
NON_WRAPPER_PUBLIC_NAMES = {
    "compressed_lifecycle_lock",
    "dlq_backing_available",
    "store_to_dlq",
}


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
    """Each PRO-only wrapper hits its resolver, else the right empty sentinel."""

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

    def test_wrapper_table_covers_every_pro_only_public_name(self):
        """The parametrized table must match the PRO-only wrapper subset of
        ``__all__`` (every public name except the backing-chain functions).
        """
        covered = {row[0] for row in WRAPPER_TABLE}
        assert covered == set(helpers.__all__) - NON_WRAPPER_PUBLIC_NAMES


# =============================================================================
# store_to_dlq / dlq_backing_available — OSS-or-PRO backing chain
# =============================================================================


class TestDlqBackingChainResolution:
    """The store path resolves PRO (ACTIVE slot) first, then the OSS backing."""

    def test_store_to_dlq_uses_oss_backing_when_slot_empty(self, monkeypatch):
        """PRO absent / unentitled (empty slot) → the OSS DLQCaptureService
        captures the failure (real ``DLQEntryResult``, not ``None``).
        """
        from baldur.models.dlq import DLQEntryResult
        from baldur.services.dlq_capture import service as capture_module

        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)

        # Seed the OSS capture singleton with a mock repo so the store is
        # deterministic (no Redis) — the point is that the OSS backing captures.
        mock_repo = MagicMock()
        mock_entry = MagicMock()
        mock_entry.id = "oss-entry-1"
        mock_repo.create.return_value = mock_entry
        mock_repo.count_all.return_value = 0
        mock_repo.count_by_domain.return_value = 0
        monkeypatch.setattr(
            capture_module,
            "_capture_service",
            capture_module.DLQCaptureService(repository=mock_repo),
        )

        result = helpers.store_to_dlq(
            domain="payment", failure_type="PG_TIMEOUT", mode="sync"
        )

        assert isinstance(result, DLQEntryResult)
        assert result.success is True
        assert result.dlq_id == "oss-entry-1"
        mock_repo.create.assert_called_once()

    def test_store_to_dlq_prefers_pro_service_when_slot_registered(self, monkeypatch):
        """PRO present (registered slot) → the PRO service wins the chain."""
        pro_service = MagicMock()
        pro_service.store_failure.return_value = "pro-result"
        monkeypatch.setattr(
            ProviderRegistry.dlq_service, "safe_get", lambda: pro_service
        )

        result = helpers.store_to_dlq(domain="payment", failure_type="t", mode="sync")

        pro_service.store_failure.assert_called_once_with(
            domain="payment", failure_type="t", mode="sync"
        )
        assert result == "pro-result"

    def test_backing_available_true_when_slot_empty(self, monkeypatch):
        """OSS backing always resolves → available even with no PRO slot."""
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
        assert helpers.dlq_backing_available() is True

    def test_backing_available_true_when_slot_registered(self, monkeypatch):
        # A bare non-None object is enough — the predicate only checks resolution.
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: object())
        assert helpers.dlq_backing_available() is True
