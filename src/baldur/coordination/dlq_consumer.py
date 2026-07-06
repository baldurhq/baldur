"""
DLQ Consumer with Leader Election.

분산 환경에서 단일 노드만 DLQ를 처리하도록 보장.
Leader Election을 통해 여러 Pod 중 하나만 활성화됩니다.

Usage:
    from baldur.coordination.dlq_consumer import DLQConsumerCoordinator

    coordinator = DLQConsumerCoordinator()
    coordinator.start()
    # ...
    coordinator.stop()
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog

from baldur.coordination.factory import get_leader_elector
from baldur.coordination.shutdown_integration import (
    register_for_graceful_shutdown,
)
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.emitter import EventEmitterMixin

if TYPE_CHECKING:
    from baldur.coordination.base import LeaderElector
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()

# DLQ Consumer 리소스 이름 (Leader Election 키)
DLQ_CONSUMER_RESOURCE = "dlq-consumer"


class DLQConsumerCoordinator(EventEmitterMixin):
    """
    DLQ Consumer 리더 선출 코디네이터.

    분산 환경에서 단일 DLQ Consumer만 활성화되도록 보장합니다.
    리더가 되면 DLQ 처리를 시작하고, 리더십을 잃으면 중단합니다.

    Attributes:
        elector: Leader Elector 인스턴스
        is_consuming: 현재 DLQ 처리 중인지 여부
    """

    _event_source = "dlq_consumer"

    def __init__(
        self,
        resource_name: str = DLQ_CONSUMER_RESOURCE,
        process_interval_seconds: float = 10.0,
        batch_size: int = 50,
    ):
        """
        초기화.

        Args:
            resource_name: 리소스 이름 (리더 선출 키)
            process_interval_seconds: DLQ 처리 주기 (초)
            batch_size: 한 번에 처리할 DLQ 항목 수
        """
        self._resource_name = resource_name
        self._process_interval = process_interval_seconds
        self._batch_size = batch_size

        self._elector: LeaderElector = get_leader_elector(resource_name)
        self._consuming = False
        self._consume_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # 콜백 등록
        self._elector.on_become_leader(self._on_become_leader)
        self._elector.on_lose_leader(self._on_lose_leader)

        # Graceful Shutdown 등록
        register_for_graceful_shutdown(self._elector)

    @property
    def is_consuming(self) -> bool:
        """현재 DLQ 처리 중인지 여부."""
        return self._consuming

    @property
    def is_leader(self) -> bool:
        """현재 리더인지 여부."""
        return self._elector.is_leader()

    def start(self) -> None:
        """DLQ Consumer 시작 (Leader Election 시작)."""
        logger.info(
            "dlq_consumer.started",
            resource_name=self._resource_name,
        )
        self._stop_event.clear()
        self._elector.start()
        self._emit_event(
            EventType.DLQ_CONSUMER_STARTED,
            data={"resource_name": self._resource_name},
        )

    def stop(self) -> None:
        """DLQ Consumer 중지 (Leader Election 중지)."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        logger.info(
            "dlq_consumer.stopping",
            resource_name=self._resource_name,
        )
        if self._handle is not None:
            self._handle.is_stopping = True
        self._stop_event.set()
        self._consuming = False

        # 소비 스레드 종료 대기
        from baldur.settings.thread_management import (
            get_thread_management_settings,
        )

        timeout = get_thread_management_settings().join_timeout
        if self._consume_thread and self._consume_thread.is_alive():
            self._consume_thread.join(timeout=timeout)

        worker_name = f"DLQConsumer-{self._resource_name}"
        unregister_daemon_worker(worker_name)
        if self._consume_thread is not None and self._consume_thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name=worker_name,
                join_timeout_seconds=timeout,
            )

        self._elector.stop()
        logger.info(
            "dlq_consumer.stopped",
            resource_name=self._resource_name,
        )
        self._emit_event(
            EventType.DLQ_CONSUMER_STOPPED,
            data={"resource_name": self._resource_name},
        )

    def _on_become_leader(self) -> None:
        """리더가 되었을 때 DLQ 처리 시작."""
        logger.info(
            "dlq_consumer.leader_dlq_processing_started",
        )
        self._consuming = True
        self._start_consume_loop()
        self._emit_event(
            EventType.DLQ_CONSUMER_LEADERSHIP_ACQUIRED,
            data={"resource_name": self._resource_name},
        )

    def _on_lose_leader(self) -> None:
        """리더십을 잃었을 때 DLQ 처리 중단."""
        logger.info(
            "dlq_consumer.leadership_lost_dlq_processing",
        )
        self._consuming = False
        self._emit_event(
            EventType.DLQ_CONSUMER_LEADERSHIP_LOST,
            data={"resource_name": self._resource_name},
        )

    def _start_consume_loop(self) -> None:
        """DLQ 소비 루프 시작 (별도 스레드)."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._consume_thread and self._consume_thread.is_alive():
            return

        self._spawn_consume_thread()
        assert self._consume_thread is not None  # populated by _spawn_consume_thread
        if self._handle is None:
            self._handle = DaemonWorkerHandle(
                thread=self._consume_thread,
                tick_interval_seconds=self._process_interval,
                restart_callback=self._spawn_consume_thread,
            )
            register_daemon_worker(f"DLQConsumer-{self._resource_name}", self._handle)
        else:
            self._handle.thread = self._consume_thread

    def _spawn_consume_thread(self) -> None:
        """Construct + start a fresh consume thread (impl 489 D9)."""
        self._consume_thread = threading.Thread(
            target=self._consume_loop_with_crash_capture,
            name=f"DLQConsumer-{self._resource_name}",
            daemon=True,
        )
        self._consume_thread.start()
        if self._handle is not None:
            self._handle.thread = self._consume_thread

    def _consume_loop_with_crash_capture(self) -> None:
        try:
            self._consume_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def _consume_loop(self) -> None:
        """DLQ 소비 루프."""
        import time as _time

        logger.info("dlq_consumer.consume_loop_started")

        while self._consuming and not self._stop_event.is_set():
            iter_start = _time.monotonic()
            try:
                # Lease 유효성 확인 (Self-Fencing)
                if not self._elector.is_leader():
                    logger.warning("dlq_consumer.leadership_check_failed")
                    break

                # DLQ 처리
                processed = self._process_dlq_batch()

                if processed > 0:
                    logger.info(
                        "dlq_consumer.dlq_entries_processed",
                        processed=processed,
                    )

                if self._handle is not None:
                    self._handle.observe_iteration(_time.monotonic() - iter_start)
                    self._handle.heartbeat()

                # 다음 처리까지 대기
                self._stop_event.wait(timeout=self._process_interval)

            except Exception as e:
                logger.exception(
                    "dlq_consumer.consume_loop_error",
                    error=e,
                )
                if self._handle is not None:
                    self._handle.heartbeat()
                self._stop_event.wait(timeout=self._process_interval)

        logger.info("dlq_consumer.consume_loop_stopped")

    def _process_dlq_batch(self) -> int:
        """
        Process a batch of pending DLQ entries.

        Returns:
            Number of entries processed
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
            if dlq_service is None:
                raise RuntimeError("baldur_pro DLQService not registered")

            # Fencing token check (guard against a stale leader).
            self._elector.get_fencing_token()

            # Fetch pending DLQ entries.
            pending_entries = dlq_service.list_pending_entries(
                limit=self._batch_size,
            )

            if not pending_entries:
                return 0

            processed = 0
            for entry in pending_entries:
                # Re-check leadership before each entry.
                if not self._consuming or not self._elector.is_leader():
                    logger.warning("dlq_consumer.leadership_lost_batch_processing")
                    break

                try:
                    # Invoke the replay service.
                    from baldur.interfaces.repositories import ResolutionTrigger
                    from baldur.services import get_replay_service

                    replay_service = get_replay_service()
                    result = replay_service.replay_single(
                        entry.id, trigger=ResolutionTrigger.DLQ_CONSUMER
                    )

                    if result.success:
                        processed += 1
                    else:
                        logger.warning(
                            "dlq_consumer.dlq_replay_failed",
                            entry_id=entry.id,
                            result_error=result.error,
                        )

                except Exception as e:
                    logger.exception(
                        "dlq_consumer.dlq_processing_error",
                        entry_id=entry.id,
                        error=e,
                    )

            return processed

        except ImportError:
            # Service unavailable (e.g. test environment).
            logger.debug("dlq_consumer.dlq_service_unavailable_test")
            return 0
        except Exception as e:
            logger.exception(
                "dlq_consumer.batch_processing_error",
                error=e,
            )
            return 0


_coordinator_cache: dict[str, DLQConsumerCoordinator] = {}
_coordinator_lock = threading.Lock()


def get_dlq_consumer_coordinator(
    resource_name: str = DLQ_CONSUMER_RESOURCE,
) -> DLQConsumerCoordinator:
    """
    Return the DLQ Consumer Coordinator singleton (one per resource_name).

    Each ``DLQConsumerCoordinator.__init__`` registers its elector with
    ``register_for_graceful_shutdown``; without caching, every call would
    accumulate a shutdown hook and bind another leader-election lifecycle
    to the same resource.

    Args:
        resource_name: Resource name (leader-election key).

    Returns:
        Cached ``DLQConsumerCoordinator`` for ``resource_name``.
    """
    coordinator = _coordinator_cache.get(resource_name)
    if coordinator is None:
        with _coordinator_lock:
            coordinator = _coordinator_cache.get(resource_name)
            if coordinator is None:
                coordinator = DLQConsumerCoordinator(resource_name=resource_name)
                _coordinator_cache[resource_name] = coordinator
    return coordinator


def reset_dlq_consumer_coordinator() -> None:
    """Clear the DLQ Consumer Coordinator cache (test-only)."""
    with _coordinator_lock:
        _coordinator_cache.clear()
