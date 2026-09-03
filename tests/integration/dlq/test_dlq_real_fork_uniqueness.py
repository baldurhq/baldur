"""Real-``fork()`` node for the DLQ composite entry-id identity (782).

The unit suite owns the per-branch matrix against a simulated inherited state:
it stamps an identity with a foreign ``origin_pid`` and asserts what the repair
does with it. That simulation constructs the child's distinct identity by hand,
which is precisely the step a real fork child does not perform — so it models
the conclusion rather than the mechanism. Only a real ``fork()`` puts one
repository object in N processes with the state it was built with, and only
there is a child's pid a fact rather than a constructor argument.

Two classes, deliberately split by what they need:

- ``TestForkedChildIdentityIsolation`` needs **no infrastructure**. It forks
  children that allocate ids and report them through a pipe. On the pre-fix
  tree every child yields the same ``{pod}:{parent-pid}:{nonce}:{seq}`` tokens,
  so this class fails there by construction.
- ``TestForkedChildEntriesDoNotOverwrite`` needs **Redis**, because the claim
  is about a *shared* store: N processes writing to one sorted set, and the
  count of what survived. No per-process memory can express it. The pre-fix
  overwrite was silent — ``create()`` returned success in every child while the
  set held a single member.

Linux only: Windows has no ``os.fork()``. The public CI runs the integration
job on ubuntu with a redis service.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import ResilientStorageBackend

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="os.fork() is POSIX-only (Windows dev host)"
)

# Four siblings, three captures each: enough that a shared counter collides on
# every id rather than by luck, small enough to stay a sub-second fork storm.
_CHILDREN = 4
_ALLOCATIONS_PER_CHILD = 3

# Generous ceiling — the parent blocks on waitpid, so this bounds a hang.
_PIPE_READ_BYTES = 65536

_DOMAIN = "fork_uniqueness"
_FAILURE_TYPE = "deterministic_failure"

# Child exit statuses, so a parent-side assertion can say which half broke.
_CHILD_OK = 0
_CHILD_RAISED = 1
_CHILD_DEGRADED = 2


def _parse_id(entry_id: str) -> tuple[str, str, str, str]:
    """Split ``{pod_id}:{pid}:{run_nonce}:{seq}`` from the right.

    From the right because a pod id is a hostname and may carry a colon of its
    own; the three trailing segments never do.
    """
    pod_id, pid, run_nonce, seq = entry_id.rsplit(":", 3)
    return pod_id, pid, run_nonce, seq


def _fork_children(work: Callable[[int], int]) -> dict[int, list[str]]:
    """Fork ``_CHILDREN`` children, each running ``work`` and reporting ids.

    ``work`` writes the ids the child produced into the fd it is handed and
    returns the child's exit status; the ids come back through a pipe
    of the child's own, keyed by the pid the *parent* observed — the child's
    self-report is never taken on trust. Children ``os._exit`` so no pytest
    teardown, atexit hook or coverage writer runs twice.
    """
    pipes: dict[int, int] = {}
    for _ in range(_CHILDREN):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            status = _CHILD_OK
            try:
                os.close(read_fd)
                status = work(write_fd)
            except BaseException:
                status = _CHILD_RAISED
                # A child cannot fail an assertion, and its stderr is not the
                # parent's; the pipe is the only channel back, so send the
                # traceback down it rather than exiting with a bare status.
                try:
                    os.write(write_fd, traceback.format_exc().encode())
                except BaseException:
                    pass
            finally:
                os._exit(status)
        os.close(write_fd)
        pipes[pid] = read_fd

    reported: dict[int, list[str]] = {}
    for pid, read_fd in pipes.items():
        try:
            payload = os.read(read_fd, _PIPE_READ_BYTES).decode()
        finally:
            os.close(read_fd)
        _, wait_status = os.waitpid(pid, 0)
        assert os.WIFEXITED(wait_status), f"child {pid} did not exit normally"
        assert os.WEXITSTATUS(wait_status) == _CHILD_OK, (
            f"child {pid} exited {os.WEXITSTATUS(wait_status)} "
            f"(1 = raised, 2 = its writes never reached Redis):\n{payload}"
        )
        reported[pid] = payload.split() if payload else []

    return reported


class TestForkedChildIdentityIsolation:
    """Each pool child allocates in a namespace no sibling can collide with."""

    def test_every_forked_child_allocates_under_its_own_pid_and_nonce(self):
        """The defect verbatim: children of one preloaded parent allocated the
        same ids, and the unconditional write made each capture overwrite the
        previous one. No mock can stand in for the inheritance — the parent's
        object has to actually cross the boundary."""
        # Given one repository built in the parent, which has already allocated
        # (a preloaded worker main does the same before the pool starts).
        repo = RedisDLQRepository(MagicMock(spec=ResilientStorageBackend))
        parent_id = repo._allocate_id()
        parent_pid = os.getpid()

        # When N children inherit it and each allocates K times.
        def _allocate(write_fd: int) -> int:
            ids = [repo._allocate_id() for _ in range(_ALLOCATIONS_PER_CHILD)]
            os.write(write_fd, " ".join(ids).encode())
            return _CHILD_OK

        reported = _fork_children(_allocate)

        # Then no two children — and no child and the parent — share an id.
        every_id = [entry_id for ids in reported.values() for entry_id in ids]
        assert len(every_id) == _CHILDREN * _ALLOCATIONS_PER_CHILD
        assert len(set(every_id)) == len(every_id), (
            f"forked siblings allocated colliding entry ids: {sorted(every_id)}"
        )
        assert parent_id not in every_id

        parent_nonce = _parse_id(parent_id)[2]
        for pid, ids in reported.items():
            for entry_id in ids:
                _, id_pid, run_nonce, _ = _parse_id(entry_id)
                assert id_pid == str(pid), (
                    f"child {pid} allocated {entry_id}, which names another process"
                )
                assert id_pid != str(parent_pid)
                assert run_nonce != parent_nonce
            # The child re-owned the counter with the namespace, so its own
            # sequence starts at 0 — safe only under a nonce of its own.
            assert [_parse_id(entry_id)[3] for entry_id in ids] == [
                str(seq) for seq in range(_ALLOCATIONS_PER_CHILD)
            ]

    def test_the_parent_keeps_allocating_in_its_own_namespace(self):
        """The repair is per-process: a child re-owning its identity must not
        disturb the parent's, which keeps allocating after the pool starts."""
        repo = RedisDLQRepository(MagicMock(spec=ResilientStorageBackend))
        before = repo._allocate_id()

        def _allocate(write_fd: int) -> int:
            os.write(write_fd, repo._allocate_id().encode())
            return _CHILD_OK

        _fork_children(_allocate)
        after = repo._allocate_id()

        _, before_pid, before_nonce, before_seq = _parse_id(before)
        _, after_pid, after_nonce, after_seq = _parse_id(after)
        assert after_pid == before_pid == str(os.getpid())
        assert after_nonce == before_nonce
        assert int(after_seq) == int(before_seq) + 1


