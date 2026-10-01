"""A second worker process for the LLM shared-wait Redis test.

Run as a script by ``redis/test_llm_wrap_shared_wait_redis.py``; never collected
as a test. The peer is configured the way a deployment is — ``BALDUR_REDIS_URL``
in its environment, nothing injected — wraps an OpenAI client pointed at the
test's local server, and makes one call once the test tells it to.

Protocol: every line the peer prints for the test starts with ``PEER `` and
carries one JSON object — ``ready`` (with the coordinator's storage type) once
it is wired, ``answered`` (the call's wall-clock start and end, and the answer)
or ``failed``, then ``done``. Configuration arrives through environment
variables:

- ``PEER_BASE_URL``: the OpenAI-compatible server to call.
- ``PEER_MODEL``: the model the call names.
- ``PEER_GO_FILE``: the peer makes its call once this file exists.
- ``PEER_STOP_FILE``: the peer exits once this file exists.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_POLL_SECONDS = 0.02
_MAX_WAIT_SECONDS = 60.0


def _emit(event: str, **fields: object) -> None:
    print("PEER " + json.dumps({"event": event, **fields}), flush=True)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + _MAX_WAIT_SECONDS
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)


def main() -> None:
    import openai

    import baldur
    from baldur.services.rate_limit_coordinator import RateLimitCoordinator

    client = openai.OpenAI(api_key="peer-key", base_url=os.environ["PEER_BASE_URL"])
    llm = baldur.llm.wrap(client)
    _emit("ready", storage=RateLimitCoordinator.get_instance().storage_type)

    _wait_for(Path(os.environ["PEER_GO_FILE"]))
    started = time.time()
    try:
        response = llm.chat.completions.create(
            model=os.environ["PEER_MODEL"],
            messages=[{"role": "user", "content": "peer"}],
        )
    except Exception as error:  # reported to the test, which fails on it
        _emit("failed", error=f"{type(error).__name__}: {error}")
    else:
        _emit(
            "answered",
            started=started,
            finished=time.time(),
            content=str(response.choices[0].message.content),
        )

    _wait_for(Path(os.environ["PEER_STOP_FILE"]))
    _emit("done")


if __name__ == "__main__":
    main()
