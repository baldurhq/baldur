"""
RBAC-Audit Actor Roles 테스트

구현 사항:
- Actor 클래스에 roles 필드 추가
- set_actor()에 roles 파라미터 추가
- set_actor_from_django_request()에서 RBAC 역할 추출
- AuditEntry에 actor_roles 필드 추가 및 자동 채우기
"""

from unittest.mock import MagicMock

from baldur.context.actor_context import (
    ANONYMOUS_ACTOR,
    RBAC_ROLE_PRIORITY,
    SYSTEM_ACTOR,
    Actor,
    ActorContext,
    get_actor_for_celery,
    get_audit_actor_info,
    restore_actor_from_celery,
)
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    _get_default_actor,
)


class TestActorRolesField:
    """Actor 클래스 roles 필드 테스트."""

    def test_actor_has_roles_field(self):
        """Actor 클래스에 roles 필드가 있는지 확인."""
        actor = Actor(actor_id="test@example.com")
        assert hasattr(actor, "roles")
        assert isinstance(actor.roles, list)

    def test_actor_roles_default_empty(self):
        """roles 기본값이 빈 리스트인지 확인."""
        actor = Actor(actor_id="test@example.com")
        assert actor.roles == []

    def test_actor_with_roles(self):
        """roles를 명시적으로 설정할 수 있는지 확인."""
        roles = ["baldur_admin", "baldur_operator"]
        actor = Actor(actor_id="admin@example.com", roles=roles)
        assert actor.roles == roles

    def test_actor_highest_role_admin(self):
        """highest_role 프로퍼티가 가장 높은 권한을 반환하는지 확인."""
        actor = Actor(
            actor_id="admin@example.com",
            roles=["baldur_viewer", "baldur_admin", "baldur_operator"],
        )
        assert actor.highest_role == "baldur_admin"

    def test_actor_highest_role_operator(self):
        """operator가 가장 높은 역할일 때 반환되는지 확인."""
        actor = Actor(
            actor_id="operator@example.com",
            roles=["baldur_viewer", "baldur_operator"],
        )
        assert actor.highest_role == "baldur_operator"

    def test_actor_highest_role_viewer(self):
        """viewer만 있을 때 반환되는지 확인."""
        actor = Actor(
            actor_id="viewer@example.com",
            roles=["baldur_viewer"],
        )
        assert actor.highest_role == "baldur_viewer"

    def test_actor_highest_role_fallback_to_actor_type(self):
        """roles가 비어있으면 actor_type을 반환하는지 확인."""
        actor = Actor(actor_id="user@example.com", actor_type="user")
        assert actor.highest_role == "user"

    def test_actor_to_dict_includes_roles(self):
        """to_dict()에 roles가 포함되는지 확인."""
        roles = ["baldur_admin"]
        actor = Actor(actor_id="admin@example.com", roles=roles)
        result = actor.to_dict()
        assert "roles" in result
        assert result["roles"] == roles

    def test_system_actor_has_empty_roles(self):
        """SYSTEM_ACTOR의 roles가 빈 리스트인지 확인."""
        assert SYSTEM_ACTOR.roles == []

    def test_anonymous_actor_has_empty_roles(self):
        """ANONYMOUS_ACTOR의 roles가 빈 리스트인지 확인."""
        assert ANONYMOUS_ACTOR.roles == []


class TestRBACRolePriority:
    """RBAC 역할 우선순위 상수 테스트."""

    def test_role_priority_exists(self):
        """RBAC_ROLE_PRIORITY 상수가 존재하는지 확인."""
        assert RBAC_ROLE_PRIORITY is not None
        assert isinstance(RBAC_ROLE_PRIORITY, dict)

    def test_admin_highest_priority(self):
        """admin이 가장 높은 우선순위를 가지는지 확인."""
        assert (
            RBAC_ROLE_PRIORITY["baldur_admin"] > RBAC_ROLE_PRIORITY["baldur_operator"]
        )
        assert (
            RBAC_ROLE_PRIORITY["baldur_operator"] > RBAC_ROLE_PRIORITY["baldur_viewer"]
        )


class TestSetActorWithRoles:
    """set_actor() roles 파라미터 테스트."""

    def test_set_actor_with_roles(self):
        """set_actor()로 roles를 설정할 수 있는지 확인."""
        roles = ["baldur_admin"]
        with ActorContext.set_actor(
            actor_id="admin@example.com",
            actor_type="baldur_admin",
            roles=roles,
        ) as actor:
            assert actor.roles == roles
            current = ActorContext.get_current()
            assert current.roles == roles

    def test_set_actor_roles_default_empty(self):
        """set_actor()에 roles를 지정하지 않으면 빈 리스트인지 확인."""
        with ActorContext.set_actor(actor_id="user@example.com") as actor:
            assert actor.roles == []

    def test_set_actor_roles_none_becomes_empty_list(self):
        """set_actor()에 roles=None을 전달하면 빈 리스트가 되는지 확인."""
        with ActorContext.set_actor(
            actor_id="user@example.com",
            roles=None,
        ) as actor:
            assert actor.roles == []


