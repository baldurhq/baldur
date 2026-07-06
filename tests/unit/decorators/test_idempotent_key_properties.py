"""Property-based tests for idempotency key encoding (595 D7 injectivity).

Complements the example-based escape tests in ``test_idempotent.py``: those
assert a handful of hand-picked escape-twin pairs, this generates adversarial
inputs (heavily sampling the separator ``|`` and escape ``\\`` characters) to
search the string space for a collision an example test would miss.

Invariant: two *different* value tuples must never assemble the same joined
key. A collision would be a false "already processed" between two genuinely
different calls — a wrong-result correctness bug.

``deadline=None``: the per-example timing deadline is disabled so a loaded
xdist worker cannot flake these on scheduling latency (the assertion, not the
wall-clock, is the contract).
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from baldur.decorators.idempotent import _escape_key_value

# Bias the alphabet toward the separator + escape chars, where an injectivity
# bug hides. A naive raw-``|`` join collides on exactly these inputs.
_adversarial_value = st.text(alphabet="ab|\\", min_size=0, max_size=6)
_value_lists = st.lists(_adversarial_value, min_size=1, max_size=4)


def _encode(values: list[str]) -> str:
    """The exact assembly the decorator's key builder uses."""
    return "|".join(_escape_key_value(v) for v in values)


@settings(max_examples=500, deadline=None)
@given(a=_value_lists, b=_value_lists)
def test_escape_join_is_injective(a: list[str], b: list[str]) -> None:
    """``encode(a) == encode(b)`` implies ``a == b`` — no false dedup."""
    if _encode(a) == _encode(b):
        assert a == b, f"key collision: {a!r} and {b!r} both encode to {_encode(a)!r}"


@settings(max_examples=200, deadline=None)
@given(values=_value_lists)
def test_encode_is_deterministic(values: list[str]) -> None:
    """The same input always assembles the same key (stable dedup identity)."""
    assert _encode(values) == _encode(list(values))
