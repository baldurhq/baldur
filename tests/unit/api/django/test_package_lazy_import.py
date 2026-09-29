"""``baldur.api.django`` imports and boots without Django REST framework.

``baldur-framework[django]`` installs Django only; Django REST framework (DRF)
ships in ``[django-api]``. Every middleware the Django adapter injects lives
under ``baldur.api.django``, so the package init must not import the DRF-backed
views or serializers, or a ``[django]`` app fails when its WSGI server builds
the middleware chain. ``runserver`` masks that failure: a Django auth system
check imports every ``MIDDLEWARE`` entry, swallows the ``ImportError`` and leaves
the middleware submodule cached behind the discarded package. The boot cases
here call ``get_wsgi_application()`` directly (no system checks) and check that
every package prefix of every Baldur middleware module is really loaded.

Without DRF, "DRF was not loaded" holds by construction, so it cannot catch an
eager import of the REST API that swallows its ``ImportError``. One case
therefore leaves DRF installed (``[django,django-api]``) and checks that
importing the package and every middleware module still does not load it.

Implementation notes:
    Each case runs in a subprocess: ``sys.modules`` is process-global and the
    unit session has DRF and every optional extra loaded. The child marks the
    packages a ``[django]`` install lacks as absent with
    ``sys.modules[name] = None`` before any Baldur import. That makes
    ``import name`` raise ``ModuleNotFoundError`` and
    ``importlib.util.find_spec(name)`` return ``None``, as in a clean venv. A
    ``sys.meta_path`` finder that raises is not used: it also makes
    ``find_spec`` raise, a failure no real install produces.

    The child inherits the session environment minus ``BALDUR_TEST_MODE``
    (which skips the adapter's metrics-middleware injection) and
    ``DJANGO_SETTINGS_MODULE`` (the child configures settings itself).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from baldur.adapters.django.auto_config import (
    DEFAULT_EARLY_GROUP,
    DEFAULT_POST_AUTH_GROUP,
    DEFAULT_TAIL_GROUP,
)

# Importable in the unit session, absent from a ``[django]`` install.
_ABSENT_FROM_DJANGO_EXTRA = (
    "rest_framework",
    "rest_framework_simplejwt",
    "drf_spectacular",
    "dj_db_conn_pool",
    "django_redis",
    "redis",
    "prometheus_client",
)

# ``[django,prometheus]``: the same install plus prometheus-client.
_ABSENT_FROM_DJANGO_PROMETHEUS_EXTRAS = tuple(
    name for name in _ABSENT_FROM_DJANGO_EXTRA if name != "prometheus_client"
)

_CHILD_ENV_DROPPED = ("BALDUR_TEST_MODE", "DJANGO_SETTINGS_MODULE")

# Below the gate's per-test --timeout so a hung child fails as TimeoutExpired.
_CHILD_TIMEOUT_S = 25

_VIEW_BODY = "pong"


def _run_child(snippet: str, report_dir: Path, absent: tuple[str, ...]) -> dict:
    """Run ``snippet`` in a child with ``absent`` modules; return its JSON report.

    The snippet writes its report to ``sys.argv[1]`` — stdout also carries the
    child's log lines.
    """
    report_path = report_dir / "report.json"
    preamble = "import sys\n" + "".join(
        f"sys.modules[{name!r}] = None\n" for name in absent
    )
    env = {k: v for k, v in os.environ.items() if k not in _CHILD_ENV_DROPPED}
    result = subprocess.run(
        [sys.executable, "-c", preamble + textwrap.dedent(snippet), str(report_path)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CHILD_TIMEOUT_S,
        env=env,
        cwd=report_dir,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout[-4000:]}\nstderr={result.stderr[-4000:]}"
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


# The settings mirror a project that ends its settings module with
# ``configure_baldur()``: the middleware list is the real groups plus the
# ``ready()`` injection, never a hand-copied list.
_WSGI_BOOT = f"""
import json
from django.conf import settings
from django.http import HttpResponse
from django.urls import path
from baldur.adapters.django import configure_baldur

ns = {{
    "INSTALLED_APPS": [
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "baldur.adapters.django",
    ],
    "MIDDLEWARE": [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
    ],
    "DATABASES": {{
        "default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
    }},
    "ROOT_URLCONF": "__main__",
    "ALLOWED_HOSTS": ["*"],
}}
configure_baldur(namespace=ns)
settings.configure(**{{k: v for k, v in ns.items() if k.isupper()}})
urlpatterns = [path("ping/", lambda request: HttpResponse({_VIEW_BODY!r}))]

