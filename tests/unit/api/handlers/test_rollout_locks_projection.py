"""Console editor rollout-lock projection ``_rollout_locks`` (685 D6).

Verification target: ``config._rollout_locks(domains)`` maps each projected
config domain to its owning canary rollout id (read from the canary rollout
store via the ProviderRegistry) so the console flags a locked domain before the
operator edits. Fail-safe: an unregistered store degrades to ``{}`` and a
per-domain read failure omits that domain rather than failing the whole panel.

PRO-absent safe (the OSS mirror runs this): ``_rollout_locks`` is OSS code that
reads the OSS store interface via the registry — no ``baldur_pro`` import. With
no store registered ``safe_get()`` returns ``None`` and the projection is ``{}``.

Reference: 685 CANARY_CONFIG_LOCK_WRITER_ENFORCEMENT (D6)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.api.handlers.config import _rollout_locks
from baldur.factory.registry import ProviderRegistry

_DOMAINS = ["retry", "circuit_breaker", "dlq"]


def _wired(store):
    """Patch the registry-resolved canary rollout store for the projection."""
    return patch.object(
        ProviderRegistry.canary_rollout_store, "safe_get", return_value=store
    )


class TestRolloutLocksProjectionBehavior:
    def test_locked_domains_map_to_their_owner(self):
        store = MagicMock()
        store.get_config_lock_owner.side_effect = lambda domain: (
            "roll-9" if domain == "retry" else None
        )
        with _wired(store):
            assert _rollout_locks(_DOMAINS) == {"retry": "roll-9"}

    def test_no_locks_projects_empty(self):
        store = MagicMock()
        store.get_config_lock_owner.return_value = None
        with _wired(store):
            assert _rollout_locks(_DOMAINS) == {}

    def test_absent_store_degrades_to_empty(self):
        # safe_get() -> None (OSS install with no canary store registered).
        with _wired(None):
            assert _rollout_locks(_DOMAINS) == {}

    def test_per_domain_read_failure_omits_only_that_domain(self):
        store = MagicMock()

        def _owner(domain):
            if domain == "circuit_breaker":
                raise RuntimeError("store read blip")
            return "roll-2" if domain == "retry" else None

        store.get_config_lock_owner.side_effect = _owner
        with _wired(store):
            # circuit_breaker is omitted; retry still surfaces — no whole-panel fail.
            assert _rollout_locks(_DOMAINS) == {"retry": "roll-2"}

    def test_store_resolution_failure_degrades_to_empty(self):
        with patch.object(
            ProviderRegistry.canary_rollout_store,
            "safe_get",
            side_effect=RuntimeError("registry exploded"),
        ):
            assert _rollout_locks(_DOMAINS) == {}
