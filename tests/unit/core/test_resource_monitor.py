"""
Tests for CgroupResourceMonitor.
core/resource_monitor.py의 cgroup 기반 리소스 모니터링에 대한 단위 테스트.
메모리 제한 감지, 사용량 조회, 안전 여유 계산 등을 검증합니다.
"""

from unittest.mock import MagicMock, patch

from baldur.core.resource_monitor import CgroupMemoryMonitor, CgroupResourceMonitor

# =============================================================================
# Helper: Mock Path 객체 생성
# =============================================================================


def _mock_path(exists: bool = True, read_text: str = ""):
    """Windows에서도 안전하게 동작하는 Mock Path 객체를 생성."""
    m = MagicMock()
    m.exists.return_value = exists
    m.read_text.return_value = read_text
    return m


# =============================================================================
# Memory Max Bytes Tests
# =============================================================================


class TestGetMemoryMaxBytes:
    """get_memory_max_bytes 메서드 테스트."""

    def test_cgroup_v2_with_limit(self):
        """Cgroup v2 with memory limit
        cgroup v2에서 메모리 제한을 올바르게 반환하는지 확인. (1GB)
        """
        with patch.object(
            CgroupResourceMonitor,
            "CGROUP_V2_MEMORY_MAX",
            _mock_path(exists=True, read_text="1073741824\n"),
        ):
            result = CgroupResourceMonitor.get_memory_max_bytes()
            assert result == 1073741824  # 1GB

    def test_cgroup_v2_unlimited(self):
        """Cgroup v2 unlimited memory
        cgroup v2에서 'max'(무제한)일 때 None을 반환하는지 확인.
        """
        with patch.object(
            CgroupResourceMonitor,
            "CGROUP_V2_MEMORY_MAX",
            _mock_path(exists=True, read_text="max\n"),
        ):
            result = CgroupResourceMonitor.get_memory_max_bytes()
            assert result is None

    def test_cgroup_v1_with_limit(self):
        """Cgroup v1 with memory limit
        cgroup v1에서 메모리 제한을 올바르게 반환하는지 확인. (512MB)
        """
        with (
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V2_MEMORY_MAX",
                _mock_path(exists=False),
            ),
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V1_MEMORY_LIMIT",
                _mock_path(exists=True, read_text="536870912\n"),
            ),
        ):
            result = CgroupResourceMonitor.get_memory_max_bytes()
            assert result == 536870912  # 512MB

    def test_cgroup_v1_unlimited(self):
        """Cgroup v1 unlimited memory
        cgroup v1에서 매우 큰 값(사실상 무제한)일 때 None을 반환하는지 확인.
        """
        with (
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V2_MEMORY_MAX",
                _mock_path(exists=False),
            ),
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V1_MEMORY_LIMIT",
                _mock_path(exists=True, read_text=f"{2**63}\n"),
            ),
        ):
            result = CgroupResourceMonitor.get_memory_max_bytes()
            assert result is None

    def test_no_cgroup_available(self):
        """No cgroup available
        cgroup이 없을 때 None을 반환하는지 확인.
        """
        with (
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V2_MEMORY_MAX",
                _mock_path(exists=False),
            ),
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V1_MEMORY_LIMIT",
                _mock_path(exists=False),
            ),
        ):
            result = CgroupResourceMonitor.get_memory_max_bytes()
            assert result is None

    def test_exception_handling(self):
        """Exception handling
        예외 발생 시 None을 반환하는지 확인.
        """
        mock_path = MagicMock()
        mock_path.exists.side_effect = Exception("Permission denied")
        with patch.object(
            CgroupResourceMonitor,
            "CGROUP_V2_MEMORY_MAX",
            mock_path,
        ):
            result = CgroupResourceMonitor.get_memory_max_bytes()
            assert result is None


# =============================================================================
# Memory Current Bytes Tests
# =============================================================================


