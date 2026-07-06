"""
Shared fixtures for Pydantic Settings tests.
"""

import pytest


@pytest.fixture
def monkeypatch_env(monkeypatch):
    """Helper fixture for environment variable manipulation."""
    return monkeypatch
