"""Django repository adapters for Baldur System."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.adapters.django.repositories.cascade_event import (
        DjangoCascadeEventArchiveRepository,
    )
    from baldur.adapters.django.repositories.postmortem import (
        DjangoPostmortemRepository,
    )
    from baldur.adapters.django.repositories.recovery_session import (
        DjangoRecoverySessionArchiveRepository,
    )


def __getattr__(name: str):  # noqa: N807
    if name == "DjangoPostmortemRepository":
        from baldur.adapters.django.repositories.postmortem import (
            DjangoPostmortemRepository,
        )

        return DjangoPostmortemRepository
    if name == "DjangoCascadeEventArchiveRepository":
        from baldur.adapters.django.repositories.cascade_event import (
            DjangoCascadeEventArchiveRepository,
        )

        return DjangoCascadeEventArchiveRepository
    if name == "DjangoRecoverySessionArchiveRepository":
        from baldur.adapters.django.repositories.recovery_session import (
            DjangoRecoverySessionArchiveRepository,
        )

        return DjangoRecoverySessionArchiveRepository
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DjangoPostmortemRepository",
    "DjangoCascadeEventArchiveRepository",
    "DjangoRecoverySessionArchiveRepository",
]