class TestGetMemoryCurrentBytes:
    """get_memory_current_bytes 메서드 테스트."""

    def test_cgroup_v2(self):
        """Cgroup v2 current memory
        cgroup v2에서 현재 사용량을 올바르게 반환하는지 확인.
        """
        with patch.object(
            CgroupResourceMonitor,
            "CGROUP_V2_MEMORY_CURRENT",
            _mock_path(exists=True, read_text="734003200\n"),
        ):
            result = CgroupResourceMonitor.get_memory_current_bytes()
            assert result == 734003200

    def test_cgroup_v1(self):
        """Cgroup v1 current memory
        cgroup v1에서 현재 사용량을 올바르게 반환하는지 확인.
        """
        with (
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V2_MEMORY_CURRENT",
                _mock_path(exists=False),
            ),
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V1_MEMORY_USAGE",
                _mock_path(exists=True, read_text="524288000\n"),
            ),
        ):
            result = CgroupResourceMonitor.get_memory_current_bytes()
            assert result == 524288000

    def test_no_cgroup(self):
        """No cgroup available
        cgroup이 없을 때 None을 반환하는지 확인.
        """
        with (
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V2_MEMORY_CURRENT",
                _mock_path(exists=False),
            ),
            patch.object(
                CgroupResourceMonitor,
                "CGROUP_V1_MEMORY_USAGE",
                _mock_path(exists=False),
            ),
        ):
            result = CgroupResourceMonitor.get_memory_current_bytes()
            assert result is None


# =============================================================================
# Available Memory Tests
# =============================================================================


class TestGetAvailableMemoryBytes:
    """get_available_memory_bytes 메서드 테스트."""

    @patch.object(
        CgroupResourceMonitor, "get_memory_max_bytes", return_value=1073741824
    )
    @patch.object(
        CgroupResourceMonitor, "get_memory_current_bytes", return_value=734003200
    )
    @patch("baldur.core.resource_monitor.get_resource_monitor_settings")
    def test_normal_calculation(self, mock_settings, mock_current, mock_max):
        """Normal available memory calculation
        (max - current) * (1 - safety_margin) 계산이 올바른지 확인.
        max=1GB, current=~700MB, margin=15%
        """
        mock_s = MagicMock()
        mock_s.safety_margin = 0.15
        mock_settings.return_value = mock_s

        result = CgroupResourceMonitor.get_available_memory_bytes()
        expected = int((1073741824 - 734003200) * (1.0 - 0.15))
        assert result == expected

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=None)
    @patch("baldur.core.resource_monitor.get_resource_monitor_settings")
    def test_no_max(self, mock_settings, mock_max):
        """No max memory info
        최대 메모리 정보가 없으면 None을 반환하는지 확인.
        """
        mock_s = MagicMock()
        mock_s.safety_margin = 0.15
        mock_settings.return_value = mock_s

        result = CgroupResourceMonitor.get_available_memory_bytes()
        assert result is None

    @patch.object(
        CgroupResourceMonitor, "get_memory_max_bytes", return_value=1073741824
    )
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=None)
    @patch("baldur.core.resource_monitor.get_resource_monitor_settings")
    def test_no_current(self, mock_settings, mock_current, mock_max):
        """No current memory info
        현재 사용량 정보가 없으면 None을 반환하는지 확인.
        """
        mock_s = MagicMock()
        mock_s.safety_margin = 0.15
        mock_settings.return_value = mock_s

        result = CgroupResourceMonitor.get_available_memory_bytes()
        assert result is None

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=100)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=200)
    @patch("baldur.core.resource_monitor.get_resource_monitor_settings")
    def test_negative_available_returns_zero(
        self, mock_settings, mock_current, mock_max
    ):
        """Negative available returns zero
        사용량이 최대보다 클 때 0을 반환하는지 확인.
        """
        mock_s = MagicMock()
        mock_s.safety_margin = 0.15
        mock_settings.return_value = mock_s

        result = CgroupResourceMonitor.get_available_memory_bytes()
        assert result == 0

    @patch.object(
        CgroupResourceMonitor, "get_memory_max_bytes", return_value=1073741824
    )
    @patch.object(
        CgroupResourceMonitor, "get_memory_current_bytes", return_value=734003200
    )
    def test_custom_safety_margin(self, mock_current, mock_max):
        """Custom safety margin
        커스텀 안전 마진이 올바르게 적용되는지 확인.
        """
        result = CgroupResourceMonitor.get_available_memory_bytes(safety_margin=0.3)
        expected = int((1073741824 - 734003200) * (1.0 - 0.3))
        assert result == expected


# =============================================================================
# Memory Usage Percent Tests
# =============================================================================


