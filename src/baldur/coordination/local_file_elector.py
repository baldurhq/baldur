"""Local single-host leader elector backed by a cross-platform file lock.

Elects exactly one process **per host** using the in-tree non-blocking
file lock (``audit/checkpoint/file_lock.py``: ``msvcrt`` on Windows,
``fcntl`` elsewhere). This is the inline scheduler's default elector when
distributed leader election is disabled (the OSS/single-host default): it
makes the framework run its scheduled jobs out-of-box on a single host —
including ``gunicorn -w N`` (the N workers contend for one lock, so only
one becomes the scheduler) — without requiring Redis or ``baldur_pro``.

Scope and trade-off:

- **Single-host**: fully correct. One process holds the OS file lock; the
  others stay followers. When the leader dies the OS releases the lock and
  a follower's retry thread acquires it (automatic failover).
- **Multi-host**: a file lock cannot coordinate across hosts (physical
  constraint), so each host elects its own leader — one scheduler per host
  rather than one per cluster. The default scheduled jobs are idempotent,
  so this is bounded (duplicate, not wrong) and strictly better than the
  previous behavior (with ``NoOpLeaderElector`` the inline scheduler ran
  *nothing*). True cluster-wide single-execution still requires distributed
  election (Redis/K8s) or Celery beat — see the multi-worker-coherence
  runbook.

Construction is side-effect-free (mirrors ``NoOpLeaderElector``): the lock
file is opened only in ``start()``, so merely building the elector — e.g.
when a test mocks the scheduler — spawns no thread and touches no file.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import structlog

from baldur.audit.checkpoint.file_lock import lock_file, unlock_file
from baldur.coordination.base import LeaderElector, LeaderInfo, LeadershipState
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["LocalFileLeaderElector", "DEFAULT_RETRY_INTERVAL_SECONDS"]

DEFAULT_RETRY_INTERVAL_SECONDS = 5.0


def _sanitize(token: str) -> str:
    """Reduce a namespace token to a filesystem-safe slug."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in token)[:64]


def _resolve_namespace() -> str:
    """Resolve a per-service lock namespace.

    Uses the configured OTEL ``service_name`` when it is set to a non-default
    value, so two distinct baldur services co-located on one host do not
    starve each other's scheduler. Falls back to a stable token derived from
    the entrypoint + working directory (which keeps ``gunicorn -w N`` workers
    of the *same* service contending for one lock, as intended).
    """
    try:
        from baldur.settings.otel import get_otel_settings

        name = get_otel_settings().service_name
        if name and name != "baldur":
            return _sanitize(name)
    except Exception:
        pass

    seed = f"{sys.argv[0] if sys.argv else ''}|{os.getcwd()}"
    return hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:12]


def _default_lock_path(resource_name: str) -> Path:
    """Per-service lock path under the platform temp dir."""
    ns = _resolve_namespace()
    return Path(tempfile.gettempdir()) / f"baldur-{resource_name}-{ns}.lock"


