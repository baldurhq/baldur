"""
RecoveryInfrastructureAdapter tests (OSS surface).

Per impl doc 528 D10-v2 / D15, the ``KubernetesRecoveryAdapter`` test classes
live in ``tests/dormant/unit/meta/test_k8s_recovery_adapter.py`` alongside
the relocated source. This module keeps the OSS-side base/Docker/NoOp tests
and the K8s-agnostic factory tests.
"""

from datetime import UTC
from unittest import mock

from baldur.meta.recovery_adapter import (
    DockerComposeRecoveryAdapter,
    NoOpRecoveryAdapter,
    RecoveryAction,
    RecoveryResult,
    get_recovery_adapter,
)


class TestRecoveryAction:
    """RecoveryAction 열거형 테스트."""

    def test_values(self):
        """값 확인."""
        assert RecoveryAction.RESTART_WORKER.value == "restart_worker"
        assert RecoveryAction.SCALE_DEPLOYMENT.value == "scale_deployment"
        assert RecoveryAction.DELETE_POD.value == "delete_pod"
        assert RecoveryAction.RESET_CONNECTION.value == "reset_connection"


class TestRecoveryResult:
    """RecoveryResult 데이터클래스 테스트."""

    def test_creation(self):
        """생성 테스트."""
        from datetime import datetime

        result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="celery-worker",
            message="Restarted successfully",
            timestamp=datetime.now(UTC),
        )

        assert result.action == RecoveryAction.RESTART_WORKER
        assert result.success is True
        assert result.target == "celery-worker"

    def test_with_details(self):
        """상세 정보 포함 테스트."""
        from datetime import datetime

        result = RecoveryResult(
            action=RecoveryAction.SCALE_DEPLOYMENT,
            success=True,
            target="api",
            message="Scaled",
            timestamp=datetime.now(UTC),
            details={"replicas": 3},
        )

        assert result.details["replicas"] == 3


class TestNoOpRecoveryAdapter:
    """NoOpRecoveryAdapter 테스트."""

    def test_is_available(self):
        """사용 가능 여부 테스트."""
        adapter = NoOpRecoveryAdapter()
        assert adapter.is_available() is True

    def test_restart_worker(self):
        """restart_worker 테스트."""
        adapter = NoOpRecoveryAdapter()
        result = adapter.restart_worker("test-worker")

        assert isinstance(result, RecoveryResult)
        assert result.success is True
        assert result.action == RecoveryAction.RESTART_WORKER
        assert "No-op" in result.message

    def test_scale_deployment(self):
        """scale_deployment 테스트."""
        adapter = NoOpRecoveryAdapter()
        result = adapter.scale_deployment("test-deployment", 3)

        assert isinstance(result, RecoveryResult)
        assert result.success is True
        assert result.action == RecoveryAction.SCALE_DEPLOYMENT

    def test_delete_pod(self):
        """delete_pod 테스트."""
        adapter = NoOpRecoveryAdapter()
        result = adapter.delete_pod("test-pod", "default")

        assert isinstance(result, RecoveryResult)
        assert result.success is True
        assert result.action == RecoveryAction.DELETE_POD


class TestDockerComposeRecoveryAdapter:
    """DockerComposeRecoveryAdapter 테스트."""

    def test_is_available(self):
        """사용 가능 여부 테스트."""
        adapter = DockerComposeRecoveryAdapter()
        # docker-compose 또는 docker 명령어 유무에 따라 결과 다름
        assert isinstance(adapter.is_available(), bool)

    def test_restart_worker_success(self):
        """restart_worker 성공 테스트 (Mock)."""
        adapter = DockerComposeRecoveryAdapter()

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="Container restarted",
                stderr="",
            )

            result = adapter.restart_worker("celery-worker")

            assert result.success is True
            assert result.action == RecoveryAction.RESTART_WORKER

    def test_restart_worker_failure(self):
        """restart_worker 실패 테스트 (Mock)."""
        adapter = DockerComposeRecoveryAdapter()

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=1,
                stdout="",
                stderr="Error: No such service",
            )

            result = adapter.restart_worker("unknown-service")

            assert result.success is False

    def test_restart_worker_timeout(self):
        """restart_worker 타임아웃 테스트 (Mock)."""
        adapter = DockerComposeRecoveryAdapter()

        with mock.patch("subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=60)

            result = adapter.restart_worker("celery-worker")

            assert result.success is False
            assert "timed out" in result.message.lower()

    def test_scale_deployment_success(self):
        """scale_deployment 성공 테스트 (Mock)."""
        adapter = DockerComposeRecoveryAdapter()

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="Scaled",
                stderr="",
            )

            result = adapter.scale_deployment("worker", 3)

            assert result.success is True
            assert result.action == RecoveryAction.SCALE_DEPLOYMENT

    def test_delete_pod_redirects_to_restart(self):
        """delete_pod가 restart로 리다이렉트 테스트."""
        adapter = DockerComposeRecoveryAdapter()

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="Restarted",
                stderr="",
            )

            result = adapter.delete_pod("container-name", "")

            # Docker Compose에서는 restart로 대체
            assert result.action == RecoveryAction.RESTART_WORKER


