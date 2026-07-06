"""Shared fixtures for Cat 7A micro-benchmarks.

Reset baldur singletons between benchmark tests so that ProtectSettings,
ProviderRegistry, and CB state do not leak between rows in the same session.

Running benchmarks
==================

`pytest-benchmark` is incompatible with `pytest-xdist` parallel mode — the
plugin auto-disables itself when xdist is active. The project's default
`pytest.ini` `addopts` enables xdist (`-n auto` / `--dist=loadfile`), so
benchmark runs need an explicit override:

    pytest tests/benchmarks/ -p no:xdist -o "addopts="

Or invoke a single row with manual quantile loop only (`time.perf_counter_ns`
loop is xdist-safe; only the pytest-benchmark cross-validation function is
disabled).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_baldur_singletons():
    """Drop ProtectSettings, CB-state, and DLQ singletons between benchmarks.

    Benchmark rows construct fresh `protect()` call sites; settings drift from
    a prior test (e.g. enabled=False) would silently invalidate the next row.
    The DLQ service singleton is also reset (impl doc 486 D9 — resolves 7A.2
    F3 DLQ count inflation across sessions). The DLQ outbox cleanup is
    handled transitively by ``reset_protect_caches`` via the #486 D8 chain.
    """
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()

    try:
        from baldur_pro.services.dlq import reset_dlq_service

        reset_dlq_service()
    except ImportError:
        pass

    yield
    reset_protect_settings()
    try:
        from baldur_pro.services.dlq import reset_dlq_service

        reset_dlq_service()
    except ImportError:
        pass
