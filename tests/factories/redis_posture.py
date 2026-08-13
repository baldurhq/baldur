"""The "redis-py installed, nobody named a Redis" posture, as a fixture.

Two lanes stop dialing an unnamed Redis in this posture — rate-limit
auto-detection and the layered circuit-breaker repository's initial L2 load —
and both gate on a predicate that reads the environment. A test that arranges
the posture by *setting* a variable would flip that predicate and measure the
opposite branch, so the URL is patched on the settings object instead.

The address the fixture installs refuses instantly (a port bound and closed,
so the kernel answers with RST rather than a listener). Nothing under test is
supposed to dial it; it is there so a regression that resumes dialing fails in
milliseconds instead of blocking the suite on a connect budget.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

__all__ = ["no_redis_posture", "refusing_redis_url"]


def refusing_redis_url() -> str:
    """A loopback Redis URL whose connect is answered by RST, not a listener.

    The port is bound only long enough to learn which one the kernel handed
    out. A later bind of the same port is possible in principle; the value of
    the address is that nothing is listening on it *now*, which is what the
    posture needs.
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    return f"redis://127.0.0.1:{port}/0"


@pytest.fixture
def no_redis_posture(monkeypatch) -> Iterator[str]:
    """Arrange "redis-py importable, no Redis named, no Redis reachable".

    Yields the unreachable URL the settings object now carries.

    The intent variables are cleared from the exported constant rather than
    from a list written here: clearing is not expressing intent, and an
    ambient ``REDIS_URL`` in a developer shell or a CI job would otherwise
    disarm both skips and leave every assertion in this lane passing against
    the configured posture.

    The whole settings root is dropped rather than the Redis class alone.
    Four settings classes copy ``get_redis_settings().url`` into their own
    field at construction, and a hand-authored list of the copiers
    regenerates this defect the moment a fifth appears.

    ``reset_redis_settings()`` is deliberately not called while the fixture is
    armed: ``get_redis_settings()`` returns a cached attribute of the settings
    root, so resetting it mid-test would hand the code under test a fresh
    object without the patch.
    """
    from baldur.settings.redis import (
        REDIS_INTENT_ENV_VARS,
        redis_absence_is_expected,
        redis_explicitly_configured,
    )
    from baldur.settings.root import get_config, reset_config

    for name in REDIS_INTENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    reset_config()

    url = refusing_redis_url()
    # Assignment on the object, never the environment. Pydantic validation is
    # not re-run here because the shared settings config leaves
    # validate_assignment off, which is what makes the object patch legal.
    get_config().adapters.redis.url = url

    # Preconditions, not decoration: both predicates gate the branches under
    # test, and a host that answers True to either one would run every case in
    # this lane against the wrong posture.
    assert redis_explicitly_configured() is False
    assert redis_absence_is_expected() is True

    yield url

    reset_config()
