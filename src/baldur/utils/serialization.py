"""Fast JSON serialization utilities.

Automatic orjson acceleration with stdlib json fallback.
Used across WAL, Audit, Kafka, and Redis hot paths.

Functions:
    fast_dumps:           Fast serialization → bytes (for file/network I/O)
    fast_dumps_str:       Fast serialization → str (for Redis/API)
    fast_canonical_dumps: Deterministic serialization → bytes (for hash computation)
    fast_dumps_pretty:    Pretty-printed serialization → str (for CLI/debug only)
    fast_loads:           Fast deserialization (accepts bytes or str)

Usage:
    from baldur.utils.serialization import fast_dumps, fast_loads

    encoded = fast_dumps(data)        # bytes
    decoded = fast_loads(encoded)     # dict
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

__all__ = [
    "fast_dumps",
    "fast_dumps_str",
    "fast_dumps_str_compact",
    "fast_canonical_dumps",
    "fast_dumps_pretty",
    "fast_loads",
    "FAST_JSON_AVAILABLE",
]

# orjson availability check
try:
    import orjson

    def fast_dumps(
        obj: Any,
        *,
        default: Callable[[Any], Any] | None = None,
    ) -> bytes:
        """Fast JSON serialization (returns bytes).

        For file/network I/O paths (WAL, Kafka, HTTP).

        Args:
            obj: Object to serialize.
            default: Fallback serializer for non-standard types (e.g., ``str``).

        Returns:
            UTF-8 encoded JSON bytes.
        """
        return orjson.dumps(obj, default=default)

    def fast_loads(data: bytes | str) -> Any:
        """Fast JSON deserialization.

        Args:
            data: JSON bytes or string.

        Returns:
            Deserialized Python object.
        """
        return orjson.loads(data)

    def fast_dumps_pretty(
        obj: Any,
        *,
        default: Callable[[Any], Any] | None = None,
    ) -> str:
        """Pretty-printed JSON for CLI/debug output only.

        NOT for hot paths.

        Args:
            obj: Object to serialize.
            default: Fallback serializer for non-standard types.

        Returns:
            Indented JSON string.
        """
        return orjson.dumps(obj, option=orjson.OPT_INDENT_2, default=default).decode(
            "utf-8"
        )

    FAST_JSON_AVAILABLE = True

except ImportError:

    def fast_dumps(  # type: ignore[misc]
        obj: Any,
        *,
        default: Callable[[Any], Any] | None = None,
    ) -> bytes:
        """Standard JSON serialization (returns bytes).

        Stdlib fallback when orjson is not installed.

        Args:
            obj: Object to serialize.
            default: Fallback serializer for non-standard types.

        Returns:
            UTF-8 encoded JSON bytes.
        """
        return json.dumps(
            obj, separators=(",", ":"), default=default, ensure_ascii=False
        ).encode("utf-8")

    def fast_loads(data: bytes | str) -> Any:  # type: ignore[misc]
        """Standard JSON deserialization.

        Args:
            data: JSON bytes or string.

        Returns:
            Deserialized Python object.
        """
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return json.loads(data)

    def fast_dumps_pretty(  # type: ignore[misc]
        obj: Any,
        *,
        default: Callable[[Any], Any] | None = None,
    ) -> str:
        """Pretty-printed JSON for CLI/debug output only.

        NOT for hot paths.

        Args:
            obj: Object to serialize.
            default: Fallback serializer for non-standard types.

        Returns:
            Indented JSON string.
        """
        return json.dumps(obj, indent=2, default=default, ensure_ascii=False)

    FAST_JSON_AVAILABLE = False


def fast_dumps_str(
    obj: Any,
    *,
    default: Callable[[Any], Any] | None = None,
) -> str:
    """Fast JSON serialization (returns str).

    For Redis storage, API responses, pub/sub.

    Args:
        obj: Object to serialize.
        default: Fallback serializer for non-standard types.

    Returns:
        JSON string.
    """
    result = fast_dumps(obj, default=default)
    if isinstance(result, bytes):
        return result.decode("utf-8")
    return result


def fast_dumps_str_compact(
    data: dict[str, Any],
    *,
    defaults: dict[str, Any],
    default: Callable[[Any], Any] | None = None,
) -> str:
    """Encode dict with default-valued keys dropped (top level only).

    Keys whose value compares equal to the matching entry in ``defaults``
    are omitted before encoding. The decode-side dataclass is responsible
    for restoring dropped fields via its own defaults — no marker is
    written, so the decoded shape exactly matches a non-compact decode.

    Recursion is intentionally not performed: nested dict/list values are
    encoded as-is. Storage savings come from omitting top-level field
    names whose values are typically empty (snapshot_data={}, error_code='').
    """
    filtered = {k: v for k, v in data.items() if k not in defaults or v != defaults[k]}
    return fast_dumps_str(filtered, default=default)


def fast_canonical_dumps(
    obj: Any,
    *,
    default: Callable[[Any], Any] | None = None,
) -> bytes:
    """Deterministic JSON serialization for hash computation.

    Guarantees: sort_keys + compact separators + ensure_ascii=False + UTF-8.
    For hash chains, Merkle trees, checksums, idempotency keys.

    ALWAYS uses stdlib json.dumps internally — never orjson.
    This guarantees byte-identical output regardless of orjson version,
    preventing hash chain / Merkle root invalidation from serializer differences.

    NOT for general serialization — sort_keys has ~20% performance cost.

    Args:
        obj: Object to serialize.
        default: Fallback serializer for non-standard types.

    Returns:
        UTF-8 encoded deterministic JSON bytes.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        default=default,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
