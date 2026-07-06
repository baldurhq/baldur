"""
CellTaggingMiddleware 수신 cell_id 검증 단위 테스트.

대상: baldur.api.django.cell.middleware.CellTaggingMiddleware
검증:
- _accept_incoming_cell_id(): 수신된 cell_id의 Trust + Topology 검증 후 수용/거부
- _is_trusted_source(): CIDR 기반 소스 IP Trust 검증
- _validate_cell_id(): CellRegistry 기반 Topology Mismatch 검증
- _record_topology_mismatch(): Prometheus 카운터 기록

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Behavior: 수신 검증 파이프라인 동작 검증 (소스 참조)

참조 소스:
- api/django/cell/middleware.py (CellTaggingMiddleware)
- context/cell_context.py (get_current_cell_id, _current_cell_id)
- settings/cell_topology.py (CellTopologySettings.trusted_source_cidrs)
- services/cell_topology/models.py (CellState, CellInfo)
- utils/network.py (extract_client_ip)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.context.cell_context import _current_cell_id

# _reset_cell_context fixture는 conftest.py에서 autouse로 제공 (§5.1: 2+ 파일 공유)


def _make_middleware(*, tagger_return="cell-0"):
    """활성화된 CellTaggingMiddleware를 생성하는 헬퍼."""
    from baldur.api.django.cell.middleware import CellTaggingMiddleware

    response_headers = {}
    response = MagicMock()
    response.__setitem__ = lambda self, k, v: response_headers.__setitem__(k, v)

    get_response = MagicMock(return_value=response)
    mw = CellTaggingMiddleware(get_response)

    mock_tagger = MagicMock()
    mock_tagger.resolve_cell_id_from_request.return_value = tagger_return
    mw._tagger = mock_tagger

    return mw, get_response, response_headers


def _make_request(*, remote_addr="10.0.1.5"):
    """기본 META를 가진 Mock request를 생성하는 헬퍼."""
    request = MagicMock()
    request.META = {"REMOTE_ADDR": remote_addr}
    return request


class TestIsTrustedSourceBehavior:
    """_is_trusted_source() CIDR 기반 Trust 검증 동작."""

    def test_private_rfc1918_class_a_trusted(self):
        """RFC 1918 Class A (10.0.0.0/8) 대역은 신뢰한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = _make_request(remote_addr="10.244.1.15")
        assert mw._is_trusted_source(request) is True

    def test_private_rfc1918_class_b_trusted(self):
        """RFC 1918 Class B (172.16.0.0/12) 대역은 신뢰한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["172.16.0.0/12"]

        request = _make_request(remote_addr="172.20.0.100")
        assert mw._is_trusted_source(request) is True

    def test_private_rfc1918_class_c_trusted(self):
        """RFC 1918 Class C (192.168.0.0/16) 대역은 신뢰한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["192.168.0.0/16"]

        request = _make_request(remote_addr="192.168.1.200")
        assert mw._is_trusted_source(request) is True

    def test_loopback_trusted(self):
        """Loopback (127.0.0.0/8) 대역은 신뢰한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["127.0.0.0/8"]

        request = _make_request(remote_addr="127.0.0.1")
        assert mw._is_trusted_source(request) is True

    def test_public_ip_untrusted(self):
        """퍼블릭 IP는 신뢰하지 않는다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
        ]

        request = _make_request(remote_addr="203.0.113.50")
        assert mw._is_trusted_source(request) is False

    def test_no_client_ip_untrusted(self):
        """클라이언트 IP를 추출할 수 없으면 신뢰하지 않는다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = MagicMock()
        request.META = {}
        assert mw._is_trusted_source(request) is False

    def test_invalid_ip_format_untrusted(self):
        """유효하지 않은 IP 형식은 신뢰하지 않는다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = _make_request(remote_addr="not-an-ip")
        assert mw._is_trusted_source(request) is False

    def test_x_forwarded_for_header_respected(self):
        """X-Forwarded-For 헤더의 첫 번째 IP로 Trust를 판별한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = MagicMock()
        request.META = {
            "HTTP_X_FORWARDED_FOR": "10.0.1.5, 172.16.0.1",
            "REMOTE_ADDR": "203.0.113.50",
        }
        assert mw._is_trusted_source(request) is True

    def test_trusted_cidrs_lazy_loaded_from_settings(self):
        """trusted_source_cidrs는 Settings에서 지연 로딩된다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        assert mw._trusted_cidrs is None  # 초기값 None

        with patch(
            "baldur.settings.cell_topology.get_cell_topology_settings"
        ) as mock_settings:
            mock_settings.return_value = MagicMock(trusted_source_cidrs=["10.0.0.0/8"])
            cidrs = mw._get_trusted_cidrs()

        assert cidrs == ["10.0.0.0/8"]
        assert mw._trusted_cidrs == ["10.0.0.0/8"]  # 캐시됨

    def test_trusted_cidrs_refreshed_after_ttl_expires(self):
        """TTL 만료 후 trusted_source_cidrs가 Settings에서 재로딩된다."""

        from baldur.api.django.cell.middleware import (
            _TRUSTED_CIDRS_CACHE_TTL_SECONDS,
            CellTaggingMiddleware,
        )

        mw = CellTaggingMiddleware(MagicMock())

        with patch(
            "baldur.settings.cell_topology.get_cell_topology_settings"
        ) as mock_settings:
            # 1차 로딩
            mock_settings.return_value = MagicMock(trusted_source_cidrs=["10.0.0.0/8"])
            first = mw._get_trusted_cidrs()
            assert first == ["10.0.0.0/8"]

            # TTL 만료 시뮬레이션
            mw._trusted_cidrs_loaded_at -= _TRUSTED_CIDRS_CACHE_TTL_SECONDS + 1

            # 2차 로딩 — 변경된 Settings 반영
            mock_settings.return_value = MagicMock(
                trusted_source_cidrs=["10.0.0.0/8", "172.16.0.0/12"]
            )
            refreshed = mw._get_trusted_cidrs()
            assert refreshed == ["10.0.0.0/8", "172.16.0.0/12"]


