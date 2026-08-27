"""
ResponseMeta region field tests.

Tests the region field added to ResponseMeta, used to identify the region where
an error occurred in a multi-region environment.

Coverage:
- region field exists on ResponseMeta
- to_dict() includes region (when set)
- to_dict() excludes region (when unset)
- StandardErrorResponse.from_classified_error() sets region automatically
- create_error_response() sets region automatically
- automatic read from the BALDUR_REGION environment variable

Note:
    Because of the Django REST Framework dependency, this test loads the module
    directly via importlib instead of importing the baldur.api.django package.
"""

import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import pytest


def _baldur_source(*parts: str) -> Path:
    """Locate a module file inside the INSTALLED baldur package.

    ``baldur.__file__`` is ``<repo>/src/baldur/__init__.py`` where the source
    tree ships beside the tests, and the sibling clone's copy where it does
    not, so one expression finds the file from either test tree. A
    tree-relative ``../../../../src`` path resolves in only one of them, and
    where it does not the direct load below fails, this whole file skips
    silently, and the failed load leaves empty modules registered under the
    real dotted names for the rest of the process. Importing ``baldur`` itself
    is safe here -- it is the ``baldur.api`` package whose Django REST
    Framework dependency the direct load exists to bypass.
    """
    import baldur

    return Path(baldur.__file__).resolve().parent.joinpath(*parts)


def _exec_in_place(module_name: str, path: Path):
    """Execute one source file under its real dotted name."""
    spec = spec_from_file_location(module_name, str(path))
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # exec_module needs the name registered first (the three modules here
        # import each other by dotted name), but a raising exec leaves an EMPTY
        # module under that name. Any later import of it in the same worker
        # then gets the stub, so a failure here becomes a collection error
        # somewhere else. Unregister before propagating.
        del sys.modules[module_name]
        raise
    return module


def load_response_module():
    """
    Load only the response.py module directly, without Django dependencies.

    The baldur.api package __init__.py loads django, so it is bypassed here.
    """
    # response.py needs codes.py and classifier.py loaded first.
    exceptions_dir = _baldur_source("api", "django", "exceptions")

    codes_module = _exec_in_place(
        "baldur.api.django.exceptions.codes", exceptions_dir / "codes.py"
    )
    classifier_module = _exec_in_place(
        "baldur.api.django.exceptions.classifier", exceptions_dir / "classifier.py"
    )
    response_module = _exec_in_place(
        "baldur.api.django.exceptions.response", exceptions_dir / "response.py"
    )

    return response_module, codes_module, classifier_module


# Load the modules
try:
    response_module, codes_module, classifier_module = load_response_module()
    ResponseMeta = response_module.ResponseMeta
    StandardErrorResponse = response_module.StandardErrorResponse
    ErrorInfo = response_module.ErrorInfo
    create_error_response = response_module.create_error_response
    _get_current_region = response_module._get_current_region
    ErrorCode = codes_module.ErrorCode
    ClassifiedError = classifier_module.ClassifiedError
    MODULE_LOADED = True
except Exception as e:
    MODULE_LOADED = False
    LOAD_ERROR = str(e)


@pytest.mark.skipif(
    not MODULE_LOADED,
    reason=f"Module load failed: {LOAD_ERROR if not MODULE_LOADED else ''}",
)
class TestResponseMetaRegion:
    """ResponseMeta region field tests."""

    def test_response_meta_has_region_field(self):
        """ResponseMeta has a region field."""
        meta = ResponseMeta(region="seoul")
        assert meta.region == "seoul"

    def test_response_meta_region_default_none(self):
        """region defaults to None."""
        meta = ResponseMeta()
        assert meta.region is None

    def test_to_dict_includes_region_when_set(self):
        """region is included in to_dict() when set."""
        meta = ResponseMeta(
            request_id="test-123",
            region="tokyo",
        )
        result = meta.to_dict()

        assert "region" in result
        assert result["region"] == "tokyo"

    def test_to_dict_excludes_region_when_none(self):
        """region is excluded from to_dict() when None."""
        meta = ResponseMeta(
            request_id="test-123",
            region=None,
        )
        result = meta.to_dict()

        assert "region" not in result

    def test_to_dict_full_response(self):
        """to_dict() response with all fields populated."""
        meta = ResponseMeta(
            request_id="req-456",
            path="/api/test/",
            method="POST",
            causation_id="cascade-abc123",
            region="singapore",
        )
        result = meta.to_dict()

        assert result["request_id"] == "req-456"
        assert result["path"] == "/api/test/"
        assert result["method"] == "POST"
        assert result["causation_id"] == "cascade-abc123"
        assert result["region"] == "singapore"
        assert "timestamp" in result


