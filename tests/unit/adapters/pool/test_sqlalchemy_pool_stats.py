"""
Tests for F14-C1-g: SQLAlchemyPoolStatsProvider AsyncEngine rejection.

Source: src/baldur/adapters/pool/sqlalchemy_stats.py
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


class TestSQLAlchemyPoolStatsAsyncEngineContract:
    """AsyncEngine must be rejected with TypeError."""

    def test_async_engine_raises_type_error(self):
        """Passing AsyncEngine raises TypeError with migration message."""
        from baldur.adapters.pool.sqlalchemy_stats import (
            SQLAlchemyPoolStatsProvider,
        )

        # Create a real class to act as AsyncEngine for isinstance checks
        class FakeAsyncEngine:
            pass

        fake_engine = FakeAsyncEngine()

        # Inject fake sqlalchemy.ext.asyncio module into sys.modules
        fake_mod = types.ModuleType("sqlalchemy.ext.asyncio")
        fake_mod.AsyncEngine = FakeAsyncEngine

        # Also ensure parent modules exist
        saved = {}
        for name in ("sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio"):
            saved[name] = sys.modules.get(name)

        try:
            sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
            sys.modules.setdefault("sqlalchemy.ext", types.ModuleType("sqlalchemy.ext"))
            sys.modules["sqlalchemy.ext.asyncio"] = fake_mod

            with pytest.raises(TypeError, match="async_engine.sync_engine"):
                SQLAlchemyPoolStatsProvider(engine=fake_engine)
        finally:
            for name, val in saved.items():
                if val is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = val

    def test_sync_engine_accepted_without_error(self):
        """Passing a regular sync engine does not raise."""
        from baldur.adapters.pool.sqlalchemy_stats import (
            SQLAlchemyPoolStatsProvider,
        )

        mock_sync_engine = MagicMock()

        # Should not raise
        provider = SQLAlchemyPoolStatsProvider(
            engine=mock_sync_engine, pool_name="test"
        )
        assert provider._engine is mock_sync_engine
        assert provider._pool_name == "test"

    def test_none_engine_accepted_without_error(self):
        """Passing None engine does not trigger AsyncEngine check."""
        from baldur.adapters.pool.sqlalchemy_stats import (
            SQLAlchemyPoolStatsProvider,
        )

        provider = SQLAlchemyPoolStatsProvider(engine=None)
        assert provider._engine is None

    def test_async_engine_import_error_gracefully_skipped(self):
        """If sqlalchemy.ext.asyncio is not installed, no check is performed."""
        from baldur.adapters.pool.sqlalchemy_stats import (
            SQLAlchemyPoolStatsProvider,
        )

        mock_engine = MagicMock()

        original_import = __import__

        def mock_import(name, *args, **kwargs):
            if "asyncio" in name:
                raise ImportError("no asyncio")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            # Should not raise even though we can't import AsyncEngine
            provider = SQLAlchemyPoolStatsProvider(engine=mock_engine)
            assert provider._engine is mock_engine
