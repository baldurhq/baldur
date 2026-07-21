"""Unit tests for the canonical PRO-distribution presence probe.

``is_pro_installed`` is the single definition of "is the PRO distribution
importable?" — every tier-resolved composition surface (beat lane entries,
admin route registration, default scheduler jobs) reads it. These tests pin
the probe's truth mapping and its non-raising contract.

Note these are the only tests that legitimately patch ``find_spec`` itself:
they exercise the probe, so its own dependency is the subject. Every other
test simulates a tier through the ``mock_oss_tier`` / ``mock_pro_tier``
fixtures, which patch the probe rather than the import machinery.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.utils.tier import is_pro_installed


class TestIsProInstalledBehavior:
    """Behavior: the probe maps find_spec's result to a bool."""

    @pytest.mark.parametrize(
        ("spec_result", "expected"),
        [
            (object(), True),
            (None, False),
        ],
        ids=["spec_found", "spec_absent"],
    )
    def test_probe_reflects_find_spec_result(self, spec_result, expected):
        """A resolvable spec probes True; an absent one probes False."""
        with patch(
            "importlib.util.find_spec", autospec=True, return_value=spec_result
        ) as mock_find_spec:
            assert is_pro_installed() is expected

        mock_find_spec.assert_called_once_with("baldur_pro")

    def test_probe_returns_bool_not_the_spec_object(self):
        """The probe narrows to bool — callers branch on it, never read a spec."""
        with patch("importlib.util.find_spec", autospec=True, return_value=object()):
            result = is_pro_installed()

        assert result is True
        assert isinstance(result, bool)

    def test_probe_does_not_raise_for_an_absent_top_level_name(self):
        """Non-raising by construction (docstring claim).

        ``find_spec`` raises ``ModuleNotFoundError`` only while traversing a
        *parent package* that is itself absent. The probe targets a top-level
        name, so there is no parent to traverse: an absent distribution returns
        ``None``. Asserted against the real import machinery with a name that
        cannot exist, which is the same shape as ``baldur_pro`` on an OSS-only
        install.
        """
        import importlib.util

        assert importlib.util.find_spec("baldur_definitely_not_installed_pkg") is None

    def test_probe_is_import_free(self):
        """The probe resolves packaging state without importing a PRO symbol.

        This is what makes it import-ordering-independent (and safe to call at
        composition time): ``find_spec`` inspects finders only, so nothing is
        added to ``sys.modules`` by probing.
        """
        import sys

        # Warm-up: the first probe in a process may pull in path-finder
        # machinery (editable-install finders). The claim under test is about
        # the *target* module, so measure a steady-state call.
        is_pro_installed()
        before = set(sys.modules)

        is_pro_installed()

        assert set(sys.modules) - before == set()