# No system check runs before the handler loads its middleware.
from django.core.wsgi import get_wsgi_application
from wsgiref.util import setup_testing_defaults

application = get_wsgi_application()
environ = {{}}
setup_testing_defaults(environ)
environ["PATH_INFO"] = "/ping/"
captured = {{}}

def start_response(status, headers, exc_info=None):
    captured["status"] = status

response = application(environ, start_response)
try:
    body = b"".join(response)
finally:
    response.close()

report = {{
    "status": captured["status"],
    "body": body.decode("utf-8"),
    "middleware": list(settings.MIDDLEWARE),
    "loaded": sorted(
        name
        for name, module in list(sys.modules.items())
        if name.startswith("baldur") and module is not None
    ),
}}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f)
"""


@pytest.fixture(
    scope="class",
    params=[_ABSENT_FROM_DJANGO_EXTRA, _ABSENT_FROM_DJANGO_PROMETHEUS_EXTRAS],
    ids=["django", "django_prometheus"],
)
def wsgi_boot(request, tmp_path_factory) -> dict:
    """One WSGI boot + request per install shape, shared by the class."""
    return _run_child(_WSGI_BOOT, tmp_path_factory.mktemp("wsgi_boot"), request.param)


class TestDjangoPackageWithoutRestFrameworkBehavior:
    """The adapter boots through WSGI and serves a request without DRF."""

    def test_wsgi_request_without_rest_framework_returns_view_response(self, wsgi_boot):
        """A request through the WSGI application reaches the view."""
        assert wsgi_boot["status"] == "200 OK"
        assert wsgi_boot["body"] == _VIEW_BODY

    def test_wsgi_boot_loads_configure_baldur_groups_and_injected_metrics_middleware(
        self, wsgi_boot
    ):
        """Every default group entry and the ready()-injected metrics middleware load."""
        # Given
        expected = DEFAULT_EARLY_GROUP + DEFAULT_POST_AUTH_GROUP + DEFAULT_TAIL_GROUP

        # When
        loaded_middleware = wsgi_boot["middleware"]

        # Then
        assert [m for m in expected if m not in loaded_middleware] == []
        assert any(m.endswith(".HttpMetricsMiddleware") for m in loaded_middleware), (
            loaded_middleware
        )

    def test_wsgi_boot_loads_every_package_prefix_of_each_baldur_middleware(
        self, wsgi_boot
    ):
        """No middleware was served by a submodule cached behind a discarded package."""
        # Given
        loaded = set(wsgi_boot["loaded"])
        baldur_middleware = [
            m for m in wsgi_boot["middleware"] if m.startswith("baldur.")
        ]

        # When
        missing = []
        for dotted in baldur_middleware:
            parts = dotted.rsplit(".", 1)[0].split(".")
            for i in range(1, len(parts) + 1):
                prefix = ".".join(parts[:i])
                if prefix not in loaded:
                    missing.append((dotted, prefix))

        # Then
        assert baldur_middleware
        assert missing == []


# The ``configure_baldur()`` group modules, the ``ready()``-injected metrics
# middleware module and the package itself — imported with DRF installed and
# settings configured, as a ``[django,django-api]`` host loads its middleware
# (configured settings let an eager REST API import load DRF instead of failing).
_IMPORT_MIDDLEWARE_WITH_REST_FRAMEWORK = """
import importlib
import json
from django.conf import settings

settings.configure()

from baldur.adapters.django.auto_config import (
    DEFAULT_EARLY_GROUP,
    DEFAULT_POST_AUTH_GROUP,
    DEFAULT_TAIL_GROUP,
)

groups = DEFAULT_EARLY_GROUP + DEFAULT_POST_AUTH_GROUP + DEFAULT_TAIL_GROUP
modules = sorted(
    {dotted.rsplit(".", 1)[0] for dotted in groups}
    | {"baldur.api.django", "baldur.api.django.middleware.http_metrics"}
)
for name in modules:
    importlib.import_module(name)
