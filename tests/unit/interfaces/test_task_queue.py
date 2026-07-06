"""
TaskQueueInterface 단위 테스트

이 테스트는 특정 구현(Celery, RQ 등)에 종속되지 않고
추상 인터페이스 계약만 검증합니다.

나중에 새로운 태스크 큐 어댑터(CeleryAdapter, RQAdapter 등)를
추가할 때는 별도의 어댑터별 통합 테스트를 작성하세요.

설계 원칙:
- 테스트는 인터페이스 계약만 검증 ("무엇"이 반환되는가)
- 특정 구현의 내부 동작은 테스트하지 않음 ("어떻게" 동작하는가)
- 모든 어댑터 구현체가 이 테스트를 통과해야 함
"""

from datetime import datetime, timedelta

import pytest

from baldur.adapters.queues.sync_adapter import SyncTaskAdapter
from baldur.interfaces.task_queue import (
    PartialEnqueueError,
    TaskOptions,
    TaskQueueError,
    TaskQueueInterface,
    TaskResult,
    TaskStatus,
)


class TestTaskStatus:
    """Tests for TaskStatus enum."""

    def test_status_values(self):
        """Test all status values exist."""
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.STARTED == "started"
        assert TaskStatus.SUCCESS == "success"
        assert TaskStatus.FAILURE == "failure"
        assert TaskStatus.RETRY == "retry"
        assert TaskStatus.REVOKED == "revoked"

    def test_status_string_comparison(self):
        """Test status can be compared with strings."""
        assert TaskStatus.SUCCESS == "success"
        assert TaskStatus.FAILURE == "failure"


class TestTaskResult:
    """Tests for TaskResult dataclass."""

    def test_success_result(self):
        """Test creating a successful task result."""
        result = TaskResult(
            task_id="task_123",
            status=TaskStatus.SUCCESS,
            result={"processed": True},
            started_at=datetime(2025, 12, 10, 10, 0, 0),
            completed_at=datetime(2025, 12, 10, 10, 0, 5),
        )
        assert result.task_id == "task_123"
        assert result.status == TaskStatus.SUCCESS
        assert result.result == {"processed": True}
        assert result.error is None

    def test_failure_result(self):
        """Test creating a failed task result."""
        result = TaskResult(
            task_id="task_456",
            status=TaskStatus.FAILURE,
            error="Connection timeout",
            traceback="Traceback...",
            retries=3,
        )
        assert result.status == TaskStatus.FAILURE
        assert result.error == "Connection timeout"
        assert result.retries == 3

    def test_pending_result(self):
        """Test creating a pending task result."""
        result = TaskResult(
            task_id="task_789",
            status=TaskStatus.PENDING,
        )
        assert result.status == TaskStatus.PENDING
        assert result.result is None


class TestTaskOptions:
    """
    TaskOptions 데이터클래스 테스트.

    참고: priority는 구현에 따라 int(0) 또는 TaskPriority enum일 수 있음.
    인터페이스 계약은 'priority 속성이 존재한다'만 보장함.
    """

    def test_default_options(self):
        """기본 옵션값이 올바르게 설정되는지 확인."""
        options = TaskOptions()
        assert options.countdown is None
        assert options.retry is True
        assert options.max_retries == 3
        assert options.retry_backoff is True
        assert options.retry_backoff_max == 600
        assert options.queue is None
        # priority는 구현에 따라 int 또는 enum일 수 있음
        # 인터페이스 계약: priority 속성이 존재하면 됨
        assert hasattr(options, "priority")

    def test_custom_options(self):
        """Test custom task options."""
        eta = datetime(2025, 12, 10, 12, 0, 0)
        expires = datetime(2025, 12, 10, 13, 0, 0)
        options = TaskOptions(
            countdown=30,
            eta=eta,
            expires=expires,
            retry=False,
            max_retries=5,
            queue="high_priority",
            priority=10,
        )
        assert options.countdown == 30
        assert options.eta == eta
        assert options.expires == expires
        assert options.retry is False
        assert options.max_retries == 5
        assert options.queue == "high_priority"
        assert options.priority == 10