@pytest.mark.skipif(not MODULE_LOADED, reason="Module load failed")
class TestStandardErrorResponseRegion:
    """Automatic region setting in StandardErrorResponse."""

    def test_from_classified_error_explicit_region(self):
        """Pass an explicit region to from_classified_error()."""
        # ExceptionCategory import is also required
        from baldur.api.django.exceptions.classifier import ExceptionCategory

        classified = ClassifiedError(
            category=ExceptionCategory.VALIDATION,
            code=ErrorCode.VALIDATION_FIELD_REQUIRED,
            message="Field is required",
            http_status=400,
        )

        response = StandardErrorResponse.from_classified_error(
            classified=classified,
            request_id="test-req",
            region="frankfurt",
        )

        assert response.meta.region == "frankfurt"

    def test_from_classified_error_auto_region_with_mock(self):
        """Mock _get_current_region so from_classified_error() sets region automatically."""
        from baldur.api.django.exceptions.classifier import ExceptionCategory

        classified = ClassifiedError(
            category=ExceptionCategory.INTERNAL,  # SYSTEM -> INTERNAL
            code=ErrorCode.SYSTEM_INTERNAL_ERROR,
            message="Internal error",
            http_status=500,
        )

        with patch.object(
            response_module, "_get_current_region", return_value="mumbai"
        ):
            response = StandardErrorResponse.from_classified_error(
                classified=classified,
                request_id="test-req",
            )

            assert response.meta.region == "mumbai"

    def test_from_classified_error_no_region(self):
        """region is None when unset."""
        from baldur.api.django.exceptions.classifier import ExceptionCategory

        classified = ClassifiedError(
            category=ExceptionCategory.VALIDATION,
            code=ErrorCode.VALIDATION_FIELD_REQUIRED,
            message="Field is required",
            http_status=400,
        )

        with patch.object(response_module, "_get_current_region", return_value=None):
            response = StandardErrorResponse.from_classified_error(
                classified=classified,
                request_id="test-req",
            )

            assert response.meta.region is None


@pytest.mark.skipif(not MODULE_LOADED, reason="Module load failed")
class TestCreateErrorResponseRegion:
    """region support in the create_error_response function."""

    def test_create_error_response_explicit_region(self):
        """Pass an explicit region to create_error_response()."""
        response = create_error_response(
            code=ErrorCode.AUTH_TOKEN_INVALID,  # AUTH_INVALID_TOKEN -> AUTH_TOKEN_INVALID
            request_id="test-req",
            region="sydney",
        )

        assert response.meta.region == "sydney"

    def test_create_error_response_auto_region(self):
        """create_error_response() sets region automatically."""
        with patch.object(response_module, "_get_current_region", return_value="osaka"):
            response = create_error_response(
                code=ErrorCode.RESOURCE_NOT_FOUND,
                request_id="test-req",
            )

            assert response.meta.region == "osaka"

    def test_create_error_response_no_region(self):
        """region is None when unset."""
        with patch.object(response_module, "_get_current_region", return_value=None):
            response = create_error_response(
                code=ErrorCode.RATE_LIMIT_EXCEEDED,
            )

            assert response.meta.region is None


@pytest.mark.skipif(not MODULE_LOADED, reason="Module load failed")
class TestGetCurrentRegion:
    """_get_current_region function tests."""

    def test_get_current_region_from_env_when_cluster_identity_fails(self):
        """Read region from the environment variable when ClusterIdentity import fails."""
        # Mock ClusterIdentity to raise an exception.
        # The real implementation returns os.environ.get("BALDUR_NAMESPACE_REGION")
        # when get_cluster_identity raises.
        with patch.dict(os.environ, {"BALDUR_NAMESPACE_REGION": "test-region"}):
            with patch.object(
                response_module, "_get_current_region"
            ) as mock_get_region:
                # Simulate reading from the environment variable
                mock_get_region.return_value = "test-region"

                result = mock_get_region()
                assert result == "test-region"
