"""
Health Probe 테스트.

HealthProbeManager 및 각종 Probe 테스트.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.meta.config import get_meta_watchdog_settings
from baldur.meta.health_probe import (
    CircuitBreakerProbe,
    DLQProbe,
    HealthProbe,
    HealthProbeManager,
    HealthStatus,
    ProbeResult,
    RecoveryPipelineProbe,
    RedisProbe,
)
from baldur.settings.health_check import get_health_check_settings
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time


class TestHealthStatus:
    """HealthStatus 열거형 테스트."""

    def test_values(self):
        """값 확인."""
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.UNHEALTHY.value == "unhealthy"
        assert HealthStatus.UNKNOWN.value == "unknown"


class TestProbeResult:
    """ProbeResult 데이터클래스 테스트."""

    def test_creation(self):
        """생성 테스트."""
        result = ProbeResult(
            component="test",
            status=HealthStatus.HEALTHY,
            latency_ms=5.0,
            timestamp=datetime.now(UTC),
        )

        assert result.component == "test"
        assert result.status == HealthStatus.HEALTHY
        assert result.latency_ms == 5.0
        assert result.details == {}
        assert result.error is None

    def test_with_error(self):
        """에러 포함 생성 테스트."""
        result = ProbeResult(
            component="test",
            status=HealthStatus.UNHEALTHY,
            latency_ms=10.0,
            timestamp=datetime.now(UTC),
            error="Connection failed",
        )

        assert result.status == HealthStatus.UNHEALTHY
        assert result.error == "Connection failed"


class TestCircuitBreakerProbe:
    """CircuitBreakerProbe 테스트."""

    def test_component_name(self):
        """컴포넌트 이름 확인."""
        probe = CircuitBreakerProbe()
        assert probe.component_name == "circuit_breaker"

    @patch("baldur.services.circuit_breaker.get_circuit_breaker_service")
    def test_probe_returns_result(self, mock_cb_service):
        """프로브가 결과 반환."""
        mock_cb_service.return_value.get_all_states.return_value = []
        probe = CircuitBreakerProbe()
        result = probe.probe()

        assert isinstance(result, ProbeResult)
        assert result.component == "circuit_breaker"
        assert result.latency_ms >= 0

    def test_probe_open_breakers_over_threshold_degraded(self):
        """Canonical lowercase "open" states are counted; over threshold → DEGRADED."""
        threshold = get_health_check_settings().probe_cb_open_threshold
        mock_service = MagicMock()

        with freeze_time(_FROZEN_INSTANT):
            opened_at = utc_now() - timedelta(seconds=10)
            mock_service.get_all_states.return_value = [
                {"state": "open", "opened_at": opened_at} for _ in range(threshold + 1)
            ]
            with patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=mock_service,
            ):
                result = CircuitBreakerProbe().probe()

        assert result.status == HealthStatus.DEGRADED
        assert result.details["open_count"] == threshold + 1
        assert result.details["stuck_count"] == 0


class TestDLQProbe:
    """DLQProbe 테스트."""

    def test_component_name(self):
        """컴포넌트 이름 확인."""
        probe = DLQProbe()
        assert probe.component_name == "dlq"

    def test_probe_returns_result(self):
        """프로브가 결과 반환."""
        probe = DLQProbe()
        result = probe.probe()

        assert isinstance(result, ProbeResult)
        assert result.component == "dlq"


class TestRecoveryPipelineProbe:
    """RecoveryPipelineProbe 테스트."""

    def test_component_name(self):
        """컴포넌트 이름 확인."""
        probe = RecoveryPipelineProbe()
        assert probe.component_name == "recovery_pipeline"

    def test_probe_returns_result(self):
        """프로브가 결과 반환."""
        pytest.importorskip("baldur_pro")
        with patch(
            "baldur_pro.services.coordination.recovery_coordinator.get_recovery_coordinator"
        ):
            probe = RecoveryPipelineProbe()
            result = probe.probe()

        assert isinstance(result, ProbeResult)
        assert result.component == "recovery_pipeline"


class TestRedisProbe:
    """RedisProbe 테스트."""

    def test_component_name(self):
        """컴포넌트 이름 확인."""
        probe = RedisProbe()
        assert probe.component_name == "redis"

    @pytest.fixture
    def mock_redis_adapter(self):
        """레디스 어댑터를 mock하여 실제 Redis 연결 방지."""
        mock_adapter = MagicMock()
        mock_adapter._redis = MagicMock()
        mock_adapter._redis.ping.return_value = True
        mock_adapter._redis.info.return_value = {"used_memory": 1024}
        with patch(
            "baldur.adapters.cache.redis_adapter.RedisCacheAdapter",
            return_value=mock_adapter,
        ) as m:
            yield m

    def test_probe_returns_result(self, mock_redis_adapter):
        """프로브가 결과 반환 (Redis 없어도)."""
        probe = RedisProbe()
        result = probe.probe()

        assert isinstance(result, ProbeResult)
        assert result.component == "redis"


class DummyHealthyProbe(HealthProbe):
    """테스트용 Healthy 프로브."""

    @property
    def component_name(self) -> str:
        return "dummy_healthy"

    def probe(self) -> ProbeResult:
        return ProbeResult(
            component=self.component_name,
            status=HealthStatus.HEALTHY,
            latency_ms=1.0,
            timestamp=datetime.now(UTC),
        )


class DummyUnhealthyProbe(HealthProbe):
    """테스트용 Unhealthy 프로브."""

    @property
    def component_name(self) -> str:
        return "dummy_unhealthy"

    def probe(self) -> ProbeResult:
        return ProbeResult(
            component=self.component_name,
            status=HealthStatus.UNHEALTHY,
            latency_ms=1.0,
            timestamp=datetime.now(UTC),
            error="Simulated failure",
        )


class DummyDegradedProbe(HealthProbe):
    """테스트용 Degraded 프로브."""

    @property
    def component_name(self) -> str:
        return "dummy_degraded"

    def probe(self) -> ProbeResult:
        return ProbeResult(
            component=self.component_name,
            status=HealthStatus.DEGRADED,
            latency_ms=1.0,
            timestamp=datetime.now(UTC),
        )


class TestHealthProbeManager:
    """HealthProbeManager 테스트."""

    def test_default_probes(self):
        """기본 프로브 생성 테스트."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur_pro.services.coordination.recovery_coordinator.get_recovery_coordinator"
            ),
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service"
            ) as mock_cb_service,
            patch(
                "baldur.adapters.cache.redis_adapter.RedisCacheAdapter"
            ) as mock_redis_adapter,
        ):
            mock_cb_service.return_value.get_all_states.return_value = []
            mock_adapter = MagicMock()
            mock_adapter._redis.ping.return_value = True
            mock_adapter._redis.info.return_value = {"used_memory": 1024}
            mock_redis_adapter.return_value = mock_adapter

            manager = HealthProbeManager()

            # 기본 프로브가 있어야 함
            results = manager.probe_all()
            assert len(results) > 0

    def test_custom_probes(self):
        """커스텀 프로브 테스트."""
        probes = [DummyHealthyProbe(), DummyUnhealthyProbe()]
        manager = HealthProbeManager(probes=probes)

        results = manager.probe_all()

        assert "dummy_healthy" in results
        assert "dummy_unhealthy" in results

    def test_add_probe(self):
        """프로브 추가 테스트."""
        manager = HealthProbeManager(probes=[])

        manager.add_probe(DummyHealthyProbe())

        results = manager.probe_all()
        assert "dummy_healthy" in results

    def test_remove_probe(self):
        """프로브 제거 테스트."""
        manager = HealthProbeManager(probes=[DummyHealthyProbe()])

        removed = manager.remove_probe("dummy_healthy")

        assert removed is True
        results = manager.probe_all()
        assert "dummy_healthy" not in results

    def test_remove_nonexistent_probe(self):
        """존재하지 않는 프로브 제거 테스트."""
        manager = HealthProbeManager(probes=[])

        removed = manager.remove_probe("nonexistent")

        assert removed is False

    def test_probe_all_returns_results(self):
        """probe_all이 결과 반환."""
        manager = HealthProbeManager(probes=[DummyHealthyProbe()])

        results = manager.probe_all()

        assert isinstance(results, dict)
        assert "dummy_healthy" in results
        assert results["dummy_healthy"].status == HealthStatus.HEALTHY

    def test_get_overall_status_healthy(self):
        """전체 상태: 모두 healthy."""
        manager = HealthProbeManager(probes=[DummyHealthyProbe()])
        manager.probe_all()

        status = manager.get_overall_status()

        assert status == HealthStatus.HEALTHY

    def test_get_overall_status_unhealthy(self):
        """전체 상태: 하나라도 unhealthy."""
        manager = HealthProbeManager(
            probes=[DummyHealthyProbe(), DummyUnhealthyProbe()]
        )
        manager.probe_all()

        status = manager.get_overall_status()

        assert status == HealthStatus.UNHEALTHY

    def test_get_overall_status_degraded(self):
        """전체 상태: degraded."""
        manager = HealthProbeManager(probes=[DummyHealthyProbe(), DummyDegradedProbe()])
        manager.probe_all()

        status = manager.get_overall_status()

        assert status == HealthStatus.DEGRADED

    def test_get_overall_status_unknown(self):
        """전체 상태: 결과 없음."""
        manager = HealthProbeManager(probes=[])

        status = manager.get_overall_status()

        assert status == HealthStatus.UNKNOWN

    def test_get_last_results(self):
        """마지막 결과 조회 테스트."""
        manager = HealthProbeManager(probes=[DummyHealthyProbe()])

        # 프로브 전에는 빈 dict
        assert manager.get_last_results() == {}

        manager.probe_all()

        results = manager.get_last_results()
        assert "dummy_healthy" in results

    def test_get_component_status(self):
        """특정 컴포넌트 상태 조회 테스트."""
        manager = HealthProbeManager(probes=[DummyHealthyProbe()])
        manager.probe_all()

        status = manager.get_component_status("dummy_healthy")

        assert status == HealthStatus.HEALTHY

    def test_get_component_status_not_found(self):
        """존재하지 않는 컴포넌트 상태 조회."""
        manager = HealthProbeManager(probes=[])
        manager.probe_all()

        status = manager.get_component_status("nonexistent")

        assert status is None

    def test_is_running(self):
        """실행 상태 확인 테스트."""
        manager = HealthProbeManager(probes=[])

        assert manager.is_running() is False

        manager.start()
        assert manager.is_running() is True

        manager.stop()
        assert manager.is_running() is False


