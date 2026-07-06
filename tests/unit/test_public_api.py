"""baldur package public API (__init__.py) unit tests.

Verification targets:
- __all__: exactly 14 public symbols (contract)
- Lazy import (PEP 562): __getattr__ deferred loading and caching behavior
- Eager import: CircuitState, FailedOperationData available immediately
- Backward compatibility: existing deep-path imports remain valid
- Lazy import isolation: importing only CircuitState does not load heavy modules
- No side-effect: import does not call configure_structlog()
- py.typed: PEP 561 marker file present
- reset_structlog_config(): _configured flag state transition
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# =============================================================================
# 계약 검증: __all__ 완전성, py.typed 존재
# =============================================================================


class TestPublicApiContract:
    """baldur 패키지 공개 API 설계 계약 검증."""

    def test_all_exports_exactly_contracted_symbols(self):
        """__all__ must declare exactly the contracted public symbols.

        History:
        - 14 → 22: 429/PR1 added the resilience facade (``protect`` /
          ``aprotect`` / ``protected`` / ``aprotected`` /
          ``protect_with_meta`` / ``aprotect_with_meta`` / ``ProtectResult``)
          and ``get_scheduler``.
        - 22 → 23: 429/PR2 added ``sql_transaction`` for cross-repo
          transactions against the framework-free SQL adapter.
        - 23 → 25: 429/PR3 added ``start_admin_server`` /
          ``stop_admin_server`` for the framework-free admin HTTP server.
        - 25 → 27: 429/PR4 added ``fastapi_lifespan`` and ``init_flask``
          framework-extra entry points.
        - 27 → 30: 508 D3/D4/D6 dropped ``protect_with_meta`` /
          ``aprotect_with_meta`` / ``ProtectResult`` / ``get_scheduler``
          (-4) and added 7 base/leaf exceptions reachable from the
          top-level API (``AdapterError``, ``DLQError``, ``ResilienceError``,
          ``TimeoutPolicyError``, ``RateLimitExceeded``,
          ``IdempotencyDuplicateError``) plus the renamed
          ``get_leader_scheduler``.
        - 30 → 31: 545 D1 added ``DomainValidationError`` as a top-level
          re-export because ``@domain_tag`` raises it at decoration time
          (leaf raised by a top-level public surface — 508 D6 rule).
        - 31 → 32: 567 D9 added ``IdempotencyUnavailableError`` as a top-level
          re-export — a leaf raised on a fail-closed cache error by the
          ``protect(idempotency_key=)`` / ``@idempotent`` top-level surfaces
          (508 D6 rule, sibling of ``IdempotencyDuplicateError``).
        """
        import baldur

        expected = {
            "__version__",
            "init",
            # Resilience facade marquee (508 D3)
            "protect",
            "aprotect",
            "protected",
            "aprotected",
            # Scheduler (508 D4)
            "get_leader_scheduler",
            # SQL storage (429 Part 4)
            "sql_transaction",
            # Admin server (429 Part 2 / PR3)
            "start_admin_server",
            "stop_admin_server",
            # Framework extras (429 Part 3 / PR4)
            "fastapi_lifespan",
            "init_flask",
            "CircuitState",
            "FailedOperationData",
            "ProviderRegistry",
            "get_circuit_breaker_service",
            "ReplayService",
            # Exceptions (508 D6)
            "BaldurError",
            "AdapterError",
            "AdapterNotFoundError",
            "CircuitBreakerError",
            "DLQError",
            "DLQReplayError",
            "ResilienceError",
            "RetryExhaustedError",
            "TimeoutPolicyError",
            "RateLimitExceeded",
            "IdempotencyDuplicateError",
            "IdempotencyUnavailableError",
            "DomainValidationError",
            "ConfigurationError",
        }
        assert set(baldur.__all__) == expected
        assert len(baldur.__all__) == len(expected)

    def test_py_typed_marker_exists_for_pep561(self):
        """PEP 561 py.typed 마커 파일이 패키지 루트에 존재해야 한다."""
        import baldur

        package_dir = Path(baldur.__file__).resolve().parent
        py_typed = package_dir / "py.typed"
        assert py_typed.exists(), f"py.typed not found at {py_typed}"


# =============================================================================
# 동작 검증: Lazy import, Eager import
# =============================================================================


class TestPublicApiBehavior:
    """baldur 패키지 공개 API lazy/eager import 동작 검증."""

    def test_all_covers_every_lazy_and_eager_export(self):
        """__all__이 _LAZY_IMPORTS 키 + eager export 를 빠짐없이 포함해야 한다."""
        import baldur

        expected = set(baldur._LAZY_IMPORTS.keys()) | {
            "__version__",
            "CircuitState",
            "FailedOperationData",
        }
        assert set(baldur.__all__) == expected

    def test_eager_imports_available_directly_in_module_namespace(self):
        """CircuitState와 FailedOperationData는 모듈 dict에 즉시 존재한다."""
        import baldur

        module_dict = vars(baldur)
        assert "CircuitState" in module_dict
        assert "FailedOperationData" in module_dict

    def test_lazy_import_resolves_to_actual_source_class(self):
        """lazy import된 BaldurError가 소스 모듈의 원본 클래스와 동일 객체이다."""
        import baldur
        from baldur.core.exceptions import BaldurError as SourceClass

        assert baldur.BaldurError is SourceClass

    def test_lazy_import_caches_in_module_globals_after_first_access(self):
        """lazy import 첫 접근 후 모듈 globals에 캐싱되어 __getattr__을 우회한다."""
        import baldur

        # Given — globals에서 제거하여 미캐싱 상태 재현
        baldur.__dict__.pop("ProviderRegistry", None)
        assert "ProviderRegistry" not in vars(baldur)

        # When — 첫 접근으로 __getattr__ 트리거
        _ = baldur.ProviderRegistry

        # Then — globals에 캐싱됨
        assert "ProviderRegistry" in vars(baldur)

    def test_getattr_unknown_name_raises_attribute_error(self):
        """_LAZY_IMPORTS에 없는 이름 접근 시 모듈명이 포함된 AttributeError 발생."""
        import baldur

        with pytest.raises(
            AttributeError, match=r"has no attribute 'NonExistentSymbol'"
        ):
            _ = baldur.NonExistentSymbol

    def test_deep_path_import_backward_compatible(self):
        """기존 깊은 경로 import가 공개 API와 동일 객체를 반환하여 하위 호환성을 유지한다."""
        import baldur
        from baldur.factory import ProviderRegistry as DeepProviderRegistry
        from baldur.interfaces.repositories import (
            FailedOperationData as DeepFailedOperationData,
        )
        from baldur.services import (
            get_circuit_breaker_service as deep_get_cb,
        )
        from baldur.services.replay_service import (
            ReplayService as DeepReplayService,
        )

        assert baldur.ProviderRegistry is DeepProviderRegistry
        assert baldur.FailedOperationData is DeepFailedOperationData
        assert baldur.get_circuit_breaker_service is deep_get_cb
        assert baldur.ReplayService is DeepReplayService

    def test_wildcard_import_matches_all(self):
        """`from baldur import *` resolves to exactly the names in ``__all__``.

        Strict-surface contract (508 D13, S9): catches both internal-symbol
        leakage into the wildcard namespace and ``__all__`` ↔ ``_LAZY_IMPORTS``
        drift. Executes the wildcard in a fresh namespace dict so the test
        does not pollute its own module globals.
        """
        import baldur

        namespace: dict[str, object] = {}
        exec("from baldur import *", namespace)  # noqa: S102 — exec is the contract
        # `exec` always injects __builtins__; everything else came from __all__.
        resolved = {name for name in namespace if name != "__builtins__"}
        assert resolved == set(baldur.__all__)

    def test_lazy_import_does_not_load_heavy_modules_until_accessed(self, monkeypatch):
        """CircuitState만 사용 시 factory, services 모듈이 로드되지 않아야 한다."""
        import baldur

        # Given — lazy 모듈 캐시를 제거하여 미로드 상태 재현
        for name in list(baldur.__dict__):
            if name in baldur._LAZY_IMPORTS:
                baldur.__dict__.pop(name, None)
        monkeypatch.delitem(sys.modules, "baldur.factory", raising=False)
        monkeypatch.delitem(
            sys.modules, "baldur.services.replay_service", raising=False
        )

        # When — eager import만 접근
        _ = baldur.CircuitState

        # Then — heavy 모듈이 sys.modules에 로드되지 않음
        assert "baldur.factory" not in sys.modules
        assert "baldur.services.replay_service" not in sys.modules


# =============================================================================
# 동작 검증: Side-effect 부재
# =============================================================================


class TestPublicApiSideEffectBehavior:
    """baldur 패키지 import 시 부수효과 부재 검증."""

    @pytest.fixture(autouse=True)
    def _reset_structlog(self):
        """structlog 설정 플래그를 테스트 전후로 리셋."""
        from baldur.observability import structlog_config

        structlog_config.reset_structlog_config()
        yield
        structlog_config.reset_structlog_config()

    def test_import_baldur_does_not_trigger_structlog_configure(self):
        """baldur 패키지 import가 configure_structlog() 부수효과를 유발하지 않는다."""
        # When — 패키지 리로드 (모듈 레벨 코드 재실행)
        import baldur
        from baldur.observability import structlog_config

        importlib.reload(baldur)

        # Then — configured가 False → configure_structlog()이 호출되지 않았음
        assert structlog_config._structlog_state().configured is False


# =============================================================================
# 동작 검증: reset_structlog_config() 상태 전이
# =============================================================================


class TestResetStructlogConfigBehavior:
    """reset_structlog_config() 상태 전이 동작 검증."""

    @pytest.fixture(autouse=True)
    def _reset_structlog(self):
        """structlog 설정 플래그를 테스트 후 복원."""
        yield
        from baldur.observability import structlog_config

        structlog_config.reset_structlog_config()

    def test_reset_structlog_config_transitions_configured_true_to_false(self):
        """configured=True → reset_structlog_config() → configured=False 상태 전이."""
        from baldur.observability import structlog_config

        # Given — 설정 완료 상태
        structlog_config._structlog_state().configured = True

        # When
        structlog_config.reset_structlog_config()

        # Then
        assert structlog_config._structlog_state().configured is False
