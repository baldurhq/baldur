"""
X-Test Throttle Simulation Views Unit Tests.

Throttle X-Test 시뮬레이션 API 기능 테스트.
"""

import os

# Django settings setup must happen before any Django imports
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from unittest.mock import MagicMock, patch


class TestThrottleEmergencySimulationView:
    """Emergency 레벨 시뮬레이션 API 테스트."""

    def test_emergency_level_0_normal(self):
        """Emergency Level 0 (NORMAL) 시뮬레이션 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleEmergencySimulationView,
        )

        view = ThrottleEmergencySimulationView()

        mock_request = MagicMock()
        mock_request.data = {"level": 0, "service": "default"}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}
        mock_request.user = MagicMock()

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    mock_throttle = MagicMock()
                    mock_throttle.current_limit = 100
                    mock_throttle.get_emergency_level.return_value = 0
                    mock_throttle.is_gradient_frozen.return_value = False
                    mock_throttle.is_full_stop_active.return_value = False
                    mock_throttle.get_recovery_dampening_progress.return_value = {
                        "active": False
                    }
                    mock_get_throttle.return_value = mock_throttle

                    response = view.post(mock_request)

                    assert response.status_code == 200
                    assert response.data["status"] == "success"
                    assert response.data["level"] == 0
                    assert response.data["multiplier"] == 1.0

    def test_emergency_level_2_half_capacity(self):
        """Emergency Level 2 (50% 용량) 시뮬레이션 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleEmergencySimulationView,
        )

        view = ThrottleEmergencySimulationView()

        mock_request = MagicMock()
        mock_request.data = {"level": 2}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    mock_throttle = MagicMock()
                    mock_throttle.current_limit = 50
                    mock_throttle.get_emergency_level.return_value = 2
                    mock_throttle.is_gradient_frozen.return_value = False
                    mock_throttle.is_full_stop_active.return_value = False
                    mock_throttle.get_recovery_dampening_progress.return_value = {}
                    mock_get_throttle.return_value = mock_throttle

                    response = view.post(mock_request)

                    assert response.status_code == 200
                    assert response.data["level"] == 2
                    assert response.data["multiplier"] == 0.5
                    mock_throttle.adjust_for_emergency.assert_called_once_with(2)

    def test_emergency_level_3_full_stop(self):
        """Emergency Level 3 (Full Stop) 시뮬레이션 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleEmergencySimulationView,
        )

        view = ThrottleEmergencySimulationView()

        mock_request = MagicMock()
        mock_request.data = {"level": 3}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    mock_throttle = MagicMock()
                    mock_throttle.current_limit = 0
                    mock_throttle.get_emergency_level.return_value = 3
                    mock_throttle.is_gradient_frozen.return_value = True
                    mock_throttle.is_full_stop_active.return_value = True
                    mock_throttle.get_recovery_dampening_progress.return_value = {}
                    mock_get_throttle.return_value = mock_throttle

                    response = view.post(mock_request)

                    assert response.status_code == 200
                    assert response.data["level"] == 3
                    assert response.data["multiplier"] == 0.0
                    assert response.data["gradient_frozen"] is True

    def test_invalid_level_returns_400(self):
        """잘못된 레벨 값 시 400 반환 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleEmergencySimulationView,
        )

        view = ThrottleEmergencySimulationView()

        mock_request = MagicMock()
        mock_request.data = {"level": 5}  # 유효 범위: 0-3
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            response = view.post(mock_request)

            assert response.status_code == 400
            assert response.data["error"] == "invalid_level"