class TestExtractBaldurRoles:
    """_extract_baldur_roles() 테스트."""

    def test_extract_roles_from_django_user(self):
        """Django User에서 baldur_ 역할이 추출되는지 확인."""
        # Django User 모킹
        mock_user = MagicMock()
        mock_user.groups.filter.return_value.values_list.return_value = [
            "baldur_admin",
            "baldur_operator",
        ]

        roles = ActorContext._extract_baldur_roles(mock_user)

        assert "baldur_admin" in roles
        assert "baldur_operator" in roles
        mock_user.groups.filter.assert_called_once_with(name__startswith="baldur_")

    def test_extract_roles_no_groups(self):
        """groups 속성이 없으면 빈 리스트를 반환하는지 확인."""
        mock_user = MagicMock(spec=[])  # groups 속성 없음
        roles = ActorContext._extract_baldur_roles(mock_user)
        assert roles == []

    def test_extract_roles_exception_returns_empty(self):
        """예외 발생 시 빈 리스트를 반환하는지 확인."""
        mock_user = MagicMock()
        mock_user.groups.filter.side_effect = Exception("DB Error")

        roles = ActorContext._extract_baldur_roles(mock_user)
        assert roles == []


class TestGetHighestRole:
    """_get_highest_role() 테스트."""

    def test_get_highest_role_admin(self):
        """admin이 가장 높은 역할로 반환되는지 확인."""
        roles = ["baldur_viewer", "baldur_admin"]
        result = ActorContext._get_highest_role(roles)
        assert result == "baldur_admin"

    def test_get_highest_role_operator(self):
        """operator가 가장 높은 역할로 반환되는지 확인."""
        roles = ["baldur_viewer", "baldur_operator"]
        result = ActorContext._get_highest_role(roles)
        assert result == "baldur_operator"

    def test_get_highest_role_empty_returns_user(self):
        """빈 리스트일 때 'user'를 반환하는지 확인."""
        result = ActorContext._get_highest_role([])
        assert result == "user"

    def test_get_highest_role_unknown_role(self):
        """알 수 없는 역할만 있을 때 해당 역할을 반환하는지 확인."""
        roles = ["unknown_role"]
        result = ActorContext._get_highest_role(roles)
        # 우선순위 0으로 처리되어 해당 역할이 반환됨
        assert result == "unknown_role"


class TestSetActorFromDjangoRequest:
    """set_actor_from_django_request() RBAC 역할 추출 테스트."""

    def test_extracts_roles_from_authenticated_user(self):
        """인증된 사용자에서 RBAC 역할이 추출되는지 확인."""
        # Django Request 모킹
        mock_request = MagicMock()
        mock_request.user.is_authenticated = True
        mock_request.user.email = "admin@example.com"
        mock_request.user.groups.filter.return_value.values_list.return_value = [
            "baldur_admin"
        ]
        mock_request.path = "/api/test/"
        mock_request.method = "POST"
        mock_request.META = {"HTTP_USER_AGENT": "TestAgent"}
        mock_request.session.session_key = None

        with ActorContext.set_actor_from_django_request(mock_request) as actor:
            assert "baldur_admin" in actor.roles
            assert actor.actor_type == "baldur_admin"  # 가장 높은 역할

    def test_actor_type_is_highest_role(self):
        """actor_type이 가장 높은 RBAC 역할로 설정되는지 확인."""
        mock_request = MagicMock()
        mock_request.user.is_authenticated = True
        mock_request.user.email = "operator@example.com"
        mock_request.user.groups.filter.return_value.values_list.return_value = [
            "baldur_viewer",
            "baldur_operator",
        ]
        mock_request.path = "/api/test/"
        mock_request.method = "GET"
        mock_request.META = {}
        mock_request.session.session_key = None

        with ActorContext.set_actor_from_django_request(mock_request) as actor:
            assert actor.actor_type == "baldur_operator"

    def test_anonymous_user_has_empty_roles(self):
        """인증되지 않은 사용자는 빈 roles를 가지는지 확인."""
        mock_request = MagicMock()
        mock_request.user.is_authenticated = False
        mock_request.path = "/api/test/"
        mock_request.method = "GET"
        mock_request.META = {}
        mock_request.session.session_key = None

        with ActorContext.set_actor_from_django_request(mock_request) as actor:
            assert actor.roles == []
            assert actor.actor_type == "anonymous"

    def test_user_without_baldur_roles(self):
        """baldur_ 역할이 없는 사용자는 actor_type='user'인지 확인."""
        mock_request = MagicMock()
        mock_request.user.is_authenticated = True
        mock_request.user.email = "regular@example.com"
        mock_request.user.groups.filter.return_value.values_list.return_value = []
        mock_request.path = "/api/test/"
        mock_request.method = "GET"
        mock_request.META = {}
        mock_request.session.session_key = None

        with ActorContext.set_actor_from_django_request(mock_request) as actor:
            assert actor.roles == []
            assert actor.actor_type == "user"


