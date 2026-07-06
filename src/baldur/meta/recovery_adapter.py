"""
Recovery Infrastructure Adapter.

Abstracts environment-specific recovery strategies (Kubernetes/Docker/Local).
Performs infrastructure-level recovery such as Pod restart and Deployment scaling.

Per impl doc 528 D10-v2, the Kubernetes adapter is relocated to
``baldur_dormant.meta.k8s_recovery_adapter``. OSS retains the ABC + the
Docker Compose / NoOp adapters; ``get_recovery_adapter()`` resolves the
``kubernetes`` branch via ``ProviderRegistry.recovery_adapter`` and falls
back through Docker Compose -> NoOp when ``baldur_dormant`` is absent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()

# Fallback timeouts (seconds) applied when the MetaWatchdogSettings read raises
# inside the recovery methods. Each mirrors its settings field default and is
# drift-guarded by a contract test.
_DOCKER_RESTART_TIMEOUT_FALLBACK_SECONDS = 60.0
_DOCKER_SCALE_TIMEOUT_FALLBACK_SECONDS = 120.0
_K8S_API_TIMEOUT_FALLBACK_SECONDS = 30.0


class RecoveryAction(str, Enum):
    """Recovery action type."""

    RESTART_WORKER = "restart_worker"
    """Restart a worker."""

    SCALE_DEPLOYMENT = "scale_deployment"
    """Scale a Deployment."""

    DELETE_POD = "delete_pod"
    """Force-delete a Pod."""

    RESET_CONNECTION = "reset_connection"
    """Reset a connection."""


@dataclass
class RecoveryResult:
    """Recovery result."""

    action: RecoveryAction
    """Action that was performed."""

    success: bool
    """Whether the action succeeded."""

    target: str
    """Target (Pod name, Deployment name, etc.)."""

    message: str
    """Result message."""

    timestamp: datetime
    """Timestamp of the action."""

    details: dict[str, Any] | None = None
    """Additional details."""


class RecoveryInfrastructureAdapter(ABC):
    """
    Recovery infrastructure adapter interface.

    Different recovery strategies per environment:
    - Kubernetes: Pod delete, Deployment scale
    - Docker Compose: Container restart
    - Local: Process signal

    Input validation policy:
    - Service name whitelist (prevents OS command injection)
    - Replicas upper bound (prevents resource-exhaustion DoS)
    """

    # Security: allowed characters in service names (letters, digits, hyphen, underscore, dot).
    _SAFE_NAME_PATTERN: re.Pattern = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-.]{0,127}$")
    # Security: maximum replica count.
    _MAX_REPLICAS: int = 50

    def _validate_service_name(self, name: str) -> None:
        """
        Validate the service name — prevents OS command injection.

        Allowed:
        - Letters, digits, hyphen, underscore, dot
        - Maximum 128 characters
        - Must start with an alphanumeric character

        Raises:
            ValueError: Invalid service name
        """
        if not name or not self._SAFE_NAME_PATTERN.match(name):
            from baldur.core.exceptions import RecoveryAdapterError

            raise RecoveryAdapterError(
                f"Invalid service name: {name!r}. "
                "Must start with alphanumeric, contain only [a-zA-Z0-9_\\-.], "
                "and be 1-128 characters long.",
                service_name=name or "",
            )

    def _validate_replicas(self, replicas: int) -> None:
        """
        Validate replica count range — prevents resource-exhaustion DoS.

        Allowed range: 0 <= replicas <= _MAX_REPLICAS (default 50)

        Raises:
            ValueError: replicas out of range
        """
        if (
            not isinstance(replicas, int)
            or replicas < 0
            or replicas > self._MAX_REPLICAS
        ):
            from baldur.core.exceptions import RecoveryAdapterError

            raise RecoveryAdapterError(
                f"Invalid replicas count: {replicas}. "
                f"Must be integer between 0 and {self._MAX_REPLICAS}.",
                replicas=replicas if isinstance(replicas, int) else None,
            )

    @abstractmethod
    def restart_worker(
        self, worker_name: str, timeout: float | None = None
    ) -> RecoveryResult:
        """
        Restart a worker.

        Args:
            worker_name: Worker name

        Returns:
            RecoveryResult
        """
        pass

    @abstractmethod
    def scale_deployment(self, name: str, replicas: int) -> RecoveryResult:
        """
        Scale a Deployment.

        Args:
            name: Deployment name
            replicas: Target replica count

        Returns:
            RecoveryResult
        """
        pass

    @abstractmethod
    def delete_pod(self, pod_name: str, namespace: str) -> RecoveryResult:
        """
        Force-delete a Pod.

        Args:
            pod_name: Pod name
            namespace: Namespace

        Returns:
            RecoveryResult
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """
        Whether the adapter is available.

        Returns:
            Availability flag
        """
        pass


