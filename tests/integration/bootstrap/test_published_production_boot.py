"""The production block each getting-started page publishes boots ``baldur.init()``.

Every getting-started page ends with a "Going to production" section whose
``bash`` block is what a reader exports before starting the server. The
production gates inside ``init()`` and those blocks are two statements of one
requirement; when a gate grew a requirement no page named, every reader who
followed the docs got a crash-looping pod, and no test saw it, because each
gate test pins its gate in isolation.

This test reads the blocks and boots ``init()`` with exactly the variables they
export, in a subprocess that sees nothing else:

- every ``BALDUR_*``, ``DJANGO_*`` and ``FALLBACK_POLICY`` variable of the
  parent (the CI integration job sets several; a licence would entitle the
  child) is removed;
- no ``.env`` reaches it. ``baldur.settings`` runs ``load_dotenv()`` at import,
  and under ``python -c`` python-dotenv searches from the working directory
  upward, so the child runs in ``tmp_path`` beside an empty ``.env`` — the
  first file found wins, shadowing every ancestor's — and with
  ``PYTHON_DOTENV_DISABLED=1``.

The child reports, after ``init()``, whether it ran in production and not in
test mode (a test-mode boot would pass vacuously), and which ``BALDUR_*``
names its environment holds. The parent asserts those names equal the parsed
ones, which checks the isolation itself: a leaked variable a future gate needs
would make the case pass while the published block is short.

The Django case writes its own minimal settings module rather than reusing the
test app's, which sets ``BALDUR_TEST_MODE`` and every secret on import. No
Redis server is needed: boot completes against a closed port.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SECTION_HEADING = "## Going to production"
_FRAMEWORK_PAGES = ("django", "fastapi", "flask", "celery")
_STRIPPED_PREFIXES = ("BALDUR_", "DJANGO_")
_STRIPPED_NAMES = frozenset({"FALLBACK_POLICY"})
_CHILD_TIMEOUT_SECONDS = 120
_RESULT_MARKER = "PUBLISHED_BOOT_RESULT="

_DJANGO_SETTINGS_MODULE = "published_boot_settings"
_DJANGO_SETTINGS = """SECRET_KEY = "published-boot-test"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "baldur.adapters.django",
]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
}
USE_TZ = True
"""

_REPORT = f"""
import json, os
from baldur.runtime import get_runtime
runtime = get_runtime()
print({_RESULT_MARKER!r} + json.dumps({{
    "production": runtime.is_production,
    "test_mode": runtime.is_test_mode,
    "baldur_env": sorted(k for k in os.environ if k.startswith("BALDUR_")),
}}))
"""

# What each case runs before the report. The framework is imported first so
# the boot happens in the process shape the page describes; Django boots
# through its app config, whose ready() calls baldur.init().
_BOOTS = {
    "django": "import django\ndjango.setup()\n",
    "fastapi": "import fastapi\nimport baldur\nbaldur.init()\n",
    "flask": "import flask\nimport baldur\nbaldur.init()\n",
    "celery": "import celery\nimport baldur\nbaldur.init()\n",
    "plain": "import baldur\nbaldur.init()\n",
}


def _getting_started_dir() -> Path:
    """The ``docs/getting-started`` directory of the repository holding this file."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "getting-started"
        if candidate.is_dir():
            return candidate
    raise AssertionError("docs/getting-started not found above this test file")


def _published_production_env(page: str) -> dict[str, str]:
    """The variables the page's first production ``bash`` block exports."""
    text = (_getting_started_dir() / f"{page}.md").read_text(encoding="utf-8")
    assert _SECTION_HEADING in text, f"{page}.md has no {_SECTION_HEADING!r}"
    section = text.split(_SECTION_HEADING, 1)[1]
    section = re.split(r"^## ", section, maxsplit=1, flags=re.M)[0]
    block = re.search(r"```bash\n(.*?)```", section, flags=re.S)
    assert block, f"{page}.md: no bash block under {_SECTION_HEADING!r}"
    exports = dict(
        re.findall(r"^export ([A-Z_][A-Z0-9_]*)=(\S+)\s*$", block.group(1), re.M)
    )
    assert exports, f"{page}.md: the production block exports nothing"
    return exports


def _isolated_env(published: dict[str, str]) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_STRIPPED_PREFIXES) and name not in _STRIPPED_NAMES
    }
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env.update(published)
    return env


def _boot(case: str, page: str, tmp_path: Path) -> tuple[dict, dict[str, str]]:
    """Boot ``init()`` in an isolated child; return its report and the block."""
    published = _published_production_env(page)
    env = _isolated_env(published)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    if case == "django":
        (tmp_path / f"{_DJANGO_SETTINGS_MODULE}.py").write_text(
            _DJANGO_SETTINGS, encoding="utf-8"
        )
        env["DJANGO_SETTINGS_MODULE"] = _DJANGO_SETTINGS_MODULE

    completed = subprocess.run(
        [sys.executable, "-c", _BOOTS[case] + _REPORT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, (
        f"{case}: baldur.init() did not boot with the block published in "
        f"{page}.md ({sorted(published)}).\n"
        f"stderr tail:\n{completed.stderr[-3000:]}"
    )
    lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith(_RESULT_MARKER)
    ]
    assert len(lines) == 1, f"{case}: no report line\n{completed.stdout[-2000:]}"
    return json.loads(lines[0][len(_RESULT_MARKER) :]), published


@pytest.mark.parametrize(
    ("case", "page"),
    [
        ("django", "django"),
        ("fastapi", "fastapi"),
        ("flask", "flask"),
        ("celery", "celery"),
        ("plain", "fastapi"),
    ],
)
def test_the_published_production_block_boots(case, page, tmp_path):
    report, published = _boot(case, page, tmp_path)

    assert report["production"] is True, f"{case}: did not boot in production"
    assert report["test_mode"] is False, f"{case}: booted in test mode"
    assert report["baldur_env"] == sorted(
        name for name in published if name.startswith("BALDUR_")
    ), f"{case}: the child saw BALDUR_* variables the block does not export"


def test_every_page_publishes_the_same_production_variables():
    """One requirement written four times must name the same variables."""
    names = {page: sorted(_published_production_env(page)) for page in _FRAMEWORK_PAGES}

    assert len({tuple(v) for v in names.values()}) == 1, names