report = {
    "modules": modules,
    "rest_framework_loaded": "rest_framework" in sys.modules,
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f)
"""


class TestDjangoMiddlewareImportWithRestFrameworkBehavior:
    """With DRF installed, importing the middleware still leaves the REST API unloaded."""

    def test_middleware_import_with_rest_framework_installed_does_not_load_it(
        self, tmp_path
    ):
        """The package and every injected middleware module import without loading DRF."""
        # When
        report = _run_child(_IMPORT_MIDDLEWARE_WITH_REST_FRAMEWORK, tmp_path, ())

        # Then
        assert "baldur.api.django.middleware.http_metrics" in report["modules"]
        assert report["rest_framework_loaded"] is False


_MOUNT_REST_API = """
import json

try:
    import baldur.api.django.urls.health  # noqa: F401
except ImportError as exc:
    cause = exc.__cause__
    report = {
        "raised": type(exc).__name__,
        "message": str(exc),
        "cause": type(cause).__name__ if cause is not None else None,
        "cause_name": getattr(cause, "name", None),
    }
else:
    report = {"raised": None}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f)
"""


class TestRestApiWithoutRestFrameworkContract:
    """Mounting the REST API without DRF names the extra that installs it."""

    def test_mounting_rest_api_without_rest_framework_raises_import_error_naming_extra(
        self, tmp_path
    ):
        """The URL package raises ImportError naming [django-api], chained from DRF's."""
        # When
        report = _run_child(_MOUNT_REST_API, tmp_path, _ABSENT_FROM_DJANGO_EXTRA)

        # Then
        assert report["raised"] == "ImportError"
        assert "baldur-framework[django-api]" in report["message"]
        assert report["cause"] == "ModuleNotFoundError"
        assert report["cause_name"] == "rest_framework"


_POOL_STATUS = """
import json
from structlog.testing import capture_logs
from baldur.api.django.pool_circuit_breaker import PoolCircuitBreaker

breaker = PoolCircuitBreaker()
with capture_logs() as logs:
    status = breaker._fetch_pool_status_internal()
report = {
    "status": status,
    "logs": [{"event": r["event"], "log_level": r["log_level"]} for r in logs],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f)
"""


@pytest.fixture(scope="class")
def pool_status_without_pool_package(tmp_path_factory) -> dict:
    """One pool-status fetch with django-db-connection-pool absent."""
    return _run_child(
        _POOL_STATUS, tmp_path_factory.mktemp("pool_status"), _ABSENT_FROM_DJANGO_EXTRA
    )


class TestPoolCircuitBreakerWithoutPoolPackageBehavior:
    """A missing django-db-connection-pool is normal flow for the pool breaker."""

    def test_fetch_pool_status_without_pool_package_returns_not_installed_sentinel(
        self, pool_status_without_pool_package
    ):
        """The fetch returns the unavailable sentinel instead of raising."""
        assert pool_status_without_pool_package["status"] == {
            "available": False,
            "reason": "dj_db_conn_pool not installed",
            "is_exhausted": False,
            "is_near_exhaustion": False,
        }

    def test_fetch_pool_status_without_pool_package_logs_below_warning(
        self, pool_status_without_pool_package
    ):
        """The refresh-tick branch logs at DEBUG, never WARNING or above."""
        # Given
        logs = pool_status_without_pool_package["logs"]

        # Then — the branch's own record proves the capture saw it
        assert {"event": "pool_circuit_breaker.available", "log_level": "debug"} in logs
        assert [
            r for r in logs if r["log_level"] in ("warning", "error", "critical")
        ] == []


class TestApiDjangoPackageExportContract:
    """``pool_circuit_breaker`` names the submodule, not the instance it defines."""

    def test_package_all_and_dir_exclude_pool_circuit_breaker(self):
        """The name is not a package export."""
        import baldur.api.django as package

        assert "pool_circuit_breaker" not in package.__all__
        assert "pool_circuit_breaker" not in dir(package)

    def test_package_pool_circuit_breaker_name_is_submodule_holding_instance(self):
        """``from baldur.api.django import pool_circuit_breaker`` yields the submodule."""
        # When
        from baldur.api.django import pool_circuit_breaker as submodule

        # Then
        assert isinstance(submodule, types.ModuleType)
        assert submodule.__name__ == "baldur.api.django.pool_circuit_breaker"
        assert isinstance(submodule.pool_circuit_breaker, submodule.PoolCircuitBreaker)
