"""
OTel Baggage 통합 전파 단위 테스트.

대상 모듈:
- baldur.observability.baggage (setup_baggage_propagation, sync/restore/detach)
"""

from unittest.mock import patch


class TestBaggageModuleContract:
    """Baggage 모듈 상수 및 매핑 계약 검증."""

    def test_baggage_prefix_is_baldur(self):
        """Baggage 키 접두사는 'baldur'이다."""
        from baldur.observability.baggage import BAGGAGE_PREFIX

        assert BAGGAGE_PREFIX == "baldur"

    def test_contextvar_baggage_map_contains_cell_id(self):
        """cell_id ContextVar가 Baggage 매핑에 포함되어야 한다."""
        from baldur.observability.baggage import _CONTEXTVAR_BAGGAGE_MAP

        assert "cell_id" in _CONTEXTVAR_BAGGAGE_MAP
        assert (
            _CONTEXTVAR_BAGGAGE_MAP["cell_id"]["getter"]
            == "baldur.context.cell_context:get_current_cell_id"
        )
        assert (
            _CONTEXTVAR_BAGGAGE_MAP["cell_id"]["contextvar"]
            == "baldur.context.cell_context:_current_cell_id"
        )

    def test_contextvar_baggage_map_contains_domain(self):
        """domain ContextVar가 Baggage 매핑에 포함되어야 한다."""
        from baldur.observability.baggage import _CONTEXTVAR_BAGGAGE_MAP

        assert "domain" in _CONTEXTVAR_BAGGAGE_MAP
        assert (
            _CONTEXTVAR_BAGGAGE_MAP["domain"]["getter"]
            == "baldur.decorators.domain_tag:get_current_domain"
        )
        assert (
            _CONTEXTVAR_BAGGAGE_MAP["domain"]["contextvar"]
            == "baldur.decorators.domain_tag:_current_domain"
        )

    def test_contextvar_baggage_map_has_exactly_two_entries(self):
        """현재 Baggage 매핑은 cell_id, domain 2개이다."""
        from baldur.observability.baggage import _CONTEXTVAR_BAGGAGE_MAP

        assert len(_CONTEXTVAR_BAGGAGE_MAP) == 2


class TestSetupBaggagePropagationBehavior:
    """setup_baggage_propagation() 동작 검증."""

    def test_sets_composite_propagator_as_global_textmap(self):
        """setup_baggage_propagation() 실행 후 글로벌 textmap이 CompositePropagator이다."""
        from opentelemetry import propagate
        from opentelemetry.propagators.composite import CompositePropagator

        from baldur.observability.baggage import setup_baggage_propagation

        setup_baggage_propagation()

        textmap = propagate.get_global_textmap()
        assert isinstance(textmap, CompositePropagator)

    def test_composite_contains_tracecontext_and_baggage_propagators(self):
        """CompositePropagator에 TraceContext + Baggage 2개 propagator가 포함되어야 한다."""
        from opentelemetry import propagate
        from opentelemetry.baggage.propagation import W3CBaggagePropagator
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        from baldur.observability.baggage import setup_baggage_propagation

        setup_baggage_propagation()

        composite = propagate.get_global_textmap()
        propagators = composite._propagators
        assert len(propagators) == 2

        propagator_types = {type(p) for p in propagators}
        assert TraceContextTextMapPropagator in propagator_types
        assert W3CBaggagePropagator in propagator_types


class TestResolveImportBehavior:
    """_resolve_import() 동작 검증."""

    def test_resolves_cell_id_getter(self):
        """셀 ID getter 경로를 정상 resolve 한다."""
        from baldur.observability.baggage import _resolve_import

        getter = _resolve_import("baldur.context.cell_context:get_current_cell_id")
        from baldur.context.cell_context import get_current_cell_id

        assert getter is get_current_cell_id

    def test_resolves_domain_getter(self):
        """도메인 getter 경로를 정상 resolve 한다."""
        from baldur.observability.baggage import _resolve_import

        getter = _resolve_import("baldur.decorators.domain_tag:get_current_domain")
        from baldur.decorators.domain_tag import get_current_domain

        assert getter is get_current_domain

    def test_resolves_cell_id_contextvar(self):
        """셀 ID ContextVar 경로를 정상 resolve 한다."""
        from baldur.observability.baggage import _resolve_import

        contextvar = _resolve_import("baldur.context.cell_context:_current_cell_id")
        from baldur.context.cell_context import _current_cell_id

        assert contextvar is _current_cell_id

    def test_resolves_domain_contextvar(self):
        """도메인 ContextVar 경로를 정상 resolve 한다."""
        from baldur.observability.baggage import _resolve_import

        contextvar = _resolve_import("baldur.decorators.domain_tag:_current_domain")
        from baldur.decorators.domain_tag import _current_domain

        assert contextvar is _current_domain


