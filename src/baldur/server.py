"""
Gunicorn hook helpers for baldur.

Provides single-function entry points for Gunicorn lifecycle hooks.
Consumer only needs to call these functions in their gunicorn.conf.py.

Usage:
    # gunicorn.conf.py
    def post_fork(server, worker):
        from baldur.factory import ProviderRegistry
        ProviderRegistry.database_health.get().close_all()
        from baldur.server import post_fork_reset
        post_fork_reset(worker)

    def post_worker_init(worker):
        from baldur.server import post_worker_init_start
        post_worker_init_start(worker)

    def worker_exit(server, worker):
        from baldur.server import worker_exit_cleanup
        worker_exit_cleanup(worker)
"""

import logging
import time

from baldur.utils.time import utc_now

logger = logging.getLogger("gunicorn.error")

_SHUTDOWN_BUDGET_MARGIN_SECONDS = 10


def post_fork_reset(worker):
    """Reset all baldur external connections after fork.

    Covers: Redis, Kafka, OpenTelemetry, mmap, RNG.
    DB connection reset is NOT included — that is Django's responsibility.

    Each reset is isolated so that a failure in one does not prevent
    subsequent resets from running (e.g. Kafka failure must not skip
    mmap reset, which would leave a stale Writer singleton).

    Args:
        worker: Gunicorn worker instance
    """
    for fn in (_reset_redis, _reset_kafka, _reset_otel, _reset_mmap, _reseed_rng):
        try:
            fn(worker)
        except Exception as exc:
            logger.warning(
                "worker.postfork_reset_failed",
                extra={
                    "worker_id": worker.pid,
                    "function": fn.__name__,
                    "error": str(exc),
                },
            )


def post_worker_init_start(worker):
    """Start background threads in forked worker.

    Args:
        worker: Gunicorn worker instance
    """
    import os

    os.environ["GUNICORN_WORKER"] = "1"

    try:
        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig.start_background_threads()
        logger.info(
            "worker.background_threads_started",
            extra={"worker_id": worker.pid},
        )
    except ImportError:
        logger.warning(
            "worker.package_not_installed",
            extra={"worker_id": worker.pid},
        )
    except Exception as exc:
        logger.warning(
            "worker.background_thread_startup_failed",
            extra={"worker_id": worker.pid, "error": str(exc)},
        )


def worker_exit_cleanup(worker):
    """Trigger graceful shutdown pipeline on worker exit.

    Time Budget pattern: distributes the available budget (gunicorn timeout
    minus margin) dynamically across shutdown stages. If budget is exceeded,
    writes an emergency dump to local disk.

    Args:
        worker: Gunicorn worker instance
    """
    gunicorn_timeout = getattr(worker.cfg, "timeout", 60)
    budget = float(gunicorn_timeout) - _SHUTDOWN_BUDGET_MARGIN_SECONDS
    deadline = time.monotonic() + budget

    # 1. Background threads graceful stop
    remaining = deadline - time.monotonic()
    if remaining > 0:
        _stop_background_threads(worker)

    # 2. Leader elector shutdown
    remaining = deadline - time.monotonic()
    if remaining > 0:
        _shutdown_leader_electors(worker)

    # 3. Audit system shutdown
    remaining = deadline - time.monotonic()
    if remaining > 0:
        _shutdown_audit_system(worker)

    remaining = deadline - time.monotonic()
    if remaining < 0:
        _emergency_dump(worker)

    logger.info(
        "worker.graceful_shutdown_completed",
        extra={"worker_id": worker.pid},
    )


def _stop_background_threads(worker):
    """Graceful stop for background daemon threads.

    Daemon threads are killed without interruption on process exit.
    This gives in-flight batch/metric pushes a chance to complete.
    """
    try:
        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig.stop_background_threads()
    except Exception as exc:
        logger.warning(
            "worker.background_thread_stop_failed",
            extra={"worker_id": worker.pid, "error": str(exc)},
        )


def _shutdown_leader_electors(worker):
    """Shutdown all registered leader electors."""
    try:
        from baldur.coordination.shutdown_integration import (
            shutdown_all_electors,
        )

        shutdown_all_electors()
    except Exception as exc:
        logger.warning(
            "worker.leader_elector_shutdown_failed",
            extra={"worker_id": worker.pid, "error": str(exc)},
        )


def _shutdown_audit_system(worker):
    """Shutdown audit system (flush WAL, save checkpoint)."""
    try:
        from baldur.audit.async_audit_lifecycle import (
            graceful_shutdown_audit_system,
        )

        graceful_shutdown_audit_system()
    except Exception as exc:
        logger.warning(
            "worker.audit_system_shutdown_failed",
            extra={"worker_id": worker.pid, "error": str(exc)},
        )


