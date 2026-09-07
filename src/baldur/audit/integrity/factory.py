"""
Hash Chain Manager Factory.

Contains:
- create_hash_chain_manager: Factory function to create appropriate manager
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from baldur.audit.integrity.ledger_tail import LedgerTailReader
from baldur.audit.integrity.local_manager import HashChainManager
from baldur.audit.integrity.protocol import HashChainManagerProtocol
from baldur.audit.integrity.redis_manager import RedisHashChainManager

logger = structlog.get_logger()


def create_hash_chain_manager(
    distributed: bool = False,
    redis_client: Any | None = None,
    key_prefix: str = "baldur:",
    state_file: Path | None = None,
    ledger: LedgerTailReader | None = None,
) -> HashChainManagerProtocol:
    """
    Factory function to create appropriate hash chain manager.

    Args:
        distributed: If True, create RedisHashChainManager
        redis_client: Redis client (required if distributed=True)
        key_prefix: Redis key prefix
        state_file: Local state file path (for fallback or standalone)
        ledger: Reader for the ledger the resulting chain's entries land in.
            Threaded into both managers — a distributed chain's fallback
            appends to the same files, so it needs the same reader.

    Returns:
        HashChainManager or RedisHashChainManager instance
    """
    local_manager = HashChainManager(state_file=state_file, ledger=ledger)

    if distributed:
        if redis_client is None:
            logger.warning(
                "hash_chain.distributed_mode_fallback",
                reason="redis_client_not_provided",
            )
            return local_manager

        return RedisHashChainManager(
            redis_client=redis_client,
            key_prefix=key_prefix,
            fallback_manager=local_manager,
            ledger=ledger,
        )

    return local_manager


__all__ = ["create_hash_chain_manager"]