class TestGetMemoryUsagePercent:
    """get_memory_usage_percent 메서드 테스트."""

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=1000)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=700)
    def test_normal_usage(self, mock_current, mock_max):
        """Normal usage percent
        사용률이 올바르게 계산되는지 확인. (700/1000 * 100 = 70.0%)
        """
        result = CgroupResourceMonitor.get_memory_usage_percent()
        assert result == 70.0

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=None)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=700)
    def test_no_max(self, mock_current, mock_max):
        """No max memory info
        최대 메모리 정보가 없으면 None을 반환하는지 확인.
        """
        result = CgroupResourceMonitor.get_memory_usage_percent()
        assert result is None

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=0)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=0)
    def test_zero_max(self, mock_current, mock_max):
        """Zero max memory
        최대 메모리가 0일 때 None을 반환하는지 확인 (ZeroDivisionError 방지).
        """
        result = CgroupResourceMonitor.get_memory_usage_percent()
        assert result is None


# =============================================================================
# Memory Constrained Tests
# =============================================================================


class TestIsMemoryConstrained:
    """is_memory_constrained 메서드 테스트."""

    @patch.object(
        CgroupResourceMonitor, "get_memory_max_bytes", return_value=1073741824
    )
    def test_constrained(self, mock_max):
        """Memory constrained
        메모리 제한이 있으면 True를 반환하는지 확인.
        """
        assert CgroupResourceMonitor.is_memory_constrained() is True

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=None)
    def test_not_constrained(self, mock_max):
        """Memory not constrained
        메모리 제한이 없으면 False를 반환하는지 확인.
        """
        assert CgroupResourceMonitor.is_memory_constrained() is False


# =============================================================================
# Check Safe for Exhaustion Tests
# =============================================================================


class TestCheckSafeForExhaustion:
    """check_safe_for_exhaustion 메서드 테스트."""

    @patch.object(
        CgroupResourceMonitor, "get_available_memory_bytes", return_value=500_000_000
    )
    def test_safe_request(self, mock_available):
        """Safe request
        요청된 메모리가 안전 한계 내일 때 True를 반환하는지 확인.
        """
        is_safe, actual = CgroupResourceMonitor.check_safe_for_exhaustion(100_000_000)
        assert is_safe is True
        assert actual == 100_000_000

    @patch.object(
        CgroupResourceMonitor, "get_available_memory_bytes", return_value=100_000_000
    )
    def test_capped_request(self, mock_available):
        """Capped request
        요청된 메모리가 안전 한계를 초과하면 캡이 적용되는지 확인.
        """
        is_safe, actual = CgroupResourceMonitor.check_safe_for_exhaustion(500_000_000)
        assert is_safe is False
        assert actual == 100_000_000  # 안전 한계로 캡핑

    @patch.object(
        CgroupResourceMonitor, "get_available_memory_bytes", return_value=None
    )
    def test_no_cgroup_allows_full(self, mock_available):
        """No cgroup allows full request
        cgroup 감지 불가 시 요청 전체를 허용하는지 확인.
        """
        is_safe, actual = CgroupResourceMonitor.check_safe_for_exhaustion(500_000_000)
        assert is_safe is True
        assert actual == 500_000_000


# =============================================================================
# Backward Compatibility Tests
# =============================================================================


class TestGetMemoryCurrentBytesEdgeCases:
    """get_memory_current_bytes 추가 엣지 케이스 테스트."""

    def test_exception_returns_none(self):
        """Exception returns None
        예외 발생 시 None을 반환하는지 확인.
        """
        mock_path = MagicMock()
        mock_path.exists.side_effect = Exception("Permission denied")
        with patch.object(
            CgroupResourceMonitor,
            "CGROUP_V2_MEMORY_CURRENT",
            mock_path,
        ):
            result = CgroupResourceMonitor.get_memory_current_bytes()
            assert result is None

    def test_read_text_exception(self):
        """Read text exception
        read_text() 예외 시 None을 반환하는지 확인.
        """
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.side_effect = OSError("Failed to read")
        with patch.object(
            CgroupResourceMonitor,
            "CGROUP_V2_MEMORY_CURRENT",
            mock_path,
        ):
            result = CgroupResourceMonitor.get_memory_current_bytes()
            assert result is None


