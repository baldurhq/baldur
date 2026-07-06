"""OSS NoOp leader elector — registry default when baldur_dormant is absent.

Per doc 528 D10-v2 "NoOp default registration requirement", the
``coordination.leader_elector`` ProviderRegistry slot pre-registers
``NoOpLeaderElector`` as default at OSS bootstrap. When
``baldur_dormant`` is not installed, ``ProviderRegistry.leader_elector.
get(...)`` returns this class so OSS callers can use ``.get()``
unconditionally without ``is not None`` guards.

Behavior: ``start()`` / ``stop()`` no-op; ``is_leader()`` returns
``False``; callbacks register but never fire. The instance never claims
leadership, so any "do work only when leader" branch correctly degrades
to "not leader" on a clean-OSS install.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from baldur.coordination.base import LeaderElector, LeaderInfo, LeadershipState

logger = structlog.get_logger()

__all__ = ["NoOpLeaderElector"]


class NoOpLeaderElector(LeaderElector):
    """Leader elector that never becomes leader.

    Registered as the OSS-side default for
    ``ProviderRegistry.leader_elector``. ``baldur_dormant.
    register_dormant_services()`` overwrites the default with
    ``K8sLeaderElector`` when present.

    The class implements ``LeaderElector`` so existing OSS callers
    (``coordination/factory.py`` after doc 528 Stage 2b routing
    refactor) can swap the concrete implementation in without
    type-checking churn.
    """

    def __init__(
        self,
        resource_name: str = "noop",
        settings: object | None = None,
    ) -> None:
        self._resource_name = resource_name
        self._fencing_token = 0

    @property
    def resource_name(self) -> str:
        return self._resource_name

    @property
    def state(self) -> LeadershipState:
        return LeadershipState.FOLLOWER

    def is_leader(self) -> bool:
        return False

    def get_leader(self) -> LeaderInfo | None:
        return None

    def get_fencing_token(self) -> int:
        return self._fencing_token

    def is_lease_valid(self) -> bool:
        return False

    def start(self) -> None:
        logger.debug(
            "leader_elector.noop_start",
            resource=self._resource_name,
            hint=(
                "baldur_dormant not installed; leader election disabled. "
                "is_leader() will always return False."
            ),
        )

    def stop(self) -> None:
        return None

    def on_become_leader(
        self,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        return callback

    def on_lose_leader(
        self,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        return callback
