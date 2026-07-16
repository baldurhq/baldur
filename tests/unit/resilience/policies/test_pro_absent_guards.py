"""D4 — the Hedging leg of the HA presets/policies fails closed PRO-absent.

``ha_pipeline()``, ``HedgingPolicy.__init__`` and ``AsyncHedgingPolicy.__init__``
compose a Hedging stage whose engine requires ``baldur_pro`` (the bulkhead
stage is core-tier and always constructs). With ``baldur_pro`` absent the gated
Hedging names are bound to ``None``; the guards must raise a clear
``RuntimeError`` naming ``baldur_pro`` at construction time (claim-wiring
integrity: a silently degraded pipeline is a false guarantee), instead of an
opaque ``TypeError``/``AttributeError``.

These tests run in BOTH tiers: they simulate PRO-absence locally rather than
relying on the harness, so they pass unchanged on both installs. The mock
point differs by binding style:
  - ``hedging.py`` binds its PRO names at MODULE level (the 503 cycle-break), so
    the executor binding is monkeypatched to ``None`` directly.
  - ``presets.ha_pipeline`` binds ``HedgingConfig``/``HedgingMode`` via a
    per-call function-body import, so the import is forced to fail with
    ``sys.modules[...] = None`` (which makes ``from baldur_pro... import ...``
    raise ImportError on entry).
"""

from __future__ import annotations

import pytest


class TestProAbsentFailClosed:
    """Construction-time RuntimeError naming baldur_pro when PRO is absent (D4 —
    Hedging leg only; the bulkhead stage is core-tier)."""

    def test_hedging_policy_pro_absent_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from baldur.resilience.policies import hedging

        # The unconditional-use binding (`self._executor = HedgingExecutor(...)`).
        monkeypatch.setattr(hedging, "HedgingExecutor", None)
        with pytest.raises(RuntimeError, match="baldur_pro"):
            hedging.HedgingPolicy(candidates=[lambda: 1])

    def test_async_hedging_policy_pro_absent_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from baldur.resilience.policies import hedging

        monkeypatch.setattr(hedging, "AsyncHedgingExecutor", None)
        with pytest.raises(RuntimeError, match="baldur_pro"):
            hedging.AsyncHedgingPolicy(candidates=[lambda: 1])

    def test_ha_pipeline_pro_absent_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import sys

        from baldur.resilience.policies.presets import ha_pipeline

        # Force ha_pipeline's function-body `from baldur_pro... import ...` to
        # fail exactly as it does PRO-absent (None in sys.modules → ImportError).
        monkeypatch.setitem(sys.modules, "baldur_pro.services.hedging.config", None)
        with pytest.raises(RuntimeError, match="baldur_pro"):
            ha_pipeline("svc", [lambda: 1])

    def test_ha_pipeline_explicit_config_still_guarded(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The guard targets the unconditional bindings, so an explicit-config
        # caller cannot bypass it PRO-absent (D4 guard-target precision).
        import sys

        from baldur.resilience.policies.presets import ha_pipeline

        monkeypatch.setitem(sys.modules, "baldur_pro.services.hedging.config", None)
        with pytest.raises(RuntimeError, match="baldur_pro"):
            ha_pipeline("svc", [lambda: 1], hedging_delay=0.05)