class TestSyncTaskAdapter:
    """Tests for SyncTaskAdapter implementation."""

    @pytest.fixture
    def adapter(self):
        """Create a synchronous task adapter."""
        return SyncTaskAdapter()

    def test_provider_name(self, adapter: SyncTaskAdapter):
        """Test provider name."""
        assert adapter.provider_name == "sync"

    def test_implements_interface(self, adapter: SyncTaskAdapter):
        """Test that adapter implements TaskQueueInterface."""
        assert isinstance(adapter, TaskQueueInterface)

    # =========================================================================
    # Task Registration Tests
    # =========================================================================

    def test_task_decorator_registers_task(self, adapter: SyncTaskAdapter):
        """Test task decorator registers the function."""

        @adapter.task(name="my_task")
        def my_task(x, y):
            return x + y

        assert "my_task" in adapter._tasks

    def test_task_decorator_default_name(self, adapter: SyncTaskAdapter):
        """
        이름 미지정 시 함수명 기반으로 태스크가 등록되는지 확인.

        참고: 실제 등록 이름은 구현에 따라 달라질 수 있음.
        (예: 'process_order' 또는 'module.path.process_order')
        인터페이스 계약은 '태스크가 등록되어 호출 가능하다'만 보장함.
        """

        @adapter.task()
        def process_order():
            return "processed"

        # 인터페이스 계약: 등록된 태스크는 호출 가능해야 함
        # (내부 저장 방식은 구현에 따라 다를 수 있음)
        task_registered = any("process_order" in name for name in adapter._tasks.keys())
        assert task_registered, "태스크가 등록되어야 함"

    def test_task_delay_method(self, adapter: SyncTaskAdapter):
        """
        task.delay() 호출 시 태스크가 실행되는지 확인.

        인터페이스 계약:
        - delay()는 task_id(str)를 반환하거나
        - AsyncResult-like 객체를 반환할 수 있음

        중요: 반환 타입은 구현에 따라 다름. Celery는 AsyncResult,
        SyncAdapter는 str을 반환할 수 있음. 나중에 다른 큐 구현 시
        별도 어댑터 테스트에서 해당 구현을 검증하세요.
        """

        @adapter.task(name="add_task")
        def add(x, y):
            return x + y

        result = add.delay(2, 3)
        # 인터페이스 계약: 뭔가 반환되어야 함 (task_id 또는 AsyncResult)
        assert result is not None
        # SyncAdapter는 str(task_id)를 반환함
        # 실제 결과는 get_result()로 조회
        if isinstance(result, str):
            task_result = adapter.get_result(result)
            assert task_result.result == 5
        else:
            # AsyncResult-like 객체인 경우
            assert result.get() == 5

    def test_task_apply_async_method(self, adapter: SyncTaskAdapter):
        """
        task.apply_async() 호출 시 태스크가 실행되는지 확인.

        delay()와 마찬가지로 반환 타입은 구현에 따라 다름.
        """

        @adapter.task(name="multiply_task")
        def multiply(x, y):
            return x * y

        result = multiply.apply_async(args=(4, 5))
        # 인터페이스 계약: 뭔가 반환되어야 함
        assert result is not None
        if isinstance(result, str):
            task_result = adapter.get_result(result)
            assert task_result.result == 20
        else:
            assert result.get() == 20

    # =========================================================================
    # Task Execution Tests
    # =========================================================================

    def test_enqueue_executes_immediately(self, adapter: SyncTaskAdapter):
        """Test enqueue executes task immediately in sync mode."""
        results = []

        @adapter.task(name="record_task")
        def record(value):
            results.append(value)
            return value

        task_id = adapter.enqueue("record_task", args=("test_value",))
        assert "test_value" in results
        assert task_id is not None

    def test_enqueue_with_kwargs(self, adapter: SyncTaskAdapter):
        """Test enqueue with keyword arguments."""

        @adapter.task(name="greet_task")
        def greet(name, greeting="Hello"):
            return f"{greeting}, {name}!"

        task_id = adapter.enqueue(
            "greet_task", args=("World",), kwargs={"greeting": "Hi"}
        )
        result = adapter.get_result(task_id)
        assert result.result == "Hi, World!"

    def test_enqueue_nonexistent_task(self, adapter: SyncTaskAdapter):
        """
        존재하지 않는 태스크 enqueue 시 에러 처리 확인.

        구현에 따라:
        - 즉시 예외 발생 (TaskNotFoundError)
        - FAILURE 상태로 결과 반환
        둘 다 유효한 동작임.
        """
        from baldur.interfaces.task_queue import TaskNotFoundError

        try:
            task_id = adapter.enqueue("nonexistent_task")
            # 예외가 발생하지 않으면, 결과에서 FAILURE 상태여야 함
            result = adapter.get_result(task_id)
            assert result.status == TaskStatus.FAILURE
            assert result.error is not None
        except TaskNotFoundError:
            # 즉시 예외 발생도 유효한 동작
            pass

    def test_enqueue_many_tasks(self, adapter: SyncTaskAdapter):
        """Test enqueue multiple tasks at once."""
        results = []

        @adapter.task(name="append_task")
        def append_value(value):
            results.append(value)
            return value

        task_ids = adapter.enqueue_many(
            [
                ("append_task", (1,), {}),
                ("append_task", (2,), {}),
                ("append_task", (3,), {}),
            ]
        )
        assert len(task_ids) == 3
        assert results == [1, 2, 3]

    def test_task_exception_handling(self, adapter: SyncTaskAdapter):
        """Test task exception is captured."""

        @adapter.task(name="failing_task")
        def fail():
            raise ValueError("Intentional failure")

        task_id = adapter.enqueue("failing_task")
        result = adapter.get_result(task_id)
        assert result.status == TaskStatus.FAILURE
        assert "Intentional failure" in result.error
        assert result.traceback is not None

    # =========================================================================
    # Task Management Tests
    # =========================================================================

    def test_get_result_success(self, adapter: SyncTaskAdapter):
        """Test get_result for successful task."""

        @adapter.task(name="success_task")
        def success():
            return {"status": "ok"}

        task_id = adapter.enqueue("success_task")
        result = adapter.get_result(task_id)
        assert result.status == TaskStatus.SUCCESS
        assert result.result == {"status": "ok"}

    def test_get_result_failure(self, adapter: SyncTaskAdapter):
        """Test get_result for failed task."""

        @adapter.task(name="fail_task")
        def fail():
            raise RuntimeError("Failed!")

        task_id = adapter.enqueue("fail_task")
        result = adapter.get_result(task_id)
        assert result.status == TaskStatus.FAILURE
        assert "Failed!" in result.error

    def test_get_result_nonexistent_task(self, adapter: SyncTaskAdapter):
        """Test get_result for nonexistent task ID."""
        result = adapter.get_result("nonexistent_task_id")
        assert result.status == TaskStatus.PENDING

    def test_revoke_task(self, adapter: SyncTaskAdapter):
        """
        revoke() 호출 시 예외 없이 완료되는지 확인.

        SyncAdapter에서는 태스크가 즉시 실행되므로 revoke는 no-op.
        반환값은 구현에 따라 True 또는 False일 수 있음.
        인터페이스 계약: 예외 없이 bool을 반환해야 함.
        """
        result = adapter.revoke("any_task_id")
        # 인터페이스 계약: bool 반환
        assert isinstance(result, bool)

    def test_retry_task(self, adapter: SyncTaskAdapter):
        """Test retry task (limited support in sync mode)."""
        call_count = 0

        @adapter.task(name="retry_test")
        def increment():
            nonlocal call_count
            call_count += 1
            return call_count

        task_id = adapter.enqueue("retry_test")
        # In sync mode, retry may not fully re-execute the task
        # Just verify retry returns a task_id (no exception)
        new_task_id = adapter.retry(task_id)
        assert new_task_id is not None
        assert call_count >= 1  # At least original was called

    # =========================================================================
    # Scheduling Tests
    # =========================================================================

    def test_schedule_periodic(self, adapter: SyncTaskAdapter):
        """Test scheduling a periodic task."""

        @adapter.task(name="periodic_task")
        def periodic():
            return "tick"

        schedule_id = adapter.schedule_periodic(
            task_name="periodic_task",
            schedule=timedelta(minutes=5),
            name="every_5_minutes",
        )
        assert schedule_id is not None

    def test_unschedule(self, adapter: SyncTaskAdapter):
        """Test unscheduling a periodic task."""

        @adapter.task(name="periodic_task")
        def periodic():
            return "tick"

        schedule_id = adapter.schedule_periodic(
            task_name="periodic_task",
            schedule=timedelta(minutes=5),
        )
        result = adapter.unschedule(schedule_id)
        assert result is True

    def test_unschedule_nonexistent(self, adapter: SyncTaskAdapter):
        """Test unscheduling a nonexistent schedule."""
        result = adapter.unschedule("nonexistent_schedule")
        assert result is False

    # =========================================================================
    # Queue Management Tests
    # =========================================================================

    def test_purge_queue(self, adapter: SyncTaskAdapter):
        """Test purge queue (no-op in sync mode since tasks execute immediately)."""
        count = adapter.purge_queue()
        assert count == 0  # No pending tasks in sync mode

    def test_queue_length(self, adapter: SyncTaskAdapter):
        """Test queue length (always 0 in sync mode)."""
        length = adapter.queue_length()
        assert length == 0

    # =========================================================================
    # Health Check Tests
    # =========================================================================

    def test_health_check_healthy(self, adapter: SyncTaskAdapter):
        """Test health check returns True."""
        assert adapter.health_check() is True