class TestCheckSafeForExhaustionEdgeCases:
    """check_safe_for_exhaustion 추가 엣지 케이스 테스트."""

    @patch.object(
        CgroupResourceMonitor, "get_available_memory_bytes", return_value=100_000_000
    )
    def test_exact_boundary(self, mock_available):
        """Exact boundary request
        요청량이 정확히 가용량과 같을 때 safe=True인지 확인.
        """
        is_safe, actual = CgroupResourceMonitor.check_safe_for_exhaustion(100_000_000)
        assert is_safe is True
        assert actual == 100_000_000

    @patch.object(CgroupResourceMonitor, "get_available_memory_bytes", return_value=0)
    def test_zero_available(self, mock_available):
        """Zero available memory
        가용 메모리가 0일 때 모든 요청이 캡핑되는지 확인.
        """
        is_safe, actual = CgroupResourceMonitor.check_safe_for_exhaustion(1000)
        assert is_safe is False
        assert actual == 0

    @patch.object(
        CgroupResourceMonitor, "get_available_memory_bytes", return_value=500_000_000
    )
    def test_custom_safety_margin(self, mock_available):
        """Custom safety margin in check_safe
        커스텀 safety_margin이 get_available_memory_bytes에 전달되는지 확인.
        """
        is_safe, actual = CgroupResourceMonitor.check_safe_for_exhaustion(
            100_000_000, safety_margin=0.3
        )
        assert is_safe is True
        mock_available.assert_called_once_with(0.3)

    @patch.object(CgroupResourceMonitor, "get_available_memory_bytes", return_value=0)
    def test_zero_request(self, mock_available):
        """Zero request
        요청량이 0일 때 safe=True인지 확인.
        """
        is_safe, actual = CgroupResourceMonitor.check_safe_for_exhaustion(0)
        assert is_safe is True
        assert actual == 0


class TestGetDefaultSafetyMargin:
    """_get_default_safety_margin() 테스트."""

    @patch("baldur.core.resource_monitor.get_resource_monitor_settings")
    def test_returns_settings_value(self, mock_settings):
        """Returns value from settings
        ResourceMonitorSettings에서 safety_margin 값을 올바르게 가져오는지 확인.
        """
        mock_s = MagicMock()
        mock_s.safety_margin = 0.20
        mock_settings.return_value = mock_s

        result = CgroupResourceMonitor._get_default_safety_margin()
        assert result == 0.20
        mock_settings.assert_called_once()


class TestGetMemoryUsagePercentEdgeCases:
    """get_memory_usage_percent 추가 엣지 케이스 테스트."""

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=1000)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=None)
    def test_no_current(self, mock_current, mock_max):
        """No current memory returns None
        현재 사용량이 None이면 None을 반환하는지 확인.
        """
        result = CgroupResourceMonitor.get_memory_usage_percent()
        assert result is None

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=1000)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=1000)
    def test_full_usage(self, mock_current, mock_max):
        """Full memory usage (100%)
        메모리가 100% 사용 중일 때 100.0을 반환하는지 확인.
        """
        result = CgroupResourceMonitor.get_memory_usage_percent()
        assert result == 100.0

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=1000)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=0)
    def test_zero_usage(self, mock_current, mock_max):
        """Zero memory usage
        메모리가 0% 사용 중일 때 0.0을 반환하는지 확인.
        """
        result = CgroupResourceMonitor.get_memory_usage_percent()
        assert result == 0.0


class TestAvailableMemoryEdgeCases:
    """get_available_memory_bytes 추가 엣지 케이스 테스트."""

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=1000)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=1000)
    def test_zero_available(self, mock_current, mock_max):
        """Zero available memory
        max == current일 때 0을 반환하는지 확인.
        """
        result = CgroupResourceMonitor.get_available_memory_bytes(safety_margin=0.0)
        assert result == 0

    @patch.object(CgroupResourceMonitor, "get_memory_max_bytes", return_value=1000)
    @patch.object(CgroupResourceMonitor, "get_memory_current_bytes", return_value=0)
    def test_full_available_no_margin(self, mock_current, mock_max):
        """Full available no margin
        margin=0일 때 max 전체가 가용한지 확인.
        """
        result = CgroupResourceMonitor.get_available_memory_bytes(safety_margin=0.0)
        assert result == 1000


class TestBackwardCompatibility:
    """이름 호환성 테스트."""

    def test_alias(self):
        """CgroupMemoryMonitor alias
        CgroupMemoryMonitor가 CgroupResourceMonitor의 별칭인지 확인.
        """
        assert CgroupMemoryMonitor is CgroupResourceMonitor
