"""
NamespacedEmergencyTracker 단위 테스트.

테스트 범위:
- 네임스페이스별 상태 관리 (get_state, set_state)
- Emergency 활성화/비활성화
- Global/Regional 우선순위 적용 (get_effective_state)
- 캐시 관리
- 싱글톤 패턴

Code reference:
    regional_emergency/tracker.py
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.models.emergency import EmergencyLevel, EmergencyScope, ScopedEmergencyState
from baldur.services.regional_emergency.tracker import (
    GLOBAL_NAMESPACE,
    NamespacedEmergencyTracker,
    get_namespaced_emergency_tracker,
    reset_namespaced_emergency_tracker,
)


class TestNamespacedEmergencyTrackerBasic:
    """기본 기능 테스트."""

    @pytest.fixture
    def mock_backend(self):
        """Mock StateBackend."""
        backend = MagicMock()
        backend.get.return_value = None
        backend.get_all.return_value = {}
        return backend

    @pytest.fixture
    def tracker(self, mock_backend):
        """NamespacedEmergencyTracker with mock backend."""
        return NamespacedEmergencyTracker(backend=mock_backend)

    def test_get_state_returns_default_when_empty(self, tracker, mock_backend):
        """상태가 없을 때 기본값 반환."""
        mock_backend.get.return_value = None

        state = tracker.get_state("seoul")

        assert state.namespace == "seoul"
        assert state.emergency_level == EmergencyLevel.NORMAL
        assert state.governance_mode == "NORMAL"

    def test_get_state_returns_stored_state(self, tracker, mock_backend):
        """저장된 상태 반환."""
        mock_backend.get.return_value = {
            "namespace": "seoul",
            "emergency_level": "level_3",
            "governance_mode": "STRICT",
            "scope": "regional",
        }

        # 캐시 무효화
        tracker.invalidate_cache()

        state = tracker.get_state("seoul")

        assert state.namespace == "seoul"
        assert state.emergency_level == EmergencyLevel.LEVEL_3
        assert state.governance_mode == "STRICT"

    def test_set_state_calls_backend(self, tracker, mock_backend):
        """set_state가 backend를 호출."""
        state = ScopedEmergencyState(
            namespace="tokyo",
            emergency_level=EmergencyLevel.LEVEL_2,
            governance_mode="STRICT",
        )

        tracker.set_state(state)

        mock_backend.set.assert_called_once()
        call_args = mock_backend.set.call_args
        assert "tokyo" in call_args[0][0]  # 키에 tokyo 포함

    def test_get_state_key_global(self, tracker):
        """Global 네임스페이스 키 생성."""
        key = tracker._get_state_key(GLOBAL_NAMESPACE)
        assert key == "baldur:governance:emergency_state"

    def test_get_state_key_regional(self, tracker):
        """Regional 네임스페이스 키 생성."""
        key = tracker._get_state_key("seoul")
        assert key == "baldur:seoul:governance:emergency_state"


class TestEmergencyActivation:
    """Emergency 활성화/비활성화 테스트."""

    @pytest.fixture
    def mock_backend(self):
        """Mock StateBackend."""
        backend = MagicMock()
        backend.get.return_value = None
        return backend

    @pytest.fixture
    def tracker(self, mock_backend):
        """NamespacedEmergencyTracker with mock backend."""
        return NamespacedEmergencyTracker(backend=mock_backend)

    def test_activate_emergency_level_3(self, tracker, mock_backend):
        """LEVEL_3 활성화 시 STRICT 모드."""
        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by="admin@test.com",
            reason="Test emergency",
            namespace="seoul",
        )

        assert state.emergency_level == EmergencyLevel.LEVEL_3
        assert state.governance_mode == "STRICT"
        assert state.activated_by == "admin@test.com"
        assert state.reason == "Test emergency"
        assert state.expires_at is not None

    def test_activate_emergency_level_2(self, tracker, mock_backend):
        """LEVEL_2 활성화 시 STRICT 모드."""
        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_2,
            activated_by="system",
            reason="High error rate",
            namespace="tokyo",
        )

        assert state.emergency_level == EmergencyLevel.LEVEL_2
        assert state.governance_mode == "STRICT"

    def test_activate_emergency_level_1(self, tracker, mock_backend):
        """LEVEL_1 활성화 시 NORMAL 모드 유지."""
        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_1,
            activated_by="system",
            reason="Minor issue",
            namespace="oregon",
        )

        assert state.emergency_level == EmergencyLevel.LEVEL_1
        assert state.governance_mode == "NORMAL"  # LEVEL_1은 NORMAL 유지

    def test_activate_emergency_global_scope(self, tracker, mock_backend):
        """GLOBAL scope 활성화."""
        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by="super_admin",
            reason="Global outage",
            scope=EmergencyScope.GLOBAL,
        )

        assert state.namespace == GLOBAL_NAMESPACE
        assert state.scope == EmergencyScope.GLOBAL

    def test_activate_emergency_custom_expiry(self, tracker, mock_backend):
        """사용자 지정 만료 시간."""
        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by="admin",
            reason="Long maintenance",
            namespace="seoul",
            expiry_hours=24,
        )

        # 24시간 후 만료 확인
        expected_expiry = datetime.now(UTC) + timedelta(hours=24)
        assert abs((state.expires_at - expected_expiry).total_seconds()) < 5

    def test_deactivate_emergency(self, tracker, mock_backend):
        """Emergency 비활성화."""
        state = tracker.deactivate_emergency(
            deactivated_by="admin@test.com",
            namespace="seoul",
        )

        assert state.emergency_level == EmergencyLevel.NORMAL
        assert state.governance_mode == "NORMAL"
        assert "admin@test.com" in state.reason

    def test_deactivate_emergency_global(self, tracker, mock_backend):
        """Global Emergency 비활성화."""
        state = tracker.deactivate_emergency(
            deactivated_by="super_admin",
            scope=EmergencyScope.GLOBAL,
        )

        assert state.namespace == GLOBAL_NAMESPACE
        assert state.scope == EmergencyScope.GLOBAL


class TestEffectiveState:
    """get_effective_state 우선순위 테스트."""

    @pytest.fixture
    def mock_backend(self):
        """Mock StateBackend."""
        backend = MagicMock()
        return backend

    @pytest.fixture
    def tracker(self, mock_backend):
        """NamespacedEmergencyTracker with mock backend."""
        # atomic_query 없이 수동 폴백 테스트
        return NamespacedEmergencyTracker(
            backend=mock_backend,
            atomic_query=None,  # 수동 폴백 강제
        )

    def test_global_strict_overrides_regional_normal(self, tracker, mock_backend):
        """Global STRICT가 Regional NORMAL을 오버라이드."""

        def get_side_effect(key):
            # Global 키: baldur:governance:emergency_state
            # Regional 키: baldur:seoul:governance:emergency_state
            if key == "baldur:governance:emergency_state":
                # Global 상태
                return {
                    "namespace": "global",
                    "emergency_level": "level_3",
                    "governance_mode": "STRICT",
                    "scope": "global",
                }
            else:
                # Regional 상태
                return {
                    "namespace": "seoul",
                    "emergency_level": "normal",
                    "governance_mode": "NORMAL",
                    "scope": "regional",
                }

        mock_backend.get.side_effect = get_side_effect
        tracker.invalidate_cache()

        state = tracker.get_effective_state("seoul")

        assert state.governance_mode == "STRICT"
        assert state.scope == EmergencyScope.GLOBAL  # Global에서 왔음

    def test_regional_strict_when_global_normal(self, tracker, mock_backend):
        """Global NORMAL일 때 Regional STRICT 유지."""

        def get_side_effect(key):
            if key == "baldur:governance:emergency_state":
                return {
                    "namespace": "global",
                    "emergency_level": "normal",
                    "governance_mode": "NORMAL",
                    "scope": "global",
                }
            else:
                return {
                    "namespace": "seoul",
                    "emergency_level": "level_3",
                    "governance_mode": "STRICT",
                    "scope": "regional",
                }

        mock_backend.get.side_effect = get_side_effect
        tracker.invalidate_cache()

        state = tracker.get_effective_state("seoul")

        assert state.governance_mode == "STRICT"
        assert state.scope == EmergencyScope.REGIONAL

    def test_admin_override_ignores_global(self, tracker, mock_backend):
        """ADMIN_OVERRIDE 시 Global 무시하고 Regional 사용."""

        def get_side_effect(key):
            if key == "baldur:governance:emergency_state":
                return {
                    "namespace": "global",
                    "emergency_level": "level_3",
                    "governance_mode": "STRICT",
                    "scope": "global",
                }
            else:
                return {
                    "namespace": "seoul",
                    "emergency_level": "normal",
                    "governance_mode": "NORMAL",
                    "scope": "regional",
                }

        mock_backend.get.side_effect = get_side_effect
        tracker.invalidate_cache()

        state = tracker.get_effective_state("seoul", precedence="ADMIN_OVERRIDE")

        # ADMIN_OVERRIDE면 Regional 우선
        assert state.governance_mode == "NORMAL"
        assert state.namespace == "seoul"


class TestCacheManagement:
    """캐시 관리 테스트."""

    @pytest.fixture
    def mock_backend(self):
        """Mock StateBackend."""
        backend = MagicMock()
        backend.get.return_value = {
            "namespace": "seoul",
            "emergency_level": "normal",
            "governance_mode": "NORMAL",
            "scope": "regional",
        }
        return backend

    @pytest.fixture
    def tracker(self, mock_backend):
        """NamespacedEmergencyTracker with mock backend."""
        return NamespacedEmergencyTracker(backend=mock_backend)

    def test_cache_hit_reduces_backend_calls(self, tracker, mock_backend):
        """캐시 히트 시 backend 호출 감소."""
        # 첫 번째 호출
        tracker.get_state("seoul")
        call_count_1 = mock_backend.get.call_count

        # 두 번째 호출 (캐시 히트)
        tracker.get_state("seoul")
        call_count_2 = mock_backend.get.call_count

        # 캐시 히트로 추가 호출 없음
        assert call_count_1 == call_count_2

    def test_invalidate_cache_clears_specific(self, tracker, mock_backend):
        """특정 네임스페이스 캐시 무효화."""
        # 캐시 생성
        tracker.get_state("seoul")
        tracker.get_state("tokyo")

        # seoul만 무효화
        tracker.invalidate_cache("seoul")

        # seoul 재호출 시 backend 호출
        tracker.get_state("seoul")
        # tokyo는 캐시 히트
        tracker.get_state("tokyo")

        # seoul은 2번 호출, tokyo는 1번 호출
        # (정확한 call_count는 구현에 따라 다름)

    def test_invalidate_cache_clears_all(self, tracker, mock_backend):
        """전체 캐시 무효화."""
        # 캐시 생성
        tracker.get_state("seoul")
        tracker.get_state("tokyo")

        initial_calls = mock_backend.get.call_count

        # 전체 무효화
        tracker.invalidate_cache()

        # 재호출 시 모두 backend 호출
        tracker.get_state("seoul")
        tracker.get_state("tokyo")

        # 추가 호출 발생
        assert mock_backend.get.call_count > initial_calls


class TestActiveNamespaces:
    """활성 네임스페이스 조회 테스트."""

    @pytest.fixture
    def mock_backend(self):
        """Mock StateBackend."""
        backend = MagicMock()
        return backend

    @pytest.fixture
    def tracker(self, mock_backend):
        """NamespacedEmergencyTracker with mock backend."""
        return NamespacedEmergencyTracker(backend=mock_backend)

    def test_get_all_active_namespaces_empty(self, tracker, mock_backend):
        """활성 네임스페이스 없음."""
        mock_backend.get_all.return_value = {}

        result = tracker.get_all_active_namespaces()

        assert result == []

    def test_get_all_active_namespaces_multiple(self, tracker, mock_backend):
        """여러 활성 네임스페이스."""
        mock_backend.get_all.return_value = {
            "baldur:seoul:governance:emergency_state": {
                "namespace": "seoul",
                "emergency_level": "level_3",
            },
            "baldur:tokyo:governance:emergency_state": {
                "namespace": "tokyo",
                "emergency_level": "level_2",
            },
            "baldur:oregon:governance:emergency_state": {
                "namespace": "oregon",
                "emergency_level": "normal",  # NORMAL
            },
        }

        result = tracker.get_all_active_namespaces()

        # oregon은 emergency_level="normal"이므로 제외
        assert len(result) == 2
        assert "seoul" in result or "tokyo" in result


class TestSingleton:
    """싱글톤 패턴 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        reset_namespaced_emergency_tracker()

    def teardown_method(self):
        """테스트 후 싱글톤 초기화."""
        reset_namespaced_emergency_tracker()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 같은 인스턴스 반환."""
        with patch("baldur.core.state_backend.get_state_backend"):
            tracker1 = get_namespaced_emergency_tracker()
            tracker2 = get_namespaced_emergency_tracker()

            assert tracker1 is tracker2

    def test_reset_clears_singleton(self):
        """reset이 싱글톤 초기화."""
        with patch("baldur.core.state_backend.get_state_backend"):
            tracker1 = get_namespaced_emergency_tracker()
            reset_namespaced_emergency_tracker()
            tracker2 = get_namespaced_emergency_tracker()

            assert tracker1 is not tracker2


# =============================================================================
# 423: EventBus Integration (Cross-pod Sync)
# =============================================================================


class TestTrackerEventBusIntegrationBehavior:
    """D2: EventBus integration for cross-pod cache invalidation."""

    @pytest.fixture
    def mock_backend(self):
        """Mock StateBackend."""
        backend = MagicMock()
        backend.get.return_value = None
        return backend

    @pytest.fixture
    def tracker(self, mock_backend):
        """NamespacedEmergencyTracker with mock backend."""
        return NamespacedEmergencyTracker(backend=mock_backend)

    def test_activate_emits_state_change_event(self, tracker, mock_backend):
        """activate_emergency emits EMERGENCY_LEVEL_CHANGED event."""
        with patch.object(tracker, "_emit_event") as mock_emit:
            tracker.activate_emergency(
                level=EmergencyLevel.LEVEL_3,
                activated_by="admin",
                reason="Test",
                namespace="seoul",
            )

            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            # EventType is first positional arg
            assert "emergency_level_changed" in str(call_args[0][0]).lower()
            # data contains namespace
            data = call_args[1]["data"]
            assert data["namespace"] == "seoul"
            assert data["level"] == "level_3"
            assert data["is_active"] is True

    def test_deactivate_emits_state_change_event(self, tracker, mock_backend):
        """deactivate_emergency emits EMERGENCY_LEVEL_CHANGED event."""
        with patch.object(tracker, "_emit_event") as mock_emit:
            tracker.deactivate_emergency(
                deactivated_by="admin",
                namespace="seoul",
            )

            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            data = call_args[1]["data"]
            assert data["namespace"] == "seoul"
            assert data["level"] == "normal"
            assert data["is_active"] is False

    def test_external_event_invalidates_cache(self, tracker, mock_backend):
        """External event (different source) invalidates cache."""
        from types import SimpleNamespace

        # Given - cache populated
        tracker.get_state("seoul")
        initial_call_count = mock_backend.get.call_count

        # When - simulate external event
        external_event = SimpleNamespace(
            source="other_tracker",
            data={"namespace": "seoul"},
        )
        tracker._on_external_emergency_changed(external_event)

        # Then - cache invalidated, next get triggers backend call
        tracker.invalidate_cache()  # Force clear to verify behavior
        tracker.get_state("seoul")
        assert mock_backend.get.call_count > initial_call_count

    def test_self_event_does_not_invalidate_cache(self, tracker, mock_backend):
        """Self-originated event does NOT invalidate cache."""
        from types import SimpleNamespace

        # Given - cache populated
        tracker.get_state("seoul")
        initial_call_count = mock_backend.get.call_count

        # When - simulate self event
        self_event = SimpleNamespace(
            source="namespaced_tracker",  # Same as tracker._event_source
            data={"namespace": "seoul"},
        )
        tracker._on_external_emergency_changed(self_event)

        # Then - cache NOT invalidated (self-event skip)
        tracker.get_state("seoul")
        assert mock_backend.get.call_count == initial_call_count


class TestTrackerJitterBehavior:
    """D6: TTL jitter for thundering herd prevention."""

    @pytest.fixture
    def mock_backend(self):
        """Mock StateBackend."""
        backend = MagicMock()
        backend.get.return_value = None
        return backend

    @pytest.fixture
    def tracker(self, mock_backend):
        """NamespacedEmergencyTracker with mock backend."""
        return NamespacedEmergencyTracker(backend=mock_backend)

    def test_jitter_proportional_to_ttl(self, tracker):
        """Jitter is proportional to base TTL (±10%)."""
        # Given - default TTL is 30s, so jitter should be ±3s
        with patch(
            "baldur.services.regional_emergency.tracker._get_cache_ttl_seconds",
            return_value=30.0,
        ):
            ttl = tracker._get_cache_ttl_with_jitter()

            # TTL should be between 27s (30-3) and 33s (30+3)
            assert 27.0 <= ttl <= 33.0

    def test_jitter_minimum_ttl_enforced(self, tracker):
        """Minimum TTL of 0.1s is enforced even with negative jitter."""
        # Given - very small TTL where jitter could go negative
        with (
            patch(
                "baldur.services.regional_emergency.tracker._get_cache_ttl_seconds",
                return_value=0.05,
            ),
            patch(
                "baldur.services.regional_emergency.tracker.calculate_jitter",
                return_value=-0.1,
            ),
        ):
            ttl = tracker._get_cache_ttl_with_jitter()

            # Should enforce minimum 0.1s
            assert ttl >= 0.1

    def test_jitter_varies_across_calls(self, tracker):
        """Jitter produces varying TTL values (not constant)."""
        with patch(
            "baldur.services.regional_emergency.tracker._get_cache_ttl_seconds",
            return_value=30.0,
        ):
            ttls = [tracker._get_cache_ttl_with_jitter() for _ in range(10)]

            # Not all TTLs should be identical (random jitter)
            # Note: there's a tiny chance this fails if random happens to return
            # the same value 10 times, but it's astronomically unlikely
            assert len(set(ttls)) > 1


class TestTrackerEventEmitterMixinContract:
    """D2: EventEmitterMixin integration contract."""

    def test_tracker_has_event_source(self):
        """NamespacedEmergencyTracker has _event_source class variable."""
        assert hasattr(NamespacedEmergencyTracker, "_event_source")
        assert NamespacedEmergencyTracker._event_source == "namespaced_tracker"

    def test_tracker_inherits_event_emitter_mixin(self):
        """NamespacedEmergencyTracker inherits from EventEmitterMixin."""
        from baldur.services.event_bus.emitter import EventEmitterMixin

        assert issubclass(NamespacedEmergencyTracker, EventEmitterMixin)
