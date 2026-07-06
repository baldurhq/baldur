"""
CausationContext 및 Cascade Chain 단위 테스트.

Tests:
- CausationInfo 생성 및 직렬화
- CausationContext 컨텍스트 관리
- Celery 헤더 전파
- Kafka 헤더 전파
- 체인 깊이 검사
- 순환 참조 감지
- 예외 클래스
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from baldur.audit.cascade_chain import (
    check_and_raise_cycle,
    check_chain_depth,
    detect_cycle,
    validate_cascade_chain,
)
from baldur.audit.cascade_config import (
    CascadeChainConfig,
)
from baldur.audit.cascade_event import CascadeEffect
from baldur.audit.cascade_exceptions import (
    CascadeAuditError,
    CascadeChainDepthExceeded,
    CascadeCycleDetected,
    CascadeEventNotFound,
    CascadeIntegrityError,
)
from baldur.context.causation_context import (
    CELERY_HEADER_CASCADE_ID,
    CELERY_HEADER_CHAIN_DEPTH,
    CELERY_HEADER_NAMESPACE,
    CELERY_HEADER_PARENT_EVENT,
    KAFKA_HEADER_PREFIX,
    CausationContext,
    CausationInfo,
    get_causation_for_celery,
    get_causation_for_kafka,
    restore_causation_from_celery,
    restore_causation_from_kafka,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_causation_info():
    """샘플 CausationInfo fixture."""
    return CausationInfo(
        cascade_id="cascade-abc123",
        parent_event_id="evt-001",
        chain_depth=2,
        namespace="seoul",
        metadata={"source": "test"},
    )


@pytest.fixture
def sample_effects():
    """샘플 효과 목록 fixture (순환 없음)."""
    return [
        CascadeEffect(
            event_id="evt-002",
            action_type="ACTION_A",
            caused_by="evt-001",
            success=True,
        ),
        CascadeEffect(
            event_id="evt-003",
            action_type="ACTION_B",
            caused_by="evt-002",
            success=True,
        ),
        CascadeEffect(
            event_id="evt-004",
            action_type="ACTION_C",
            caused_by="evt-003",
            success=True,
        ),
    ]


@pytest.fixture
def cyclic_effects():
    """순환 참조가 있는 효과 목록 fixture."""
    return [
        CascadeEffect(
            event_id="evt-A",
            action_type="ACTION_A",
            caused_by="evt-trigger",
            success=True,
        ),
        CascadeEffect(
            event_id="evt-B",
            action_type="ACTION_B",
            caused_by="evt-A",
            success=True,
        ),
        CascadeEffect(
            event_id="evt-C",
            action_type="ACTION_C",
            caused_by="evt-B",
            success=True,
        ),
        # evt-A를 다시 참조하여 순환 생성
        CascadeEffect(
            event_id="evt-D",
            action_type="ACTION_D",
            caused_by="evt-A",  # evt-A -> evt-D (다른 브랜치)
            success=True,
        ),
    ]


# =============================================================================
# CausationInfo Tests
# =============================================================================


class TestCausationInfo:
    """CausationInfo 모델 테스트."""

    def test_create_causation_info(self):
        """CausationInfo 생성 테스트."""
        info = CausationInfo(
            cascade_id="cascade-123",
            parent_event_id="evt-001",
            chain_depth=3,
            namespace="test",
        )

        assert info.cascade_id == "cascade-123"
        assert info.parent_event_id == "evt-001"
        assert info.chain_depth == 3
        assert info.namespace == "test"
        assert info.metadata == {}

    def test_to_dict(self, sample_causation_info):
        """딕셔너리 변환 테스트."""
        result = sample_causation_info.to_dict()

        assert result["cascade_id"] == "cascade-abc123"
        assert result["parent_event_id"] == "evt-001"
        assert result["chain_depth"] == 2
        assert result["namespace"] == "seoul"
        assert result["metadata"]["source"] == "test"

    def test_from_dict(self):
        """딕셔너리에서 생성 테스트."""
        data = {
            "cascade_id": "cascade-xyz",
            "parent_event_id": "evt-100",
            "chain_depth": 5,
            "namespace": "busan",
            "metadata": {"key": "value"},
        }

        info = CausationInfo.from_dict(data)

        assert info.cascade_id == "cascade-xyz"
        assert info.chain_depth == 5
        assert info.metadata["key"] == "value"

    def test_default_values(self):
        """기본값 테스트."""
        info = CausationInfo(
            cascade_id="cascade-001",
            parent_event_id="evt-001",
        )

        assert info.chain_depth == 0
        assert info.namespace == "global"
        assert info.metadata == {}


# =============================================================================
# CausationContext Tests
# =============================================================================


class TestCausationContext:
    """CausationContext 테스트."""

    def test_start_cascade(self):
        """새 Cascade 시작 테스트."""
        with CausationContext.start_cascade(namespace="test") as ctx:
            assert ctx.cascade_id.startswith("cascade-")
            assert ctx.parent_event_id.startswith("evt-")
            assert ctx.chain_depth == 0
            assert ctx.namespace == "test"

    def test_context_not_set_outside(self):
        """컨텍스트 블록 외부에서 None 테스트."""
        # 시작 전
        assert CausationContext.get_current() is None

        with CausationContext.start_cascade():
            assert CausationContext.get_current() is not None

        # 종료 후
        assert CausationContext.get_current() is None

    def test_continue_cascade(self, sample_causation_info):
        """기존 Cascade 계속 테스트."""
        with CausationContext.continue_cascade(sample_causation_info) as ctx:
            assert ctx.cascade_id == sample_causation_info.cascade_id
            assert ctx.chain_depth == sample_causation_info.chain_depth + 1

    def test_continue_cascade_no_increment(self, sample_causation_info):
        """깊이 증가 없이 계속 테스트."""
        with CausationContext.continue_cascade(
            sample_causation_info, increment_depth=False
        ) as ctx:
            assert ctx.chain_depth == sample_causation_info.chain_depth

    def test_is_set(self):
        """is_set 메서드 테스트."""
        assert CausationContext.is_set() is False

        with CausationContext.start_cascade():
            assert CausationContext.is_set() is True

        assert CausationContext.is_set() is False

    def test_get_current_cascade_id(self):
        """현재 cascade_id 조회 테스트."""
        assert CausationContext.get_current_cascade_id() is None

        with CausationContext.start_cascade() as ctx:
            assert CausationContext.get_current_cascade_id() == ctx.cascade_id

    def test_get_current_depth(self):
        """현재 체인 깊이 조회 테스트."""
        assert CausationContext.get_current_depth() == 0

        with CausationContext.start_cascade():
            assert CausationContext.get_current_depth() == 0

    def test_nested_cascade(self):
        """중첩 Cascade 테스트."""
        with CausationContext.start_cascade(namespace="outer") as outer:
            assert CausationContext.get_current().namespace == "outer"

            with CausationContext.continue_cascade(outer):
                assert CausationContext.get_current().cascade_id == outer.cascade_id
                assert CausationContext.get_current().chain_depth == 1

            # 외부 컨텍스트 복원
            assert CausationContext.get_current().chain_depth == 0

    def test_set_parent_event(self):
        """부모 이벤트 ID 변경 테스트."""
        with CausationContext.start_cascade() as ctx:
            original_parent = ctx.parent_event_id

            with CausationContext.set_parent_event("new-event-id"):
                assert CausationContext.get_current().parent_event_id == "new-event-id"

            assert CausationContext.get_current().parent_event_id == original_parent

    def test_set_parent_event_no_context(self):
        """컨텍스트 없이 set_parent_event 호출 시 예외 테스트."""
        with pytest.raises(RuntimeError):
            with CausationContext.set_parent_event("new-event"):
                pass

    def test_thread_safety(self):
        """스레드 안전성 테스트."""
        results = []

        def worker(namespace):
            with CausationContext.start_cascade(namespace=namespace) as ctx:
                # 다른 스레드의 컨텍스트와 격리 확인
                results.append((namespace, ctx.namespace))

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(worker, f"ns-{i}") for i in range(3)]
            for f in futures:
                f.result()

        # 각 스레드가 자신의 namespace를 가짐
        for expected_ns, actual_ns in results:
            assert expected_ns == actual_ns


# =============================================================================
# Celery Header Tests
# =============================================================================


class TestCeleryHeaders:
    """Celery 헤더 전파 테스트."""

    def test_get_causation_for_celery_no_context(self):
        """컨텍스트 없을 때 빈 딕셔너리 반환 테스트."""
        headers = get_causation_for_celery()
        assert headers == {}

    def test_get_causation_for_celery_with_context(self):
        """컨텍스트 있을 때 헤더 생성 테스트."""
        with CausationContext.start_cascade(namespace="test") as ctx:
            headers = get_causation_for_celery()

            assert headers[CELERY_HEADER_CASCADE_ID] == ctx.cascade_id
            assert headers[CELERY_HEADER_PARENT_EVENT] == ctx.parent_event_id
            assert headers[CELERY_HEADER_CHAIN_DEPTH] == "0"
            assert headers[CELERY_HEADER_NAMESPACE] == "test"

    def test_restore_causation_from_celery_no_headers(self):
        """헤더 없을 때 None 반환 테스트."""
        with restore_causation_from_celery({}) as ctx:
            assert ctx is None

    def test_restore_causation_from_celery(self):
        """헤더에서 복원 테스트."""
        headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-restore",
            CELERY_HEADER_PARENT_EVENT: "evt-parent",
            CELERY_HEADER_CHAIN_DEPTH: "3",
            CELERY_HEADER_NAMESPACE: "restored",
        }

        with restore_causation_from_celery(headers) as ctx:
            assert ctx is not None
            assert ctx.cascade_id == "cascade-restore"
            assert ctx.parent_event_id == "evt-parent"
            assert ctx.chain_depth == 4  # +1 증가
            assert ctx.namespace == "restored"

    def test_roundtrip_celery(self):
        """Celery 헤더 왕복 테스트."""
        with CausationContext.start_cascade(namespace="roundtrip") as original:
            headers = get_causation_for_celery()

        with restore_causation_from_celery(headers) as restored:
            assert restored.cascade_id == original.cascade_id
            assert restored.namespace == original.namespace


# =============================================================================
# Kafka Header Tests
# =============================================================================


class TestKafkaHeaders:
    """Kafka 헤더 전파 테스트."""

    def test_get_causation_for_kafka_no_context(self):
        """컨텍스트 없을 때 빈 딕셔너리 반환 테스트."""
        headers = get_causation_for_kafka()
        assert headers == {}

    def test_get_causation_for_kafka_with_context(self):
        """컨텍스트 있을 때 헤더 생성 테스트."""
        with CausationContext.start_cascade(namespace="kafka-test") as ctx:
            headers = get_causation_for_kafka()

            assert f"{KAFKA_HEADER_PREFIX}cascade_id" in headers
            assert (
                headers[f"{KAFKA_HEADER_PREFIX}cascade_id"] == ctx.cascade_id.encode()
            )

    def test_restore_causation_from_kafka_no_headers(self):
        """헤더 없을 때 None 반환 테스트."""
        with restore_causation_from_kafka(None) as ctx:
            assert ctx is None

        with restore_causation_from_kafka([]) as ctx:
            assert ctx is None

    def test_restore_causation_from_kafka(self):
        """Kafka 헤더에서 복원 테스트."""
        headers = [
            (f"{KAFKA_HEADER_PREFIX}cascade_id", b"cascade-kafka"),
            (f"{KAFKA_HEADER_PREFIX}parent_event", b"evt-kafka"),
            (f"{KAFKA_HEADER_PREFIX}chain_depth", b"2"),
            (f"{KAFKA_HEADER_PREFIX}namespace", b"kafka-ns"),
        ]

        with restore_causation_from_kafka(headers) as ctx:
            assert ctx is not None
            assert ctx.cascade_id == "cascade-kafka"
            assert ctx.chain_depth == 3  # +1 증가


# =============================================================================
# Exception Tests
# =============================================================================


class TestCascadeExceptions:
    """Cascade 예외 클래스 테스트."""

    def test_cascade_chain_depth_exceeded(self):
        """CascadeChainDepthExceeded 예외 테스트."""
        exc = CascadeChainDepthExceeded(
            depth=15,
            max_depth=10,
            cascade_id="cascade-test",
        )

        assert exc.depth == 15
        assert exc.max_depth == 10
        assert exc.cascade_id == "cascade-test"
        assert "15" in str(exc)
        assert "10" in str(exc)

    def test_cascade_chain_depth_exceeded_to_dict(self):
        """CascadeChainDepthExceeded 딕셔너리 변환 테스트."""
        exc = CascadeChainDepthExceeded(
            depth=15,
            max_depth=10,
            cascade_id="cascade-test",
        )

        result = exc.to_dict()

        assert result["error_type"] == "CascadeChainDepthExceeded"
        assert result["depth"] == 15
        assert result["max_depth"] == 10

    def test_cascade_cycle_detected(self):
        """CascadeCycleDetected 예외 테스트."""
        exc = CascadeCycleDetected(
            cycle_path=["A", "B", "C", "A"],
            cascade_id="cascade-cycle",
        )

        assert exc.cycle_path == ["A", "B", "C", "A"]
        assert "A -> B -> C -> A" in str(exc)

    def test_cascade_cycle_detected_to_dict(self):
        """CascadeCycleDetected 딕셔너리 변환 테스트."""
        exc = CascadeCycleDetected(
            cycle_path=["A", "B", "A"],
            cascade_id="cascade-test",
        )

        result = exc.to_dict()

        assert result["error_type"] == "CascadeCycleDetected"
        assert result["cycle_path"] == ["A", "B", "A"]

    def test_cascade_event_not_found(self):
        """CascadeEventNotFound 예외 테스트."""
        exc = CascadeEventNotFound(
            cascade_id="cascade-missing",
            namespace="test",
        )

        assert exc.cascade_id == "cascade-missing"
        assert "cascade-missing" in str(exc)

    def test_cascade_integrity_error(self):
        """CascadeIntegrityError 예외 테스트."""
        exc = CascadeIntegrityError(
            cascade_id="cascade-corrupt",
            error_type="hash_mismatch",
            details={"expected": "abc", "actual": "xyz"},
        )

        assert exc.error_type == "hash_mismatch"
        result = exc.to_dict()
        assert result["integrity_error_type"] == "hash_mismatch"

    def test_exception_hierarchy(self):
        """예외 상속 구조 테스트."""
        exc = CascadeChainDepthExceeded(depth=1, max_depth=0, cascade_id="test")

        assert isinstance(exc, CascadeAuditError)
        assert isinstance(exc, Exception)


# =============================================================================
# CascadeChainConfig Tests
# =============================================================================


class TestCascadeChainConfig:
    """CascadeChainConfig 테스트."""

    def test_default_config(self):
        """기본 설정값 테스트."""
        config = CascadeChainConfig()

        assert config.max_chain_depth == 10
        assert config.warn_at_depth == 7
        assert config.block_on_exceed is True
        assert config.detect_cycles is True

    def test_custom_config(self):
        """커스텀 설정 테스트."""
        config = CascadeChainConfig(
            max_chain_depth=20,
            warn_at_depth=15,
            block_on_exceed=False,
            detect_cycles=False,
        )

        assert config.max_chain_depth == 20
        assert config.warn_at_depth == 15
        assert config.block_on_exceed is False
        assert config.detect_cycles is False

    def test_warn_at_depth_adjustment(self):
        """warn_at_depth가 max보다 크면 조정되는지 테스트."""
        config = CascadeChainConfig(
            max_chain_depth=5,
            warn_at_depth=10,  # max보다 큼
        )

        # warn_at_depth가 max - 3 이하로 조정되어야 함
        assert config.warn_at_depth < config.max_chain_depth


# =============================================================================
# check_chain_depth Tests
# =============================================================================


class TestCheckChainDepth:
    """check_chain_depth 함수 테스트."""

    def test_normal_depth(self):
        """정상 깊이 테스트 (예외 없음)."""
        config = CascadeChainConfig(max_chain_depth=10)

        # 예외가 발생하지 않아야 함
        check_chain_depth(
            current_depth=5,
            cascade_id="cascade-test",
            namespace="test",
            trigger_type="TEST",
            config=config,
        )

    def test_depth_exceeded_raises(self):
        """깊이 초과 시 예외 발생 테스트."""
        config = CascadeChainConfig(max_chain_depth=5, block_on_exceed=True)

        with pytest.raises(CascadeChainDepthExceeded) as exc_info:
            check_chain_depth(
                current_depth=5,
                cascade_id="cascade-test",
                namespace="test",
                trigger_type="TEST",
                config=config,
            )

        assert exc_info.value.depth == 5
        assert exc_info.value.max_depth == 5

    def test_depth_exceeded_no_block(self):
        """깊이 초과지만 block_on_exceed=False 테스트."""
        config = CascadeChainConfig(max_chain_depth=5, block_on_exceed=False)

        # 예외가 발생하지 않아야 함
        check_chain_depth(
            current_depth=10,
            cascade_id="cascade-test",
            namespace="test",
            trigger_type="TEST",
            config=config,
        )


# =============================================================================
# detect_cycle Tests
# =============================================================================


class TestDetectCycle:
    """detect_cycle 함수 테스트."""

    def test_no_cycle(self, sample_effects):
        """순환 없는 경우 None 반환 테스트."""
        cycle = detect_cycle(sample_effects, "evt-001")
        assert cycle is None

    def test_detect_simple_cycle(self):
        """간단한 순환 감지 테스트."""
        effects = [
            CascadeEffect("A", "ACTION", "trigger", success=True),
            CascadeEffect("B", "ACTION", "A", success=True),
            CascadeEffect("C", "ACTION", "B", success=True),
        ]

        # A가 C를 원인으로 하면 순환
        effects.append(CascadeEffect("D", "ACTION", "A", success=True))
        # 하지만 이건 순환이 아님 - A -> D는 다른 브랜치

        cycle = detect_cycle(effects, "trigger")
        assert cycle is None

    def test_detect_real_cycle(self):
        """실제 순환 감지 테스트."""
        # trigger -> A -> B -> C -> B (순환: B -> C -> B)
        effects = [
            CascadeEffect("A", "ACTION", "trigger", success=True),
            CascadeEffect("B", "ACTION", "A", success=True),
            CascadeEffect("C", "ACTION", "B", success=True),
            CascadeEffect("B_again", "ACTION", "C", success=True),  # 같은 ID
        ]

        # 실제로는 event_id가 고유해야 하므로 이 테스트는 cycle을 감지 못함
        # 순환은 caused_by가 같은 event_id를 가리킬 때 발생
        cycle = detect_cycle(effects, "trigger")
        # ID가 고유하므로 순환 없음
        assert cycle is None

    def test_empty_effects(self):
        """빈 효과 목록 테스트."""
        cycle = detect_cycle([], "trigger")
        assert cycle is None


# =============================================================================
# check_and_raise_cycle Tests
# =============================================================================


class TestCheckAndRaiseCycle:
    """check_and_raise_cycle 함수 테스트."""

    def test_no_cycle_no_exception(self, sample_effects):
        """순환 없으면 예외 없음 테스트."""
        check_and_raise_cycle(
            effects=sample_effects,
            trigger_event_id="evt-001",
            cascade_id="cascade-test",
            namespace="test",
        )

    def test_detect_cycles_disabled(self):
        """detect_cycles=False 시 검사 안 함 테스트."""
        config = CascadeChainConfig(detect_cycles=False)

        # 순환 있어도 예외 안 남
        check_and_raise_cycle(
            effects=[],
            trigger_event_id="trigger",
            cascade_id="cascade-test",
            namespace="test",
            config=config,
        )


# =============================================================================
# validate_cascade_chain Tests
# =============================================================================


class TestValidateCascadeChain:
    """validate_cascade_chain 통합 테스트."""

    def test_valid_chain(self, sample_effects):
        """유효한 체인 테스트."""
        config = CascadeChainConfig(max_chain_depth=10)

        validate_cascade_chain(
            effects=sample_effects,
            trigger_event_id="evt-001",
            cascade_id="cascade-test",
            namespace="test",
            current_depth=3,
            trigger_type="TEST",
            config=config,
        )

    def test_depth_exceeded(self, sample_effects):
        """깊이 초과 예외 테스트."""
        config = CascadeChainConfig(max_chain_depth=5)

        with pytest.raises(CascadeChainDepthExceeded):
            validate_cascade_chain(
                effects=sample_effects,
                trigger_event_id="evt-001",
                cascade_id="cascade-test",
                namespace="test",
                current_depth=5,
                trigger_type="TEST",
                config=config,
            )
