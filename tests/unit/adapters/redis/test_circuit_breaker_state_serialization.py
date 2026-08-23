"""``RedisCircuitBreakerStateRepository`` enum-state serialization tests (#466 DBF2).

Pin the wire-format produced when callers pass ``CircuitBreakerStateEnum``
values directly. Under Python 3.11+ ``str(Enum)`` returns the qualified
name (``"CircuitBreakerStateEnum.OPEN"``) rather than the ``.value``
(``"open"``), which would corrupt every Redis HGET inspection (Grafana,
jq pipelines). The repository normalizes at the wire boundary so callers
can keep passing enums.

Also covers the read side of the half-open window watermark, which the
acquire contract reads back through ``_parse_unix_timestamp``.

Reference: ``466`` DBF2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

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


# =============================================================================
# Contract — half-open window watermark parsing at the HGET boundary
# =============================================================================


class TestRedisUnixTimestampParseContract:
    """``half_open_window_started_at`` reads back as UTC, or as absent.

    The watermark is stored as Unix seconds so the acquire script can do the
    window-age arithmetic in Lua. This parser is the read side of that
    choice, and the acquire contract leans on one property of it: an
    unparseable stored value folds into the same "absent" case as a missing
    one, never into a bogus timestamp that would make a stalled window look
    fresh.
    """

    @pytest.mark.parametrize(
        "stored",
        [None, "", "not-a-number", "12:34:56", "NaN-ish"],
        ids=["missing", "empty", "garbage", "iso_like", "almost_numeric"],
    )
    def test_unparseable_watermark_reads_as_absent(self, stored):
        assert RedisCircuitBreakerStateRepository._parse_unix_timestamp(stored) is None

    def test_unix_seconds_string_reads_back_as_utc_datetime(self):
        # 2026-02-10 10:00:00 UTC as Unix seconds.
        epoch_seconds = datetime(2026, 2, 10, 10, 0, 0, tzinfo=UTC).timestamp()

        parsed = RedisCircuitBreakerStateRepository._parse_unix_timestamp(
            str(epoch_seconds)
        )

        assert parsed == datetime(2026, 2, 10, 10, 0, 0, tzinfo=UTC)
        assert parsed.tzinfo is not None

    def test_float_value_is_accepted_without_string_coercion(self):
        """The Lua script writes a string, but a client may hand back a float."""
        parsed = RedisCircuitBreakerStateRepository._parse_unix_timestamp(1.0)

        assert parsed == datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)
