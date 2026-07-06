"""
Leader Elector factory.

Singleton management of LeaderElector instances per resource name.
Both concrete backends route through ``ProviderRegistry.leader_elector``:
redis (``baldur_pro.coordination.redis_elector`` per doc 599 D4) and
kubernetes (``baldur_dormant.coordination.kubernetes_elector`` per doc
528 D10-v2). OSS keeps the chassis (base/config/noop/factory) only.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.coordination.base import LeaderElector  # noqa: F401
from baldur.coordination.config import (
    LeaderElectionSettings,
    get_leader_election_settings,
)
from baldur.coordination.noop_elector import NoOpLeaderElector

logger = structlog.get_logger()

_electors: dict[str, LeaderElector] = {}
_lock = threading.Lock()


def get_leader_elector(
    resource_name: str,
    settings: LeaderElectionSettings | None = None,
) -> LeaderElector:
    """
    Return a singleton LeaderElector for the resource.

    Same ``resource_name`` always returns the same instance.

    Resolution order (599 D4):

    1. ``settings.enabled is False`` (default) — return ``NoOpLeaderElector``
       immediately: no provider lookup, no warning. This preserves the
       pre-relocation default behavior exactly (the redis elector used to be
       constructed on this path but only ``start()`` gated on ``enabled``,
       so the default path never led).
    2. ``enabled=True``, backend ``redis`` — resolve the provider class via
       ``ProviderRegistry.leader_elector.get_provider("redis")`` (registered
       by ``baldur_pro.register_pro_services()``).
    3. ``enabled=True``, redis provider absent — WARNING + ``NoOpLeaderElector``.
       Fail-safe direction: never-leader prevents duplicate scheduled
       execution; assume-leader would be unsafe in clusters.
    4. ``enabled=True``, backend ``kubernetes`` — unchanged 528 D10-v2 routing
       (RuntimeError when the K8s provider is absent: an explicitly
       configured non-default backend that cannot be satisfied is a
       configuration error, not a degradable default).

    Args:
        resource_name: resource identifier (e.g. "dlq-consumer", "scheduler")
        settings: leader-election settings (defaults to module settings)

    Returns:
        LeaderElector instance

    Raises:
        RuntimeError: kubernetes backend requested but no K8s provider
            registered (install ``baldur-pro[kubernetes]``).
        ValueError: unknown backend specified.

    Usage:
        elector = get_leader_elector("dlq-consumer")
        elector.start()
    """
    global _electors

    if resource_name in _electors:
        return _electors[resource_name]

    with _lock:
        # Double-check locking
        if resource_name in _electors:
            return _electors[resource_name]

        settings = settings or get_leader_election_settings()

        elector: Any
        if not settings.enabled:
            # 599 D4 step 1 — default-OFF short-circuit (no lookup, no warning).
            elector = NoOpLeaderElector(resource_name, settings)
        elif settings.backend == "redis":
            from baldur.factory.base import AdapterNotFoundError
            from baldur.factory.registry import ProviderRegistry

            try:
                provider_cls = ProviderRegistry.leader_elector.get_provider("redis")
            except AdapterNotFoundError:
                logger.warning(
                    "leader_election.provider_unavailable",
                    backend="redis",
                    resource_name=resource_name,
                    hint=(
                        "leader election enabled but the redis elector is not "
                        "registered. Install: pip install baldur-pro. Falling "
                        "back to NoOpLeaderElector (never-leader)."
                    ),
                )
                elector = NoOpLeaderElector(resource_name, settings)
            else:
                # provider_cls is the registered concrete elector class;
                # instantiate per-resource (a singleton-cached ``get()``
                # would crash because the constructor takes args).
                elector = provider_cls(resource_name, settings)  # type: ignore[call-arg]
        elif settings.backend == "kubernetes":
            # 528 D10-v2: K8sLeaderElector relocated to baldur_dormant.
            # Route through ProviderRegistry so OSS doesn't import it
            # directly. ``get_provider`` returns the registered class so
            # we can instantiate per-resource (a singleton-cached
            # ``get()`` would crash because K8sLeaderElector requires
            # constructor args).
            from baldur.factory.base import AdapterNotFoundError
            from baldur.factory.registry import ProviderRegistry

            try:
                provider_cls = ProviderRegistry.leader_elector.get_provider("k8s")
            except AdapterNotFoundError as exc:
                raise RuntimeError(
                    "kubernetes leader elector requested but no K8s provider "
                    "registered. Install: pip install baldur-pro[kubernetes]"
                ) from exc
            # provider_cls is the registered concrete elector (see comment
            # above); its constructor args are not on the abstract LeaderElector.
            elector = provider_cls(resource_name, settings)  # type: ignore[call-arg]
        else:
            raise ValueError(f"Unknown backend: {settings.backend}")

        _electors[resource_name] = elector
        return elector


def reset_leader_electors() -> None:
    """
    Reset all electors (test utility).

    Stops every active elector and clears the cache.
    """
    global _electors

    with _lock:
        for elector in _electors.values():
            try:
                elector.stop()
            except Exception:
                pass
        _electors.clear()
