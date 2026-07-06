"""
BasePostmortemRecordAdmin 단위 테스트.

baldur 패키지의 BasePostmortemRecordAdmin 클래스 테스트입니다.
Django Admin 기본 클래스의 설정 및 메서드를 테스트합니다.
"""

import os
from unittest.mock import MagicMock

import pytest

# Django 설정 (테스트 환경에서 필요)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

# 이제 Django가 설정되었으므로 admin 모듈 import 가능
from baldur.adapters.django.admin import BasePostmortemRecordAdmin

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def admin_class():
    """BasePostmortemRecordAdmin 클래스를 반환합니다."""
    return BasePostmortemRecordAdmin


@pytest.fixture
def admin_instance():
    """BasePostmortemRecordAdmin 인스턴스를 반환합니다."""
    from django.contrib.admin.sites import AdminSite

    from baldur.adapters.django.models import PostmortemRecord

    site = AdminSite()
    return BasePostmortemRecordAdmin(model=PostmortemRecord, admin_site=site)


# =============================================================================
# Display Methods Tests
# =============================================================================


class TestBasePostmortemAdminDurationDisplay:
    """지속 시간 표시 메서드 테스트."""

    def test_duration_display_returns_dash_when_none(self, admin_instance):
        """duration_seconds가 None일 때 '-' 반환."""
        obj = MagicMock()
        obj.duration_seconds = None

        result = admin_instance.duration_display(obj)
        assert result == "-"

    def test_duration_display_seconds_format(self, admin_instance):
        """60초 미만일 때 초 단위 표시."""
        obj = MagicMock()
        obj.duration_seconds = 45

        result = admin_instance.duration_display(obj)
        assert "45" in result
        assert "sec" in result

    def test_duration_display_minutes_format(self, admin_instance):
        """60초~3600초일 때 분 단위 표시."""
        obj = MagicMock()
        obj.duration_seconds = 300  # 5분

        result = admin_instance.duration_display(obj)
        assert "5.0" in result
        assert "min" in result

    def test_duration_display_hours_format(self, admin_instance):
        """3600초 이상일 때 시간 단위 표시."""
        obj = MagicMock()
        obj.duration_seconds = 7200  # 2시간

        result = admin_instance.duration_display(obj)
        assert "2.00" in result
        assert "hr" in result

    def test_duration_display_zero(self, admin_instance):
        """0초일 때 0초 표시."""
        obj = MagicMock()
        obj.duration_seconds = 0

        result = admin_instance.duration_display(obj)
        assert "0" in result
        assert "sec" in result


class TestBasePostmortemAdminAffectedServicesDisplay:
    """영향 서비스 표시 메서드 테스트."""

    def test_affected_services_display_empty_list(self, admin_instance):
        """빈 리스트일 때 '-' 반환."""
        obj = MagicMock()
        obj.affected_services = []

        result = admin_instance.affected_services_display(obj)
        assert result == "-"

    def test_affected_services_display_none(self, admin_instance):
        """None일 때 '-' 반환."""
        obj = MagicMock()
        obj.affected_services = None

        result = admin_instance.affected_services_display(obj)
        assert result == "-"

    def test_affected_services_display_single_service(self, admin_instance):
        """서비스 1개일 때 전체 표시."""
        obj = MagicMock()
        obj.affected_services = ["payment"]

        result = admin_instance.affected_services_display(obj)
        assert result == "payment"

    def test_affected_services_display_three_services(self, admin_instance):
        """서비스 3개일 때 전체 표시."""
        obj = MagicMock()
        obj.affected_services = ["payment", "order", "inventory"]

        result = admin_instance.affected_services_display(obj)
        assert "payment" in result
        assert "order" in result
        assert "inventory" in result

    def test_affected_services_display_more_than_three(self, admin_instance):
        """서비스 4개 이상일 때 3개만 표시 + more."""
        obj = MagicMock()
        obj.affected_services = [
            "payment",
            "order",
            "inventory",
            "notification",
            "user",
        ]

        result = admin_instance.affected_services_display(obj)
        assert "payment" in result
        assert "order" in result
        assert "inventory" in result
        assert "2 more" in result