class DockerComposeRecoveryAdapter(RecoveryInfrastructureAdapter):
    """
    Docker Compose recovery adapter.

    For local development. Uses docker-compose commands.
    Service-name validation and replicas upper bound are inherited.
    """

    def is_available(self) -> bool:
        """Whether docker-compose or docker compose is available."""
        return (
            shutil.which("docker-compose") is not None
            or shutil.which("docker") is not None
        )

    def _get_compose_command(self) -> list[str]:
        """Return the docker-compose or docker compose command."""
        if shutil.which("docker-compose"):
            return ["docker-compose"]
        return ["docker", "compose"]

    def restart_worker(
        self,
        worker_name: str,
        timeout: float | None = None,
    ) -> RecoveryResult:
        """Docker container restart.

        Args:
            worker_name: Service name
            timeout: Ignored (docker-compose has its own timeout)
        """
        # Resolve the subprocess timeout in a dedicated try/except so a settings
        # failure degrades to the fallback constant instead of aborting recovery
        # (a failure inside the outer try would return success=False without
        # ever attempting the restart).
        try:
            from baldur.meta.config import get_meta_watchdog_settings

            restart_timeout = (
                get_meta_watchdog_settings().docker_restart_timeout_seconds
            )
        except Exception as e:
            logger.warning(
                "recovery_adapter.settings_load_failed",
                error=str(e),
            )
            restart_timeout = _DOCKER_RESTART_TIMEOUT_FALLBACK_SECONDS
        try:
            self._validate_service_name(worker_name)
            cmd = self._get_compose_command() + ["restart", worker_name]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=restart_timeout,
            )
            success = result.returncode == 0
            return RecoveryResult(
                action=RecoveryAction.RESTART_WORKER,
                success=success,
                target=worker_name,
                message=result.stdout if success else result.stderr,
                timestamp=utc_now(),
            )
        except subprocess.TimeoutExpired:
            return RecoveryResult(
                action=RecoveryAction.RESTART_WORKER,
                success=False,
                target=worker_name,
                message="Command timed out",
                timestamp=utc_now(),
            )
        except Exception as e:
            return RecoveryResult(
                action=RecoveryAction.RESTART_WORKER,
                success=False,
                target=worker_name,
                message=str(e),
                timestamp=utc_now(),
            )

    def scale_deployment(self, name: str, replicas: int) -> RecoveryResult:
        """
        Docker Compose scale.

        Args:
            name: Service name
            replicas: Target replica count

        Returns:
            RecoveryResult
        """
        # Dedicated try/except (see restart_worker) — a settings failure must
        # not abort the scale attempt.
        try:
            from baldur.meta.config import get_meta_watchdog_settings

            scale_timeout = get_meta_watchdog_settings().docker_scale_timeout_seconds
        except Exception as e:
            logger.warning(
                "recovery_adapter.settings_load_failed",
                error=str(e),
            )
            scale_timeout = _DOCKER_SCALE_TIMEOUT_FALLBACK_SECONDS
        try:
            self._validate_service_name(name)
            self._validate_replicas(replicas)
            cmd = self._get_compose_command() + [
                "up",
                "-d",
                "--scale",
                f"{name}={replicas}",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=scale_timeout,
            )
            success = result.returncode == 0
            return RecoveryResult(
                action=RecoveryAction.SCALE_DEPLOYMENT,
                success=success,
                target=name,
                message=f"Scaled to {replicas}" if success else result.stderr,
                timestamp=utc_now(),
            )
        except subprocess.TimeoutExpired:
            return RecoveryResult(
                action=RecoveryAction.SCALE_DEPLOYMENT,
                success=False,
                target=name,
                message="Command timed out",
                timestamp=utc_now(),
            )
        except Exception as e:
            return RecoveryResult(
                action=RecoveryAction.SCALE_DEPLOYMENT,
                success=False,
                target=name,
                message=str(e),
                timestamp=utc_now(),
            )

    def delete_pod(self, pod_name: str, namespace: str = "") -> RecoveryResult:
        """
        Docker container deletion (mapped to restart).

        Args:
            pod_name: Container name
            namespace: (ignored)

        Returns:
            RecoveryResult
        """
        # In Docker we map "delete pod" to a restart.
        return self.restart_worker(pod_name)


