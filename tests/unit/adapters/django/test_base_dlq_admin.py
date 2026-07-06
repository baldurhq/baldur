"""
BaseDLQEntryAdmin 단위 테스트.

baldur 패키지의 BaseDLQEntryAdmin 클래스 테스트입니다.
Django Admin 기본 클래스의 설정 및 메서드를 테스트합니다.
"""

import os
from unittest.mock import MagicMock

import pytest

# Django 설정 (테스트 환경에서 필요)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from baldur.adapters.django.admin import BaseDLQEntryAdmin

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def admin_class():
    """BaseDLQEntryAdmin 클래스를 반환합니다."""
    return BaseDLQEntryAdmin


@pytest.fixture
def admin_instance():
    """BaseDLQEntryAdmin 인스턴스를 반환합니다."""
    from django.contrib.admin.sites import AdminSite

    from baldur.adapters.django.models import FailedOperation

    site = AdminSite()
    return BaseDLQEntryAdmin(model=FailedOperation, admin_site=site)


# =============================================================================
# Display Methods Tests
# =============================================================================


class TestBaseDLQAdminStatusDisplay:
    """상태 표시 메서드 테스트."""

    def test_status_display_pending(self, admin_instance):
        """pending 상태일 때 주황색으로 표시."""
        obj = MagicMock()
        obj.status = "pending"
        obj.get_status_display.return_value = "Pending"

        result = admin_instance.status_display(obj)
        assert "orange" in result
        assert "Pending" in result

    def test_status_display_resolved(self, admin_instance):
        """resolved 상태일 때 녹색으로 표시."""
        obj = MagicMock()
        obj.status = "resolved"
        obj.get_status_display.return_value = "Resolved"

        result = admin_instance.status_display(obj)
        assert "green" in result
        assert "Resolved" in result

    def test_status_display_requires_review(self, admin_instance):
        """requires_review 상태일 때 빨간색으로 표시."""
        obj = MagicMock()
        obj.status = "requires_review"
        obj.get_status_display.return_value = "Requires Review"

        result = admin_instance.status_display(obj)
        assert "red" in result
        assert "Requires Review" in result

    def test_status_display_rejected(self, admin_instance):
        """rejected 상태일 때 회색으로 표시."""
        obj = MagicMock()
        obj.status = "rejected"
        obj.get_status_display.return_value = "Rejected"

        result = admin_instance.status_display(obj)
        assert "gray" in result
        assert "Rejected" in result


class TestBaseDLQAdminEntityDisplay:
    """엔티티 표시 메서드 테스트."""

    def test_entity_display_with_data(self, admin_instance):
        """엔티티 정보가 있을 때 표시."""
        obj = MagicMock()
        obj.entity_type = "order"
        obj.entity_id = "12345"

        result = admin_instance.entity_display(obj)
        assert "Order" in result
        assert "12345" in result

    def test_entity_display_no_entity_type(self, admin_instance):
        """엔티티 타입이 없을 때 '-' 반환."""
        obj = MagicMock()
        obj.entity_type = None
        obj.entity_id = "12345"

        result = admin_instance.entity_display(obj)
        assert result == "-"

    def test_entity_display_no_entity_id(self, admin_instance):
        """엔티티 ID가 없을 때 '-' 반환."""
        obj = MagicMock()
        obj.entity_type = "order"
        obj.entity_id = None

        result = admin_instance.entity_display(obj)
        assert result == "-"


class TestBaseDLQAdminUserLink:
    """사용자 링크 메서드 테스트."""

    def test_user_link_no_user(self, admin_instance):
        """사용자가 없을 때 '-' 반환."""
        obj = MagicMock()
        obj.user = None

        result = admin_instance.user_link(obj)
        assert result == "-"

    def test_user_link_with_user(self, admin_instance):
        """사용자가 있을 때 링크 표시."""
        obj = MagicMock()
        obj.user = MagicMock()
        obj.user.id = 1
        obj.user.username = "testuser"

        admin_instance.get_user_admin_url = MagicMock(
            return_value="/admin/auth/user/1/change/"
        )
        result = admin_instance.user_link(obj)
        assert "testuser" in result
        assert "href" in result


# =============================================================================
# Configuration Tests
# =============================================================================


class TestBaseDLQAdminConfiguration:
    """Admin 설정 테스트."""

    def test_list_display_fields(self, admin_class):
        """list_display 필드 확인."""
        expected_fields = [
            "id",
            "domain",
            "failure_type",
            "status_display",
            "entity_display",
            "user_link",
            "retry_count",
            "created_at",
            "resolved_at",
        ]
        assert admin_class.list_display == expected_fields

    def test_list_filter_fields(self, admin_class):
        """list_filter 필드 확인."""
        expected_filters = [
            "domain",
            "status",
            "failure_type",
            "created_at",
            "resolved_at",
        ]
        assert admin_class.list_filter == expected_filters

    def test_search_fields(self, admin_class):
        """search_fields 필드 확인."""
        assert "failure_type" in admin_class.search_fields
        assert "error_message" in admin_class.search_fields
        assert "entity_id" in admin_class.search_fields

    def test_ordering_descending(self, admin_class):
        """최신순 정렬 확인."""
        assert admin_class.ordering == ["-created_at"]

    def test_actions_available(self, admin_class):
        """Admin actions 확인."""
        expected_actions = [
            "replay_selected",
            "mark_as_resolved",
            "mark_as_rejected",
            "mark_as_requires_review",
        ]
        assert admin_class.actions == expected_actions

    def test_fieldsets_structure(self, admin_class):
        """fieldsets 구조 확인."""
        fieldsets = admin_class.fieldsets

        section_names = [fs[0] for fs in fieldsets]
        assert "Classification" in section_names
        assert "References" in section_names
        assert "Error Details" in section_names
        assert "Retry Information" in section_names
        assert "Resolution" in section_names
        assert "Forensic Data" in section_names
        assert "Timestamps" in section_names
