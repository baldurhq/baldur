"""DjangoPostmortemRepository.update_fields unit tests.

Tests for the model-field validation logic that filters out non-model
fields before calling Django's save(update_fields=...).

These tests use MagicMock to simulate Django Model behavior without
requiring a running Django environment.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# =============================================================================
# Helpers — Mock Django Model
# =============================================================================


def _make_mock_model_class(field_names: list[str]):
    """Create a mock Django Model class with specified field names.

    Returns a class whose instances have:
    - Attributes for each field (gettable/settable)
    - _meta.get_fields() returning mock Field objects with .name
    - objects.filter().first() returning the instance
    - save() that records call args
    """
    mock_class = MagicMock()

    # Mock instance
    instance = MagicMock()
    for name in field_names:
        setattr(instance, name, MagicMock())

    # _meta.get_fields()
    mock_fields = []
    for name in field_names:
        f = MagicMock()
        f.name = name
        mock_fields.append(f)
    instance._meta.get_fields.return_value = mock_fields

    # objects.filter().first() -> instance
    mock_class.objects.filter.return_value.first.return_value = instance

    return mock_class, instance


# =============================================================================
# Behavior Tests — update_fields model-field filtering
# =============================================================================


class TestDjangoPostmortemRepoUpdateFieldsBehavior:
    """DjangoPostmortemRepository.update_fields model-field filtering."""

    def _make_repo(self, model_class):
        """Create DjangoPostmortemRepository with injected model class."""
        from baldur.adapters.django.repositories.postmortem import (
            DjangoPostmortemRepository,
        )

        return DjangoPostmortemRepository(model=model_class)

    def test_valid_fields_saved_to_model(self):
        """Valid model fields are set and included in save(update_fields=...).

        Purpose:
            Verify that fields matching model _meta are applied and saved.
        Expected:
            - setattr called for valid field
            - save(update_fields=...) contains only valid field names
        """
        model_cls, instance = _make_mock_model_class(
            ["source", "duration_seconds", "system_snapshot"]
        )
        instance.source = "auto"
        repo = self._make_repo(model_cls)

        result = repo.update_fields("inc-001", {"source": "manual"})

        assert result is True
        instance.save.assert_called_once()
        actual_update_fields = instance.save.call_args[1]["update_fields"]
        assert "source" in actual_update_fields

    def test_non_model_fields_are_excluded(self):
        """Non-model fields are silently skipped from save(update_fields=...).

        Purpose:
            Verify that fields NOT in model _meta are filtered out, preventing
            Django ValueError from save(update_fields=...).
        Expected:
            - Non-model field is NOT in update_fields list
            - Valid model field IS in update_fields list
            - save() is still called with valid fields
        """
        model_cls, instance = _make_mock_model_class(
            ["source", "duration_seconds", "system_snapshot"]
        )
        instance.source = "auto"
        instance.duration_seconds = 60.0
        repo = self._make_repo(model_cls)

        result = repo.update_fields(
            "inc-001",
            {
                "source": "manual",
                "correlation_timeline": {"events": []},  # not a model field
            },
        )

        assert result is True
        actual_update_fields = instance.save.call_args[1]["update_fields"]
        assert "source" in actual_update_fields
        assert "correlation_timeline" not in actual_update_fields

    def test_all_non_model_fields_returns_false(self):
        """Returns False when ALL fields are non-model (nothing to save).

        Purpose:
            Verify that if every field in the dict is invalid, no save occurs.
        Expected:
            - save() is NOT called
            - Returns False
        """
        model_cls, instance = _make_mock_model_class(["source", "duration_seconds"])
        repo = self._make_repo(model_cls)

        result = repo.update_fields(
            "inc-001",
            {
                "nonexistent_field_a": "value",
                "nonexistent_field_b": 123,
            },
        )

        assert result is False
        instance.save.assert_not_called()

    def test_dict_field_deep_merged(self):
        """Dict model fields are deep-merged (not replaced).

        Purpose:
            Verify that dict-type fields merge new keys into existing data.
        Expected:
            - Existing dict keys preserved
            - New keys added
        """
        model_cls, instance = _make_mock_model_class(["system_snapshot", "source"])
        instance.system_snapshot = {"cpu": 80, "memory": 60}
        repo = self._make_repo(model_cls)

        result = repo.update_fields(
            "inc-001",
            {"system_snapshot": {"disk": 90}},
        )

        assert result is True
        assert instance.system_snapshot == {"cpu": 80, "memory": 60, "disk": 90}

    def test_record_not_found_returns_false(self):
        """Returns False when the record does not exist.

        Purpose:
            Verify graceful handling when filter().first() returns None.
        Expected:
            - Returns False without raising
        """
        model_cls = MagicMock()
        model_cls.objects.filter.return_value.first.return_value = None
        repo = self._make_repo(model_cls)

        result = repo.update_fields("missing-id", {"source": "manual"})

        assert result is False