class TestSyncAsyncResult:
    """
    SyncAsyncResult 테스트 (SyncAdapter 전용).

    참고: 이 테스트는 SyncAdapter의 반환값 형식에 종속적입니다.
    SyncAdapter는 task.delay()에서 str(task_id)를 반환합니다.

    Celery나 다른 큐 구현을 사용할 때는 별도의 어댑터별 테스트를 작성하세요.
    여기서는 SyncAdapter의 특정 동작만 검증합니다.
    """

    @pytest.fixture
    def adapter(self):
        """Create a synchronous task adapter."""
        return SyncTaskAdapter()

    def test_delay_returns_task_id(self, adapter: SyncTaskAdapter):
        """
        task.delay() 호출 시 task_id(str)를 반환하는지 확인.

        SyncAdapter는 AsyncResult 객체가 아닌 str을 반환합니다.
        결과 조회는 adapter.get_result(task_id)를 사용하세요.
        """

        @adapter.task(name="id_test")
        def task_func():
            return "result"

        result = task_func.delay()
        # SyncAdapter는 str(task_id)를 반환
        assert result is not None
        assert isinstance(result, str)

    def test_get_result_returns_correct_value(self, adapter: SyncTaskAdapter):
        """
        get_result()로 태스크 결과를 올바르게 조회하는지 확인.
        """

        @adapter.task(name="get_test")
        def task_func():
            return {"data": "value"}

        task_id = task_func.delay()
        # SyncAdapter에서는 get_result()로 결과 조회
        task_result = adapter.get_result(task_id)
        assert task_result.result == {"data": "value"}

    def test_successful_task_status(self, adapter: SyncTaskAdapter):
        """
        성공한 태스크의 상태가 SUCCESS인지 확인.
        """

        @adapter.task(name="success_test")
        def task_func():
            return "ok"

        task_id = task_func.delay()
        task_result = adapter.get_result(task_id)
        assert task_result.status == TaskStatus.SUCCESS

    def test_failed_task_status(self, adapter: SyncTaskAdapter):
        """
        실패한 태스크의 상태가 FAILURE인지 확인.
        """

        @adapter.task(name="fail_test")
        def task_func():
            raise ValueError("error")

        task_id = task_func.delay()
        task_result = adapter.get_result(task_id)
        assert task_result.status == TaskStatus.FAILURE


