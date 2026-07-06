"""
Resilient Hash Chain Manager.

Provides a unified add_integrity() interface that transparently
switches between Lua 2-phase atomic operations and FallbackChain.

Lua 2-phase (reserve → hash → commit): Normal Redis mode, 2 RTT.
FallbackChain (Tier 2+): Redis failure or unavailability.

Neither lua_atomic.py nor fallback.py is modified — both are reused as-is.

Reference:
    384 §B-5, D-13
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    from baldur.audit.graceful_degradation.fallback import HashChainFallbackChain
    from baldur.audit.performance.lua_atomic import LuaAtomicHashChain

logger = structlog.get_logger()

__all__ = ["ResilientHashChainManager"]


class ResilientHashChainManager:
    """Lua 2-phase atomic → FallbackChain auto-switch.

    .. note::
        **Tier**: Dormant (compliance-grade enhancement, no standalone demand)
        **Status**: Not auto-wired. Available for custom integration engagements.
        PRO Audit (full) uses the basic file-based ``HashChainManager`` in
        ``audit/integrity/`` instead.

    Provides add_integrity(entry) → dict interface so existing call sites
    (ContinuousAuditRecorder, ResilientRecorder) need no changes.
    """

    def __init__(
        self,
        lua_chain: LuaAtomicHashChain | None,
        fallback_chain: HashChainFallbackChain,
        compute_hash_fn: Callable[[dict[str, Any]], str],
    ):
        """
        Args:
            lua_chain: LuaAtomicHashChain instance (None if Redis unavailable).
            fallback_chain: HashChainFallbackChain instance (always required).
            compute_hash_fn: Chain-hash function. MUST be
                ``baldur.audit.integrity.models.compute_hash`` — the single
                keyed-aware source. It applies HMAC-SHA256 when
                ``audit_signing_key`` is configured and keyless SHA-256 when it
                is unset. Injecting any other (e.g. an inline keyless)
                implementation breaks coherence: a keyed verifier sharing
                ``compute_hash`` would reject entries hashed here.
        """
        self._lua_chain = lua_chain
        self._fallback = fallback_chain
        self._compute_hash = compute_hash_fn

    def add_integrity(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add integrity fields with Lua 2-phase → FallbackChain auto-switch.

        Tier 1: Lua 2-phase (reserve → hash → commit, 2 RTT).
        Tier 2+: FallbackChain (already handles multi-tier internally).

        Returns:
            Entry dict with integrity fields added.
        """
        if self._lua_chain:
            try:
                previous_hash = self._get_previous_hash()
                ok, seq, err = self._lua_chain.reserve_sequence_atomic(
                    expected_hash="",
                    previous_hash=previous_hash,
                )
                if ok:
                    entry["integrity"] = {
                        "sequence": seq,
                        "previous_hash": previous_hash,
                        "tier": "lua_atomic",
                    }
                    current_hash = self._compute_hash(entry)
                    entry["integrity"]["current_hash"] = current_hash

                    commit_ok, _ = self._lua_chain.commit_sequence_atomic(
                        seq,
                        current_hash,
                    )
                    if commit_ok:
                        return entry
                    # Commit failed → pending entry auto-expires via TTL
            except Exception:
                logger.warning("audit.hash_chain_lua_fallback")

        # Tier 2+: FallbackChain (existing multi-tier: Redis replica → local → memory)
        return self._fallback.add_integrity(entry)

    def _get_previous_hash(self) -> str:
        """Retrieve previous hash from Lua chain state."""
        try:
            if self._lua_chain:
                keys = self._lua_chain._get_keys()
                state_key = keys["state"]
                # Access via registry's Redis client
                result = self._lua_chain._registry.execute(
                    "batch_get",
                    keys=[state_key],
                    args=[],
                )
                if result and isinstance(result, dict):
                    return result.get("previous_hash", "GENESIS")
                if result and isinstance(result, list) and len(result) >= 2:
                    prev = result[1]
                    if isinstance(prev, bytes):
                        return prev.decode("utf-8")
                    return str(prev) if prev else "GENESIS"
        except Exception:
            logger.debug("audit.hash_chain_previous_hash_unavailable")
        return "GENESIS"
