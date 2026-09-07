"""``init()`` reconciles a distributed audit chain before its first write.

Source: ``src/baldur/bootstrap.py`` — ``_reconcile_distributed_hash_chain``.

After a restart Redis and the local files can disagree: Redis lost its data
and the files are ahead, or a process died mid-write and Redis is ahead.
Reconciling raises the Redis counter to match the files and sweeps the PENDING
reservations the crash left, so the first entry this process writes continues
the chain instead of re-using a sequence.

The step used to hang off a Django settings attribute nothing in the tree ever
set, so it ran on no configuration at all. Two properties replace that:

- **The gate is the constructed chain manager, not a settings name.** A
  process whose promotion fell back to the local chain holds a local manager
  and does no Redis work here — automatically right on all three paths
  (stated, inferred, inferred-then-fell-back) with no flag of its own.
- **Both key prefixes are read off the objects that wrote them.** The chain
  manager carries the partition-namespaced prefix, the adapter carries the
  bare root its ``PendingSequenceManager`` was built with. Re-deriving either
  from settings is how the reconciliation drifted away from the writer.

Best-effort throughout: a stated-intent misconfiguration makes every adapter
resolution raise, and this step must not turn that into a broken ``init()``.

Companion files: ``tests/unit/audit/hash_chain_core/test_startup_sync.py`` and
``test_startup_sync_from_manager.py`` — the sync object this step builds.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.3 State transition (local manager / Redis manager / no manager).
- §8.4 Side effects (the events, and the Redis work not done).
- §8.5 Dependency interaction (what ``from_manager`` is handed).
- §8.2 Exception/edge cases (every failure is absorbed).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur import bootstrap
from baldur.adapters.audit.hashchain_adapter import HashChainFileAuditLogAdapter
from baldur.audit.integrity import (
    LedgerTailReader,
    RedisHashChainManager,
    StartupHashChainSync,
)
from tests.factories import MockRedisClient

# The installation-wide Redis root, deliberately not the shipped ``baldur:``
# default: a step that re-derived the prefix from settings would still pass
# against the default and fail here.
_ROOT_PREFIX = "acme:"
_PARTITION = "eu-west"

_RECONCILED_EVENT = "audit.chain_reconciled"
_FAILED_EVENT = "audit.chain_reconciliation_failed"
_SKIPPED_EVENT = "audit.chain_reconciliation_skipped"


def _adapter(tmp_path, *, distributed: bool) -> HashChainFileAuditLogAdapter:
    """A real adapter, holding whichever chain manager the flag selects."""
    return HashChainFileAuditLogAdapter(
        log_dir=str(tmp_path / "audit"),
        distributed_hash_chain=distributed,
        redis_client=MockRedisClient() if distributed else None,
        redis_key_prefix=_ROOT_PREFIX,
        partition=_PARTITION,
        enable_anchor_backup=False,
    )


def _events(mock_logger, level: str, name: str) -> list:
    return [
        call
        for call in getattr(mock_logger, level).call_args_list
        if call.args and call.args[0] == name
    ]


class TestChainReconciliationStep:
    """The gate, the wiring it hands over, and the failures it absorbs."""

    def test_a_local_chain_does_no_redis_work(self, tmp_path):
        """The single-host majority. A step gated on the settings flag would
        run here on an inferred-then-fell-back process and reconcile a
        namespace nothing writes."""
        adapter = _adapter(tmp_path, distributed=False)

        with patch.object(StartupHashChainSync, "sync", autospec=True) as mock_sync:
            bootstrap._reconcile_distributed_hash_chain(adapter)

        mock_sync.assert_not_called()

    def test_a_local_chain_reports_why_it_skipped(self, tmp_path):
        adapter = _adapter(tmp_path, distributed=False)

        with patch.object(bootstrap, "logger") as mock_logger:
            bootstrap._reconcile_distributed_hash_chain(adapter)

        skipped = _events(mock_logger, "debug", _SKIPPED_EVENT)
        assert len(skipped) == 1
        assert skipped[0].kwargs["reason"] == "not_distributed"

    def test_an_adapter_with_no_chain_manager_is_skipped(self, tmp_path):
        """``enable_hash_chain=False`` leaves the manager ``None`` — an
        ``isinstance`` gate has to absorb that rather than fall through."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / "audit"),
            enable_hash_chain=False,
            enable_anchor_backup=False,
        )

        with patch.object(bootstrap, "logger") as mock_logger:
            bootstrap._reconcile_distributed_hash_chain(adapter)

        assert adapter.hash_chain_manager is None
        assert len(_events(mock_logger, "debug", _SKIPPED_EVENT)) == 1

    def test_a_distributed_chain_runs_the_sync(self, tmp_path):
        adapter = _adapter(tmp_path, distributed=True)

        with patch.object(bootstrap, "logger") as mock_logger:
            bootstrap._reconcile_distributed_hash_chain(adapter)

        assert isinstance(adapter.hash_chain_manager, RedisHashChainManager)
        reconciled = _events(mock_logger, "info", _RECONCILED_EVENT)
        assert len(reconciled) == 1
        assert reconciled[0].kwargs["sync_action"] == "fresh_start"
        assert reconciled[0].kwargs["pending_cleaned"] == 0

    def test_the_sync_is_built_from_the_objects_that_wrote_the_keys(self, tmp_path):
        """The prefixes come off the manager and the adapter, not off
        settings — a second derivation is how the reconciliation ended up
        reading a key the writer never writes."""
        adapter = _adapter(tmp_path, distributed=True)

        with patch.object(
            StartupHashChainSync, "from_manager", autospec=True
        ) as mock_from_manager:
            bootstrap._reconcile_distributed_hash_chain(adapter)

        args = mock_from_manager.call_args.args
        assert args[0] is adapter.hash_chain_manager
        assert args[1] is adapter.ledger_tail_reader
        assert args[2] == _ROOT_PREFIX

    def test_a_failed_sync_is_reported_as_a_warning_not_a_success(self, tmp_path):
        """``sync()`` catches internally and reports through its return value,
        so the step must read that value rather than assume success."""
        adapter = _adapter(tmp_path, distributed=True)

        with (
            patch.object(
                StartupHashChainSync,
                "sync",
                autospec=True,
                return_value={"status": "error", "error": "redis went away"},
            ),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._reconcile_distributed_hash_chain(adapter)

        failures = _events(mock_logger, "warning", _FAILED_EVENT)
        assert len(failures) == 1
        assert failures[0].kwargs["sync_error"] == "redis went away"
        assert _events(mock_logger, "info", _RECONCILED_EVENT) == []

    def test_a_raising_sync_does_not_propagate_out_of_init(self, tmp_path):
        adapter = _adapter(tmp_path, distributed=True)

        with (
            patch.object(
                StartupHashChainSync,
                "sync",
                autospec=True,
                side_effect=RuntimeError("connection reset"),
            ),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._reconcile_distributed_hash_chain(adapter)

        failures = _events(mock_logger, "warning", _FAILED_EVENT)
        assert len(failures) == 1
        assert "connection reset" in failures[0].kwargs["error"]

    def test_an_omitted_adapter_is_resolved_from_the_registry(self, tmp_path):
        """``init()`` calls this with no argument; the default is the resolved
        audit adapter, which is what the step order exists to settle first."""
        adapter = _adapter(tmp_path, distributed=True)

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_audit_adapter",
                return_value=adapter,
            ) as mock_get,
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._reconcile_distributed_hash_chain()

        mock_get.assert_called_once_with()
        assert len(_events(mock_logger, "info", _RECONCILED_EVENT)) == 1

    def test_a_raising_registry_resolve_is_absorbed(self):
        """A stated-intent misconfiguration makes every audit resolution
        raise. That is already reported at ERROR by admission; turning it into
        a failed ``init()`` here would take the whole process down for a
        best-effort startup sweep."""
        with (
            patch(
                "baldur.factory.ProviderRegistry.get_audit_adapter",
                side_effect=RuntimeError("distributed chain refused"),
            ),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._reconcile_distributed_hash_chain()

        failures = _events(mock_logger, "warning", _FAILED_EVENT)
        assert len(failures) == 1
        assert "distributed chain refused" in failures[0].kwargs["error"]

    @pytest.mark.parametrize(
        ("present", "missing"),
        [
            (["hash_chain_manager", "log_dir"], "ledger_tail_reader"),
            (
                ["hash_chain_manager", "log_dir", "ledger_tail_reader"],
                "redis_key_prefix",
            ),
        ],
    )
    def test_an_adapter_without_the_prefix_property_fails_loudly_at_the_call(
        self, tmp_path, present, missing
    ):
        """A third-party audit adapter can carry a Redis chain manager without
        the construction facts this step reads. It has to fail into the
        WARNING rather than reconcile a guessed namespace, whichever fact is
        the one it lacks."""
        stub = MagicMock(spec=present)
        stub.hash_chain_manager = RedisHashChainManager(
            redis_client=MockRedisClient(),
            key_prefix=f"{_ROOT_PREFIX}hashchain:{_PARTITION}:",
        )
        stub.log_dir = tmp_path / "audit"
        if "ledger_tail_reader" in present:
            stub.ledger_tail_reader = LedgerTailReader(tmp_path / "audit")

        with patch.object(bootstrap, "logger") as mock_logger:
            bootstrap._reconcile_distributed_hash_chain(stub)

        failures = _events(mock_logger, "warning", _FAILED_EVENT)
        assert len(failures) == 1
        assert missing in failures[0].kwargs["error"]


class TestChainReconciliationStepOrderContract:
    """Where the step sits in ``init()`` is the defect this document removes.

    It must run **after** the audit backend is settled — a PRO entitlement
    hook can promote the chain, so a step that ran earlier reads a
    pre-promotion state — and **before** the audit pipeline starts writing.
    """

    @pytest.mark.parametrize(
        ("earlier", "later"),
        [
            ("_apply_audit_default_provider", "_reconcile_distributed_hash_chain"),
            ("_reconcile_distributed_hash_chain", "_start_audit_pipeline_if_enabled"),
        ],
        ids=["after_backend_settled", "before_first_write"],
    )
    def test_init_calls_the_step_in_the_required_order(self, earlier, later):
        import inspect

        source = inspect.getsource(bootstrap.init)

        assert source.index(f"{earlier}()") < source.index(f"{later}()")