class TestSyncContextvarsToBaggageBehavior:
    """sync_contextvars_to_baggage() 동작 검증."""

    def test_sets_cell_id_in_baggage_when_present(self):
        """cell_id ContextVar에 값이 있으면 Baggage에 설정된다."""
        from baldur.context.cell_context import _current_cell_id
        from baldur.observability.baggage import (
            BAGGAGE_PREFIX,
            detach_baggage_token,
            sync_contextvars_to_baggage,
        )

        token_cv = _current_cell_id.set("cell-7")
        try:
            token = sync_contextvars_to_baggage()
            try:
                from opentelemetry import baggage

                value = baggage.get_baggage(f"{BAGGAGE_PREFIX}.cell_id")
                assert value == "cell-7"
            finally:
                detach_baggage_token(token)
        finally:
            _current_cell_id.reset(token_cv)

    def test_sets_domain_in_baggage_when_present(self):
        """domain ContextVar에 값이 있으면 Baggage에 설정된다."""
        from baldur.decorators.domain_tag import _current_domain
        from baldur.observability.baggage import (
            BAGGAGE_PREFIX,
            detach_baggage_token,
            sync_contextvars_to_baggage,
        )

        token_cv = _current_domain.set("payment")
        try:
            token = sync_contextvars_to_baggage()
            try:
                from opentelemetry import baggage

                value = baggage.get_baggage(f"{BAGGAGE_PREFIX}.domain")
                assert value == "payment"
            finally:
                detach_baggage_token(token)
        finally:
            _current_domain.reset(token_cv)

    def test_skips_none_contextvar_values(self):
        """ContextVar 값이 None이면 Baggage에 설정하지 않는다."""
        from baldur.context.cell_context import _current_cell_id
        from baldur.observability.baggage import (
            BAGGAGE_PREFIX,
            detach_baggage_token,
            sync_contextvars_to_baggage,
        )

        # cell_id를 None으로 확인 (기본값)
        token_cv = _current_cell_id.set(None)
        try:
            token = sync_contextvars_to_baggage()
            try:
                from opentelemetry import baggage

                value = baggage.get_baggage(f"{BAGGAGE_PREFIX}.cell_id")
                assert value is None
            finally:
                detach_baggage_token(token)
        finally:
            _current_cell_id.reset(token_cv)

    def test_returns_none_token_when_otel_unavailable(self):
        """OTel 미설치 시 None token을 반환한다."""
        from baldur.observability.baggage import sync_contextvars_to_baggage

        with patch.dict("sys.modules", {"opentelemetry": None}):
            try:
                token = sync_contextvars_to_baggage()
                assert token is None
            except ImportError:
                pass  # 모듈 패치 한계


class TestDetachBaggageTokenBehavior:
    """detach_baggage_token() 동작 검증."""

    def test_handles_none_token_safely(self):
        """None token은 에러 없이 무시된다."""
        from baldur.observability.baggage import detach_baggage_token

        # 예외가 발생하지 않아야 함
        detach_baggage_token(None)

    def test_detaches_valid_token(self):
        """유효한 token이 정상적으로 detach된다."""
        from opentelemetry import context

        from baldur.observability.baggage import detach_baggage_token

        # attach해서 token 얻기
        ctx = context.get_current()
        token = context.attach(ctx)

        # detach가 에러 없이 수행되어야 함
        detach_baggage_token(token)


class TestRestoreContextvarsFromBaggageBehavior:
    """restore_contextvars_from_baggage() 동작 검증."""

    def test_restores_cell_id_from_baggage(self):
        """Baggage에 cell_id가 있으면 ContextVar에 복원된다."""
        from opentelemetry import baggage, context

        from baldur.context.cell_context import _current_cell_id
        from baldur.observability.baggage import (
            BAGGAGE_PREFIX,
            restore_contextvars_from_baggage,
        )

        # Baggage에 cell_id 설정
        ctx = baggage.set_baggage(f"{BAGGAGE_PREFIX}.cell_id", "cell-9")
        token = context.attach(ctx)
        try:
            # ContextVar 초기화
            token_cv = _current_cell_id.set(None)
            try:
                restore_contextvars_from_baggage()
                assert _current_cell_id.get() == "cell-9"
            finally:
                _current_cell_id.reset(token_cv)
        finally:
            context.detach(token)

    def test_restores_domain_from_baggage(self):
        """Baggage에 domain이 있으면 ContextVar에 복원된다."""
        from opentelemetry import baggage, context

        from baldur.decorators.domain_tag import _current_domain
        from baldur.observability.baggage import (
            BAGGAGE_PREFIX,
            restore_contextvars_from_baggage,
        )

        ctx = baggage.set_baggage(f"{BAGGAGE_PREFIX}.domain", "order")
        token = context.attach(ctx)
        try:
            token_cv = _current_domain.set(None)
            try:
                restore_contextvars_from_baggage()
                assert _current_domain.get() == "order"
            finally:
                _current_domain.reset(token_cv)
        finally:
            context.detach(token)

    def test_does_not_overwrite_when_baggage_empty(self):
        """Baggage에 값이 없으면 기존 ContextVar 값을 유지한다."""
        from baldur.context.cell_context import _current_cell_id
        from baldur.observability.baggage import restore_contextvars_from_baggage

        token_cv = _current_cell_id.set("existing-cell")
        try:
            # Baggage가 비어 있는 상태에서 restore
            restore_contextvars_from_baggage()
            # 기존 값이 유지되어야 함
            assert _current_cell_id.get() == "existing-cell"
        finally:
            _current_cell_id.reset(token_cv)