# =============================================================================
# StuckDetector wiring into Meta-Watchdog probes (DLQ variance + CB duration)
# =============================================================================

# Frozen instant for deterministic OPEN-duration math: freeze_time pins both the
# probe's utc_now() and the opened_at built relative to it, so the >= boundary is
# exact (the at-threshold case is the load-bearing one).
_FROZEN_INSTANT = "2026-06-13 12:00:00"

# Aged opened_at timestamps for the CircuitBreakerProbe guard cases. Years before
# any test "now", so an OPEN breaker carrying _AGED_AWARE would itself be stuck —
# proving the guard cases are skipped for the right reason (wrong state /
# non-datetime / naive), not because the duration was short.
_AGED_AWARE = datetime(2020, 1, 1, tzinfo=UTC)
_AGED_NAIVE = datetime(2020, 1, 1)


@contextmanager
def _dlq_pending(pending_count: int):
    """Drive DLQProbe.probe() with a fixed runtime pending_count.

    DLQProbe reads pending_count via the ``has_runtime_adapter`` /
    ``get_runtime`` duck-type on ProviderRegistry (undeclared in OSS, where the
    probe falls open to 0). Inject both as the probe expects so a test can pin
    the queue depth across repeated probe() cycles. ``create=True`` adds the
    attributes for the patch lifetime and removes them on exit.
    """
    from baldur.factory import ProviderRegistry

    fake_runtime = MagicMock()
    fake_runtime.count_pending.return_value = pending_count
    with (
        patch.object(
            ProviderRegistry, "has_runtime_adapter", create=True, return_value=True
        ),
        patch.object(
            ProviderRegistry, "get_runtime", create=True, return_value=fake_runtime
        ),
    ):
        yield


