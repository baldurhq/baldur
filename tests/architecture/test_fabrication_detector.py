"""Detector-layer tests for the measurement-fallback scanner (748).

``_fabrication_scan`` decides, for the whole tree, which numeric literals count
as a manufactured measurement. G84 then ratchets its output against a committed
verdict baseline — which means a detector that quietly stops matching a shape
produces a *smaller* hit set that still agrees with a baseline regenerated from
the same weakened detector. Nothing in the gate can see that, so the three axes
are pinned here instead.

The name class is a Contract: its token set, its config exclusions and its
counter exclusion are stated in the design and asserted as literals. Eligibility
and merging are Behaviors, driven by synthetic sources so each rule fails for
its own reason — in particular the nested-function rule and the loop-assembled
site, which is the shape the founding exemplar used and which an
expression-level rule missed entirely.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from tests.architecture._fabrication_scan import (
    CONFIG_NAME_SUFFIXES,
    is_measurement_name,
    is_payload_assembly,
    scan_source,
)


def _function(source: str, name: str | None = None) -> ast.AST:
    """The named (or first) function definition in ``source``."""
    tree = ast.parse(textwrap.dedent(source))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (name is None or node.name == name)
    ]
    assert functions, f"no function {name or '<any>'} in source"
    return functions[0]


class TestMeasurementNameContract:
    """The spec'd name class: measurement tokens in, configuration out."""

    @pytest.mark.parametrize(
        "name",
        [
            "failure_rate",
            "error_percent",
            "hit_ratio",
            "health_score",
            "cpu_utilization",
        ],
    )
    def test_is_measurement_name_accepts_each_token_as_a_suffix(
        self, name: str
    ) -> None:
        assert is_measurement_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "failure_rate_5m",
            "error_percent_total",
            "cache_ratio_window",
            "risk_score_v2",
            "pool_utilization_now",
        ],
    )
    def test_is_measurement_name_accepts_each_token_as_an_infix(
        self, name: str
    ) -> None:
        assert is_measurement_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "error_rate_threshold",
            "success_rate_limit",
            "backoff_rate_multiplier",
            "failure_rate_min",
            "utilization_max",
        ],
    )
    def test_is_measurement_name_rejects_each_config_suffix(self, name: str) -> None:
        # Given a configured parameter, whose numeric default is legitimate
        assert is_measurement_name(name) is False

    def test_config_suffix_set_is_the_five_documented_names(self) -> None:
        assert CONFIG_NAME_SUFFIXES == (
            "_threshold",
            "_limit",
            "_multiplier",
            "_min",
            "_max",
        )

    @pytest.mark.parametrize(
        "name", ["retry_count", "requests_total", "dlq_entries", "failures"]
    )
    def test_is_measurement_name_rejects_counters(self, name: str) -> None:
        # Zero is the correct default for "how many happened"
        assert is_measurement_name(name) is False

    @pytest.mark.parametrize(
        "name",
        [
            "separate",  # contains "rate" but carries no token boundary
            "duration_rating",  # "_rate" not followed by "_" or end of name
            "rate",  # bare token, no leading separator
            "scoreboard",
        ],
    )
    def test_is_measurement_name_requires_a_bounded_token(self, name: str) -> None:
        assert is_measurement_name(name) is False


class TestPayloadAssemblyEligibility:
    """Which functions are scanned at all — decided per function, not per node."""

    def test_is_payload_assembly_accepts_a_returned_dict_literal(self) -> None:
        assert is_payload_assembly(_function('def build():\n    return {"a": 1}\n'))

    def test_is_payload_assembly_accepts_a_dict_nested_in_the_return_expression(
        self,
    ) -> None:
        source = 'def build(flag):\n    return {"a": 1} if flag else None\n'

        assert is_payload_assembly(_function(source))

    def test_is_payload_assembly_accepts_a_local_the_function_assigned_a_dict_to(
        self,
    ) -> None:
        # Given the loop-assembly shape: an empty dict filled in, then returned
        source = """
        def build(items):
            out = {}
            for item in items:
                out[item] = 1
            return out
        """

        assert is_payload_assembly(_function(source))

    def test_is_payload_assembly_accepts_an_annotated_dict_local(self) -> None:
        source = """
        def build():
            out: dict[str, int] = {}
            return out
        """

        assert is_payload_assembly(_function(source))

    def test_is_payload_assembly_accepts_a_returned_constructor_call(self) -> None:
        assert is_payload_assembly(_function("def build():\n    return Report(a=1)\n"))

    def test_is_payload_assembly_accepts_a_json_sink_with_no_return(self) -> None:
        source = """
        def handle(response, payload):
            response.json(payload)
        """

        assert is_payload_assembly(_function(source))

    def test_is_payload_assembly_rejects_a_bare_return(self) -> None:
        assert not is_payload_assembly(_function("def build():\n    return\n"))

    def test_is_payload_assembly_rejects_a_returned_call_to_a_lowercase_name(
        self,
    ) -> None:
        assert not is_payload_assembly(_function("def build():\n    return helper()\n"))

    def test_is_payload_assembly_rejects_a_json_attribute_call_with_no_arguments(
        self,
    ) -> None:
        source = """
        def handle(response):
            response.json()
        """

        assert not is_payload_assembly(_function(source))

    def test_is_payload_assembly_decides_a_nested_function_on_its_own_body(
        self,
    ) -> None:
        # Given an outer function whose only dict assembly lives in a nested def
        source = """
        def outer():
            def inner():
                return {"a": 1}
            return inner
        """

        # Then the nested def does not lend its eligibility upward
        assert not is_payload_assembly(_function(source, "outer"))
        assert is_payload_assembly(_function(source, "inner"))