class TestThrottleCBOpenSimulationView:
    """CB OPEN 시뮬레이션 API 테스트."""

    def test_cb_open_min_limit(self):
        """CB OPEN 시 min_limit 적용 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleCBOpenSimulationView,
        )

        view = ThrottleCBOpenSimulationView()

        mock_request = MagicMock()
        mock_request.data = {"service": "payment-api", "state": "open"}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur.settings.throttle.get_throttle_settings"
                    ) as mock_get_settings:
                        mock_throttle = MagicMock()
                        mock_throttle.current_limit = 100
                        mock_throttle._base_limit_before_emergency = 100
                        mock_get_throttle.return_value = mock_throttle

                        mock_settings = MagicMock()
                        mock_settings.cb_open_limit_percent = 0.0
                        mock_settings.min_limit = 10
                        mock_get_settings.return_value = mock_settings

                        response = view.post(mock_request)

                        assert response.status_code == 200
                        assert response.data["cb_state"] == "open"
                        assert response.data["service"] == "payment-api"

    def test_cb_half_open_50_percent(self):
        """CB HALF_OPEN 시 50% limit 적용 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleCBOpenSimulationView,
        )

        view = ThrottleCBOpenSimulationView()

        mock_request = MagicMock()
        mock_request.data = {"service": "payment-api", "state": "half_open"}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur.settings.throttle.get_throttle_settings"
                    ) as mock_get_settings:
                        mock_throttle = MagicMock()
                        mock_throttle.current_limit = 100
                        mock_throttle._base_limit_before_emergency = 100
                        mock_get_throttle.return_value = mock_throttle

                        mock_settings = MagicMock()
                        mock_settings.cb_half_open_limit_percent = 0.5
                        mock_settings.min_limit = 10
                        mock_get_settings.return_value = mock_settings

                        response = view.post(mock_request)

                        assert response.status_code == 200
                        assert response.data["cb_state"] == "half_open"

    def test_cb_closed_full_recovery(self):
        """CB CLOSED 시 전체 limit 복구 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleCBOpenSimulationView,
        )

        view = ThrottleCBOpenSimulationView()

        mock_request = MagicMock()
        mock_request.data = {"service": "payment-api", "state": "closed"}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur.settings.throttle.get_throttle_settings"
                    ) as mock_get_settings:
                        mock_throttle = MagicMock()
                        mock_throttle.current_limit = 50
                        mock_throttle._base_limit_before_emergency = 100
                        mock_get_throttle.return_value = mock_throttle

                        mock_settings = MagicMock()
                        mock_settings.min_limit = 10
                        mock_get_settings.return_value = mock_settings

                        response = view.post(mock_request)

                        assert response.status_code == 200
                        assert response.data["cb_state"] == "closed"
                        # base_limit (100)으로 복구
                        assert response.data["base_limit"] == 100

    def test_invalid_cb_state_returns_400(self):
        """잘못된 CB 상태 값 시 400 반환 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleCBOpenSimulationView,
        )

        view = ThrottleCBOpenSimulationView()

        mock_request = MagicMock()
        mock_request.data = {"service": "test", "state": "invalid_state"}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            response = view.post(mock_request)

            assert response.status_code == 400
            assert response.data["error"] == "invalid_state"


class TestThrottleRTTDelayInjectionView:
    """RTT 지연 주입 API 테스트."""

    def test_inject_single_rtt_sample(self):
        """단일 RTT 샘플 주입 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleRTTDelayInjectionView,
        )

        view = ThrottleRTTDelayInjectionView()

        mock_request = MagicMock()
        mock_request.data = {"rtt_ms": 150, "count": 1}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur.settings.throttle.get_throttle_settings"
                    ) as mock_get_settings:
                        mock_throttle = MagicMock()
                        mock_throttle.current_limit = 100
                        mock_throttle._gradient_calculator.get_gradient.return_value = (
                            0.05
                        )
                        mock_throttle._gradient_calculator.get_current_rtt.return_value = 150.0
                        mock_get_throttle.return_value = mock_throttle

                        mock_settings = MagicMock()
                        mock_settings.sla_warning_ms = 200
                        mock_settings.sla_critical_ms = 500
                        mock_get_settings.return_value = mock_settings

                        response = view.post(mock_request)

                        assert response.status_code == 200
                        assert response.data["rtt_ms"] == 150
                        assert response.data["samples_injected"] == 1
                        assert response.data["sla_status"] == "normal"
                        mock_throttle.record_response.assert_called_once_with(150.0)

    def test_inject_multiple_rtt_samples(self):
        """여러 RTT 샘플 주입 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleRTTDelayInjectionView,
        )

        view = ThrottleRTTDelayInjectionView()

        mock_request = MagicMock()
        mock_request.data = {"rtt_ms": 300, "count": 5, "interval_ms": 0}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur.settings.throttle.get_throttle_settings"
                    ) as mock_get_settings:
                        mock_throttle = MagicMock()
                        mock_throttle.current_limit = 80
                        mock_throttle._gradient_calculator.get_gradient.return_value = (
                            0.1
                        )
                        mock_throttle._gradient_calculator.get_current_rtt.return_value = 300.0
                        mock_get_throttle.return_value = mock_throttle

                        mock_settings = MagicMock()
                        mock_settings.sla_warning_ms = 200
                        mock_settings.sla_critical_ms = 500
                        mock_get_settings.return_value = mock_settings

                        response = view.post(mock_request)

                        assert response.status_code == 200
                        assert response.data["samples_injected"] == 5
                        # 300ms >= 200ms (warning)
                        assert response.data["sla_status"] == "warning"
                        assert mock_throttle.record_response.call_count == 5

    def test_inject_critical_rtt(self):
        """Critical RTT 주입 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleRTTDelayInjectionView,
        )

        view = ThrottleRTTDelayInjectionView()

        mock_request = MagicMock()
        mock_request.data = {"rtt_ms": 600, "count": 1}
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur.settings.throttle.get_throttle_settings"
                    ) as mock_get_settings:
                        mock_throttle = MagicMock()
                        mock_throttle.current_limit = 50
                        mock_throttle._gradient_calculator.get_gradient.return_value = (
                            0.3
                        )
                        mock_throttle._gradient_calculator.get_current_rtt.return_value = 600.0
                        mock_get_throttle.return_value = mock_throttle

                        mock_settings = MagicMock()
                        mock_settings.sla_warning_ms = 200
                        mock_settings.sla_critical_ms = 500
                        mock_get_settings.return_value = mock_settings

                        response = view.post(mock_request)

                        assert response.status_code == 200
                        # 600ms >= 500ms (critical)
                        assert response.data["sla_status"] == "critical"

    def test_invalid_rtt_returns_400(self):
        """잘못된 RTT 값 시 400 반환 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleRTTDelayInjectionView,
        )

        view = ThrottleRTTDelayInjectionView()

        mock_request = MagicMock()
        mock_request.data = {"rtt_ms": -100}  # 음수
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            response = view.post(mock_request)

            assert response.status_code == 400
            assert response.data["error"] == "invalid_rtt"


