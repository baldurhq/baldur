"""Unit tests for the ``file_hashchain`` provider factory.

Source: ``src/baldur/factory/adapters.py::discover_audit_adapters`` — the
inner ``_create_hashchain_adapter`` closure, reached by resolving the
``file_hashchain`` provider out of ``ProviderRegistry.audit``.

Two repairs are pinned here:

- **Distributed chain wiring.** The factory used to probe a duck-typed
  ``ProviderRegistry.get_cache_adapter`` attribute that exists on no
  registry, so ``redis_client`` was always ``None``:
  ``BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=true`` built a *local* file-locked
  chain and said so only at WARNING. The factory now calls
  ``create_hash_chain_redis_client()``.
- **Writable-directory resolution.** The log directory went straight into
  the adapter, whose ``__init__`` ends in an unguarded ``mkdir`` against
  the *relative* default ``logs/audit``. On a read-only root filesystem
  every resolution raised. It now goes through ``resolve_writable_dir``,
  which falls back for a hardcoded default and still fails loudly for an
  operator-chosen path.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.2 Exception/edge cases (unwritable preferred directory, both origins).
- §8.4 Side effects (which manager type the adapter ends up holding).
- §8.5 Dependency interaction (the resolver's arguments, the client's route).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import redis

from baldur.adapters.audit.hashchain_adapter import HashChainFileAuditLogAdapter
from baldur.audit.integrity import HashChainManager, RedisHashChainManager
from baldur.factory.adapters import _AUDIT_LOG_DIR_ENV, discover_audit_adapters
from baldur.factory.registry import ProviderRegistry
from baldur.settings.audit import override_audit_settings
from baldur.utils.fs import ResolvedDir, reset_writable_dir_resolutions

# The env var an operator sets to choose the hash-chain audit directory.
# Hardcoded rather than imported so a rename shows up as a failure here.
_LOG_DIR_ENV = "BALDUR_AUDIT_LOG_DIR"


@pytest.fixture(autouse=True)
def _isolate_hashchain_factory(monkeypatch, tmp_path):
    """Registry registration + a private resolver registry are both global."""
    monkeypatch.setenv(_LOG_DIR_ENV, str(tmp_path / "audit"))
    reset_writable_dir_resolutions()
    with ProviderRegistry.audit.snapshot():
        discover_audit_adapters()
        yield
    reset_writable_dir_resolutions()


def _resolved(path) -> ResolvedDir:
    """A real resolution outcome — the factory only reads ``.path``, but a
    real dataclass keeps the stand-in honest about the shape it returns."""
    return ResolvedDir(path=path, preferred=path, fell_back=False)


def _build_adapter() -> HashChainFileAuditLogAdapter:
    """Resolve the provider under test through the registry, as init() does."""
    return ProviderRegistry.audit.get("file_hashchain")


class TestHashChainAdapterFactoryContract:
    """The env var name and the default directory are published contract."""

    def test_log_dir_env_var_name_is_baldur_audit_log_dir(self):
        assert _AUDIT_LOG_DIR_ENV == _LOG_DIR_ENV

    def test_default_log_dir_is_the_relative_logs_audit(self):
        """The zero-config default is relative — which is why it must be
        resolved rather than handed to the adapter's ``mkdir``."""
        assert HashChainFileAuditLogAdapter.DEFAULT_LOG_DIR == "logs/audit"


class TestHashChainAdapterDistributedWiringBehavior:
    """``distributed_hash_chain`` reaches the chain manager, or is refused."""

    def test_distributed_setting_routes_the_resolved_client_into_the_adapter(self):
        # Given: distributed on and a client available
        mock_client = MagicMock(spec=redis.Redis)

        # When
        with (
            override_audit_settings(distributed_hash_chain=True),
            patch(
                "baldur.audit.config.create_hash_chain_redis_client",
                return_value=mock_client,
            ) as mock_create,
        ):
            adapter = _build_adapter()

        # Then: a Redis-backed chain manager, built from that exact client
        mock_create.assert_called_once_with()
        assert isinstance(adapter._hash_chain, RedisHashChainManager)

    def test_distributed_setting_off_never_resolves_a_client(self):
        """The gate lives in the factory, not in the helper."""
        with (
            override_audit_settings(distributed_hash_chain=False),
            patch("baldur.audit.config.create_hash_chain_redis_client") as mock_create,
        ):
            adapter = _build_adapter()

        mock_create.assert_not_called()
        assert isinstance(adapter._hash_chain, HashChainManager)

    def test_unresolvable_client_falls_back_to_the_local_chain(self):
        """The helper's ``None`` sentinel degrades to the file-locked chain
        rather than leaving the adapter unconstructible."""
        with (
            override_audit_settings(distributed_hash_chain=True),
            patch(
                "baldur.audit.config.create_hash_chain_redis_client",
                return_value=None,
            ),
        ):
            adapter = _build_adapter()

        assert isinstance(adapter._hash_chain, HashChainManager)

    def test_use_file_lock_and_partition_settings_reach_the_adapter(self):
        """Settings-aware means every settings field, not just the chain one."""
        with override_audit_settings(
            distributed_hash_chain=False,
            use_file_lock=False,
            partition="eu-west",
        ):
            adapter = _build_adapter()

        assert adapter._partition == "eu-west"
        assert adapter._hash_chain._use_file_lock is False