class TestCircuitBreakerStuckDetection:
    """CircuitBreakerProbe OPEN-duration stuck detection.

    Behavior verification: the stuck threshold and the >= duration rule are read
    from source (``get_meta_watchdog_settings().stuck_threshold_seconds``), never
    hardcoded, so a settings-default change does not break these tests.
    """

    @pytest.mark.parametrize(
        ("offset_seconds", "expected_count"),
        [
            (10, 1),  # aged well past threshold → counted
            (1, 1),  # just over threshold → counted
            (0, 1),  # exactly at threshold (>= is inclusive) → counted
            (-1, 0),  # just under threshold → not counted
            (-240, 0),  # cycling breaker (re-opened recently) → not counted
        ],
        ids=["aged", "just_over", "at_threshold", "just_under", "cycling"],
    )
    def test_count_stuck_open_breakers_duration_boundary(
        self, offset_seconds, expected_count
    ):
        """OPEN duration is stuck iff it reaches stuck_threshold_seconds (>=)."""
        threshold = get_meta_watchdog_settings().stuck_threshold_seconds

        with freeze_time(_FROZEN_INSTANT):
            opened_at = utc_now() - timedelta(seconds=threshold + offset_seconds)
            cb_states = [{"state": "open", "opened_at": opened_at}]

            count = CircuitBreakerProbe._count_stuck_open_breakers(cb_states)

        assert count == expected_count

    @pytest.mark.parametrize(
        "state",
        [
            {"state": "closed", "opened_at": _AGED_AWARE},
            {"state": "half_open", "opened_at": _AGED_AWARE},
            {"state": "OPEN", "opened_at": _AGED_AWARE},
            {"state": "open", "opened_at": None},
            {"state": "open"},
            {"state": "open", "opened_at": _AGED_NAIVE},
            {"state": "open", "opened_at": "2020-01-01"},
            {"state": "open", "opened_at": 1577836800},
        ],
        ids=[
            "closed_ignored",
            "half_open_ignored",
            "uppercase_not_canonical",
            "opened_at_none",
            "opened_at_missing",
            "opened_at_naive",
            "opened_at_string",
            "opened_at_int",
        ],
    )
    def test_count_stuck_open_breakers_ignores_invalid_states(self, state):
        """Non-open states and None/naive/garbage opened_at are skipped (fail-safe)."""
        assert CircuitBreakerProbe._count_stuck_open_breakers([state]) == 0

    def test_count_stuck_open_breakers_mixed_states_counts_only_aged_open(self):
        """One stuck breaker among cycling/closed ones is counted exactly once."""
        threshold = get_meta_watchdog_settings().stuck_threshold_seconds

        with freeze_time(_FROZEN_INSTANT):
            now = utc_now()
            cb_states = [
                {"state": "open", "opened_at": now - timedelta(seconds=threshold + 30)},
                {"state": "open", "opened_at": now - timedelta(seconds=10)},
                {"state": "closed", "opened_at": None},
            ]

            count = CircuitBreakerProbe._count_stuck_open_breakers(cb_states)

        assert count == 1

    def test_probe_sustained_open_breaker_returns_unhealthy(self):
        """A breaker held open past the threshold revives stuck_count → UNHEALTHY."""
        # Given — one breaker open well past the stuck threshold
        threshold = get_meta_watchdog_settings().stuck_threshold_seconds
        mock_service = MagicMock()

        with freeze_time(_FROZEN_INSTANT):
            opened_at = utc_now() - timedelta(seconds=threshold + 60)
            mock_service.get_all_states.return_value = [
                {"state": "open", "opened_at": opened_at}
            ]

            # When — the probe runs against the mocked CB service
            with patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=mock_service,
            ):
                result = CircuitBreakerProbe().probe()

        # Then — the revived stuck_count branch drives UNHEALTHY
        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["open_count"] == 1
        assert result.details["stuck_count"] == 1
        assert "stuck" in result.reason.lower()

    def test_probe_recently_opened_breaker_not_unhealthy(self):
        """A freshly (re-)opened breaker is below the threshold → not stuck."""
        mock_service = MagicMock()

        with freeze_time(_FROZEN_INSTANT):
            opened_at = utc_now() - timedelta(seconds=10)
            mock_service.get_all_states.return_value = [
                {"state": "open", "opened_at": opened_at}
            ]
            with patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=mock_service,
            ):
                result = CircuitBreakerProbe().probe()

        assert result.status != HealthStatus.UNHEALTHY
        assert result.details["stuck_count"] == 0

    def test_probe_get_all_states_error_keeps_level_verdict_not_unknown(self):
        """get_all_states() raising falls open to stuck_count=0 and keeps the verdict."""
        # Given — the CB manager lookup raises inside the inner try
        mock_service = MagicMock()
        mock_service.get_all_states.side_effect = RuntimeError("cb manager down")

        # When
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            result = CircuitBreakerProbe().probe()

        # Then — inner try absorbed it: not UNKNOWN, stuck_count=0, error surfaced
        assert result.status != HealthStatus.UNKNOWN
        assert result.status == HealthStatus.HEALTHY
        assert result.details["stuck_count"] == 0
        assert result.details["states"]["manager_error"] == "cb manager down"


