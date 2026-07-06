"""``RedisCircuitBreakerStateRepository`` enum-state serialization tests (#466 DBF2).

Pin the wire-format produced when callers pass ``CircuitBreakerStateEnum``
values directly. Under Python 3.11+ ``str(Enum)`` returns the qualified
name (``"CircuitBreakerStateEnum.OPEN"``) rather than the ``.value``
(``"open"``), which would corrupt every Redis HGET inspection (Grafana,
jq pipelines). The repository normalizes at the wire boundary so callers
can keep passing enums.

Reference: ``466`` DBF2.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.adapters.redis.circuit_breaker import RedisCircuitBreakerStateRepository
from baldur.interfaces.repositories import CircuitBreakerStateEnum


def _make_repo() -> tuple[RedisCircuitBreakerStateRepository, MagicMock]:
    """Construct a repository over a mock backend (no Redis required)."""
    backend = MagicMock()
    backend.hset.return_value = True
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    return repo, backend


# =============================================================================
# Contract — enum-arg normalization at the HSET boundary
# =============================================================================


class TestRedisCircuitBreakerStateSerializationContract:
    """``CircuitBreakerStateEnum`` arguments serialize to ``.value`` on the wire."""

    def test_update_state_with_enum_writes_value_string(self):
        repo, backend = _make_repo()

        repo.update_state("payment", state=CircuitBreakerStateEnum.OPEN)

        # First hset call carries the {service}: hash update for the state.
        first_call_args = backend.hset.call_args_list[0]
        updates = first_call_args.args[1]
        assert updates["state"] == "open"
        # Negative regression: pre-DBF2 the qualified name leaked through.
        assert updates["state"] != "CircuitBreakerStateEnum.OPEN"

    def test_update_state_with_string_passes_through(self):
        repo, backend = _make_repo()

        repo.update_state("payment", state="closed")

        updates = backend.hset.call_args_list[0].args[1]
        assert updates["state"] == "closed"

    def test_set_manual_control_with_enum_writes_value_string(self):
        repo, backend = _make_repo()

        repo.set_manual_control(
            "payment",
            state=CircuitBreakerStateEnum.OPEN,
            controlled_by_id=99,
            reason="ops",
        )

        updates = backend.hset.call_args.args[1]
        assert updates["state"] == "open"
        assert updates["state"] != "CircuitBreakerStateEnum.OPEN"

    def test_set_manual_control_with_string_passes_through(self):
        repo, backend = _make_repo()

        repo.set_manual_control("payment", state="open")

        updates = backend.hset.call_args.args[1]
        assert updates["state"] == "open"