class TestHashChainAdapterWritableDirBehavior:
    """The directory goes through the canonical resolver, origin-split."""

    def test_operator_directory_is_passed_as_operator_set(self, monkeypatch, tmp_path):
        # Given: an operator-chosen directory
        chosen = tmp_path / "operator-audit"
        monkeypatch.setenv(_LOG_DIR_ENV, str(chosen))
        reset_writable_dir_resolutions()

        # When
        with (
            override_audit_settings(distributed_hash_chain=False),
            patch(
                "baldur.utils.fs.resolve_writable_dir", autospec=True
            ) as mock_resolve,
        ):
            mock_resolve.return_value = _resolved(chosen)
            _build_adapter()

        # Then: origin reported as operator-set, remedy names the env var
        kwargs = mock_resolve.call_args.kwargs
        assert mock_resolve.call_args.args[0] == str(chosen)
        assert kwargs["operator_set"] is True
        assert kwargs["purpose"] == "audit_hashchain"
        assert kwargs["env_override_name"] == _LOG_DIR_ENV

    def test_unset_env_resolves_the_relative_default_as_not_operator_set(
        self, monkeypatch, tmp_path
    ):
        # Given: no operator override
        monkeypatch.delenv(_LOG_DIR_ENV, raising=False)
        reset_writable_dir_resolutions()

        # When
        with (
            override_audit_settings(distributed_hash_chain=False),
            patch(
                "baldur.utils.fs.resolve_writable_dir", autospec=True
            ) as mock_resolve,
        ):
            mock_resolve.return_value = _resolved(tmp_path / "fallback")
            _build_adapter()

        # Then: the hardcoded default is allowed to fall back
        assert mock_resolve.call_args.args[0] == "logs/audit"
        assert mock_resolve.call_args.kwargs["operator_set"] is False

    def test_adapter_writes_into_the_resolved_directory_not_the_preferred_one(
        self, monkeypatch, tmp_path
    ):
        """The resolver's answer is what the adapter gets — the whole point
        of the indirection is that these two can differ."""
        monkeypatch.delenv(_LOG_DIR_ENV, raising=False)
        reset_writable_dir_resolutions()
        resolved = tmp_path / "elsewhere"

        with (
            override_audit_settings(distributed_hash_chain=False),
            patch(
                "baldur.utils.fs.resolve_writable_dir", autospec=True
            ) as mock_resolve,
        ):
            mock_resolve.return_value = _resolved(resolved)
            adapter = _build_adapter()

        assert adapter._log_dir == resolved

    def test_unwritable_hardcoded_default_falls_back_instead_of_raising(
        self, monkeypatch, tmp_path, writable_dir_chain, deny_dir
    ):
        """The pre-fix failure mode: an unwritable ``logs/audit`` made every
        resolution of this provider raise ``OSError`` out of the adapter's
        ``mkdir``, so a read-only root filesystem had no audit trail at all.
        """
        # Given: the relative default cannot be created, no operator override
        monkeypatch.delenv(_LOG_DIR_ENV, raising=False)
        monkeypatch.chdir(tmp_path)
        deny_dir("logs")

        # When
        with override_audit_settings(distributed_hash_chain=False):
            adapter = _build_adapter()

        # Then: constructed against a fallback base, not the preferred path
        assert adapter._log_dir.is_dir()
        assert writable_dir_chain.state in adapter._log_dir.parents

    def test_unwritable_operator_directory_raises_rather_than_relocating(
        self, monkeypatch, tmp_path, writable_dir_chain, deny_dir
    ):
        """An operator who names a compliance path must not have the trail
        silently written somewhere else."""
        from baldur.core.exceptions import ConfigurationError

        # Given: an operator-chosen directory that cannot be created
        chosen = tmp_path / "compliance" / "audit"
        monkeypatch.setenv(_LOG_DIR_ENV, str(chosen))
        deny_dir(tmp_path / "compliance")

        # When / Then
        with (
            override_audit_settings(distributed_hash_chain=False),
            pytest.raises(ConfigurationError),
        ):
            _build_adapter()
