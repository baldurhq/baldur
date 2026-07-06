# All tests in this directory are auto-marked ``pytest.mark.dormant``
# via the hook below.  They run in the nightly CI job, not the default PR suite.

from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.path.is_relative_to(_THIS_DIR):
            item.add_marker(pytest.mark.dormant)