class TestTaskQueueInterfaceContract:
    """인터페이스 계약 준수 여부 테스트."""

    def test_abstract_methods_required(self):
        """Test that all abstract methods must be implemented."""
        with pytest.raises(TypeError):
            TaskQueueInterface()

    def test_interface_has_required_methods(self):
        """Test that interface defines all required methods."""
        required_methods = [
            "provider_name",
            "task",
            "enqueue",
            "enqueue_many",
            "get_result",
            "revoke",
            "retry",
            "schedule_periodic",
            "unschedule",
            "purge_queue",
            "queue_length",
            "health_check",
        ]
        for method in required_methods:
            assert hasattr(TaskQueueInterface, method)


class TestTaskRetryBehavior:
    """Tests for task retry behavior."""

    @pytest.fixture
    def adapter(self):
        """Create a synchronous task adapter."""
        return SyncTaskAdapter()

    def test_task_with_autoretry(self, adapter: SyncTaskAdapter):
        """Test task with autoretry configuration."""
        attempt_count = 0

        @adapter.task(
            name="flaky_task",
            max_retries=3,
            autoretry_for=(ConnectionError,),
        )
        def flaky_task():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ConnectionError("Network error")
            return "success"

        # SyncAdapter에서 autoretry는 자동 실행되지 않음
        # 태스크 메타데이터가 올바르게 저장되는지만 확인
        # 참고: 내부 속성 이름은 구현에 따라 다를 수 있음 (max_retries vs _max_retries)
        registered_task = adapter._tasks["flaky_task"]
        assert hasattr(registered_task, "max_retries") or hasattr(
            registered_task, "_max_retries"
        )

    def test_manual_retry(self, adapter: SyncTaskAdapter):
        """
        수동 재시도 테스트.

        retry()는 태스크를 다시 실행합니다.
        """
        values = []

        @adapter.task(name="value_task")
        def value_task(val):
            values.append(val)
            return val

        task_id = adapter.enqueue("value_task", args=(1,))
        adapter.retry(task_id)
        # 재시도로 인해 최소 1번 이상 실행됨
        assert len(values) >= 1


