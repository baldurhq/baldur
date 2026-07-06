"""
Pipeline Batch Query (Multi-key retrieval).

Provides batch query operations using Redis pipelines.
"""

from typing import Any

import structlog

logger = structlog.get_logger()


class PipelineBatchQuery:
    """
    Redis pipeline-based batch query for hash chain state.

    Problem:
        Querying multiple chain states requires N round trips.

    Solution:
        Use Redis pipeline to batch all queries into single RTT.

    Pattern source:
        api/django/rate_limit/redis_adapter.py#L149-153

    Usage:
        batch = PipelineBatchQuery(redis_client)
        states = batch.get_multiple_chain_states(["chain1", "chain2"])
    """

    def __init__(self, redis_client: Any, key_prefix: str = "baldur:"):
        """
        Initialize pipeline batch query.

        Args:
            redis_client: Redis client instance
            key_prefix: Prefix for Redis keys
        """
        self._redis = redis_client
        self._key_prefix = key_prefix

    def get_multiple_chain_states(
        self,
        chain_keys: list[str],
    ) -> dict[str, dict[str, Any]]:
        """
        Get multiple chain states in single round trip.

        Args:
            chain_keys: List of chain identifiers

        Returns:
            Dict mapping chain_key to {sequence, previous_hash, updated_at}
        """
        if not chain_keys:
            return {}

        try:
            with self._redis.pipeline(transaction=False) as pipe:
                for key in chain_keys:
                    state_key = f"{self._key_prefix}audit:hash_chain:state:{key}"
                    pipe.hgetall(state_key)

                results = pipe.execute()

            states = {}
            for i, key in enumerate(chain_keys):
                if i < len(results) and results[i]:
                    raw = results[i]
                    states[key] = {
                        "sequence": int(raw.get(b"sequence", raw.get("sequence", 0))),
                        "previous_hash": (
                            raw.get(
                                b"previous_hash", raw.get("previous_hash", b"GENESIS")
                            )
                        ),
                        "updated_at": raw.get(b"updated_at", raw.get("updated_at", "")),
                    }
                    # Decode bytes if needed
                    if isinstance(states[key]["previous_hash"], bytes):
                        states[key]["previous_hash"] = states[key][
                            "previous_hash"
                        ].decode()
                else:
                    states[key] = {
                        "sequence": 0,
                        "previous_hash": "GENESIS",
                        "updated_at": "",
                    }

            return states

        except Exception as e:
            logger.exception(
                "pipeline_batch_query.batch_query_failed",
                error=e,
            )
            return {
                key: {"sequence": 0, "previous_hash": "GENESIS"} for key in chain_keys
            }

    def batch_check_pending(
        self,
        sequences: list[int],
    ) -> dict[int, bool]:
        """
        Check multiple pending states in single round trip.

        Args:
            sequences: List of sequence numbers to check

        Returns:
            Dict mapping sequence to exists (True/False)
        """
        if not sequences:
            return {}

        try:
            with self._redis.pipeline(transaction=False) as pipe:
                for seq in sequences:
                    pending_key = f"{self._key_prefix}audit:hash_chain:pending:{seq}"
                    pipe.exists(pending_key)

                results = pipe.execute()

            return {
                seq: bool(results[i]) if i < len(results) else False
                for i, seq in enumerate(sequences)
            }

        except Exception as e:
            logger.exception(
                "pipeline_batch_query.pending_check_failed",
                error=e,
            )
            return dict.fromkeys(sequences, False)