class TestBasePostmortemAdminSourceDisplay:
    """출처 표시 메서드 테스트."""

    def test_source_display_auto(self, admin_instance):
        """source가 'auto'일 때 🤖 Auto 표시."""
        obj = MagicMock()
        obj.source = "auto"

        result = admin_instance.source_display(obj)
        assert "Auto" in result
        assert "🤖" in result

    def test_source_display_manual(self, admin_instance):
        """source가 'manual'일 때 👤 Manual 표시."""
        obj = MagicMock()
        obj.source = "manual"

        result = admin_instance.source_display(obj)
        assert "Manual" in result
        assert "👤" in result


# =============================================================================
# Permission Methods Tests
# =============================================================================


class TestBasePostmortemAdminPermissions:
    """Admin 권한 메서드 테스트."""

    def test_has_add_permission_always_false(self, admin_instance):
        """수동 추가 항상 비허용."""
        request = MagicMock()

        result = admin_instance.has_add_permission(request)
        assert result is False

    def test_has_change_permission_always_false(self, admin_instance):
        """수정 항상 비허용."""
        request = MagicMock()

        result = admin_instance.has_change_permission(request)
        assert result is False

    def test_has_change_permission_with_obj_always_false(self, admin_instance):
        """객체 지정해도 수정 비허용."""
        request = MagicMock()
        obj = MagicMock()

        result = admin_instance.has_change_permission(request, obj)
        assert result is False

    def test_has_delete_permission_superuser_allowed(self, admin_instance):
        """superuser는 삭제 가능."""
        request = MagicMock()
        request.user.is_superuser = True

        result = admin_instance.has_delete_permission(request)
        assert result is True

    def test_has_delete_permission_regular_user_denied(self, admin_instance):
        """일반 사용자는 삭제 불가."""
        request = MagicMock()
        request.user.is_superuser = False

        result = admin_instance.has_delete_permission(request)
        assert result is False


# =============================================================================
# Configuration Tests
# =============================================================================


class TestBasePostmortemAdminConfiguration:
    """Admin 설정 테스트."""

    def test_list_display_fields(self, admin_class):
        """list_display 필드 확인."""
        expected_fields = [
            "incident_id",
            "started_at",
            "duration_display",
            "affected_services_display",
            "source_display",
            "created_at",
        ]

        assert admin_class.list_display == expected_fields

    def test_list_filter_fields(self, admin_class):
        """list_filter 필드 확인."""
        expected_filters = ["source", "started_at", "created_at"]
        assert admin_class.list_filter == expected_filters

    def test_search_fields(self, admin_class):
        """search_fields 필드 확인."""
        assert "incident_id" in admin_class.search_fields
        assert "affected_services" in admin_class.search_fields

    def test_ordering_descending(self, admin_class):
        """최신순 정렬 확인."""
        assert admin_class.ordering == ["-started_at"]

    def test_date_hierarchy(self, admin_class):
        """date_hierarchy 설정 확인."""
        assert admin_class.date_hierarchy == "started_at"

    def test_readonly_fields_contains_all_model_fields(self, admin_class):
        """모든 주요 필드가 readonly인지 확인."""
        expected_readonly = [
            "id",
            "incident_id",
            "started_at",
            "resolved_at",
            "duration_seconds",
            "affected_services",
            "timeline",
            "auto_actions",
            "recommendations",
            "system_snapshot",
            "created_at",
            "source",
        ]

        assert admin_class.readonly_fields == expected_readonly

    def test_fieldsets_structure(self, admin_class):
        """fieldsets 구조 확인."""
        fieldsets = admin_class.fieldsets

        # 섹션 이름 확인
        section_names = [fs[0] for fs in fieldsets]
        assert "Incident Overview" in section_names
        assert "Timeline" in section_names
        assert "Impact Analysis" in section_names
        assert "Detailed Data" in section_names
        assert "System Snapshot" in section_names
        assert "Metadata" in section_names

    def test_fieldsets_collapse_sections(self, admin_class):
        """접힌 상태로 표시되는 섹션 확인."""
        fieldsets = admin_class.fieldsets

        collapse_sections = [
            fs[0] for fs in fieldsets if "collapse" in fs[1].get("classes", ())
        ]

        assert "Detailed Data" in collapse_sections
        assert "System Snapshot" in collapse_sections
        assert "Metadata" in collapse_sections
