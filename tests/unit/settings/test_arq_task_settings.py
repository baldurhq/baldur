"""
ArqTaskSettings unit tests.

Verifies arq task queue settings: default values (contract),
boundary validation, env override, and singleton lifecycle.

Test Categories:
    A. Contract: default values from design doc §6.1
    B. Behavior: boundary validation, env override, singleton pair
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from baldur.settings.arq_task import (
    ArqTaskSettings,
    get_arq_task_settings,
    reset_arq_task_settings,
)

# =============================================================================
# A. Contract Tests
# =============================================================================


class TestArqTaskSettingsDefaultsContract:
    """ArqTaskSettings default value contract verification (§6.1)."""

    def test_enabled_default_is_false(self):
        """enabled defaults to False (fail-safe)."""
        assert ArqTaskSettings().enabled is False

    def test_redis_host_default(self):
        """redis_host defaults to 'localhost'."""
        assert ArqTaskSettings().redis_host == "localhost"

    def test_redis_port_default(self):
        """redis_port defaults to 6379."""
        assert ArqTaskSettings().redis_port == 6379

    def test_redis_database_default(self):
        """redis_database defaults to 1 (separate from Celery DB 0)."""
        assert ArqTaskSettings().redis_database == 1

    def test_redis_password_default_is_none(self):
        """redis_password defaults to None."""
        assert ArqTaskSettings().redis_password is None

    def test_redis_ssl_default_is_false(self):
        """redis_ssl defaults to False."""
        assert ArqTaskSettings().redis_ssl is False

    def test_max_jobs_default(self):
        """max_jobs defaults to 10."""
        assert ArqTaskSettings().max_jobs == 10

    def test_job_timeout_default(self):
        """job_timeout defaults to 300 seconds."""
        assert ArqTaskSettings().job_timeout == 300

    def test_max_tries_default(self):
        """max_tries defaults to 3."""
        assert ArqTaskSettings().max_tries == 3

    def test_retry_delay_default(self):
        """retry_delay defaults to 60 seconds."""
        assert ArqTaskSettings().retry_delay == 60

    def test_queue_name_default(self):
        """queue_name defaults to 'arq:baldur'."""
        assert ArqTaskSettings().queue_name == "arq:baldur"

    def test_health_check_interval_default(self):
        """health_check_interval defaults to 60 seconds."""
        assert ArqTaskSettings().health_check_interval == 60

    def test_keep_result_default(self):
        """keep_result defaults to 3600 seconds."""
        assert ArqTaskSettings().keep_result == 3600

    def test_enqueue_batch_size_default(self):
        """enqueue_batch_size defaults to 100 (§344)."""
        assert ArqTaskSettings().enqueue_batch_size == 100

    def test_enqueue_failure_threshold_default(self):
        """enqueue_failure_threshold defaults to 0.5 (§344)."""
        assert ArqTaskSettings().enqueue_failure_threshold == 0.5

    def test_env_prefix_is_baldur_arq(self):
        """Environment variable prefix is BALDUR_ARQ_TASK_."""
        assert ArqTaskSettings.model_config["env_prefix"] == "BALDUR_ARQ_TASK_"


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestArqTaskSettingsBoundaryBehavior:
    """Boundary validation for constrained fields."""

    # -- redis_port: ge=1, le=65535 --

    def test_redis_port_below_minimum_raises(self):
        """redis_port=0 violates ge=1."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(redis_port=0)

    def test_redis_port_at_minimum_accepted(self):
        """redis_port=1 is valid."""
        assert ArqTaskSettings(redis_port=1).redis_port == 1

    def test_redis_port_at_maximum_accepted(self):
        """redis_port=65535 is valid."""
        assert ArqTaskSettings(redis_port=65535).redis_port == 65535

    def test_redis_port_above_maximum_raises(self):
        """redis_port=65536 violates le=65535."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(redis_port=65536)

    # -- max_jobs: ge=1, le=100 --

    def test_max_jobs_below_minimum_raises(self):
        """max_jobs=0 violates ge=1."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(max_jobs=0)

    def test_max_jobs_at_minimum_accepted(self):
        """max_jobs=1 is valid."""
        assert ArqTaskSettings(max_jobs=1).max_jobs == 1

    def test_max_jobs_at_maximum_accepted(self):
        """max_jobs=100 is valid."""
        assert ArqTaskSettings(max_jobs=100).max_jobs == 100

    def test_max_jobs_above_maximum_raises(self):
        """max_jobs=101 violates le=100."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(max_jobs=101)

    # -- job_timeout: ge=30, le=3600 --

    def test_job_timeout_below_minimum_raises(self):
        """job_timeout=29 violates ge=30."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(job_timeout=29)

    def test_job_timeout_at_minimum_accepted(self):
        """job_timeout=30 is valid."""
        assert ArqTaskSettings(job_timeout=30).job_timeout == 30

    def test_job_timeout_at_maximum_accepted(self):
        """job_timeout=3600 is valid."""
        assert ArqTaskSettings(job_timeout=3600).job_timeout == 3600

    def test_job_timeout_above_maximum_raises(self):
        """job_timeout=3601 violates le=3600."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(job_timeout=3601)

    # -- max_tries: ge=0, le=10 --

    def test_max_tries_at_zero_accepted(self):
        """max_tries=0 (no retries) is valid."""
        assert ArqTaskSettings(max_tries=0).max_tries == 0

    def test_max_tries_above_maximum_raises(self):
        """max_tries=11 violates le=10."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(max_tries=11)

    # -- redis_database: ge=0, le=15 --

    def test_redis_database_at_zero_accepted(self):
        """redis_database=0 is valid."""
        assert ArqTaskSettings(redis_database=0).redis_database == 0

    def test_redis_database_above_maximum_raises(self):
        """redis_database=16 violates le=15."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(redis_database=16)

    # -- keep_result: ge=60, le=86400 --

    def test_keep_result_below_minimum_raises(self):
        """keep_result=59 violates ge=60."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(keep_result=59)

    def test_keep_result_at_maximum_accepted(self):
        """keep_result=86400 is valid."""
        assert ArqTaskSettings(keep_result=86400).keep_result == 86400

    # -- enqueue_batch_size: ge=10, le=1000 --

    def test_enqueue_batch_size_below_minimum_raises(self):
        """enqueue_batch_size=9 violates ge=10."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(enqueue_batch_size=9)

    def test_enqueue_batch_size_at_minimum_accepted(self):
        """enqueue_batch_size=10 is valid."""
        assert ArqTaskSettings(enqueue_batch_size=10).enqueue_batch_size == 10

    def test_enqueue_batch_size_at_maximum_accepted(self):
        """enqueue_batch_size=1000 is valid."""
        assert ArqTaskSettings(enqueue_batch_size=1000).enqueue_batch_size == 1000

    def test_enqueue_batch_size_above_maximum_raises(self):
        """enqueue_batch_size=1001 violates le=1000."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(enqueue_batch_size=1001)

    # -- enqueue_failure_threshold: ge=0.0, le=1.0 --

    def test_enqueue_failure_threshold_at_minimum_accepted(self):
        """enqueue_failure_threshold=0.0 is valid (abort on any failure)."""
        s = ArqTaskSettings(enqueue_failure_threshold=0.0)
        assert s.enqueue_failure_threshold == 0.0

    def test_enqueue_failure_threshold_at_maximum_accepted(self):
        """enqueue_failure_threshold=1.0 is valid (abort only on 100% failure)."""
        s = ArqTaskSettings(enqueue_failure_threshold=1.0)
        assert s.enqueue_failure_threshold == 1.0

    def test_enqueue_failure_threshold_above_maximum_raises(self):
        """enqueue_failure_threshold=1.1 violates le=1.0."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(enqueue_failure_threshold=1.1)

    def test_enqueue_failure_threshold_below_minimum_raises(self):
        """enqueue_failure_threshold=-0.1 violates ge=0.0."""
        with pytest.raises(ValidationError):
            ArqTaskSettings(enqueue_failure_threshold=-0.1)


class TestArqTaskSettingsEnvOverrideBehavior:
    """Environment variable override verification."""

    def test_env_override_enabled(self):
        """BALDUR_ARQ_TASK_ENABLED=true overrides default."""
        with patch.dict("os.environ", {"BALDUR_ARQ_TASK_ENABLED": "true"}):
            settings = ArqTaskSettings()
        assert settings.enabled is True

    def test_env_override_redis_host(self):
        """BALDUR_ARQ_TASK_REDIS_HOST overrides default."""
        with patch.dict("os.environ", {"BALDUR_ARQ_TASK_REDIS_HOST": "redis.prod"}):
            settings = ArqTaskSettings()
        assert settings.redis_host == "redis.prod"

    def test_env_override_max_jobs(self):
        """BALDUR_ARQ_TASK_MAX_JOBS overrides default."""
        with patch.dict("os.environ", {"BALDUR_ARQ_TASK_MAX_JOBS": "50"}):
            settings = ArqTaskSettings()
        assert settings.max_jobs == 50


class TestArqTaskSettingsSingletonBehavior:
    """Singleton pair (get/reset) lifecycle verification."""

    def setup_method(self):
        """Ensure clean state before each test."""
        reset_arq_task_settings()

    def teardown_method(self):
        """Clean up after each test."""
        reset_arq_task_settings()

    def test_get_returns_same_instance(self):
        """get_arq_task_settings() returns cached singleton."""
        first = get_arq_task_settings()
        second = get_arq_task_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset_arq_task_settings() causes next get to return new instance."""
        first = get_arq_task_settings()
        reset_arq_task_settings()
        second = get_arq_task_settings()
        assert first is not second

    def test_reset_is_idempotent(self):
        """Calling reset_arq_task_settings() multiple times is safe."""
        reset_arq_task_settings()
        reset_arq_task_settings()
        result = get_arq_task_settings()
        assert result is not None
