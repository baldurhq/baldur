"""Two worker processes on one LLM endpoint share the provider's wait through Redis.

``baldur.llm.wrap`` names each endpoint after its host and model, and the wait a
429 installs is kept under that name in Baldur's rate-limit store. With Redis
as the store, a second worker process calling the same endpoint finds the wait
and sends nothing until it ends — the fleet asks the provider once, not once
per worker.

Process A is this test process; process B is ``_llm_wait_peer.py``, configured
the way a deployment is (``BALDUR_REDIS_URL`` in its environment). Both call
the real ``openai`` SDK against one local OpenAI-compatible server, which
counts the requests it receives inside the wait it asked for.

Test Categories:
    A. A 429 one process receives holds back the other:
        - A is answered 429 with ``retry-after: 3``; the wait lands in Redis
          under the endpoint's name
        - B, starting its call inside that wait, reaches the server only after
          it ends; the server sees one request inside the wait in all

All tests auto-skip without Redis (``requires_redis``).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import redis

from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage
from baldur.llm import wrap
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from baldur.settings.protect import reset_protect_settings
from tests.factories.llm_doubles import (
    OpenAICompatibleStub,
    completion_body,
    error_body,
)
from tests.factories.peer_process import PeerProcess

openai = pytest.importorskip("openai")

pytestmark = pytest.mark.requires_redis

_PEER = Path(__file__).parent.parent / "_llm_wait_peer.py"
_MODEL = "gpt-4o-mini"
# Both processes derive this name from the server's host and the model.
_ENDPOINT = "llm.127_0_0_1.gpt_4o_mini"
_PROVIDER_WAIT_SECONDS = 3
_READY_TIMEOUT_SECONDS = 60.0
_ANSWER_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.02


class _RateLimitingProvider:
    """Answers 429 with ``retry-after`` for a window opened by the first request."""

    def __init__(self, wait_seconds: int) -> None:
        self.wait_seconds = wait_seconds
        self.window_started: float | None = None
        self.first_refusal = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, content: str) -> Any:
        with self._lock:
            now = time.time()
            if self.window_started is None:
                self.window_started = now
            if now - self.window_started < self.wait_seconds:
                self.first_refusal.set()
                return (
                    429,
                    error_body("Rate limit reached for requests.", "requests"),
                    {"retry-after": str(self.wait_seconds)},
                )
        return 200, completion_body(f"answer to {content}"), {}

    def window_end(self) -> float:
        assert self.window_started is not None
        return self.window_started + self.wait_seconds


@pytest.fixture
def coordinator(redis_url) -> Iterator[RateLimitCoordinator]:
    """Process A's coordinator over the shared Redis, without jitter."""
    client = redis.from_url(redis_url, decode_responses=True)
    instance = RateLimitCoordinator(
        storage=RedisRateLimitStorage(client),
        config=RateLimitCoordinatorConfig(
            jitter_percent=0.0, debounce_window_seconds=0.0, default_retry_after=0.5
        ),
    )
    reset_protect_settings()
    with (
        patch.object(RateLimitCoordinator, "_instance", instance),
        patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True),
    ):
        yield instance
        RateLimitCoordinator.reset_instance()
    reset_protect_settings()
    client.close()


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(_POLL_SECONDS)
    return predicate()


class TestLlmWrapSharedWaitAcrossProcesses:
    """One endpoint, two processes, one Redis: the provider's wait is the fleet's."""

    def test_a_429_in_one_process_holds_back_the_other_until_the_wait_ends(
        self, redis_url, coordinator, tmp_path
    ):
        """
        Purpose:
            The wait a provider asks for is honored by every worker process, not
            only the one it was said to.
        Expected:
            - process B is wired to Redis by its environment alone
            - A's 429 leaves a wait in Redis under the endpoint's name
            - B starts its call inside that wait and its request reaches the
              server only after the wait ends
            - the server receives exactly one request inside the wait (A's)
            - both calls are answered
        """
        # Given — a server that asks for a 3 s wait, and process B, ready
        provider = _RateLimitingProvider(_PROVIDER_WAIT_SECONDS)
        go_file = tmp_path / f"go-{uuid.uuid4().hex}"
        with OpenAICompatibleStub() as stub:
            stub.answer = provider
            base = {
                k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"
            }
            peer = PeerProcess(
                _PEER,
                {
                    **base,
                    "BALDUR_REDIS_URL": redis_url,
                    "PEER_BASE_URL": stub.base_url,
                    "PEER_MODEL": _MODEL,
                    "PEER_GO_FILE": str(go_file),
                },
                tmp_path / f"stop-{uuid.uuid4().hex}",
                shutdown_timeout=_READY_TIMEOUT_SECONDS,
            )
            try:
                ready = peer.next_event("ready", timeout=_READY_TIMEOUT_SECONDS)
                answers: dict[str, Any] = {}

                def worker_a() -> None:
                    llm = wrap(openai.OpenAI(api_key="a-key", base_url=stub.base_url))
                    response = llm.chat.completions.create(
                        model=_MODEL, messages=[{"role": "user", "content": "a"}]
                    )
                    answers["a"] = response.choices[0].message.content

                # When — A is refused and records the wait; then B calls
                thread_a = threading.Thread(target=worker_a, daemon=True)
                thread_a.start()
                refused = provider.first_refusal.wait(_ANSWER_TIMEOUT_SECONDS)
                waiting = _wait_until(
                    lambda: coordinator.get_state(_ENDPOINT).is_in_cooldown,
                    _ANSWER_TIMEOUT_SECONDS,
                )
                go_file.touch()
                answered = peer.next_event("answered", timeout=_ANSWER_TIMEOUT_SECONDS)
                thread_a.join(_ANSWER_TIMEOUT_SECONDS)
            finally:
                if peer.proc.poll() is None:
                    peer.finish()
                peer.kill()
            requests = list(stub.requests)

        # Then — B was wired to Redis, and the wait held it back
        assert ready["storage"] == "redis"
        assert refused
        assert waiting
        window_end = provider.window_end()
        peer_requests = [r for r in requests if r.content == "peer"]
        assert answered["started"] < window_end
        assert [r.at >= window_end for r in peer_requests] == [True]
        assert sum(r.at < window_end for r in requests) == 1
        assert answered["content"] == "answer to peer"
        assert answers["a"] == "answer to a"
