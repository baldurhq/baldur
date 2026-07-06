"""
Regression tests for #460 — `baldur` core import chain stays usable when
`prometheus_client` is not installed.

Strategy:
    Run each scenario inside a subprocess with `sys.modules['prometheus_client']
    = None` set BEFORE any `baldur` import. In-process `monkeypatch.delitem`
    is insufficient — already-imported baldur modules in the test runner
    cache resolved prometheus symbols and would mask the regression.

Pattern source: 460 §D2.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


# tests/unit/metrics/conftest.py defines an autouse fixture that skips the
# whole module when prometheus_client is absent in the parent. Override it
# here: subprocess tests poison prometheus_client inside the child, so the
# parent's installation status does not matter. The single happy-path test
# (TestServicesLazyAttrsResolveWithPrometheusBehavior) guards itself
# explicitly with PROMETHEUS_AVAILABLE.
@pytest.fixture(autouse=True)
def _check_prometheus():
    return


def _run_poisoned(snippet: str) -> subprocess.CompletedProcess:
    """Run a Python snippet in a subprocess with prometheus_client poisoned."""
    script = "import sys\nsys.modules['prometheus_client'] = None\n" + textwrap.dedent(
        snippet
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestRegistryImportsWithoutPrometheusContract:
    """`baldur.metrics.registry` itself loads with PROMETHEUS_AVAILABLE=False."""

    def test_registry_module_loads_with_flag_false_and_registry_none(self):
        # When
        result = _run_poisoned(
            """
            import baldur.metrics.registry as r
            assert r.PROMETHEUS_AVAILABLE is False, r.PROMETHEUS_AVAILABLE
            assert r.REGISTRY is None, r.REGISTRY
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout


class TestHelpersRaiseWhenPrometheusAbsentContract:
    """`get_or_create_*` helpers raise ImportError with the install hint."""

    @pytest.mark.parametrize(
        "helper_name",
        ["get_or_create_counter", "get_or_create_gauge", "get_or_create_histogram"],
    )
    def test_helper_raises_importerror_naming_baldur_prometheus_extra(
        self, helper_name
    ):
        # When
        result = _run_poisoned(
            f"""
            from baldur.metrics.registry import {helper_name}
            try:
                {helper_name}('m_name', 'desc', ['lbl'])
            except ImportError as e:
                msg = str(e)
                assert 'prometheus_client is required' in msg, msg
                assert 'baldur-framework[prometheus]' in msg, msg
                print('OK')
            else:
                print('NO_RAISE')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout, f"stdout={result.stdout}"


class TestProtectImportsWithoutPrometheusBehavior:
    """`from baldur import protected` resolves the full protect → services → metrics chain."""

    def test_protected_lazy_attr_resolves_with_prometheus_poisoned(self):
        # When
        result = _run_poisoned(
            """
            from baldur import protected
            assert callable(protected), type(protected)
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout


class TestServicesLazyAttrsRaiseWithoutPrometheusContract:
    """services/__init__ PEP 562 lazy attrs raise ImportError on first access."""

    @pytest.mark.parametrize(
        "attr_name",
        ["record_sla_breach", "collect_all_metrics"],
    )
    def test_lazy_metric_attr_raises_importerror_with_install_hint(self, attr_name):
        # When
        result = _run_poisoned(
            f"""
            import baldur.services as s
            try:
                getattr(s, '{attr_name}')
            except ImportError as e:
                assert 'baldur-framework[prometheus]' in str(e), str(e)
                print('OK')
            else:
                print('NO_RAISE')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout, f"stdout={result.stdout}"


class TestServicesLazyAttrsResolveWithPrometheusBehavior:
    """Happy path: PEP 562 lazy attrs resolve to real objects when prometheus is installed."""

    @pytest.mark.parametrize(
        ("attr_name", "must_be_callable"),
        [
            ("record_sla_breach", True),
            ("collect_all_metrics", True),
        ],
    )
    def test_lazy_metric_attr_resolves(self, attr_name, must_be_callable):
        from baldur.metrics.registry import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed in parent env")

        # When
        import baldur.services as s

        value = getattr(s, attr_name)
        # Then
        assert value is not None
        if must_be_callable:
            assert callable(value)


class TestDefaultDomainsAccessibleWithoutPrometheusContract:
    """`DEFAULT_DOMAINS` is a pure-Python list, importable without prometheus_client."""

    def test_default_domains_returns_design_contract_list(self):
        # Contract values from registry.py DEFAULT_DOMAINS (design spec)
        # When
        result = _run_poisoned(
            """
            from baldur.metrics.registry import DEFAULT_DOMAINS
            assert DEFAULT_DOMAINS == [
                'external_service',
                'internal_process',
                'async_task',
                'notification',
                'data_sync',
            ], DEFAULT_DOMAINS
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout


class TestAdapterImportsWithoutPrometheusBehavior:
    """Django/FastAPI/Flask adapter modules load without prometheus_client.

    Each subprocess imports `protected` first so the
    `baldur.protect → services → metrics.registry` chain has already
    evaluated under the poison; then the adapter import exercises a
    distinct second code path. A future regression that introduces an
    unguarded `from prometheus_client import …` (or a top-level
    `get_or_create_*()` call) in any adapter file fails this test.
    """

    @pytest.mark.parametrize(
        ("import_stmt", "label"),
        [
            ("from baldur.adapters.django import apps", "django"),
            (
                "from baldur.adapters.fastapi.middleware import BaldurMiddleware",
                "fastapi",
            ),
            # Flask adapter exposes install_baldur_request_hooks (the
            # release-checklist's BaldurMiddleware reference is stale —
            # tracked separately).
            (
                "from baldur.adapters.flask.middleware import install_baldur_request_hooks",
                "flask",
            ),
        ],
    )
    def test_adapter_module_imports_with_prometheus_poisoned(self, import_stmt, label):
        # When
        result = _run_poisoned(
            f"""
            from baldur import protected
            {import_stmt}
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, (
            f"adapter={label} returncode={result.returncode} stderr={result.stderr}"
        )
        assert "OK" in result.stdout, f"adapter={label} stdout={result.stdout}"


class TestServicesGetattrUnknownNameContract:
    """`services.__getattr__` falls through to AttributeError for unknown names."""

    def test_unknown_attr_raises_attribute_error_with_module_in_message(self):
        import baldur.services as s

        # When / Then
        with pytest.raises(
            AttributeError, match=r"has no attribute 'definitely_not_a_real_attr'"
        ):
            s.definitely_not_a_real_attr