class TestValidateCellIdBehavior:
    """_validate_cell_id() Topology Mismatch 검증 동작."""

    def _make_cell_info(self, *, state=None):
        """테스트용 CellInfo Mock 생성."""
        from baldur.services.cell_topology.models import CellState

        cell_info = MagicMock()
        cell_info.state = state if state is not None else CellState.ACTIVE
        return cell_info

    def test_active_cell_accepted(self):
        """ACTIVE 상태의 Cell은 수용한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware
        from baldur.services.cell_topology.models import CellState

        mw = CellTaggingMiddleware(MagicMock())
        cell_info = self._make_cell_info(state=CellState.ACTIVE)

        with patch(
            "baldur.services.cell_topology.registry.get_cell_registry"
        ) as mock_registry:
            mock_registry.return_value.get_cell_info.return_value = cell_info
            result = mw._validate_cell_id("cell-3")

        assert result == "cell-3"

    def test_warmup_cell_accepted(self):
        """WARMUP 상태의 Cell은 수용한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware
        from baldur.services.cell_topology.models import CellState

        mw = CellTaggingMiddleware(MagicMock())
        cell_info = self._make_cell_info(state=CellState.WARMUP)

        with patch(
            "baldur.services.cell_topology.registry.get_cell_registry"
        ) as mock_registry:
            mock_registry.return_value.get_cell_info.return_value = cell_info
            result = mw._validate_cell_id("cell-1")

        assert result == "cell-1"

    def test_draining_cell_rejected(self):
        """DRAINING 상태의 Cell은 격리 정책 우회 방지를 위해 거부한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware
        from baldur.services.cell_topology.models import CellState

        mw = CellTaggingMiddleware(MagicMock())
        cell_info = self._make_cell_info(state=CellState.DRAINING)

        with patch(
            "baldur.services.cell_topology.registry.get_cell_registry"
        ) as mock_registry:
            mock_registry.return_value.get_cell_info.return_value = cell_info
            mock_registry.return_value.get_all_cells.return_value = {}
            result = mw._validate_cell_id("cell-2")

        assert result is None

    def test_isolated_cell_rejected(self):
        """ISOLATED 상태의 Cell은 격리 정책 우회 방지를 위해 거부한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware
        from baldur.services.cell_topology.models import CellState

        mw = CellTaggingMiddleware(MagicMock())
        cell_info = self._make_cell_info(state=CellState.ISOLATED)

        with patch(
            "baldur.services.cell_topology.registry.get_cell_registry"
        ) as mock_registry:
            mock_registry.return_value.get_cell_info.return_value = cell_info
            mock_registry.return_value.get_all_cells.return_value = {}
            result = mw._validate_cell_id("cell-4")

        assert result is None

    def test_unknown_cell_rejected(self):
        """로컬 Registry에 존재하지 않는 Cell은 거부한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())

        with patch(
            "baldur.services.cell_topology.registry.get_cell_registry"
        ) as mock_registry:
            mock_registry.return_value.get_cell_info.return_value = None
            mock_registry.return_value.get_all_cells.return_value = {
                "cell-0": MagicMock(),
                "cell-1": MagicMock(),
            }
            result = mw._validate_cell_id("cell-99")

        assert result is None

    def test_unknown_cell_records_mismatch_metric(self):
        """존재하지 않는 Cell 수신 시 topology_mismatch 메트릭이 기록된다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())

        with (
            patch(
                "baldur.services.cell_topology.registry.get_cell_registry"
            ) as mock_registry,
            patch.object(
                CellTaggingMiddleware, "_record_topology_mismatch"
            ) as mock_record,
        ):
            mock_registry.return_value.get_cell_info.return_value = None
            mock_registry.return_value.get_all_cells.return_value = {}
            mw._validate_cell_id("cell-99")

        mock_record.assert_called_once_with("cell-99", "cell_not_found")

    def test_draining_cell_records_not_active_metric(self):
        """DRAINING Cell 수신 시 cell_not_active 메트릭이 기록된다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware
        from baldur.services.cell_topology.models import CellState

        mw = CellTaggingMiddleware(MagicMock())
        cell_info = self._make_cell_info(state=CellState.DRAINING)

        with (
            patch(
                "baldur.services.cell_topology.registry.get_cell_registry"
            ) as mock_registry,
            patch.object(
                CellTaggingMiddleware, "_record_topology_mismatch"
            ) as mock_record,
        ):
            mock_registry.return_value.get_cell_info.return_value = cell_info
            mw._validate_cell_id("cell-3")

        mock_record.assert_called_once_with("cell-3", "cell_not_active")


class TestAcceptIncomingCellIdBehavior:
    """_accept_incoming_cell_id() 전체 파이프라인 동작 검증."""

    def test_no_incoming_cell_id_returns_none(self):
        """수신된 cell_id가 없으면 None을 반환한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        request = _make_request()

        # ContextVar에 cell_id가 없는 상태
        result = mw._accept_incoming_cell_id(request)
        assert result is None

    def test_untrusted_source_returns_none(self):
        """신뢰할 수 없는 소스에서 온 cell_id는 거부한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = _make_request(remote_addr="203.0.113.50")  # 퍼블릭 IP

        # ContextVar에 cell_id 설정 (BaggageSyncMiddleware가 복원한 상태 시뮬레이션)
        token = _current_cell_id.set("cell-3")
        try:
            result = mw._accept_incoming_cell_id(request)
        finally:
            _current_cell_id.reset(token)

        assert result is None

    def test_trusted_source_valid_cell_accepted(self):
        """신뢰할 수 있는 소스에서 유효한 cell_id는 수용한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware
        from baldur.services.cell_topology.models import CellState

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = _make_request(remote_addr="10.0.1.5")

        cell_info = MagicMock()
        cell_info.state = CellState.ACTIVE

        token = _current_cell_id.set("cell-3")
        try:
            with patch(
                "baldur.services.cell_topology.registry.get_cell_registry"
            ) as mock_registry:
                mock_registry.return_value.get_cell_info.return_value = cell_info
                result = mw._accept_incoming_cell_id(request)
        finally:
            _current_cell_id.reset(token)

        assert result == "cell-3"

    def test_trusted_source_invalid_cell_falls_back(self):
        """신뢰할 수 있는 소스지만 유효하지 않은 cell_id는 None을 반환한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        mw = CellTaggingMiddleware(MagicMock())
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = _make_request(remote_addr="10.0.1.5")

        token = _current_cell_id.set("cell-99")
        try:
            with patch(
                "baldur.services.cell_topology.registry.get_cell_registry"
            ) as mock_registry:
                mock_registry.return_value.get_cell_info.return_value = None
                mock_registry.return_value.get_all_cells.return_value = {}
                result = mw._accept_incoming_cell_id(request)
        finally:
            _current_cell_id.reset(token)

        assert result is None


class TestCellTaggingMiddlewareIncomingIntegrationBehavior:
    """CellTaggingMiddleware.__call__()에서 수신 cell_id 수용 통합 동작 검증."""

    def test_accepted_incoming_skips_local_hashing(self):
        """수신 cell_id가 수용되면 로컬 해싱을 건너뛴다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware
        from baldur.services.cell_topology.models import CellState

        response_headers = {}
        response = MagicMock()
        response.__setitem__ = lambda self, k, v: response_headers.__setitem__(k, v)
        get_response = MagicMock(return_value=response)

        mw = CellTaggingMiddleware(get_response)
        mock_tagger = MagicMock()
        mock_tagger.resolve_cell_id_from_request.return_value = "cell-0"
        mw._tagger = mock_tagger
        mw._trusted_cidrs = ["10.0.0.0/8"]

        request = _make_request(remote_addr="10.0.1.5")

        cell_info = MagicMock()
        cell_info.state = CellState.ACTIVE

        # BaggageSyncMiddleware가 이미 ContextVar를 복원한 상태
        token = _current_cell_id.set("cell-5")
        try:
            with (
                patch("django.conf.settings") as mock_django_settings,
                patch(
                    "baldur.services.cell_topology.registry.get_cell_registry"
                ) as mock_registry,
            ):
                mock_django_settings.BALDUR_CELL_TOPOLOGY_ENABLED = True
                mock_django_settings.BALDUR_CELL_TAGGING_ENABLED = True
                mock_registry.return_value.get_cell_info.return_value = cell_info

                mw(request)
        finally:
            _current_cell_id.reset(token)

        # 상위 서비스의 cell_id가 수용되어 로컬 해싱 미호출
        mock_tagger.resolve_cell_id_from_request.assert_not_called()
        assert request.cell_id == "cell-5"
        assert response_headers["X-Cell-Id"] == "cell-5"

    def test_rejected_incoming_falls_back_to_local_hashing(self):
        """수신 cell_id가 거부되면 로컬 해싱으로 폴백한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        response_headers = {}
        response = MagicMock()
        response.__setitem__ = lambda self, k, v: response_headers.__setitem__(k, v)
        get_response = MagicMock(return_value=response)

        mw = CellTaggingMiddleware(get_response)
        mock_tagger = MagicMock()
        mock_tagger.resolve_cell_id_from_request.return_value = "cell-2"
        mw._tagger = mock_tagger
        mw._trusted_cidrs = ["10.0.0.0/8"]

        # 퍼블릭 IP로부터의 위조 시도
        request = _make_request(remote_addr="203.0.113.50")

        token = _current_cell_id.set("cell-0")
        try:
            with patch("django.conf.settings") as mock_django_settings:
                mock_django_settings.BALDUR_CELL_TOPOLOGY_ENABLED = True
                mock_django_settings.BALDUR_CELL_TAGGING_ENABLED = True

                mw(request)
        finally:
            _current_cell_id.reset(token)

        # 위조된 cell_id 무시, 로컬 해싱 사용
        mock_tagger.resolve_cell_id_from_request.assert_called_once()
        assert request.cell_id == "cell-2"
        assert response_headers["X-Cell-Id"] == "cell-2"


class TestRecordTopologyMismatchBehavior:
    """_record_topology_mismatch() 메트릭 기록 동작 검증."""

    def test_counter_singleton_no_duplicate_registration(self):
        """동일 카운터를 연속 호출해도 중복 등록 예외가 발생하지 않는다."""
        import baldur.api.django.cell.middleware as mw_module
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        # 싱글톤 초기화
        original = mw_module._topology_mismatch_counter
        mw_module._topology_mismatch_counter = None

        try:
            with patch(
                "baldur.metrics.drift_metrics._get_or_create_counter"
            ) as mock_create:
                mock_counter = MagicMock()
                mock_create.return_value = mock_counter

                # 2회 연속 호출 — 중복 등록 없이 동작
                CellTaggingMiddleware._record_topology_mismatch(
                    "cell-1", "cell_not_found"
                )
                CellTaggingMiddleware._record_topology_mismatch(
                    "cell-2", "cell_not_active"
                )

            # _get_or_create_counter는 1회만 호출 (싱글톤 캐시)
            mock_create.assert_called_once()
            assert mock_counter.labels.call_count == 2
        finally:
            mw_module._topology_mismatch_counter = original

    def test_counter_labels_match_reason_strings(self):
        """메트릭 라벨이 reason 문자열과 cell_id를 정확히 전달한다."""
        import baldur.api.django.cell.middleware as mw_module
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        original = mw_module._topology_mismatch_counter
        mw_module._topology_mismatch_counter = None

        try:
            with patch(
                "baldur.metrics.drift_metrics._get_or_create_counter"
            ) as mock_create:
                mock_counter = MagicMock()
                mock_create.return_value = mock_counter

                CellTaggingMiddleware._record_topology_mismatch(
                    "cell-99", "cell_not_found"
                )

            mock_counter.labels.assert_called_once_with(
                incoming_cell_id="cell-99",
                reason="cell_not_found",
            )
            mock_counter.labels.return_value.inc.assert_called_once()
        finally:
            mw_module._topology_mismatch_counter = original
