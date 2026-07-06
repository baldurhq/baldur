"""Standard policy for broken admin-route import groups.

Each per-domain ``_register_<domain>_routes`` function uses a try/except
around the handler-module imports so a single missing/renamed handler
does not block the rest of the admin routes from registering.

Per 526 D7, the except branch follows two different policies
depending on environment:

- ``BALDUR_ENV=dev``: re-raise so broken imports surface immediately during
  local development. mypy alone does not catch all such regressions (some
  paths require runtime resolution); fail-fast at server startup is the
  signal-to-noise win.

- Otherwise (production / CI): WARNING log so SRE has visibility (previously
  ``logger.debug`` was effectively invisible), then fail-open so the rest of
  the admin routes register.

Lives in a dedicated module to keep submodule imports out of the circular
``routes/__init__.py`` path — ``__init__.py`` re-exports the helper for
test convenience.
"""

from __future__ import annotations

import os

import structlog

__all__ = ["handle_route_import_failure"]


_logger = structlog.get_logger()


def handle_route_import_failure(event_name: str, exc: BaseException) -> None:
    """Apply the dev fail-fast / production fail-open policy."""
    if os.environ.get("BALDUR_ENV") == "dev":
        raise exc
    _logger.warning(event_name, error=exc)
