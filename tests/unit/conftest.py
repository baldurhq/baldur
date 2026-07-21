"""Register the writable-directory fixtures for the unit tree.

``resolve_writable_dir`` is adopted by durability surfaces that live in
separate directories — ``utils/``, ``audit/checkpoint/``, ``audit/wal/``,
``audit/persistence/``, ``adapters/resilient/`` and the bootstrap tests — so
the fixtures are registered at the tree root rather than in one directory's
conftest (guidelines §5.1, "entire subdirectory tree").

The definitions live in ``tests/factories/writable_dir.py`` so the integration
tree can register the same ones. None is ``autouse``: a test that does not
request one is unaffected.
"""

from __future__ import annotations

from tests.factories.writable_dir import (  # noqa: F401 - fixture registration
    deny_dir,
    no_state_dir,
    writable_dir_chain,
)