class LocalFileLeaderElector(LeaderElector):
    """Leader elector that holds an OS file lock to elect one process per host.

    Implements the full :class:`LeaderElector` contract. ``is_leader()``
    reflects whether this process currently holds the lock. ``start()``
    attempts a non-blocking acquire: on success the process is leader and the
    registered ``on_become_leader`` callbacks fire (this is what spawns the
    scheduler loop); on failure the process stays a follower and a lightweight
    daemon thread re-attempts acquisition on ``retry_interval_seconds`` so it
    takes over when the current leader dies.
    """

    def __init__(
        self,
        resource_name: str = "scheduler",
        settings: object | None = None,
        *,
        lock_path: str | Path | None = None,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    ) -> None:
        # Side-effect-free: store config only. No file is opened and no thread
        # is spawned until start() — mirrors NoOpLeaderElector so merely
        # constructing the elector (e.g. a mocked scheduler in tests) is inert.
        self._resource_name = resource_name
        self._lock_path = Path(lock_path) if lock_path else None
        self._retry_interval = retry_interval_seconds

        self._state_lock = threading.Lock()
        self._lock_handle = None  # type: ignore[var-annotated]
        self._is_leader = False
        self._fencing_token = 0
        self._elected_at = None  # type: ignore[var-annotated]

        self._on_become_callbacks: list[Callable[[], None]] = []
        self._on_lose_callbacks: list[Callable[[], None]] = []

        self._retry_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # -- LeaderElector contract ------------------------------------------------

    @property
    def resource_name(self) -> str:
        return self._resource_name

    @property
    def state(self) -> LeadershipState:
        with self._state_lock:
            if self._stop_event.is_set():
                return LeadershipState.STOPPED
            if self._is_leader:
                return LeadershipState.LEADER
            # start() not called yet — no acquire attempt, no retry thread
            if (
                self._elected_at is None
                and self._lock_handle is None
                and not self._retry_thread
            ):
                return LeadershipState.NOT_STARTED
            return LeadershipState.FOLLOWER

    def is_leader(self) -> bool:
        with self._state_lock:
            return self._is_leader

    def get_leader(self) -> LeaderInfo | None:
        with self._state_lock:
            if not self._is_leader or self._elected_at is None:
                return None
            return LeaderInfo(
                node_id=f"{self._resource_name}@pid{os.getpid()}",
                elected_at=self._elected_at,
                lease_expires_at=self._elected_at,
                fencing_token=self._fencing_token,
                is_self=True,
            )

    def get_fencing_token(self) -> int:
        with self._state_lock:
            return self._fencing_token

    def is_lease_valid(self) -> bool:
        # The held OS lock IS the lease; valid while we hold it.
        return self.is_leader()

    def start(self) -> None:
        """Attempt to acquire leadership; spawn a retry thread if contended."""
        self._stop_event.clear()
        if self._lock_path is None:
            self._lock_path = _default_lock_path(self._resource_name)

        acquired = self._try_acquire()
        if acquired:
            logger.info(
                "local_file_elector.acquired",
                resource=self._resource_name,
                lock_path=str(self._lock_path),
            )
            self._fire_become_leader()
        else:
            logger.info(
                "local_file_elector.follower",
                resource=self._resource_name,
                lock_path=str(self._lock_path),
                hint="another process holds the scheduler lock on this host",
            )
            self._start_retry_thread()

    def stop(self) -> None:
        """Release the lock (if held), stop the retry thread."""
        self._stop_event.set()

        thread = self._retry_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=self._retry_interval + 1.0)
        self._retry_thread = None

        with self._state_lock:
            was_leader = self._is_leader
            self._release_locked()

        if was_leader:
            self._fire_lose_leader()
        logger.debug("local_file_elector.stopped", resource=self._resource_name)

    def on_become_leader(
        self,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        self._on_become_callbacks.append(callback)
        return callback

    def on_lose_leader(
        self,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        self._on_lose_callbacks.append(callback)
        return callback

    # -- Internals -------------------------------------------------------------

    def _try_acquire(self) -> bool:
        """Non-blocking acquire attempt. Returns True if this process is leader."""
        with self._state_lock:
            if self._is_leader:
                return True
            assert self._lock_path is not None  # set in start()
            try:
                # Intentionally long-lived: the open handle holds the OS lock for
                # the leader's whole lifetime and is closed in stop()/_release_locked.
                # A context manager would release the lock immediately.
                handle = open(self._lock_path, "a+b")  # noqa: SIM115
            except OSError as e:
                logger.warning(
                    "local_file_elector.open_failed",
                    resource=self._resource_name,
                    lock_path=str(self._lock_path),
                    error=e,
                )
                return False
            try:
                handle.seek(0)
                lock_file(handle, blocking=False)
            except OSError:
                # Lock held by another process — stay follower.
                handle.close()
                return False
            self._lock_handle = handle
            self._is_leader = True
            self._fencing_token += 1
            self._elected_at = utc_now()
            return True

    def _release_locked(self) -> None:
        """Release the held lock. Caller must hold ``self._state_lock``."""
        handle = self._lock_handle
        if handle is not None:
            try:
                unlock_file(handle)
            except OSError as e:
                logger.debug(
                    "local_file_elector.unlock_failed",
                    resource=self._resource_name,
                    error=e,
                )
            try:
                handle.close()
            except OSError:
                pass
        self._lock_handle = None
        self._is_leader = False

    def _start_retry_thread(self) -> None:
        if self._retry_thread is not None and self._retry_thread.is_alive():
            return
        self._retry_thread = threading.Thread(
            target=self._retry_loop,
            name=f"LocalFileElector-{self._resource_name}",
            daemon=True,
        )
        self._retry_thread.start()

    def _retry_loop(self) -> None:
        """Periodically re-attempt acquisition until it succeeds or stop()."""
        while not self._stop_event.wait(timeout=self._retry_interval):
            if self._try_acquire():
                logger.info(
                    "local_file_elector.acquired_on_failover",
                    resource=self._resource_name,
                    lock_path=str(self._lock_path),
                )
                self._fire_become_leader()
                return

    def _fire_become_leader(self) -> None:
        for callback in list(self._on_become_callbacks):
            try:
                callback()
            except Exception as e:
                logger.exception(
                    "local_file_elector.on_become_callback_failed",
                    resource=self._resource_name,
                    error=e,
                )

    def _fire_lose_leader(self) -> None:
        for callback in list(self._on_lose_callbacks):
            try:
                callback()
            except Exception as e:
                logger.exception(
                    "local_file_elector.on_lose_callback_failed",
                    resource=self._resource_name,
                    error=e,
                )