class TestScanMerging:
    """One row per (file, symbol, field); lines are evidence, never identity."""

    def test_scan_source_merges_two_sites_of_one_field_into_one_row(self) -> None:
        # Given the same measurement field assembled at two distinct sites
        source = textwrap.dedent(
            """
            def build(items, flag):
                out = {}
                for item in items:
                    out[item] = {"failure_rate": 0.0}
                if flag:
                    return {"failure_rate": 0.0}
                return out
            """
        )

        # When scanning
        hits = scan_source(source, file="mod.py")

        # Then one row carries both lines, and the count is what regresses
        assert len(hits) == 1
        assert hits[0].field == "failure_rate"
        assert hits[0].occurrences == 2
        assert len(set(hits[0].lines)) == 2
        assert hits[0].key == "mod.py::build::failure_rate"
        assert hits[0].evidence == f"mod.py:{hits[0].lines[0]},{hits[0].lines[1]}"

    def test_scan_source_merges_two_shapes_describing_one_site(self) -> None:
        # Given a kwarg whose value is itself a .get fallback — both shapes match
        source = textwrap.dedent(
            """
            def build(overview):
                return Report(
                    resolution_rate_percent=overview.get("resolution_rate_percent", 0.0)
                )
            """
        )

        # When scanning
        hits = scan_source(source, file="mod.py")

        # Then the site is counted once, not twice
        assert len(hits) == 1
        assert hits[0].field == "resolution_rate_percent"
        assert hits[0].occurrences == 1
        assert hits[0].shape in {"kwarg-get-fallback", "get-fallback"}

    def test_scan_source_counts_two_sites_of_one_field_on_one_line_separately(
        self,
    ) -> None:
        # Given the same field fabricated twice on a single line
        source = 'def build():\n    return {"a": {"hit_ratio": 0.0}, "b": {"hit_ratio": 0.0}}\n'

        # When scanning
        hits = scan_source(source, file="mod.py")

        # Then both sites count: a site keyed on its line alone would merge them
        # and let a second fabricated value ride in under the existing row
        assert len(hits) == 1
        assert hits[0].occurrences == 2
        assert hits[0].lines == (2,)

    def test_scan_source_keeps_two_fields_on_one_line_as_two_rows(self) -> None:
        source = 'def build():\n    return {"failure_rate": 0.0, "hit_ratio": 0.0}\n'

        hits = scan_source(source, file="mod.py")

        assert [hit.field for hit in hits] == ["failure_rate", "hit_ratio"]
        assert {hit.lines for hit in hits} == {(2,)}

    def test_scan_source_does_not_treat_a_boolean_as_a_numeric_literal(self) -> None:
        # Given a boolean beside a real fabricated float on the same dict
        source = 'def build():\n    return {"success_rate": True, "hit_ratio": 0.0}\n'

        # Then only the numeric fallback is flagged
        assert [hit.field for hit in scan_source(source)] == ["hit_ratio"]

    def test_scan_source_flags_a_bare_numeric_kwarg(self) -> None:
        source = "def build():\n    return Report(failure_rate=0.0)\n"

        hits = scan_source(source, file="mod.py")

        assert [(hit.field, hit.shape) for hit in hits] == [("failure_rate", "kwarg")]

    def test_scan_source_skips_a_function_that_assembles_no_payload(self) -> None:
        # Given a measurement-named numeric kwarg in a logging call
        source = "def emit(logger):\n    logger.info('done', failure_rate=0.0)\n"

        # Then eligibility keeps it out of the hit set entirely
        assert scan_source(source) == []

    def test_scan_source_attributes_a_method_to_its_class_qualified_symbol(
        self,
    ) -> None:
        source = textwrap.dedent(
            """
            class Handler:
                def build(self):
                    return {"failure_rate": 0.0}
            """
        )

        hits = scan_source(source, file="mod.py")

        assert [hit.symbol for hit in hits] == ["Handler.build"]
        assert hits[0].key == "mod.py::Handler.build::failure_rate"

    def test_scan_source_scans_a_nested_function_as_its_own_candidate(self) -> None:
        # Given an outer function that is not itself a payload assembly
        source = textwrap.dedent(
            """
            def outer():
                def inner():
                    return {"failure_rate": 0.0}
                return inner
            """
        )

        # Then the nested assembly is still reached, under its own qualname
        assert [hit.symbol for hit in scan_source(source)] == ["outer.<locals>.inner"]

    def test_scan_source_returns_empty_for_unparsable_source(self) -> None:
        assert scan_source("def build(:\n") == []

    def test_scan_source_sorts_rows_by_symbol_then_field(self) -> None:
        source = textwrap.dedent(
            """
            def zeta():
                return {"hit_ratio": 0.0, "failure_rate": 0.0}

            def alpha():
                return {"error_percent": 0.0}
            """
        )

        hits = scan_source(source, file="mod.py")

        assert [(hit.symbol, hit.field) for hit in hits] == [
            ("alpha", "error_percent"),
            ("zeta", "failure_rate"),
            ("zeta", "hit_ratio"),
        ]