class NoOpRecoveryAdapter(RecoveryInfrastructureAdapter):
    """
    No-Op recovery adapter.

    For test / dry-run environments. Does not perform real recovery.
    """

    def is_available(self) -> bool:
        return True

    def restart_worker(
        self,
        worker_name: str,
        timeout: float | None = None,
    ) -> RecoveryResult:
        logger.info(
            "no_op_recovery_adapter.restart",
            worker_name=worker_name,
        )
        return RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target=worker_name,
            message="No-op (dry run)",
            timestamp=utc_now(),
        )

    def scale_deployment(self, name: str, replicas: int) -> RecoveryResult:
        logger.info(
            "no_op_recovery_adapter.scale",
            recovery_adapter_name=name,
            replicas=replicas,
        )
        return RecoveryResult(
            action=RecoveryAction.SCALE_DEPLOYMENT,
            success=True,
            target=name,
            message=f"No-op (would scale to {replicas})",
            timestamp=utc_now(),
        )

    def delete_pod(self, pod_name: str, namespace: str = "") -> RecoveryResult:
        logger.info(
            "no_op_recovery_adapter.delete_pod",
            pod_name=pod_name,
        )
        return RecoveryResult(
            action=RecoveryAction.DELETE_POD,
            success=True,
            target=pod_name,
            message="No-op (dry run)",
            timestamp=utc_now(),
        )


def _try_create_k8s_adapter(
    api_timeout_seconds: float,
) -> RecoveryInfrastructureAdapter | None:
    """Resolve the K8s recovery adapter via ProviderRegistry.

    528 D10-v2 relocated ``KubernetesRecoveryAdapter`` to
    ``baldur_dormant.meta.k8s_recovery_adapter``. ``register_dormant_services()``
    populates ``ProviderRegistry.recovery_adapter`` with the ``"k8s"`` provider;
    when ``baldur_dormant`` is absent, the slot returns the OSS NoOp default
    (which has ``is_available()`` -> True) and we surface that here as
    ``None`` so the Docker Compose fallback path runs.
    """
    from baldur.factory.base import AdapterNotFoundError
    from baldur.factory.registry import ProviderRegistry

    try:
        provider_cls = ProviderRegistry.recovery_adapter.get_provider("k8s")
    except AdapterNotFoundError:
        return None

    try:
        # provider_cls is the dynamically-registered concrete adapter; the
        # api_timeout_seconds kwarg is not on the abstract return type.
        adapter = provider_cls(api_timeout_seconds=api_timeout_seconds)  # type: ignore[call-arg]
    except TypeError:
        # Provider doesn't accept api_timeout_seconds (e.g., NoOp registered
        # under "k8s" for a test seam) — instantiate without kwargs.
        adapter = provider_cls()
    if adapter.is_available():
        return adapter
    return None


def get_recovery_adapter() -> RecoveryInfrastructureAdapter:
    """
    Return the recovery adapter appropriate for the environment.

    Selection via the ``BALDUR_RECOVERY_ADAPTER`` environment variable:
    - kubernetes (default, K8s environment)
    - docker (Docker Compose environment)
    - noop (test / dry run)

    Returns:
        RecoveryInfrastructureAdapter
    """
    adapter_type = os.environ.get("BALDUR_RECOVERY_ADAPTER", "kubernetes").lower()

    if adapter_type == "noop":
        return NoOpRecoveryAdapter()
    if adapter_type == "docker":
        return DockerComposeRecoveryAdapter()
    # Read k8s_api_timeout from MetaWatchdogSettings
    api_timeout = _K8S_API_TIMEOUT_FALLBACK_SECONDS
    try:
        from baldur.meta.config import get_meta_watchdog_settings

        api_timeout = get_meta_watchdog_settings().k8s_api_timeout_seconds
    except Exception as e:
        logger.warning(
            "recovery_adapter.settings_load_failed",
            error=str(e),
        )

    k8s_adapter = _try_create_k8s_adapter(api_timeout)
    if k8s_adapter is not None:
        return k8s_adapter

    # Fall back to Docker when K8s is unavailable.
    docker_adapter = DockerComposeRecoveryAdapter()
    if docker_adapter.is_available():
        logger.info("recovery_adapter.unavailable_falling_back_docker")
        return docker_adapter

    # Final fallback: NoOp.
    logger.warning("recovery_adapter.no_adapter_available_using")
    return NoOpRecoveryAdapter()