def _emergency_dump(worker):
    """Write minimal state to local disk when shutdown budget is exceeded.

    Preserves enough state for recovery on next boot, preventing complete
    data loss from SIGKILL.
    """
    import json
    import os
    from pathlib import Path

    dump_dir = Path(
        os.environ.get(
            "BALDUR_EMERGENCY_DUMP_DIR",
            "/tmp/baldur_emergency",
        )
    )
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_file = dump_dir / f"worker_{worker.pid}_{int(time.time())}.json"

    try:
        dump_file.write_text(
            json.dumps(
                {
                    "worker_pid": worker.pid,
                    "timestamp": utc_now().isoformat(),
                    "reason": "shutdown_budget_exceeded",
                }
            )
        )
        logger.warning(
            "worker.shutdown_budget_exceeded",
            extra={
                "worker_id": worker.pid,
                "dump_file": str(dump_file),
            },
        )
    except Exception as exc:
        logger.exception(
            "worker.emergency_dump_failed",
            extra={"worker_id": worker.pid, "error": str(exc)},
        )


# === Internal reset functions ===


def _reset_redis(worker):
    """Reset Redis connections after fork.

    Invalidates the ProviderRegistry singleton so the next get_cache()
    call creates a fresh RedisCacheAdapter with a new ConnectionPool.
    The parent's stale pool (with inherited FDs) is abandoned.
    """
    from baldur.factory import ProviderRegistry

    ProviderRegistry.cache.invalidate_instance("redis")
    logger.info(
        "worker.postfork_redis_invalidated",
        extra={"worker_id": worker.pid},
    )


def _reset_kafka(worker):
    """Reset Kafka producer after fork (fork-safe).

    Uses reset_kafka_producer(cleanup=False) which only drops the reference
    without calling close()/flush(), avoiding deadlock from dead librdkafka
    background threads.

    Kafka adapters live in ``baldur_dormant`` per doc 528 D10-v2; this
    fork hook is a no-op when ``baldur_dormant`` is not installed.
    """
    try:
        from baldur_dormant.adapters.kafka.config import get_kafka_settings
        from baldur_dormant.adapters.kafka.producer import reset_kafka_producer
    except ImportError:
        logger.debug(
            "worker.postfork_kafka_skipped_no_dormant",
            extra={"worker_id": worker.pid},
        )
        return

    settings = get_kafka_settings()
    if not settings.bootstrap_servers:
        logger.debug(
            "worker.postfork_kafka_skipped",
            extra={"worker_id": worker.pid},
        )
        return

    reset_kafka_producer(cleanup=False)
    logger.info(
        "worker.postfork_kafka_reset",
        extra={"worker_id": worker.pid},
    )


def _reset_otel(worker):
    """Reset OpenTelemetry state after fork.

    Drops master's gRPC channels/thread references so the worker
    creates fresh Exporters on next use (Reset + Lazy Reinitialize).
    """
    from baldur.observability import reset_opentelemetry

    reset_opentelemetry()
    logger.info(
        "worker.postfork_otel_reset",
        extra={"worker_id": worker.pid},
    )


def _reset_mmap(worker):
    """Re-initialize mmap CB snapshot as Reader after fork.

    mmap FD itself is shared via MAP_SHARED, but the Python wrapper
    (Lock, daemon thread, Writer flag) is not fork-safe. Destroy the
    master's is_writer=True singleton and create a Reader instance.

    2-layer defense:
    - L1 (cb_state_snapshot.py): reset uses try/finally to guarantee
      _snapshot_instance = None even if stop() fails.
    - L2 (here): reset and recreate are isolated so that a reset failure
      does not prevent Reader creation.
    """
    from baldur.adapters.ipc import (
        configure_cb_state_snapshot,
        reset_cb_state_snapshot,
    )
    from baldur.adapters.ipc.cb_state_snapshot import CBStateSnapshot

    try:
        reset_cb_state_snapshot()
    except Exception as exc:
        logger.warning(
            "worker.postfork_mmap_reset_failed",
            extra={"worker_id": worker.pid, "error": str(exc)},
        )

    configure_cb_state_snapshot(CBStateSnapshot(is_writer=False))
    logger.info(
        "worker.postfork_mmap_reader_initialized",
        extra={"worker_id": worker.pid},
    )


def _reseed_rng(worker):
    """Reseed random number generator after fork."""
    import random

    random.seed()
    logger.info(
        "worker.postfork_rng_reseeded",
        extra={"worker_id": worker.pid},
    )
