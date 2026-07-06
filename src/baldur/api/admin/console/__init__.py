"""Built-in admin web console package (536).

Holds the self-contained ``console.html`` asset (in-package data, auto-included
by hatchling like ``py.typed``) and the :mod:`~baldur.api.admin.console.handler`
that serves it at ``GET /``. Being a regular package (this ``__init__``) lets
``importlib.resources.files("baldur.api.admin.console")`` locate the asset from
an installed wheel.
"""

from __future__ import annotations

__all__: list[str] = []
