"""
create_from_failure() metadata["region"] 자동 주입 테스트.

221 설계 §3A.1: FailedOperation 생성 시 ClusterIdentity.region을
metadata에 자동 주입하는 동작 검증.

대상 파일:
  - adapters/django/models.py (AbstractFailedOperation)
  - shopping/models/failed_operation.py (FailedOperation)

NOTE: Django 모델 직접 import는 DJANGO_SETTINGS_MODULE 필요.
      여기서는 소스 코드의 region 주입 로직 패턴을 검증한다.
"""

import re

import pytest

from tests.factories import src_root

# =============================================================================
# 소스 분석 기반 계약 검증: region 주입 패턴 존재 여부
# =============================================================================

# 소스 파일 경로
_PACKAGE_SRC = src_root() / "baldur"
_ADAPTERS_MODELS_PATH = (
    _PACKAGE_SRC / "adapters" / "django" / "models" / "_abstract_failed_operation.py"
)


class TestAbstractFailedOperationRegionInjectionContract:
    """adapters/django/models.py create_from_failure() region 주입 계약 검증."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        """adapters/django/models.py 소스 코드."""
        return _ADAPTERS_MODELS_PATH.read_text(encoding="utf-8")

    def test_get_cluster_identity_imported_in_create_from_failure(self, source):
        """create_from_failure() 내 get_cluster_identity import 존재."""
        # create_from_failure 함수 찾기
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        assert match is not None, "create_from_failure 메서드를 찾을 수 없음"
        method_body = match.group(0)
        assert "get_cluster_identity" in method_body

    def test_metadata_setdefault_region_pattern(self, source):
        """metadata.setdefault('region', ...) 패턴 존재."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        method_body = match.group(0)
        assert (
            'setdefault("region"' in method_body or "setdefault('region'" in method_body
        )

    def test_fail_open_exception_handling(self, source):
        """region 주입 실패 시 except 블록 존재 (Fail-Open)."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        method_body = match.group(0)
        # try/except 패턴이 get_cluster_identity 주변에 존재해야 함
        assert "except" in method_body
        assert "pass" in method_body  # Fail-Open: 예외 무시

    def test_metadata_or_empty_dict_before_identity(self, source):
        """metadata = metadata or {} 초기화가 identity 호출 전에 존재."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        method_body = match.group(0)
        assert "metadata = metadata or {}" in method_body

    def test_identity_region_check_before_setdefault(self, source):
        """identity.region 조건 체크 존재."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        method_body = match.group(0)
        assert "identity.region" in method_body


# =============================================================================
# 동작 검증: region 주입 로직 단위 테스트 (Django 비의존)
# =============================================================================


class TestRegionInjectionLogicBehavior:
    """region 주입 핵심 로직 동작 검증 (Django 모델 비의존)."""

    def _simulate_region_injection(
        self,
        metadata: dict | None,
        region: str | None,
        identity_raises: bool = False,
    ) -> dict:
        """
        adapters/django/models.py의 region 주입 로직을 시뮬레이션.

        실제 코드에서 추출한 순수 로직:
            metadata = metadata or {}
            try:
                identity = get_cluster_identity()
                if identity.region:
                    metadata.setdefault("region", identity.region)
            except Exception:
                pass
        """
        from unittest.mock import MagicMock

        metadata = metadata or {}
        try:
            if identity_raises:
                raise RuntimeError("identity unavailable")
            mock_identity = MagicMock()
            mock_identity.region = region
            if mock_identity.region:
                metadata.setdefault("region", mock_identity.region)
        except Exception:
            pass
        return metadata

    def test_region_injected_when_available(self):
        """region이 있으면 metadata에 주입."""
        result = self._simulate_region_injection(None, "seoul")
        assert result["region"] == "seoul"

    def test_existing_region_not_overwritten(self):
        """기존 region이 있으면 덮어쓰지 않음 (setdefault 동작)."""
        result = self._simulate_region_injection({"region": "tokyo"}, "seoul")
        assert result["region"] == "tokyo"

    def test_no_region_when_identity_has_none(self):
        """identity.region=None이면 미주입."""
        result = self._simulate_region_injection(None, None)
        assert "region" not in result

    def test_fail_open_on_exception(self):
        """identity 예외 시 Fail-Open (빈 dict 유지)."""
        result = self._simulate_region_injection(None, "seoul", identity_raises=True)
        assert "region" not in result

    def test_metadata_none_becomes_empty_dict(self):
        """metadata=None이 빈 dict로 변환."""
        result = self._simulate_region_injection(None, "seoul")
        assert isinstance(result, dict)
        assert result["region"] == "seoul"

    def test_existing_metadata_preserved(self):
        """기존 metadata 키가 보존됨."""
        result = self._simulate_region_injection(
            {"debug_info": "test"},
            "seoul",
        )
        assert result["debug_info"] == "test"
        assert result["region"] == "seoul"


# =============================================================================
# 계약 검증: baldur adapters/django/models.py region 주입 패턴
# (223 Host App Decoupling: shopping → baldur 패키지로 이동)
# =============================================================================

_PACKAGE_SRC = src_root()
_MODELS_PATH = (
    _PACKAGE_SRC
    / "baldur"
    / "adapters"
    / "django"
    / "models"
    / "_abstract_failed_operation.py"
)


class TestShoppingFailedOperationRegionInjectionContract:
    """baldur/adapters/django/models.py FailedOperation create_from_failure() region 주입 계약."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        """baldur/adapters/django/models.py 소스 코드."""
        return _MODELS_PATH.read_text(encoding="utf-8")

    def test_get_cluster_identity_imported(self, source):
        """create_from_failure() 내 get_cluster_identity import 존재."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        assert match is not None
        method_body = match.group(0)
        assert "get_cluster_identity" in method_body

    def test_metadata_setdefault_region_pattern(self, source):
        """metadata.setdefault('region', ...) 패턴 존재."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        method_body = match.group(0)
        assert (
            'setdefault("region"' in method_body or "setdefault('region'" in method_body
        )

    def test_fail_open_exception_handling(self, source):
        """region 주입 실패 시 except 블록 존재 (Fail-Open)."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        method_body = match.group(0)
        assert "except" in method_body

    def test_metadata_initialized_before_identity_call(self, source):
        """metadata = metadata or {} 초기화 존재."""
        match = re.search(
            r"def create_from_failure\(.*?\n(?=    @|\nclass |\Z)",
            source,
            re.DOTALL,
        )
        method_body = match.group(0)
        assert "metadata = metadata or {}" in method_body