@pytest.mark.requires_redis
class TestForkedChildEntriesDoNotOverwrite:
    """N children capture into one Redis store and every capture survives."""

    def test_every_capture_from_every_child_survives_in_the_shared_index(
        self, redis_dlq_repository, redis_client
    ):
        """
        Purpose:
            Verify that concurrent prefork children each land their own DLQ
            entry rather than overwriting the previous sibling's.
        Expected:
            - The global index holds exactly N x K members after the storm
            - Those members are exactly the ids the children reported
            - Every entry's id names the child that captured it
        """
        # Given a repository the parent built and never wrote through, so each
        # child opens its own Redis connection on first use.
        repo = redis_dlq_repository
        all_key = repo._backend._get_full_key(repo.ALL_KEY)
        assert redis_client.zcard(all_key) == 0

        # When N children each capture K failures through it.
        def _capture(write_fd: int) -> int:
            ids = []
            for seq in range(_ALLOCATIONS_PER_CHILD):
                entry = repo.create(
                    domain=_DOMAIN,
                    failure_type=_FAILURE_TYPE,
                    error_message=f"pid={os.getpid()} seq={seq}",
                )
                ids.append(entry.id)
            # A degraded child would return success from every create() and
            # land nothing in the shared index, which would read here as a
            # passing count of zero rather than as the setup failure it is.
            if not repo._backend.is_redis_available:
                return _CHILD_DEGRADED
            os.write(write_fd, " ".join(ids).encode())
            return _CHILD_OK

        reported = _fork_children(_capture)

        # Then every capture survived. The count is asserted against the number
        # of captures issued, never against the number of distinct ids reported
        # — colliding ids would make that comparison agree with itself while
        # every overwritten entry stayed lost.
        captured = _CHILDREN * _ALLOCATIONS_PER_CHILD
        assert redis_client.zcard(all_key) == captured, (
            "the global DLQ index lost captures across forked children"
        )
        expected = {entry_id for ids in reported.values() for entry_id in ids}
        assert len(expected) == captured
        assert set(redis_client.zrange(all_key, 0, -1)) == expected

        for pid, ids in reported.items():
            for entry_id in ids:
                stored = repo.get_by_id(entry_id)
                assert stored is not None
                assert _parse_id(entry_id)[1] == str(pid)
                assert f"pid={pid}" in stored.error_message