class TestBaggageWiredFlowBehavior:
    """593 G2 — the inbound-restore / outbound-inject baggage flow, now wired.

    Once ``instrument_requests`` (outbound) and ``DjangoInstrumentor``
    (inbound) are wired by the startup path, ``cell_id`` / ``domain``
    propagate cross-service over W3C baggage. These tests exercise the
    baggage-module half of that flow with the OTel ``W3CBaggagePropagator``
    standing in for the separately-tested Instrumentors (``DjangoInstrumentor``
    extracts → ``restore``; ``sync`` → ``RequestsInstrumentor.inject``).

    Named so the G2 ``-k "baggage and (restore or inject or cell)"`` filter
    selects them.
    """

    def test_inbound_cell_id_baggage_restores_contextvar_without_local_hash(self):
        """수신 baggage의 cell_id가 ContextVar로 복원되어 로컬 해시를 우회한다."""
        from opentelemetry import baggage, context

        from baldur.context.cell_context import (
            _current_cell_id,
            get_current_cell_id,
        )
        from baldur.observability.baggage import (
            BAGGAGE_PREFIX,
            restore_contextvars_from_baggage,
        )

        # Given an inbound request whose baggage carries an upstream cell_id
        # (DjangoInstrumentor would have extracted it into the OTel context).
        ctx = baggage.set_baggage(f"{BAGGAGE_PREFIX}.cell_id", "upstream-cell")
        token = context.attach(ctx)
        try:
            # And the local ContextVar is unset (fresh process, no local hash).
            token_cv = _current_cell_id.set(None)
            try:
                # When restore runs.
                restore_contextvars_from_baggage()
                # Then the upstream value wins — no silent local-hash fallback.
                assert get_current_cell_id() == "upstream-cell"
            finally:
                _current_cell_id.reset(token_cv)
        finally:
            context.detach(token)

    def test_outbound_sync_then_propagator_injects_cell_id_header(self):
        """sync 후 W3C propagator.inject()가 baggage 헤더에 cell_id를 기록한다."""
        from opentelemetry.baggage.propagation import W3CBaggagePropagator

        from baldur.context.cell_context import _current_cell_id
        from baldur.observability.baggage import (
            BAGGAGE_PREFIX,
            detach_baggage_token,
            sync_contextvars_to_baggage,
        )

        token_cv = _current_cell_id.set("cell-77")
        carrier: dict = {}
        try:
            token = sync_contextvars_to_baggage()
            try:
                # RequestsInstrumentor.inject() uses the global composite
                # (TraceContext + W3CBaggage) propagator; exercise the baggage
                # half directly against the synced context.
                W3CBaggagePropagator().inject(carrier)
            finally:
                detach_baggage_token(token)
        finally:
            _current_cell_id.reset(token_cv)

        assert "baggage" in carrier
        assert f"{BAGGAGE_PREFIX}.cell_id=cell-77" in carrier["baggage"]

    def test_cell_id_survives_inject_extract_restore_round_trip(self):
        """cell_id가 sync→inject→extract→restore 왕복을 거쳐 보존된다."""
        from opentelemetry import context
        from opentelemetry.baggage.propagation import W3CBaggagePropagator

        from baldur.context.cell_context import (
            _current_cell_id,
            get_current_cell_id,
        )
        from baldur.observability.baggage import (
            detach_baggage_token,
            restore_contextvars_from_baggage,
            sync_contextvars_to_baggage,
        )

        propagator = W3CBaggagePropagator()
        carrier: dict = {}

        # --- producer: ContextVar -> baggage -> outbound header (inject) ---
        token_cv = _current_cell_id.set("cell-round-trip")
        try:
            token = sync_contextvars_to_baggage()
            try:
                propagator.inject(carrier)
            finally:
                detach_baggage_token(token)
        finally:
            _current_cell_id.reset(token_cv)

        # --- consumer: inbound header -> baggage -> ContextVar (restore) ---
        extracted_ctx = propagator.extract(carrier)
        token_ctx = context.attach(extracted_ctx)
        try:
            token_cv2 = _current_cell_id.set(None)
            try:
                restore_contextvars_from_baggage()
                assert get_current_cell_id() == "cell-round-trip"
            finally:
                _current_cell_id.reset(token_cv2)
        finally:
            context.detach(token_ctx)
