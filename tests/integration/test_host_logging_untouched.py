"""Adding baldur leaves the host application's logging alone.

An application that configured its root logger — ``basicConfig``, a
``dictConfig`` with a ``root`` entry, a framework ``LOGGING`` — keeps that
configuration when baldur is initialised: its level, handlers, format and
stream are unchanged, each of its records is written once, and baldur's own
events reach the application's handlers readably, in the application's
format. A process that configured nothing still gets baldur's stdout JSON
handler.

None of this is observable from inside the test session: pytest owns the
root logger, and the suite has already configured logging and run
``init()``. So every case measures a real child interpreter with the
``BALDUR_*`` environment stripped — a plain script, a ``dictConfig`` after
``init()``, a Django project with a ``root`` entry in ``LOGGING`` and a
Flask app in the quickstart order — and reads the child's streams.

Mock-based in the sense that matters here: no infrastructure is configured.
The admin server and the scheduler are told not to auto-start so that
parallel test workers never contend for one port, and the observability
profile is pinned to ``local`` — the shape of an install without the OTel
SDK, which is what the public CI runs. With the SDK present the ``auto``
profile enables OTLP export to a collector this test does not run: its
logging instrumentation adds an OTel shipping handler to the root, and its
exporter retries a flush for its whole timeout window whenever the host
reconfigures logging or the process exits. OTel wiring is outside this
contract; the host-logging guarantee is measured without it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_CHILD_TIMEOUT_SECONDS = 120

# The host's own format: level and logger name first, so a baldur event that
# arrives through this handler is recognisable as such and a dict repr would
# be unmistakable.
_HOST_FORMAT = "%(levelname)s %(name)s %(message)s"

# A baldur event emitted through baldur's configured structlog pipeline; the
# name follows the event-name convention so the validator lets it through.
_PROBE_EVENT = "probe.host_event_emitted"
_PROBE_EMIT = (
    "import structlog\n"
    f"structlog.get_logger('baldur.probe').warning('{_PROBE_EVENT}', key='v')\n"
)

_REPORT_PREFIX = "HOST_LOGGING_REPORT "

# Reports the root logger's state as one JSON line on stdout, under a prefix
# no logging line carries, so the parent can find it among whatever else the
# child wrote.
_REPORT_ROOT = (
    "import json, logging, sys\n"
    "root = logging.getLogger()\n"
    "print('" + _REPORT_PREFIX + "' + json.dumps({'level': root.level, "
    "'handlers': [type(h).__name__ for h in root.handlers], "
    "'streams': [getattr(getattr(h, 'stream', None), 'name', None) for h in root.handlers]}), "
    "flush=True)\n"
)


def _child_environment() -> dict[str, str]:
    """A host-application environment: every ``BALDUR_*`` variable removed,
    then the two auto-starters switched off and OTel export left out."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BALDUR_")}
    env.pop("REDIS_URL", None)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["BALDUR_ADMIN_AUTOSTART"] = "0"
    env["BALDUR_SCHEDULER_AUTOSTART"] = "0"
    env["BALDUR_OBSERVABILITY_PROFILE"] = "local"
    return env


def _run_child(script: str) -> tuple[str, str]:
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        env=_child_environment(),
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    # Decode explicitly: text=True picks the console codepage on Windows and
    # can silently yield an empty stream on a decode failure.
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert completed.returncode == 0, stdout + stderr
    return stdout, stderr


def _reports(stdout: str) -> list[dict]:
    return [
        json.loads(line[len(_REPORT_PREFIX) :])
        for line in stdout.splitlines()
        if line.startswith(_REPORT_PREFIX)
    ]


class TestAScriptThatConfiguredLoggingFirst:
    """Case (a): ``basicConfig(INFO, stderr, own format)`` before ``init()``."""

    _SCRIPT = (
        "import logging, sys\n"
        f"logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='{_HOST_FORMAT}')\n"
        "host = logging.getLogger('app.radar')\n" + _REPORT_ROOT + "import baldur\n"
        "baldur.init()\n"
        "baldur.protect('d795', lambda: 1)\n"
        + _REPORT_ROOT
        + _PROBE_EMIT
        + "host.info('app.source_swept', extra={})\n"
        "host.warning('app.source_stale')\n"
    )

    @pytest.fixture(scope="class")
    def run(self):
        return _run_child(self._SCRIPT)

    def test_the_hosts_info_record_reaches_stderr_in_its_own_format(self, run):
        stdout, stderr = run
        assert "INFO app.radar app.source_swept" in stderr
        assert "app.source_swept" not in stdout

    def test_the_hosts_warning_is_written_exactly_once(self, run):
        stdout, stderr = run
        assert (stdout + stderr).count("app.source_stale") == 1
        assert "WARNING app.radar app.source_stale" in stderr

    def test_the_root_level_and_handlers_are_unchanged_by_init(self, run):
        stdout, _ = run
        before, after = _reports(stdout)
        assert after == before
        assert after["level"] == 20
        assert after["handlers"] == ["StreamHandler"]
        assert "_BaldurStreamHandler" not in after["handlers"]

    def test_a_baldur_event_is_readable_through_the_hosts_handler(self, run):
        stdout, stderr = run
        assert f"WARNING baldur.probe {_PROBE_EVENT} key='v'" in stderr
        assert "{'event'" not in stderr
        assert "{'event'" not in stdout

    def test_nothing_baldur_writes_lands_on_stdout(self, run):
        """The CLI's stdout is its product; every baldur line went through
        the host's stderr handler."""
        stdout, _ = run
        assert [
            line for line in stdout.splitlines() if not line.startswith(_REPORT_PREFIX)
        ] == []