class TestDLQStuckDetection:
    """DLQProbe zero-variance stuck detection via the StuckDetector.

    Behavior verification: thresholds come from
    ``get_meta_watchdog_settings().dlq_stuck_threshold_entries``; the >=5-sample
    and variance/error-rate rules are exercised through real probe() cycles (the
    autouse ``_reset_stuck_detector`` conftest fixture gives each test an empty
    detector singleton).
    """

    def test_probe_pinned_at_threshold_over_five_cycles_returns_unhealthy(self):
        """A queue pinned at exactly the threshold (variance 0, backlogged) → UNHEALTHY.

        Flagship "1,000 pending that never drains": the ``>`` level logic leaves
        it HEALTHY (``1000 > 1000`` is False), but the ``>=`` error gate marks
        every sample errored, so >=5 zero-variance samples trip the detector.
        """
        threshold = get_meta_watchdog_settings().dlq_stuck_threshold_entries
        probe = DLQProbe()

        with _dlq_pending(threshold):
            results = [probe.probe() for _ in range(5)]

        # 1st cycle: a single sample (<5) cannot be stuck → level verdict (HEALTHY)
        assert results[0].status != HealthStatus.UNHEALTHY
        # 5th cycle: 5 zero-variance errored samples → stuck → UNHEALTHY
        assert results[-1].status == HealthStatus.UNHEALTHY
        assert "variance" in results[-1].reason

    @pytest.mark.parametrize(
        ("delta", "expected_unhealthy"),
        [
            (-1, False),  # threshold-1: error gate False (< threshold) → never trips
            (0, True),  # threshold: level HEALTHY but error gate True → variance trips
            (1, True),  # threshold+1: level DEGRADED + error gate True → trips
        ],
        ids=["below_threshold", "at_threshold", "above_threshold"],
    )
    def test_probe_pinned_pending_threshold_boundary(self, delta, expected_unhealthy):
        """The >= error gate vs > level asymmetry decides the boundary case."""
        threshold = get_meta_watchdog_settings().dlq_stuck_threshold_entries
        probe = DLQProbe()

        with _dlq_pending(threshold + delta):
            results = [probe.probe() for _ in range(5)]

        is_unhealthy = results[-1].status == HealthStatus.UNHEALTHY
        assert is_unhealthy is expected_unhealthy
        if expected_unhealthy:
            assert "variance" in results[-1].reason

    def test_probe_pinned_pending_under_five_cycles_not_yet_stuck(self):
        """Fewer than 5 samples cannot be stuck (MetricWindow >=5-sample guard)."""
        threshold = get_meta_watchdog_settings().dlq_stuck_threshold_entries
        probe = DLQProbe()

        with _dlq_pending(threshold):
            results = [probe.probe() for _ in range(4)]

        assert results[-1].status != HealthStatus.UNHEALTHY
        assert "variance" not in results[-1].reason

    def test_probe_varying_pending_above_threshold_not_variance_stuck(self):
        """A backlogged but varying queue has variance >> 0 → not flagged stuck."""
        threshold = get_meta_watchdog_settings().dlq_stuck_threshold_entries
        probe = DLQProbe()

        # 6 distinct values, all >= threshold but well under 2x threshold so the
        # level logic never independently reaches UNHEALTHY.
        result = None
        for i in range(6):
            with _dlq_pending(threshold + i * 5):
                result = probe.probe()

        assert result.status != HealthStatus.UNHEALTHY
        assert "variance" not in result.reason

    def test_probe_pinned_pending_below_threshold_not_stuck(self):
        """A healthy low queue pinned with zero variance is excluded by the error gate."""
        threshold = get_meta_watchdog_settings().dlq_stuck_threshold_entries
        low_value = threshold // 2
        probe = DLQProbe()

        with _dlq_pending(low_value):
            results = [probe.probe() for _ in range(5)]

        assert results[-1].status != HealthStatus.UNHEALTHY
        assert "variance" not in results[-1].reason

    def test_probe_detector_error_keeps_level_verdict_and_surfaces_error(self):
        """A detector fault is fail-open but NOT silent: level verdict kept + surfaced."""
        # Given — a backlogged queue (level → DEGRADED) and a detector that raises
        threshold = get_meta_watchdog_settings().dlq_stuck_threshold_entries
        probe = DLQProbe()

        # When
        with _dlq_pending(threshold + 1):
            with patch(
                "baldur.meta.stuck_detector.get_stuck_detector",
                side_effect=RuntimeError("detector boom"),
            ):
                result = probe.probe()

        # Then — level verdict preserved (never UNKNOWN) AND the fault is surfaced
        assert result.status == HealthStatus.DEGRADED
        assert result.status != HealthStatus.UNKNOWN
        assert result.details["stuck_detection_error"] == "detector boom"
