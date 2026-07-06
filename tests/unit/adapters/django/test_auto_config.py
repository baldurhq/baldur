"""Unit tests for baldur.adapters.django.auto_config module (320).

Tests configure_baldur() wrapper, middleware group injection,
toggle filtering, exception handler setup, and prerequisite validation.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from baldur.adapters.django.auto_config import (
    DEFAULT_EARLY_GROUP,
    DEFAULT_POST_AUTH_GROUP,
    DEFAULT_TAIL_GROUP,
    MIDDLEWARE_TOGGLES,
    _filter_by_toggles,
    _find_insert_point,
    _inject_middleware_groups,
    _is_gunicorn_master,
    _setup_exception_handler,
    _validate_prerequisites,
    configure_baldur,
)

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def base_namespace():
    """Minimal consumer namespace with Django core middleware."""
    return {
        "INSTALLED_APPS": ["baldur.adapters.django", "myapp"],
        "MIDDLEWARE": [
            "django_prometheus.middleware.PrometheusBeforeMiddleware",
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
            "django.middleware.clickjacking.XFrameOptionsMiddleware",
            "django_prometheus.middleware.PrometheusAfterMiddleware",
        ],
        "REST_FRAMEWORK": {
            "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
        },
    }


# =========================================================================
# Contract Tests — design doc values
# =========================================================================


class TestMiddlewareGroupContract:
    """320 설계 문서에 정의된 미들웨어 그룹 구성 계약 검증."""

    def test_early_group_has_eight_middlewares(self):
        """early 그룹은 8개 미들웨어로 구성된다 (471: drain-aware + request-tracking 추가)."""
        assert len(DEFAULT_EARLY_GROUP) == 8

    def test_early_group_starts_with_trace_id(self):
        """early 그룹의 첫 번째는 trace_id_middleware이다."""
        assert DEFAULT_EARLY_GROUP[0] == "baldur.audit.trace.trace_id_middleware"

    def test_early_group_drain_aware_after_health_bridge(self):
        """471 D8: DrainAwareMiddleware는 HealthBridgeMiddleware 직후에 위치한다."""
        bridge_idx = DEFAULT_EARLY_GROUP.index(
            "baldur.api.django.middleware.HealthBridgeMiddleware"
        )
        drain_idx = DEFAULT_EARLY_GROUP.index(
            "baldur.api.django.middleware.DrainAwareMiddleware"
        )
        assert drain_idx == bridge_idx + 1

    def test_early_group_request_tracking_after_drain_aware(self):
        """471 D13: RequestTrackingMiddleware는 DrainAwareMiddleware 직후에 위치한다."""
        drain_idx = DEFAULT_EARLY_GROUP.index(
            "baldur.api.django.middleware.DrainAwareMiddleware"
        )
        track_idx = DEFAULT_EARLY_GROUP.index(
            "baldur.api.django.middleware.RequestTrackingMiddleware"
        )
        assert track_idx == drain_idx + 1

    def test_early_group_ends_with_actor_context(self):
        """early 그룹의 마지막은 ActorContextMiddleware이다."""
        assert (
            DEFAULT_EARLY_GROUP[-1]
            == "baldur.api.django.middleware.actor_context.ActorContextMiddleware"
        )

    def test_post_auth_group_has_four_middlewares(self):
        """post_auth 그룹은 4개 미들웨어로 구성된다."""
        assert len(DEFAULT_POST_AUTH_GROUP) == 4

    def test_post_auth_group_starts_with_cell_tagging(self):
        """post_auth 그룹의 첫 번째는 CellTaggingMiddleware이다."""
        assert (
            DEFAULT_POST_AUTH_GROUP[0]
            == "baldur.api.django.cell.middleware.CellTaggingMiddleware"
        )

    def test_tail_group_has_one_middleware(self):
        """tail 그룹은 AuditMiddleware 1개로 구성된다."""
        assert len(DEFAULT_TAIL_GROUP) == 1
        assert (
            DEFAULT_TAIL_GROUP[0]
            == "baldur.api.django.audit_middleware.AuditMiddleware"
        )

    def test_toggle_mapping_has_seven_entries(self):
        """MIDDLEWARE_TOGGLES는 7개 미들웨어에 대한 토글 매핑을 가진다 (471: +2)."""
        assert len(MIDDLEWARE_TOGGLES) == 7

    def test_drain_aware_toggle_setting_name(self):
        """471 D8: DrainAwareMiddleware 토글은 BALDUR_DRAIN_AWARE_MIDDLEWARE_ENABLED."""
        key = "baldur.api.django.middleware.DrainAwareMiddleware"
        assert MIDDLEWARE_TOGGLES[key] == "BALDUR_DRAIN_AWARE_MIDDLEWARE_ENABLED"

    def test_request_tracking_toggle_setting_name(self):
        """471 D13: RequestTrackingMiddleware 토글은 BALDUR_REQUEST_TRACKING_MIDDLEWARE_ENABLED."""
        key = "baldur.api.django.middleware.RequestTrackingMiddleware"
        assert MIDDLEWARE_TOGGLES[key] == "BALDUR_REQUEST_TRACKING_MIDDLEWARE_ENABLED"

    def test_tiering_toggle_setting_name(self):
        """TieringMiddleware의 토글 설정명은 BALDUR_TIERING_MIDDLEWARE_ENABLED이다."""
        key = "baldur.api.django.tiering.TieringMiddleware"
        assert MIDDLEWARE_TOGGLES[key] == "BALDUR_TIERING_MIDDLEWARE_ENABLED"

    def test_audit_toggle_setting_name(self):
        """AuditMiddleware의 토글 설정명은 BALDUR_AUDIT_MIDDLEWARE_ENABLED이다."""
        key = "baldur.api.django.audit_middleware.AuditMiddleware"
        assert MIDDLEWARE_TOGGLES[key] == "BALDUR_AUDIT_MIDDLEWARE_ENABLED"


# =========================================================================
# Behavior Tests — _find_insert_point
# =========================================================================


class TestFindInsertPointBehavior:
    """_find_insert_point 삽입 위치 탐색 동작 검증."""

    def test_target_found_after_true_returns_next_index(self):
        """target 발견 + after=True이면 target 다음 인덱스를 반환한다."""
        mw = ["a.Foo", "b.Bar", "c.Baz"]
        result = _find_insert_point(mw, "Bar", after=True, fallback=99)
        assert result == 2

    def test_target_found_after_false_returns_same_index(self):
        """target 발견 + after=False이면 target 인덱스를 반환한다."""
        mw = ["a.Foo", "b.Bar", "c.Baz"]
        result = _find_insert_point(mw, "Bar", after=False, fallback=99)
        assert result == 1

    def test_target_not_found_returns_fallback(self):
        """target이 없으면 fallback 값을 반환한다."""
        mw = ["a.Foo", "b.Bar"]
        result = _find_insert_point(mw, "Missing", after=True, fallback=42)
        assert result == 42

    def test_substring_match_works(self):
        """전체 경로가 아닌 부분 문자열로 매칭한다."""
        mw = ["django.contrib.auth.middleware.AuthenticationMiddleware"]
        result = _find_insert_point(
            mw, "AuthenticationMiddleware", after=True, fallback=0
        )
        assert result == 1

    def test_empty_list_returns_fallback(self):
        """빈 리스트에서는 fallback을 반환한다."""
        result = _find_insert_point([], "Foo", after=True, fallback=0)
        assert result == 0


# =========================================================================
# Behavior Tests — _filter_by_toggles
# =========================================================================


class TestFilterByTogglesBehavior:
    """_filter_by_toggles 미들웨어 토글 필터링 동작 검증."""

    def test_toggle_false_removes_middleware(self):
        """토글이 False인 미들웨어는 필터링된다."""
        group = ["baldur.api.django.tiering.TieringMiddleware"]
        ns = {"BALDUR_TIERING_MIDDLEWARE_ENABLED": False}
        result = _filter_by_toggles(group, ns)
        assert result == []

    def test_toggle_true_keeps_middleware(self):
        """토글이 True인 미들웨어는 유지된다."""
        group = ["baldur.api.django.tiering.TieringMiddleware"]
        ns = {"BALDUR_TIERING_MIDDLEWARE_ENABLED": True}
        result = _filter_by_toggles(group, ns)
        assert len(result) == 1

    def test_toggle_missing_defaults_to_enabled(self):
        """토글 설정이 없으면 기본값 True (활성화)로 처리된다."""
        group = ["baldur.api.django.tiering.TieringMiddleware"]
        result = _filter_by_toggles(group, {})
        assert len(result) == 1

    def test_middleware_without_toggle_always_included(self):
        """MIDDLEWARE_TOGGLES에 등록되지 않은 미들웨어는 항상 포함된다."""
        group = ["baldur.audit.trace.trace_id_middleware"]
        result = _filter_by_toggles(group, {})
        assert len(result) == 1

    def test_does_not_mutate_input_group(self):
        """원본 group 리스트를 변경하지 않는다."""
        group = [
            "baldur.api.django.tiering.TieringMiddleware",
            "baldur.audit.trace.trace_id_middleware",
        ]
        original = group.copy()
        _filter_by_toggles(group, {"BALDUR_TIERING_MIDDLEWARE_ENABLED": False})
        assert group == original


# =========================================================================
# Behavior Tests — _setup_exception_handler
# =========================================================================


class TestSetupExceptionHandlerBehavior:
    """_setup_exception_handler DRF 예외 핸들러 자동 설정 동작 검증."""

    def test_sets_handler_when_not_present(self):
        """EXCEPTION_HANDLER가 없으면 자동 설정한다."""
        ns = {"REST_FRAMEWORK": {"PAGE_SIZE": 10}}
        _setup_exception_handler(ns)
        assert "EXCEPTION_HANDLER" in ns["REST_FRAMEWORK"]
        assert "baldur_exception_handler" in ns["REST_FRAMEWORK"]["EXCEPTION_HANDLER"]

    def test_does_not_override_existing_handler(self):
        """Consumer가 이미 설정한 EXCEPTION_HANDLER는 덮어쓰지 않는다."""
        custom_handler = "myapp.exceptions.custom_handler"
        ns = {"REST_FRAMEWORK": {"EXCEPTION_HANDLER": custom_handler}}
        _setup_exception_handler(ns)
        assert ns["REST_FRAMEWORK"]["EXCEPTION_HANDLER"] == custom_handler

    def test_creates_rest_framework_dict_when_missing(self):
        """REST_FRAMEWORK가 없으면 새로 생성하여 설정한다."""
        ns = {}
        _setup_exception_handler(ns)
        assert "REST_FRAMEWORK" in ns
        assert "EXCEPTION_HANDLER" in ns["REST_FRAMEWORK"]


# =========================================================================
# Behavior Tests — _validate_prerequisites
# =========================================================================


class TestValidatePrerequisitesBehavior:
    """_validate_prerequisites 필수 설정 검증 동작."""

    def test_missing_middleware_raises_improperly_configured(self):
        """MIDDLEWARE가 없으면 ImproperlyConfigured 예외를 발생시킨다."""
        ns = {"INSTALLED_APPS": ["myapp"]}
        with pytest.raises(ImproperlyConfigured, match="MIDDLEWARE"):
            _validate_prerequisites(ns)

    def test_missing_installed_apps_raises_improperly_configured(self):
        """INSTALLED_APPS가 없으면 ImproperlyConfigured 예외를 발생시킨다."""
        ns = {"MIDDLEWARE": []}
        with pytest.raises(ImproperlyConfigured, match="INSTALLED_APPS"):
            _validate_prerequisites(ns)

    def test_valid_namespace_passes_without_error(self):
        """MIDDLEWARE와 INSTALLED_APPS가 모두 있으면 정상 통과한다."""
        ns = {"MIDDLEWARE": [], "INSTALLED_APPS": []}
        _validate_prerequisites(ns)


# =========================================================================
# Behavior Tests — _is_gunicorn_master
# =========================================================================


class TestIsGunicornMasterBehavior:
    """_is_gunicorn_master Gunicorn 마스터 프로세스 감지 동작 검증."""

    def test_dev_server_returns_false(self):
        """개발 서버 (SERVER_SOFTWARE 미설정)에서는 False를 반환한다."""
        with patch.dict("os.environ", {}, clear=True):
            assert _is_gunicorn_master() is False

    def test_gunicorn_master_returns_true(self):
        """Gunicorn 마스터 (SERVER_SOFTWARE=gunicorn, GUNICORN_WORKER 미설정)에서 True."""
        env = {"SERVER_SOFTWARE": "gunicorn/21.2.0"}
        with patch.dict("os.environ", env, clear=True):
            assert _is_gunicorn_master() is True

    def test_gunicorn_worker_returns_false(self):
        """Gunicorn 워커 (GUNICORN_WORKER=1)에서는 False를 반환한다."""
        env = {"SERVER_SOFTWARE": "gunicorn/21.2.0", "GUNICORN_WORKER": "1"}
        with patch.dict("os.environ", env, clear=True):
            assert _is_gunicorn_master() is False


# =========================================================================
# Behavior Tests — _inject_middleware_groups (ordering)
# =========================================================================


class TestInjectMiddlewareGroupsBehavior:
    """_inject_middleware_groups 미들웨어 그룹 삽입 순서 동작 검증."""

    def test_early_group_inserted_after_prometheus_before(self, base_namespace):
        """early 그룹은 PrometheusBeforeMiddleware 직후에 삽입된다."""
        _inject_middleware_groups(
            base_namespace,
            early=list(DEFAULT_EARLY_GROUP),
            post_auth=[],
            tail=[],
        )
        mw = base_namespace["MIDDLEWARE"]
        prom_idx = next(
            i for i, m in enumerate(mw) if "PrometheusBeforeMiddleware" in m
        )
        first_early = DEFAULT_EARLY_GROUP[0]
        early_idx = mw.index(first_early)
        assert early_idx == prom_idx + 1

    def test_post_auth_group_inserted_after_xframe(self, base_namespace):
        """post_auth 그룹은 XFrameOptionsMiddleware 이후에 삽입된다."""
        _inject_middleware_groups(
            base_namespace,
            early=[],
            post_auth=list(DEFAULT_POST_AUTH_GROUP),
            tail=[],
        )
        mw = base_namespace["MIDDLEWARE"]
        xframe_idx = next(i for i, m in enumerate(mw) if "XFrameOptionsMiddleware" in m)
        first_post_auth = DEFAULT_POST_AUTH_GROUP[0]
        post_auth_idx = mw.index(first_post_auth)
        assert post_auth_idx > xframe_idx

    def test_tail_group_inserted_before_prometheus_after(self, base_namespace):
        """tail 그룹은 PrometheusAfterMiddleware 직전에 삽입된다."""
        _inject_middleware_groups(
            base_namespace,
            early=[],
            post_auth=[],
            tail=list(DEFAULT_TAIL_GROUP),
        )
        mw = base_namespace["MIDDLEWARE"]
        prom_after_idx = next(
            i for i, m in enumerate(mw) if "PrometheusAfterMiddleware" in m
        )
        tail_mw = DEFAULT_TAIL_GROUP[0]
        tail_idx = mw.index(tail_mw)
        assert tail_idx == prom_after_idx - 1

    def test_full_injection_preserves_group_order(self, base_namespace):
        """전체 삽입 시 early → core → post_auth → tail 순서가 보장된다."""
        _inject_middleware_groups(
            base_namespace,
            early=list(DEFAULT_EARLY_GROUP),
            post_auth=list(DEFAULT_POST_AUTH_GROUP),
            tail=list(DEFAULT_TAIL_GROUP),
        )
        mw = base_namespace["MIDDLEWARE"]

        # early 내부 순서 보장
        early_indices = [mw.index(m) for m in DEFAULT_EARLY_GROUP]
        assert early_indices == sorted(early_indices)

        # post_auth 내부 순서 보장
        post_auth_indices = [mw.index(m) for m in DEFAULT_POST_AUTH_GROUP]
        assert post_auth_indices == sorted(post_auth_indices)

        # early < Django core < post_auth < tail
        assert max(early_indices) < min(post_auth_indices)
        tail_idx = mw.index(DEFAULT_TAIL_GROUP[0])
        assert max(post_auth_indices) < tail_idx

    def test_already_present_middleware_not_duplicated(self, base_namespace):
        """Consumer가 수동으로 넣은 미들웨어는 중복 삽입하지 않는다."""
        # 수동으로 trace_id_middleware를 추가
        base_namespace["MIDDLEWARE"].insert(1, DEFAULT_EARLY_GROUP[0])
        original_count = base_namespace["MIDDLEWARE"].count(DEFAULT_EARLY_GROUP[0])

        _inject_middleware_groups(
            base_namespace,
            early=list(DEFAULT_EARLY_GROUP),
            post_auth=[],
            tail=[],
        )
        new_count = base_namespace["MIDDLEWARE"].count(DEFAULT_EARLY_GROUP[0])
        assert new_count == original_count

    def test_no_prometheus_before_inserts_early_at_start(self):
        """PrometheusBeforeMiddleware가 없으면 early를 맨 앞에 삽입한다."""
        ns = {
            "MIDDLEWARE": [
                "django.middleware.security.SecurityMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        }
        _inject_middleware_groups(
            ns, early=["test.EarlyMiddleware"], post_auth=[], tail=[]
        )
        assert ns["MIDDLEWARE"][0] == "test.EarlyMiddleware"

    def test_no_prometheus_after_appends_tail_at_end(self):
        """PrometheusAfterMiddleware가 없으면 tail을 맨 뒤에 추가한다."""
        ns = {
            "MIDDLEWARE": [
                "django.middleware.security.SecurityMiddleware",
            ]
        }
        _inject_middleware_groups(
            ns, early=[], post_auth=[], tail=["test.TailMiddleware"]
        )
        assert ns["MIDDLEWARE"][-1] == "test.TailMiddleware"

    def test_toggle_disabled_middleware_excluded_from_injection(self, base_namespace):
        """토글 비활성화된 미들웨어는 삽입되지 않는다."""
        base_namespace["BALDUR_TIERING_MIDDLEWARE_ENABLED"] = False
        _inject_middleware_groups(
            base_namespace,
            early=list(DEFAULT_EARLY_GROUP),
            post_auth=[],
            tail=[],
        )
        mw = base_namespace["MIDDLEWARE"]
        assert "baldur.api.django.tiering.TieringMiddleware" not in mw


# =========================================================================
# Behavior Tests — configure_baldur (integration of sub-functions)
# =========================================================================


class TestConfigureBaldurBehavior:
    """configure_baldur() 래퍼 함수 통합 동작 검증."""

    @patch(
        "baldur.adapters.django.auto_config._initialize_otel",
        autospec=True,
    )
    def test_basic_call_injects_all_groups(self, mock_otel, base_namespace):
        """기본 호출 시 모든 미들웨어 그룹과 EXCEPTION_HANDLER가 설정된다."""
        configure_baldur(namespace=base_namespace)
        mw = base_namespace["MIDDLEWARE"]

        for m in DEFAULT_EARLY_GROUP:
            assert m in mw

        for m in DEFAULT_POST_AUTH_GROUP:
            assert m in mw

        for m in DEFAULT_TAIL_GROUP:
            assert m in mw

    @patch(
        "baldur.adapters.django.auto_config._initialize_otel",
        autospec=True,
    )
    def test_custom_early_group_overrides_default(self, mock_otel, base_namespace):
        """early_group 파라미터로 기본 그룹을 오버라이드할 수 있다."""
        custom = ["mycompany.EarlyMiddleware"]
        configure_baldur(namespace=base_namespace, early_group=custom)
        mw = base_namespace["MIDDLEWARE"]
        assert "mycompany.EarlyMiddleware" in mw
        # Default early가 아닌 custom이 삽입됨
        assert DEFAULT_EARLY_GROUP[0] not in mw

    @patch(
        "baldur.adapters.django.auto_config._initialize_otel",
        autospec=True,
    )
    def test_domains_parameter_sets_core_domains(self, mock_otel, base_namespace):
        """domains 파라미터가 BALDUR_CORE_DOMAINS를 설정한다."""
        domains = ["payment", "order"]
        configure_baldur(namespace=base_namespace, domains=domains)
        assert base_namespace["BALDUR_CORE_DOMAINS"] == domains

    @patch(
        "baldur.adapters.django.auto_config._initialize_otel",
        autospec=True,
    )
    def test_disable_auto_otel_skips_otel_init(self, mock_otel, base_namespace):
        """disable_auto_otel=True이면 OTEL 초기화를 건너뛴다."""
        configure_baldur(namespace=base_namespace, disable_auto_otel=True)
        mock_otel.assert_not_called()

    @patch(
        "baldur.adapters.django.auto_config._initialize_otel",
        autospec=True,
    )
    def test_auto_middleware_false_skips_injection(self, mock_otel, base_namespace):
        """BALDUR_AUTO_CONFIG_MIDDLEWARE=false이면 미들웨어 삽입을 건너뛴다."""
        from baldur.settings.auto_config import reset_auto_config_settings

        reset_auto_config_settings()
        env = {
            "BALDUR_AUTO_CONFIG_MIDDLEWARE": "false",
            "BALDUR_AUTO_CONFIG_EXCEPTION_HANDLER": "false",
            "BALDUR_OBSERVABILITY_PROFILE": "local",
        }
        with patch.dict("os.environ", env):
            reset_auto_config_settings()
            original_mw = list(base_namespace["MIDDLEWARE"])
            configure_baldur(namespace=base_namespace)
            assert base_namespace["MIDDLEWARE"] == original_mw
            reset_auto_config_settings()

    @patch(
        "baldur.adapters.django.auto_config._initialize_otel",
        autospec=True,
    )
    def test_idempotent_double_call_no_duplicate(self, mock_otel, base_namespace):
        """configure_baldur()을 2회 호출해도 미들웨어가 중복되지 않는다."""
        configure_baldur(namespace=base_namespace)
        first_mw = list(base_namespace["MIDDLEWARE"])
        configure_baldur(namespace=base_namespace)
        second_mw = base_namespace["MIDDLEWARE"]
        assert first_mw == second_mw


# =========================================================================
# Behavior Tests — _initialize_otel (logging)
# =========================================================================


class TestInitializeOtelBehavior:
    """_initialize_otel OTEL 초기화 동작 검증."""

    @patch("baldur.adapters.django.auto_config.logger")
    def test_import_error_does_not_log_warning(self, mock_logger):
        """OTEL 모듈 미설치 시 warning 로그를 남기지 않는다."""
        from baldur.adapters.django.auto_config import _initialize_otel

        ns: dict = {}
        with patch.dict("sys.modules", {"baldur.observability": None}):
            _initialize_otel(ns)

        assert ns["_otel_initialized"] is False
        mock_logger.warning.assert_not_called()

    @patch("baldur.adapters.django.auto_config.logger")
    @patch(
        "baldur.observability.initialize_opentelemetry",
        side_effect=RuntimeError("tracer init failed"),
    )
    def test_runtime_error_logs_warning(self, _mock_init, mock_logger):
        """OTEL 초기화 중 예외 발생 시 warning 로그를 남긴다."""
        from baldur.adapters.django.auto_config import _initialize_otel

        ns: dict = {}
        _initialize_otel(ns)

        assert ns["_otel_initialized"] is False
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "baldur.otel_initialization_failed"