class TestDockerRecoveryTimeoutBehavior:
    """687 D3/D10 — docker recovery subprocess timeouts resolve from settings,
    fall back to a named constant on a settings-load failure, and log a WARNING
    before degrading.
    """

    def test_restart_worker_uses_settings_timeout(self):
        # Given: MetaWatchdogSettings supplies a non-default restart timeout
        from baldur.meta.recovery_adapter import DockerComposeRecoveryAdapter
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        adapter = DockerComposeRecoveryAdapter()
        settings = MetaWatchdogSettings(docker_restart_timeout_seconds=99.0)

        # When
        with (
            mock.patch(
                "baldur.meta.config.get_meta_watchdog_settings", return_value=settings
            ),
            mock.patch("subprocess.run", autospec=True) as mock_run,
        ):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            adapter.restart_worker("celery-worker")

        # Then: the settings value is forwarded to subprocess.run
        assert mock_run.call_args.kwargs["timeout"] == 99.0

    def test_scale_deployment_uses_settings_timeout(self):
        # Given: MetaWatchdogSettings supplies a non-default scale timeout
        from baldur.meta.recovery_adapter import DockerComposeRecoveryAdapter
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        adapter = DockerComposeRecoveryAdapter()
        settings = MetaWatchdogSettings(docker_scale_timeout_seconds=150.0)

        # When
        with (
            mock.patch(
                "baldur.meta.config.get_meta_watchdog_settings", return_value=settings
            ),
            mock.patch("subprocess.run", autospec=True) as mock_run,
        ):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            adapter.scale_deployment("worker", 3)

        # Then
        assert mock_run.call_args.kwargs["timeout"] == 150.0

    def test_restart_worker_falls_back_and_warns_on_settings_failure(self):
        # Given: the settings read raises inside restart_worker
        from baldur.meta.recovery_adapter import (
            _DOCKER_RESTART_TIMEOUT_FALLBACK_SECONDS,
            DockerComposeRecoveryAdapter,
        )

        adapter = DockerComposeRecoveryAdapter()

        # When
        with (
            mock.patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                side_effect=RuntimeError("settings boom"),
            ),
            mock.patch("subprocess.run", autospec=True) as mock_run,
            mock.patch("baldur.meta.recovery_adapter.logger") as mock_logger,
        ):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            result = adapter.restart_worker("celery-worker")

        # Then: recovery still attempts with the fallback timeout and warns
        assert (
            mock_run.call_args.kwargs["timeout"]
            == _DOCKER_RESTART_TIMEOUT_FALLBACK_SECONDS
        )
        assert result.success is True
        mock_logger.warning.assert_called_once()
        assert (
            mock_logger.warning.call_args.args[0]
            == "recovery_adapter.settings_load_failed"
        )


class TestRecoveryFallbackDriftContract:
    """687 D9 (G3) — recovery_adapter fallback constants must mirror their
    MetaWatchdogSettings field defaults (drift guard).
    """

    def test_docker_and_k8s_fallbacks_match_settings_defaults(self):
        from baldur.meta.recovery_adapter import (
            _DOCKER_RESTART_TIMEOUT_FALLBACK_SECONDS,
            _DOCKER_SCALE_TIMEOUT_FALLBACK_SECONDS,
            _K8S_API_TIMEOUT_FALLBACK_SECONDS,
        )
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        fields = MetaWatchdogSettings.model_fields
        assert (
            _DOCKER_RESTART_TIMEOUT_FALLBACK_SECONDS
            == fields["docker_restart_timeout_seconds"].default
        )
        assert (
            _DOCKER_SCALE_TIMEOUT_FALLBACK_SECONDS
            == fields["docker_scale_timeout_seconds"].default
        )
        assert (
            _K8S_API_TIMEOUT_FALLBACK_SECONDS
            == fields["k8s_api_timeout_seconds"].default
        )


class TestRecoveryAdapterFactory:
    """Factory tests that don't depend on the K8s class.

    Kubernetes-branch tests moved to
    ``tests/dormant/unit/meta/test_k8s_recovery_adapter.py`` per impl doc 528
    D15 (settings-load fallback, ProviderRegistry routing, autospec mocks).
    """

    def test_get_adapter_noop(self):
        """NOOP env var returns NoOpRecoveryAdapter."""
        import os

        with mock.patch.dict(os.environ, {"BALDUR_RECOVERY_ADAPTER": "noop"}):
            adapter = get_recovery_adapter()
            assert isinstance(adapter, NoOpRecoveryAdapter)

    def test_get_adapter_docker(self):
        """Docker env var returns DockerComposeRecoveryAdapter."""
        import os

        with mock.patch.dict(os.environ, {"BALDUR_RECOVERY_ADAPTER": "docker"}):
            adapter = get_recovery_adapter()
            assert isinstance(adapter, DockerComposeRecoveryAdapter)

    def test_get_adapter_default(self):
        """Default factory returns a usable adapter."""
        adapter = get_recovery_adapter()
        assert adapter is not None