class TestAuditEntryActorRoles:
    """AuditEntry actor_roles 필드 테스트."""

    def test_audit_entry_has_actor_roles_field(self):
        """AuditEntry에 actor_roles 필드가 있는지 확인."""
        entry = AuditEntry(action=AuditAction.CB_FORCE_OPEN)
        assert hasattr(entry, "actor_roles")
        assert isinstance(entry.actor_roles, list)

    def test_audit_entry_actor_roles_default_empty(self):
        """actor_roles 기본값이 빈 리스트인지 확인."""
        entry = AuditEntry(action=AuditAction.CB_FORCE_OPEN)
        # ActorContext가 설정되어 있지 않으면 빈 리스트
        assert entry.actor_roles == []

    def test_audit_entry_explicit_actor_roles(self):
        """actor_roles를 명시적으로 설정할 수 있는지 확인."""
        roles = ["baldur_admin"]
        entry = AuditEntry(
            action=AuditAction.CB_FORCE_OPEN,
            actor_id="admin@example.com",
            actor_type="baldur_admin",
            actor_roles=roles,
        )
        assert entry.actor_roles == roles

    def test_audit_entry_auto_fills_roles_from_context(self):
        """ActorContext에서 actor_roles가 자동으로 채워지는지 확인."""
        roles = ["baldur_admin", "baldur_operator"]
        with ActorContext.set_actor(
            actor_id="admin@example.com",
            actor_type="baldur_admin",
            roles=roles,
        ):
            entry = AuditEntry(action=AuditAction.CB_FORCE_OPEN)
            assert entry.actor_id == "admin@example.com"
            assert entry.actor_type == "baldur_admin"
            assert entry.actor_roles == roles

    def test_audit_entry_to_dict_includes_actor_roles(self):
        """to_dict()에 actor_roles가 포함되는지 확인."""
        roles = ["baldur_admin"]
        entry = AuditEntry(
            action=AuditAction.CB_FORCE_OPEN,
            actor_roles=roles,
        )
        result = entry.to_dict()
        assert "actor_roles" in result
        assert result["actor_roles"] == roles


class TestGetDefaultActor:
    """_get_default_actor() 함수 테스트."""

    def test_returns_tuple_with_three_elements(self):
        """3개 요소 튜플을 반환하는지 확인 (actor_id, actor_type, roles)."""
        result = _get_default_actor()
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_returns_roles_from_context(self):
        """ActorContext에서 roles를 가져오는지 확인."""
        roles = ["baldur_admin"]
        with ActorContext.set_actor(
            actor_id="admin@example.com",
            actor_type="baldur_admin",
            roles=roles,
        ):
            actor_id, actor_type, actor_roles = _get_default_actor()
            assert actor_id == "admin@example.com"
            assert actor_type == "baldur_admin"
            assert actor_roles == roles

    def test_returns_empty_roles_when_no_context(self):
        """ActorContext가 없을 때 빈 roles를 반환하는지 확인."""
        # 컨텍스트 외부에서 호출
        actor_id, actor_type, roles = _get_default_actor()
        # SYSTEM_ACTOR가 반환됨 (roles=[])
        assert roles == []


class TestCeleryActorRoles:
    """Celery Task에서 roles 전달 테스트."""

    def test_get_actor_for_celery_includes_roles(self):
        """get_actor_for_celery()가 roles를 포함하는지 확인."""
        roles = ["baldur_admin"]
        with ActorContext.set_actor(
            actor_id="admin@example.com",
            actor_type="baldur_admin",
            roles=roles,
        ):
            actor_info = get_actor_for_celery()
            assert "roles" in actor_info
            assert actor_info["roles"] == roles

    def test_restore_actor_from_celery_restores_roles(self):
        """restore_actor_from_celery()가 roles를 복원하는지 확인."""
        actor_info = {
            "actor_id": "admin@example.com",
            "actor_type": "baldur_admin",
            "source": "celery_from_api",
            "roles": ["baldur_admin", "baldur_operator"],
        }

        with restore_actor_from_celery(actor_info) as actor:
            assert actor.roles == ["baldur_admin", "baldur_operator"]
            current = ActorContext.get_current()
            assert current.roles == actor_info["roles"]

    def test_restore_actor_from_celery_empty_roles(self):
        """actor_info에 roles가 없으면 빈 리스트인지 확인."""
        actor_info = {
            "actor_id": "user@example.com",
            "actor_type": "user",
        }

        with restore_actor_from_celery(actor_info) as actor:
            assert actor.roles == []


class TestGetAuditActorInfo:
    """get_audit_actor_info() 함수 테스트."""

    def test_includes_actor_roles(self):
        """get_audit_actor_info()가 actor_roles를 포함하는지 확인."""
        roles = ["baldur_admin"]
        with ActorContext.set_actor(
            actor_id="admin@example.com",
            actor_type="baldur_admin",
            roles=roles,
        ):
            info = get_audit_actor_info()
            assert "actor_roles" in info
            assert info["actor_roles"] == roles

    def test_empty_roles_when_no_roles(self):
        """roles가 없으면 빈 리스트인지 확인."""
        with ActorContext.set_actor(actor_id="user@example.com"):
            info = get_audit_actor_info()
            assert info["actor_roles"] == []