class TestADictConfigAfterInit:
    """Case (b): a ``dictConfig`` with a ``root`` entry after ``init()``
    replaces baldur's handler — stdlib removes every root handler before
    installing its own. ``disable_existing_loggers`` is set ``False``; with
    the stdlib default every logger ``init()`` created would be disabled,
    baldur's included (documented as a stdlib-wide caveat)."""

    _SCRIPT = (
        "import logging, logging.config, sys\n"
        "import baldur\n"
        "baldur.init()\n" + _REPORT_ROOT + "logging.config.dictConfig({\n"
        "    'version': 1,\n"
        "    'disable_existing_loggers': False,\n"
        "    'formatters': {'plain': {'format': 'HOST " + _HOST_FORMAT + "'}},\n"
        "    'handlers': {'host': {'class': 'logging.StreamHandler', 'stream': 'ext://sys.stderr', 'formatter': 'plain'}},\n"
        "    'root': {'level': 'INFO', 'handlers': ['host']},\n"
        "})\n" + _REPORT_ROOT + _PROBE_EMIT
    )

    @pytest.fixture(scope="class")
    def run(self):
        return _run_child(self._SCRIPT)

    def test_init_installed_baldurs_handler_on_the_empty_root(self, run):
        stdout, _ = run
        before, _ = _reports(stdout)
        assert before["handlers"] == ["_BaldurStreamHandler"]

    def test_the_hosts_dictconfig_replaced_it(self, run):
        stdout, _ = run
        _, after = _reports(stdout)
        assert after["handlers"] == ["StreamHandler"]
        assert after["level"] == 20

    def test_a_baldur_warning_reaches_the_new_handler(self, run):
        _, stderr = run
        assert f"HOST WARNING baldur.probe {_PROBE_EVENT} key='v'" in stderr


class TestADjangoProjectWithARootEntry:
    """Case (c): Django's ``configure_logging`` runs before ``ready()`` calls
    ``init()``; a user ``LOGGING`` with a ``root`` entry is what the process
    keeps."""

    _SCRIPT = (
        "import django, json, logging, sys\n"
        "from django.conf import settings\n"
        "settings.configure(\n"
        "    SECRET_KEY='test-not-secret',\n"
        "    DEBUG=False,\n"
        "    INSTALLED_APPS=['django.contrib.contenttypes', 'django.contrib.auth', 'baldur.adapters.django'],\n"
        "    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},\n"
        "    USE_TZ=True,\n"
        "    LOGGING={\n"
        "        'version': 1,\n"
        "        'disable_existing_loggers': False,\n"
        "        'formatters': {'plain': {'format': 'HOST " + _HOST_FORMAT + "'}},\n"
        "        'handlers': {'console': {'class': 'logging.StreamHandler', 'stream': 'ext://sys.stderr', 'formatter': 'plain'}},\n"
        "        'root': {'level': 'INFO', 'handlers': ['console']},\n"
        "    },\n"
        ")\n"
        "django.setup()\n" + _REPORT_ROOT + _PROBE_EMIT
    )

    @pytest.fixture(scope="class")
    def run(self):
        pytest.importorskip("django")
        return _run_child(self._SCRIPT)

    def test_the_root_level_and_handler_list_are_the_users(self, run):
        stdout, _ = run
        (after,) = _reports(stdout)
        assert after["level"] == 20
        assert after["handlers"] == ["StreamHandler"]

    def test_a_baldur_warning_reaches_the_users_handler(self, run):
        _, stderr = run
        assert f"HOST WARNING baldur.probe {_PROBE_EVENT} key='v'" in stderr
        assert "{'event'" not in stderr


class TestAFlaskAppInTheQuickstartOrder:
    """Case (d): ``init_flask(app)`` before ``app.logger`` is first used.
    Flask attaches its default handler only when no handler up the tree
    would show the record; baldur's root handler is one, so the app's
    WARNING is written once."""

    _SCRIPT = (
        "from flask import Flask\n"
        "from baldur.adapters.flask import init_flask\n"
        "app = Flask('probe')\n"
        "init_flask(app)\n"
        "app.logger.warning('flask.host_probe_emitted')\n"
    )

    @pytest.fixture(scope="class")
    def run(self):
        pytest.importorskip("flask")
        return _run_child(self._SCRIPT)

    def test_the_apps_warning_is_written_once(self, run):
        stdout, stderr = run
        assert (stdout + stderr).count("flask.host_probe_emitted") == 1
