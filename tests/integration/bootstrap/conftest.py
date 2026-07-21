"""Register the writable-directory fixtures for the bootstrap integration tree.

Definitions live in ``tests/factories/writable_dir.py``; see
``tests/unit/conftest.py`` for the rationale.
"""

from __future__ import annotations

from tests.factories.writable_dir import (  # noqa: F401 - fixture registration
    deny_dir,
    writable_dir_chain,
)