class TestThrottleStatusView:
    """Throttle 상태 조회 API 테스트."""

    def test_get_throttle_status(self):
        """Throttle 상태 조회 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleStatusView,
        )

        view = ThrottleStatusView()

        mock_request = MagicMock()
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur.settings.throttle.get_throttle_settings"
                    ) as mock_get_settings:
                        mock_throttle = MagicMock()
                        mock_throttle.current_limit = 100
                        mock_throttle.get_stats.return_value = {
                            "emergency": {"active": False, "level": 0},
                            "recovery": {"dampening_active": False},
                            "adaptive": {"adjustments_up": 5, "adjustments_down": 2},
                            "gradient": {"sample_count": 10},
                        }
                        mock_throttle._gradient_calculator.get_gradient.return_value = (
                            0.02
                        )
                        mock_throttle._gradient_calculator.get_current_rtt.return_value = 120.0
                        mock_get_throttle.return_value = mock_throttle

                        mock_settings = MagicMock()
                        mock_settings.min_limit = 10
                        mock_settings.max_limit = 500
                        mock_settings.initial_limit = 100
                        mock_settings.sla_warning_ms = 200
                        mock_settings.sla_critical_ms = 500
                        mock_settings.recovery_dampening_enabled = True
                        mock_get_settings.return_value = mock_settings

                        response = view.get(mock_request)

                        assert response.status_code == 200
                        assert response.data["status"] == "success"
                        assert "throttle" in response.data
                        assert response.data["throttle"]["current_limit"] == 100


class TestThrottleResetView:
    """Throttle 리셋 API 테스트."""

    def test_reset_throttle(self):
        """Throttle 리셋 테스트."""
        from baldur.api.django.views.xtest.throttle_simulation import (
            ThrottleResetView,
        )

        view = ThrottleResetView()

        mock_request = MagicMock()
        mock_request.headers = {"X-Test-Mode": "chaos-monkey"}

        with patch.object(view, "check_chaos_permission", return_value=None):
            with patch.object(view, "log_xtest_audit"):
                with patch(
                    "baldur_pro.services.throttle.adaptive.get_adaptive_throttle"
                ) as mock_get_throttle:
                    with patch(
                        "baldur_pro.services.throttle.adaptive.reset_adaptive_throttle"
                    ) as mock_reset:
                        # 리셋 전 상태
                        mock_throttle_before = MagicMock()
                        mock_throttle_before.current_limit = 50
                        mock_throttle_before.get_emergency_level.return_value = 2

                        # 리셋 후 상태
                        mock_throttle_after = MagicMock()
                        mock_throttle_after.current_limit = 100

                        mock_get_throttle.side_effect = [
                            mock_throttle_before,
                            mock_throttle_after,
                        ]

                        response = view.post(mock_request)

                        assert response.status_code == 200
                        assert response.data["action"] == "throttle_reset"
                        assert response.data["previous_limit"] == 50
                        assert response.data["new_limit"] == 100
                        assert response.data["previous_level"] == 2
                        mock_reset.assert_called_once()