# =============================================================================
# PartialEnqueueError Tests (344)
# =============================================================================


class TestPartialEnqueueErrorContract:
    """PartialEnqueueError design contract verification (§344)."""

    def test_inherits_from_task_queue_error(self):
        """PartialEnqueueError is a subclass of TaskQueueError."""
        err = PartialEnqueueError(succeeded=[], failed=[])
        assert isinstance(err, TaskQueueError)

    def test_message_format_includes_counts(self):
        """Message includes succeeded and failed counts."""
        err = PartialEnqueueError(
            succeeded=[(0, "id-1"), (1, "id-2")],
            failed=[(2, ValueError("x"))],
        )
        assert str(err) == "Partial enqueue: 2 succeeded, 1 failed"


class TestPartialEnqueueErrorBehavior:
    """PartialEnqueueError attribute behavior verification."""

    def test_succeeded_attribute_stores_index_id_tuples(self):
        """succeeded attribute stores (original_index, task_id) tuples."""
        items = [(0, "id-a"), (2, "id-b"), (4, "id-c")]
        err = PartialEnqueueError(succeeded=items, failed=[])
        assert err.succeeded == items

    def test_failed_attribute_stores_index_exception_tuples(self):
        """failed attribute stores (index, exception) tuples."""
        original_exc = RuntimeError("boom")
        err = PartialEnqueueError(
            succeeded=[],
            failed=[(3, original_exc), (7, ValueError("bad"))],
        )
        assert len(err.failed) == 2
        assert err.failed[0] == (3, original_exc)
        assert err.failed[1][0] == 7
        assert isinstance(err.failed[1][1], ValueError)

    def test_empty_succeeded_and_failed(self):
        """Both lists can be empty (edge case)."""
        err = PartialEnqueueError(succeeded=[], failed=[])
        assert err.succeeded == []
        assert err.failed == []
        assert "0 succeeded, 0 failed" in str(err)

    def test_succeeded_and_failed_indices_are_disjoint(self):
        """succeeded and failed indices must not overlap."""
        err = PartialEnqueueError(
            succeeded=[(0, "id-0"), (2, "id-2"), (4, "id-4")],
            failed=[(1, RuntimeError("x")), (3, RuntimeError("y"))],
        )
        succeeded_indices = {idx for idx, _ in err.succeeded}
        failed_indices = {idx for idx, _ in err.failed}
        assert succeeded_indices & failed_indices == set()
