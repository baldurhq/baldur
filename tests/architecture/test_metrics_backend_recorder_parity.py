"""Architectural fitness functions — metrics backend recorder parity.

Two related gates live here. **G46** (648 D4) locks recorder *surface* parity
(family + method + facade). **G47** (651 D5) locks histogram *bucket-boundary*
parity — every OTel-native histogram MUST carry the same explicit buckets its
Prometheus-recorder counterpart defines, else its quantiles mis-render under the
default OTel backend. The G46 charter narrative follows.

G46 — metrics backend recorder parity (648 D4).

``get_metrics()`` returns one of two backend implementations selected at boot:
``BaldurMetrics`` (Prometheus) or ``OTELBaldurMetrics`` (OTel — the AUTO-default
whenever the ``opentelemetry`` extra is installed, because
``BALDUR_OBSERVABILITY_PROFILE=auto`` resolves to ``otel_collector``).
Historically the OTel backend wired only a subset of recorder families and some
present families were incomplete ports, so on the default backend a
``getattr(get_metrics(), "<family>", None)`` consumer silently no-op'd, or a
present family's missing method raised an ``AttributeError`` that the caller's
fail-open ``try/except`` swallowed. The defect is the claim-wiring behaviorally-invisible class — a dead recording path changes no test output — so
unit tests and ``/verify`` never caught it; only a live render smoke did.

This rule locks full parity at three levels, reflection-based and enforced-empty:

* (a) family-attribute-set parity (``prom_families == otel_families``),
* (b) for every shared family, public recorder-method-set parity, and
* (c) facade public-method parity (``otel_facade`` is a superset of
  ``prom_facade`` — the OTel facade MAY add methods, e.g. ``set_dlq_pending_count``,
  but MUST implement every Prometheus facade method).

No allowlist: under 648 D1–D3 full parity is the target, so any divergence is a
real regression — a recorder family OR method added to only one backend fails CI.

Non-vacuous guards:

* ``importorskip("opentelemetry")`` — the gap only manifests when the OTel
  backend is constructible. The canonical full-suite env installs the extra
  (``pyproject`` ``dev`` -> ``[all]``), so the gate runs there rather than
  silently skipping into a green-looking false pass.
* The OTel backend is constructed as a REAL meter-backed instance and the gate
  asserts ``_initialized is True`` before the parity assertions, so a meter-init
  failure in an otel-equipped env fails loudly instead of comparing a
  degraded/empty attribute set.
* Guard-of-the-guard tests scratch-remove one family and one method and assert
  the parity comparison flags the divergence, so the enforced-empty pass can
  never go vacuous.

Rule registry:
``ARCHITECTURE.md#g46-metrics-backend-recorder-parity``
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("opentelemetry")


def _families(backend) -> set[str]:
    """Public recorder-family attributes (recorder instances) on a backend.

    Both backends set their recorders as instance attributes in ``__init__``;
    everything private (``_initialized`` / ``_gauge_stores``) and the scalar
    ``prefix`` is excluded so only the recorder family names remain.
    """
    return {
        name for name in vars(backend) if not name.startswith("_") and name != "prefix"
    }


def _public_methods(obj) -> set[str]:
    """Public callable names on an object (recorder or facade)."""
    return {
        name
        for name, _member in inspect.getmembers(obj, callable)
        if not name.startswith("_")
    }


def _otel_native_histograms(otel) -> dict[str, object]:
    """Map OTel metric ``.name`` -> ``_advisory.explicit_bucket_boundaries`` for
    every native OTel histogram instrument on the backend.

    Duck-types on ``_advisory`` (only ``_Histogram`` instruments carry it; OTel
    counters / observable gauges do not), so a future 7th native histogram
    auto-enrolls without a test edit (651 D5). Walks the natively-ported families
    only — ``retry`` / ``replay`` / ``infra`` (cb / dlq / others reuse Prometheus
    recorders, covered by their own bucket definitions).
    """
    native: dict[str, object] = {}
    for family in ("retry", "replay", "infra"):
        recorder = getattr(otel, family)
        for value in vars(recorder).values():
            advisory = getattr(value, "_advisory", None)
            name = getattr(value, "name", None)
            if advisory is not None and name is not None:
                native[name] = advisory.explicit_bucket_boundaries
    return native


def _prom_histogram_bounds(prom) -> dict[str, list]:
    """Map prometheus ``_name`` -> upper bounds (trailing ``+Inf`` stripped) for
    every prometheus ``Histogram`` across the backend's recorder families.

    Duck-types on ``_upper_bounds`` (only histograms carry it). The prometheus
    client exposes no public ``.name`` — ``_name`` is the canonical join key.
    """
    bounds: dict[str, list] = {}
    for family in _families(prom):
        recorder = getattr(prom, family)
        for value in vars(recorder).values():
            upper = getattr(value, "_upper_bounds", None)
            name = getattr(value, "_name", None)
            if upper is not None and name is not None:
                bounds[name] = upper[:-1]  # strip the trailing +Inf sentinel
    return bounds


def _boundaries_equal(otel_advisory, prom_bounds) -> bool:
    """Element-wise boundary equality with a length check — NOT container ``==``.

    ``_advisory.explicit_bucket_boundaries`` stores the value passed to
    ``create_histogram`` **unconverted** (a frozen dataclass field never coerced
    to list/tuple), while ``_upper_bounds`` is a list — so a tuple-vs-list
    container ``==`` is unconditionally ``False`` and would falsely fail the gate.
    Boundaries are literal copies (no arithmetic), so ``int == float`` (``1 ==
    1.0``) is exact.
    """
    return len(otel_advisory) == len(prom_bounds) and all(
        a == b for a, b in zip(otel_advisory, prom_bounds, strict=True)
    )


@pytest.fixture
def backends(monkeypatch):
    """A real meter-backed OTel backend + the Prometheus backend.

    Pins the OTel profile via ``monkeypatch.setenv`` (function-scoped — NOT
    ``patch.dict(clear=True)``, which strips the suite's env pins and leaks
    singletons across xdist workers; see UNIT_TEST_GUIDELINES §6.5) and resets
    the observability settings singleton so ``effective_otel_enabled`` re-reads
    the pin. Both recorder sets register against the shared ``prometheus_client``
    REGISTRY via ``get_or_create_*`` (idempotent), so constructing both backends
    raises no duplicate-registration error.
    """
    monkeypatch.setenv("BALDUR_OBSERVABILITY_PROFILE", "otel_collector")
    from baldur.settings.observability import reset_observability_settings

    reset_observability_settings()
    from baldur.observability import get_meter, initialize_meter_provider

    initialize_meter_provider()
    assert get_meter() is not None, (
        "OTel meter provider failed to initialize in an otel-equipped env — the "
        "parity gate must not run against a degraded backend"
    )

    from baldur.metrics.otel_backend import OTELBaldurMetrics
    from baldur.metrics.prometheus import BaldurMetrics

    otel = OTELBaldurMetrics(prefix="baldur")
    prom = BaldurMetrics(prefix="baldur")
    try:
        yield prom, otel
    finally:
        reset_observability_settings()


class TestMetricsBackendRecorderParity:
    """648 D4 / G46 — the two metrics backends must expose an identical recorder
    surface so a family/method added to one backend cannot silently no-op on the
    other (the AUTO-default OTel backend)."""

    def test_otel_backend_is_real_meter_backed(self, backends):
        """Non-vacuous guard: the OTel backend constructed as a real instance, so
        the parity assertions below compare a fully-wired backend (not a
        degraded/empty one)."""
        _prom, otel = backends
        assert otel._initialized is True, (
            "OTELBaldurMetrics._initialized is False in an otel-equipped env — the "
            "parity assertions would compare a degraded/empty backend (vacuous)"
        )

    def test_family_attribute_set_parity(self, backends):
        """(a) Every Prometheus recorder family is present on the OTel backend and
        vice versa."""
        prom, otel = backends
        prom_families = _families(prom)
        otel_families = _families(otel)
        assert prom_families == otel_families, (
            "metrics backend family divergence — a recorder family exists on one "
            "backend only and will silently no-op (getattr->None) on the other.\n"
            f"prometheus-only: {sorted(prom_families - otel_families)}\n"
            f"otel-only: {sorted(otel_families - prom_families)}"
        )

    def test_shared_family_method_set_parity(self, backends):
        """(b) For every shared family the public recorder-method sets are equal,
        so a present-family consumer cannot hit an AttributeError on one backend."""
        prom, otel = backends
        diffs: dict[str, dict[str, list[str]]] = {}
        for family in sorted(_families(prom) & _families(otel)):
            prom_methods = _public_methods(getattr(prom, family))
            otel_methods = _public_methods(getattr(otel, family))
            if prom_methods != otel_methods:
                diffs[family] = {
                    "prometheus-only": sorted(prom_methods - otel_methods),
                    "otel-only": sorted(otel_methods - prom_methods),
                }
        assert not diffs, (
            "recorder method divergence between backends — a method on one "
            "backend's recorder is absent on the other, so the present-family "
            "consumer's call raises AttributeError (swallowed by the caller's "
            f"fail-open envelope) on the backend that lacks it.\n{diffs}"
        )

    def test_facade_method_superset(self, backends):
        """(c) Every Prometheus facade delegate method exists on the OTel facade.

        Superset (not strict equality): the OTel facade may add methods the
        Prometheus facade lacks (e.g. ``set_dlq_pending_count``)."""
        prom, otel = backends
        missing = _public_methods(prom) - _public_methods(otel)
        assert not missing, (
            "OTel facade is missing a Prometheus facade delegate method — every "
            "prometheus facade method must exist on the OTel backend so a facade "
            f"caller never no-ops on the default backend: {sorted(missing)}"
        )

    def test_parity_check_detects_a_removed_family(self, backends):
        """Guard-of-the-guard: removing a family from one backend must be detected
        by the family-set comparison, so the enforced-empty pass cannot go
        vacuous."""
        prom, otel = backends
        prom_families = _families(prom)
        scratched = _families(otel) - {"idempotency"}
        assert prom_families != scratched, (
            "family-parity comparison no longer detects a missing family — the "
            "enforced-empty assertion would pass vacuously"
        )

    def test_parity_check_detects_a_removed_method(self, backends):
        """Guard-of-the-guard: a method missing from a shared family must be
        detected by the method-set comparison."""
        prom, otel = backends
        family = "circuit_breaker"
        prom_methods = _public_methods(getattr(prom, family))
        scratched = _public_methods(getattr(otel, family)) - {"record_blocked"}
        assert prom_methods != scratched, (
            "method-parity comparison no longer detects a missing method — the "
            "enforced-empty assertion would pass vacuously"
        )


class TestMetricsBackendHistogramBucketParity:
    """651 D5 / G47 — every OTel-native histogram MUST carry the same explicit
    bucket boundaries its Prometheus-recorder counterpart defines.

    A native ``meter.create_histogram(...)`` with no ``explicit_bucket_boundaries_
    advisory`` falls back to the OTel SDK's default millisecond-scale boundaries
    (``[0, 5, …, 10000]``), so a seconds-valued duration histogram mis-renders
    under ``histogram_quantile`` (the live demo rendered minutes for millisecond
    requests). The defect is behaviorally invisible to G46 (which compares family/
    method/facade *surface*, not buckets) and to per-backend unit tests (each
    recorder is exercised against its own backend, so neither side compares
    buckets) — only a live Grafana P95/P99 render exposed it. This gate is the
    introspection-based fitness function that locks bucket-boundary parity.

    Rule registry:
    ``ARCHITECTURE.md#g47-metrics-backend-histogram-bucket-parity``
    """

    def test_native_histograms_are_discovered(self, backends):
        """Non-vacuous guard: the duck-typed enumeration finds the native OTel
        histograms, so the parity assertions below are not comparing an empty
        set."""
        _prom, otel = backends
        native = _otel_native_histograms(otel)
        assert native, (
            "no native OTel histogram instruments discovered — the `_advisory` "
            "duck-typing seam broke (SDK rename?), so the parity assertions would "
            "pass vacuously"
        )

    def test_every_native_histogram_has_nonempty_advisory(self, backends):
        """(a) Each native OTel histogram declares non-empty explicit boundaries —
        neither ``None`` (the kwarg omitted) nor ``[]`` (the exact G1 bug: a native
        histogram added with no boundaries)."""
        _prom, otel = backends
        native = _otel_native_histograms(otel)
        empty = sorted(name for name, boundaries in native.items() if not boundaries)
        assert not empty, (
            "native OTel histogram(s) created with no explicit bucket boundaries — "
            "the SDK applies its default ms-scale boundaries, so histogram_quantile "
            f"mis-renders the seconds-valued metric: {empty}"
        )

    def test_native_histogram_boundaries_match_prometheus(self, backends):
        """(b) Per metric name, the OTel advisory boundaries equal the Prometheus
        recorder's buckets (element-wise; the Prometheus recorder is the source of
        truth)."""
        prom, otel = backends
        native = _otel_native_histograms(otel)
        prom_bounds = _prom_histogram_bounds(prom)

        missing_counterpart = sorted(n for n in native if n not in prom_bounds)
        assert not missing_counterpart, (
            "native OTel histogram(s) have no Prometheus counterpart to mirror — "
            f"the parity source of truth is absent: {missing_counterpart}"
        )

        mismatches = {
            name: {
                "otel_advisory": list(advisory),
                "prometheus_buckets": list(prom_bounds[name]),
            }
            for name, advisory in native.items()
            if not _boundaries_equal(advisory, prom_bounds[name])
        }
        assert not mismatches, (
            "OTel-native histogram bucket boundaries diverge from the Prometheus "
            "recorder's buckets — histogram_quantile would render different "
            f"quantiles under the two backends: {mismatches}"
        )

    def test_parity_check_detects_a_boundary_change(self, backends):
        """Guard-of-the-guard: a scratch change to one native histogram's
        boundaries must be flagged by the element-wise comparison, so the
        enforced-empty parity pass can never go vacuous."""
        prom, otel = backends
        native = _otel_native_histograms(otel)
        prom_bounds = _prom_histogram_bounds(prom)

        name = next(iter(native))
        scratched = list(native[name])
        scratched[0] = scratched[0] + 999  # drift one boundary off parity
        assert not _boundaries_equal(scratched, prom_bounds[name]), (
            "element-wise boundary comparison no longer detects a drifted boundary "
            "— the enforced-empty parity assertion would pass vacuously"
        )

    def test_parity_check_detects_an_empty_advisory(self, backends):
        """Guard-of-the-guard: the non-empty check must flag a histogram whose
        advisory is ``None`` (the omitted-kwarg G1 bug) — not silently treat it as
        a pass."""
        empty_advisory_native = {"baldur_probe_duration_seconds": None}
        flagged = [n for n, b in empty_advisory_native.items() if not b]
        assert flagged == ["baldur_probe_duration_seconds"], (
            "the `not boundaries` emptiness predicate no longer flags a None "
            "advisory — the G1 omitted-boundaries bug would pass the gate"
        )
