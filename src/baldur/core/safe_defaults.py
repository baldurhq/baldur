"""
Safe Default Values for Baldur Configuration.

Defines safe default values for every setting.
Falls back to these values on configuration errors.

Fail-Safe Default hardening.

PARTIAL DEPRECATION NOTICE:
- SAFE_DEFAULTS: retained (legacy compatibility)
- VALIDATION_RULES: partially deprecated -> replaced by Field constraints on Pydantic Settings
- FATAL_CONFIGS: retained (prevents dangerous configuration changes)

For new code, use the Pydantic Settings in the baldur.settings module.
"""

from typing import Any

import structlog

from baldur.core.exceptions import ConfigurationError

logger = structlog.get_logger()


# =============================================================================
# Safe Default Values
# =============================================================================

SAFE_DEFAULTS: dict[str, dict[str, Any]] = {
    # Circuit Breaker - conservative settings (opens sooner, prioritizes system protection)
    "circuit_breaker": {
        "enabled": True,  # Always enabled
        "failure_threshold": 5,  # Keep low
        "recovery_timeout": 60,  # 1 minute
        "success_threshold": 2,
        "half_open_max_calls": 3,
        "rate_limit_cascade_threshold": 10,
        "rate_limit_cascade_window_seconds": 60,
        "rate_limit_cascade_rate": 10.0,
        "rate_limit_cascade_minimum_calls": 20,
        "self_ddos_protection_enabled": True,
        "self_ddos_rps_limit": 200,
        "self_ddos_window_seconds": 10,
        "self_ddos_backoff_multiplier": 2.0,
    },
    # DLQ - conservative settings (retains longer, prevents data loss)
    "dlq": {
        "enabled": True,
        "retry_delay": 60,
        "expiry_hours": 72,
        "retention_days": 30,
        "batch_size": 10,
        "max_replay_attempts": 2,
    },
    # Retry - conservative settings (less aggressive, protects the backend)
    "retry": {
        "max_attempts": 3,
        "backoff_strategy": "exponential",
        "backoff_base": 4,
        "base_delay": 1.0,
        "max_delay": 300.0,
        "min_delay": 1,
        "jitter": True,
        "jitter_percent": 25,
    },
    # Rate Limit - reasonable limits
    "rate_limit": {
        "base_delay": 1.0,
        "max_delay": 60.0,
        "jitter_percent": 30.0,
        "default_retry_after": 5.0,
        "backoff_multiplier": 2.0,
        "control_api_rate_limit": 100,
        "control_api_window_seconds": 60,
        "emergency_rate_limit": 10,
        "emergency_window_seconds": 60,
    },
    # SLA - reasonable defaults
    "sla": {
        "default_hours": 24,
    },
    # SLO - Google SRE recommended values
    "slo": {
        "default_window_days": 30,
        "default_target": 0.999,
        "default_fast_burn_rate": 14.4,
        "default_slow_burn_rate": 3.0,
    },
    # Security - strict settings
    "security": {
        "temporary_ban_hours": 1,
        "permanent_ban_threshold": 5,
        "suspicious_ip_cache_timeout": 86400,
        "injection_ban_hours": 24,
    },
    # Forensic - conservative size limits (protects memory)
    "forensic": {
        "error_message_max_length": 500,
        "response_body_max_length": 5000,
        "user_agent_max_length": 500,
        "max_stack_frames": 50,
        "max_context_size_bytes": 65536,  # 64KB
        "include_local_variables": False,  # Disabled for security
        "sanitize_sensitive_data": True,
    },
    # Logging - default INFO level
    "logging": {
        "dlq_log_level": "INFO",
        "circuit_breaker_log_level": "INFO",
        "replay_log_level": "INFO",
        "sla_log_level": "INFO",
        "forensic_log_level": "DEBUG",
        "emergency_log_level": "WARNING",
        "chaos_log_level": "INFO",
        "l2_storage_log_level": "INFO",
        "include_timestamps": True,
        "include_request_id": True,
        "include_user_info": False,  # Disabled for security
        "structured_json": True,
    },
    # Notification - reasonable limits
    "notification": {
        "enabled": True,
        "critical_threshold": 10,
        "warning_threshold": 5,
        "slack_block_text_limit": 3000,
        "description_max_length": 500,
        "action_taken_max_length": 200,
        "title_max_length": 150,
        "notification_timeout_seconds": 10,
    },
    # Metrics - enabled by default
    "metrics": {
        "enabled": True,
        "prefix": "baldur",
        "jitter_enabled": True,
        "jitter_max_delay_seconds": 60.0,
    },
    # Error Budget - Google SRE recommended values
    "error_budget": {
        "threshold_healthy": 75.0,
        "threshold_caution": 50.0,
        "threshold_warning": 20.0,
        "threshold_critical": 0.0,
        "burn_rate_fast_critical": 14.4,
        "burn_rate_fast_warning": 6.0,
        "burn_rate_slow_warning": 3.0,
        "burn_rate_slow_info": 1.0,
        "failsafe_alert_enabled": True,
        "failsafe_cooldown_seconds": 300,
        "heartbeat_enabled": True,
        "heartbeat_interval_seconds": 60,
        "heartbeat_timeout_seconds": 120,
        "recovery_alert_enabled": True,
        "recovery_alert_include_downtime": True,
        "escalation_enabled": True,
    },
    # Idempotency - appropriate TTL
    "idempotency": {
        "default_cache_ttl": 60,
        "extended_cache_ttl": 300,
        "clock_skew_tolerance_seconds": 5.0,
    },
    # Chaos - conservative settings (safety first)
    "chaos": {
        "enabled": False,  # Disabled by default
        "max_blast_radius": 0.05,  # Limited to 5%
        "dry_run": True,  # Dry run by default
        "failure_rate": 0.01,  # 1%
        "latency_max_ms": 1000,
    },
    # Emergency - conservative settings
    "emergency": {
        "auto_trigger_enabled": False,  # Manual trigger only
        "auto_release_enabled": True,
        "gradual_recovery_steps": 5,
        "recovery_step_duration_seconds": 60,
    },
    # Governance - conservative settings for RBAC and approvals
    "governance": {
        "approval_timeout_hours": 24,  # Approval wait time
        "max_approval_retries": 3,  # Max re-approval request count
        "threshold_operator": 0.15,  # Operator-level threshold (15%)
        "threshold_admin": 0.30,  # Admin-level threshold (30%)
        "emergency_expiry_hours": 4,  # Emergency mode default expiry
        "audit_log_retention_days": 90,  # Audit log retention period
        "require_reason_for_changes": True,  # Change reason required
    },
    # L2 Storage - conservative settings for external storage integration
    "l2_storage": {
        "enabled": False,  # Disabled by default (explicit opt-in required)
        "redis_timeout_ms": 50,  # Redis timeout 50ms (L2 is supplementary; fail-fast)
        "reconciliation_interval_seconds": 300,  # Reconciliation check every 5 minutes
        "reconciliation_jitter_percent": 20,  # Reconciliation check jitter 20%
        "max_retry_on_failure": 3,  # Max retries on failure
        "connection_pool_size": 10,  # Connection pool size
    },
    # Drift Threshold - conservative settings for metric drift detection
    "drift_threshold": {
        "enabled": True,  # Enabled by default (anomaly detection needed)
        "warning_percent": 5.0,  # Warn on >=5% variation
        "critical_percent": 20.0,  # Critical on >=20% variation
        "check_interval_seconds": 60,  # Check every 60 seconds
        "window_size_seconds": 300,  # 5-minute window
        "min_samples_required": 10,  # Minimum 10 samples required
        "suppress_duplicate_alerts_seconds": 300,  # Suppress duplicate alerts for 5 minutes
    },
}


# =============================================================================
# Validation Rules
# =============================================================================

# Per-setting validation rules
VALIDATION_RULES: dict[str, dict[str, tuple[Any, Any]]] = {
    "circuit_breaker": {
        "failure_threshold": (1, 100),
        "recovery_timeout": (1, 3600),
        "success_threshold": (1, 100),
        "half_open_max_calls": (1, 100),
        "rate_limit_cascade_rate": (0.0, 100.0),
        "rate_limit_cascade_minimum_calls": (1, 100),
        "self_ddos_rps_limit": (1, 10000),
        "self_ddos_window_seconds": (1, 300),
        "self_ddos_backoff_multiplier": (1.0, 10.0),
    },
    "dlq": {
        "retry_delay": (1, 3600),
        "expiry_hours": (1, 720),
        "retention_days": (1, 365),
        "batch_size": (1, 1000),
        "max_replay_attempts": (1, 10),
    },
    "retry": {
        "max_attempts": (1, 20),
        "backoff_base": (1, 10),
        "base_delay": (0.1, 60.0),
        "max_delay": (1.0, 3600.0),
        "min_delay": (1, 60),
        "jitter_percent": (0, 100),
    },
    "rate_limit": {
        "base_delay": (0.1, 60.0),
        "max_delay": (1.0, 300.0),
        "jitter_percent": (0.0, 100.0),
        "default_retry_after": (0.1, 60.0),
        "backoff_multiplier": (1.0, 10.0),
        "control_api_rate_limit": (1, 10000),
        "emergency_rate_limit": (1, 100),
    },
    "security": {
        "temporary_ban_hours": (1, 168),
        "permanent_ban_threshold": (1, 100),
        "injection_ban_hours": (1, 720),
    },
    "forensic": {
        "error_message_max_length": (50, 5000),
        "response_body_max_length": (100, 100000),
        "user_agent_max_length": (50, 2000),
        "max_stack_frames": (10, 200),
        "max_context_size_bytes": (1024, 1048576),
    },
    "notification": {
        "critical_threshold": (1, 100),
        "warning_threshold": (1, 100),
        "slack_block_text_limit": (100, 10000),
        "description_max_length": (50, 5000),
        "action_taken_max_length": (50, 1000),
        "title_max_length": (20, 500),
        "notification_timeout_seconds": (1, 60),
    },
    "metrics": {
        "jitter_max_delay_seconds": (0.0, 300.0),
    },
    "error_budget": {
        "threshold_healthy": (50.0, 100.0),
        "threshold_caution": (20.0, 80.0),
        "threshold_warning": (5.0, 50.0),
        "threshold_critical": (0.0, 20.0),
        "burn_rate_fast_critical": (10.0, 50.0),
        "burn_rate_fast_warning": (3.0, 15.0),
        "burn_rate_slow_warning": (1.0, 10.0),
        "burn_rate_slow_info": (0.5, 3.0),
        "failsafe_cooldown_seconds": (60, 3600),
        "heartbeat_interval_seconds": (10, 300),
        "heartbeat_timeout_seconds": (30, 600),
    },
    "idempotency": {
        "default_cache_ttl": (1, 3600),
        "extended_cache_ttl": (1, 86400),
        "clock_skew_tolerance_seconds": (0.0, 60.0),
    },
    "chaos": {
        "max_blast_radius": (0.0, 0.5),  # Cannot exceed 50%
        "failure_rate": (0.0, 0.5),  # Cannot exceed 50%
        "latency_max_ms": (0, 10000),
    },
    "sla": {
        "default_hours": (1, 720),
    },
    "slo": {
        "default_window_days": (1, 365),
        "default_target": (0.9, 1.0),
        "default_fast_burn_rate": (1.0, 100.0),
        "default_slow_burn_rate": (0.5, 50.0),
    },
    # Governance validation rules
    "governance": {
        "approval_timeout_hours": (1, 168),  # 1 hour ~ 7 days
        "max_approval_retries": (1, 10),
        "threshold_operator": (0.01, 1.0),  # 1% ~ 100% (percentage)
        "threshold_admin": (0.01, 1.0),  # 1% ~ 100% (percentage)
        "emergency_expiry_hours": (1, 48),  # Max 48 hours
        "audit_log_retention_days": (7, 365),  # 7 days ~ 1 year
    },
    # L2 storage validation rules
    "l2_storage": {
        "redis_timeout_ms": (10, 1000),  # 10ms ~ 1s (L2 is supplementary; fail-fast)
        "reconciliation_interval_seconds": (60, 3600),  # 1 minute ~ 1 hour
        "reconciliation_jitter_percent": (0, 50),  # 0% ~ 50%
        "max_retry_on_failure": (1, 10),
        "connection_pool_size": (1, 100),
    },
    # Drift threshold validation rules
    "drift_threshold": {
        "warning_percent": (1.0, 50.0),  # 1% ~ 50%
        "critical_percent": (5.0, 100.0),  # 5% ~ 100%
        "check_interval_seconds": (10, 600),  # 10 seconds ~ 10 minutes
        "window_size_seconds": (60, 3600),  # 1 minute ~ 1 hour
        "min_samples_required": (1, 100),
        "suppress_duplicate_alerts_seconds": (60, 3600),
    },
}

# Valid log levels
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Valid backoff strategies
VALID_BACKOFF_STRATEGIES = {"exponential", "linear", "constant", "decorrelated_jitter"}


# =============================================================================
# Fatal Config Classification (is_fatal)
# =============================================================================
#
# Fatal configs: critical settings that block system startup when violated
# Non-fatal configs: replaced with a Safe Default on violation, warning only
#
# Design principle:
# - Security, Chaos, Error Budget settings are Fatal (directly tied to system stability)
# - Operational settings like Circuit Breaker and DLQ are Non-fatal (Safe Default applied)
# =============================================================================

FATAL_CONFIGS: dict[str, set[str]] = {
    # Security: core security-related settings
    "security": {
        "injection_ban_hours",  # Essential for SQL injection defense
    },
    # Chaos: misconfiguration in production is fatal
    "chaos": {
        "max_blast_radius",  # System outage above 50%
        "failure_rate",  # Service unavailable above 50%
    },
    # Error Budget: wrong thresholds cause auto-recovery malfunction
    "error_budget": {
        "threshold_critical",  # Threshold cannot be below 0
        "burn_rate_fast_critical",  # Risk of exceeding the burn-rate range
    },
}

# Whether a fatal config violation activates Quarantine Mode (True = LEVEL_3 isolation)
ENABLE_QUARANTINE_ON_FATAL = True


def is_fatal_config(config_type: str, key: str) -> bool:
    """
    Check whether a setting is a Fatal (required) config.

    A fatal config violation triggers:
    - A hard block in CI/CD (abnormal termination)
    - Quarantine Mode (LEVEL_3) at runtime

    Args:
        config_type: Config type (security, chaos, etc.)
        key: Config key

    Returns:
        True if fatal config, False otherwise
    """
    fatal_keys = FATAL_CONFIGS.get(config_type, set())
    return key in fatal_keys


def get_all_fatal_configs() -> dict[str, set[str]]:
    """
    Return the list of all fatal configs.

    Returns:
        A dict of the form {config_type: {key1, key2, ...}, ...}
    """
    return {k: v.copy() for k, v in FATAL_CONFIGS.items()}


# =============================================================================
# Helper Functions
# =============================================================================


def get_safe_default(config_type: str, key: str) -> Any | None:
    """
    Return the safe default value.

    Args:
        config_type: Config type (circuit_breaker, dlq, retry, etc.)
        key: Config key

    Returns:
        The safe default value, or None
    """
    defaults = SAFE_DEFAULTS.get(config_type, {})
    return defaults.get(key)


def get_safe_defaults_for_type(config_type: str) -> dict[str, Any]:
    """
    Return all safe default values for a specific config type.

    Args:
        config_type: Config type

    Returns:
        A dict of safe default values for that config type
    """
    return SAFE_DEFAULTS.get(config_type, {}).copy()


def is_valid_value(config_type: str, key: str, value: Any) -> bool:  # noqa: C901
    """
    Validate a value.

    Args:
        config_type: Config type
        key: Config key
        value: Value to validate

    Returns:
        True if valid, False otherwise
    """
    # None check
    if value is None:
        return False

    # When a range validation rule exists
    rules = VALIDATION_RULES.get(config_type, {})
    if key in rules:
        min_val, max_val = rules[key]
        try:
            if value < min_val or value > max_val:
                return False
        except TypeError:
            # Non-comparable type
            return False

    # Log level validation
    if key.endswith("_log_level") and value not in VALID_LOG_LEVELS:
        return False

    # Backoff strategy validation
    if key == "backoff_strategy" and value not in VALID_BACKOFF_STRATEGIES:
        return False

    # Boolean validation
    return not (
        (key.startswith("enabled") or key.endswith("_enabled"))
        and not isinstance(value, bool)
    )


def validate_with_safe_fallback(
    config_type: str, values: dict[str, Any], log_changes: bool = True
) -> dict[str, Any]:
    """
    Validate config values and fall back to safe values.

    Invalid values are replaced with the Safe Default.

    Args:
        config_type: Config type
        values: Config values to validate
        log_changes: Whether to log changes

    Returns:
        A dict of validated and fallback-applied config values
    """
    result = {}
    defaults = SAFE_DEFAULTS.get(config_type, {})

    for key, value in values.items():
        if not is_valid_value(config_type, key, value):
            safe_value = defaults.get(key)
            if safe_value is not None:
                if log_changes:
                    logger.warning(
                        "safe_default.invalid_using_safe_default",
                        config_type=config_type,
                        config_key=key,
                        config_value=value,
                        safe_value=safe_value,
                    )
                result[key] = safe_value
            else:
                # No safe default: keep the original value but warn
                if log_changes:
                    logger.warning(
                        "safe_default.invalid_no_safe_default",
                        config_type=config_type,
                        config_key=key,
                        config_value=value,
                    )
                result[key] = value
        else:
            result[key] = value

    return result


def validate_all_with_safe_fallback(
    config_dict: dict[str, dict[str, Any]], log_changes: bool = True
) -> dict[str, dict[str, Any]]:
    """
    Validate the entire config dict and fall back to safe values.

    Args:
        config_dict: Config of the form {config_type: {key: value, ...}, ...}
        log_changes: Whether to log changes

    Returns:
        The full validated and fallback-applied config dict
    """
    result = {}
    for config_type, values in config_dict.items():
        result[config_type] = validate_with_safe_fallback(
            config_type, values, log_changes
        )
    return result


def apply_safe_defaults_to_missing(
    config_type: str, values: dict[str, Any]
) -> dict[str, Any]:
    """
    Apply Safe Defaults to missing settings.

    Existing values are kept; Safe Defaults are added only for missing keys.

    Args:
        config_type: Config type
        values: Current config values

    Returns:
        A config dict with Safe Defaults filled in
    """
    defaults = SAFE_DEFAULTS.get(config_type, {})
    result = defaults.copy()
    result.update(values)  # Existing values take precedence
    return result


def get_validation_errors(config_type: str, values: dict[str, Any]) -> dict[str, str]:
    """
    Validate config values and return the list of errors.

    Args:
        config_type: Config type
        values: Config values to validate

    Returns:
        An error dict of the form {key: error_message}
    """
    errors = {}
    rules = VALIDATION_RULES.get(config_type, {})

    for key, value in values.items():
        if value is None:
            errors[key] = "Value cannot be None"
            continue

        # Range validation
        if key in rules:
            min_val, max_val = rules[key]
            try:
                if value < min_val:
                    errors[key] = f"Value {value} is below minimum {min_val}"
                elif value > max_val:
                    errors[key] = f"Value {value} exceeds maximum {max_val}"
            except TypeError:
                errors[key] = f"Value {value!r} is not a valid number"

        # Log level validation
        if key.endswith("_log_level") and value not in VALID_LOG_LEVELS:
            errors[key] = (
                f"Invalid log level: {value}. Must be one of {VALID_LOG_LEVELS}"
            )

        # Backoff strategy validation
        if key == "backoff_strategy" and value not in VALID_BACKOFF_STRATEGIES:
            errors[key] = (
                f"Invalid backoff strategy: {value}. Must be one of {VALID_BACKOFF_STRATEGIES}"
            )

    return errors


# =============================================================================
# Startup Validation
# =============================================================================


class FatalConfigError(ConfigurationError):
    """
    Fatal configuration violation.

    Raised when a setting with is_fatal=True is invalid.
    Used for CI/CD hard block and runtime quarantine mode.
    """

    def __init__(self, violations: dict[str, dict[str, str]]):
        self.violations = violations
        violation_list = [
            f"{config_type}.{key}: {msg}"
            for config_type, keys in violations.items()
            for key, msg in keys.items()
        ]
        super().__init__(
            "Fatal config violations detected:\n" + "\n".join(violation_list)
        )

    def extra_context(self) -> dict:
        ctx = super().extra_context()
        ctx["violations"] = self.violations
        return ctx


class ConfigValidationResult:
    """Config validation result."""

    def __init__(self):
        self.changes_count: int = 0
        self.fatal_violations: dict[str, dict[str, str]] = {}
        self.non_fatal_warnings: dict[str, dict[str, str]] = {}

    @property
    def has_fatal_violations(self) -> bool:
        return len(self.fatal_violations) > 0

    @property
    def is_valid(self) -> bool:
        return not self.has_fatal_violations

    def add_fatal_violation(self, config_type: str, key: str, error_msg: str) -> None:
        """Record a fatal config violation."""
        if config_type not in self.fatal_violations:
            self.fatal_violations[config_type] = {}
        self.fatal_violations[config_type][key] = error_msg

    def add_non_fatal_warning(self, config_type: str, key: str, error_msg: str) -> None:
        """Record a non-fatal warning."""
        if config_type not in self.non_fatal_warnings:
            self.non_fatal_warnings[config_type] = {}
        self.non_fatal_warnings[config_type][key] = error_msg


# config_type -> config attribute mapping (constant)
_CONFIG_TYPE_MAPPING: dict[str, str] = {
    "circuit_breaker": "circuit_breaker",
    "dlq": "dlq",
    "retry": "retry",
    "sla": "sla",
    "security": "security",
    "forensic": "forensic",
    "metrics": "metrics",
    "notification": "notification",
    "rate_limit": "rate_limit",
    "idempotency": "idempotency",
    "chaos": "chaos",
    "error_budget": "error_budget",
}


def _handle_fatal_violation(
    result: ConfigValidationResult,
    config_type: str,
    key: str,
    current: Any,
    error_msg: str,
    log_changes: bool,
) -> None:
    """Handle a fatal config violation."""
    result.add_fatal_violation(config_type, key, error_msg)
    if log_changes:
        logger.error(
            "fatal.invalid_critical_config_violation",
            config_type=config_type,
            config_key=key,
            current=current,
        )


def _handle_non_fatal_violation(
    result: ConfigValidationResult,
    sub_config: Any,
    config_type: str,
    key: str,
    current: Any,
    safe_value: Any,
    error_msg: str,
    log_changes: bool,
) -> None:
    """Handle a non-fatal config violation and apply the Safe Default."""
    result.add_non_fatal_warning(config_type, key, error_msg)
    if log_changes:
        logger.warning(
            "startup.invalid_applying_safe_default",
            config_type=config_type,
            config_key=key,
            current=current,
            safe_value=safe_value,
        )
    try:
        setattr(sub_config, key, safe_value)
        result.changes_count += 1
    except AttributeError:
        # For a frozen dataclass
        if log_changes:
            logger.warning(
                "startup.cannot_modify_frozen",
                config_type=config_type,
                config_key=key,
            )


def _validate_single_config_value(
    result: ConfigValidationResult,
    sub_config: Any,
    config_type: str,
    key: str,
    safe_value: Any,
    log_changes: bool,
) -> None:
    """Validate and handle a single config value."""
    current = getattr(sub_config, key, None)

    if is_valid_value(config_type, key, current):
        return  # Early return if the value is valid

    error_msg = f"Invalid value {current!r}, expected safe default: {safe_value!r}"

    if is_fatal_config(config_type, key):
        _handle_fatal_violation(
            result, config_type, key, current, error_msg, log_changes
        )
    else:
        _handle_non_fatal_violation(
            result,
            sub_config,
            config_type,
            key,
            current,
            safe_value,
            error_msg,
            log_changes,
        )


def _finalize_validation(
    result: ConfigValidationResult,
    log_changes: bool,
    raise_on_fatal: bool,
) -> None:
    """Post-validation handling (logging and raising)."""
    if log_changes and result.changes_count > 0:
        logger.info(
            "startup.applied_safe_default",
            changes_count=result.changes_count,
        )

    if result.has_fatal_violations:
        if log_changes:
            logger.critical(
                "fatal.fatal_config_violations_detected",
                fatal_violations_count=len(result.fatal_violations),
                fatal_violation_keys=list(result.fatal_violations.keys()),
            )
        if raise_on_fatal:
            raise FatalConfigError(result.fatal_violations)


def validate_startup_config(
    config: Any, log_changes: bool = True, raise_on_fatal: bool = False
) -> int:
    """
    Validate settings at startup and apply Safe Defaults.

    Validates every setting on a BaldurConfig instance and replaces
    invalid values with the Safe Default.

    When a fatal config (is_fatal=True) is invalid:
    - raise_on_fatal=True: raises FatalConfigError (for CI/CD)
    - raise_on_fatal=False: logs a warning only (runtime best-effort)

    Args:
        config: BaldurConfig instance
        log_changes: Whether to log changes
        raise_on_fatal: Whether to raise on a fatal config violation

    Returns:
        The number of modified settings

    Raises:
        FatalConfigError: when raise_on_fatal=True and a fatal config is violated
    """
    result = ConfigValidationResult()

    for config_type, attr_name in _CONFIG_TYPE_MAPPING.items():
        sub_config = getattr(config, attr_name, None)
        if sub_config is None:
            continue

        defaults = SAFE_DEFAULTS.get(config_type, {})
        for key, safe_value in defaults.items():
            _validate_single_config_value(
                result, sub_config, config_type, key, safe_value, log_changes
            )

    _finalize_validation(result, log_changes, raise_on_fatal)
    return result.changes_count


def validate_config_preflight(config: Any) -> ConfigValidationResult:
    """
    Pre-flight config validation (for CI/CD).

    Validates all settings and returns the result.
    Does not modify the actual config.

    Args:
        config: BaldurConfig instance

    Returns:
        ConfigValidationResult instance
    """
    result = ConfigValidationResult()

    config_mapping = {
        "circuit_breaker": "circuit_breaker",
        "dlq": "dlq",
        "retry": "retry",
        "sla": "sla",
        "security": "security",
        "forensic": "forensic",
        "metrics": "metrics",
        "notification": "notification",
        "rate_limit": "rate_limit",
        "idempotency": "idempotency",
        "chaos": "chaos",
        "error_budget": "error_budget",
    }

    for config_type, attr_name in config_mapping.items():
        sub_config = getattr(config, attr_name, None)
        if sub_config is None:
            continue

        defaults = SAFE_DEFAULTS.get(config_type, {})

        for key, safe_value in defaults.items():
            current = getattr(sub_config, key, None)

            if not is_valid_value(config_type, key, current):
                is_fatal = is_fatal_config(config_type, key)
                error_msg = (
                    f"Value {current!r} is invalid (safe default: {safe_value!r})"
                )

                if is_fatal:
                    if config_type not in result.fatal_violations:
                        result.fatal_violations[config_type] = {}
                    result.fatal_violations[config_type][key] = error_msg
                else:
                    if config_type not in result.non_fatal_warnings:
                        result.non_fatal_warnings[config_type] = {}
                    result.non_fatal_warnings[config_type][key] = error_msg

    return result


# =============================================================================
# Chaos-Specific Safety Guards
# =============================================================================


def validate_chaos_config(values: dict[str, Any]) -> dict[str, Any]:
    """
    Special validation for Chaos settings.

    Chaos engineering is especially dangerous, so extra safety guards apply.

    Args:
        values: Chaos config values

    Returns:
        Safely validated config values
    """
    result = values.copy()

    # Force-clamp Blast Radius (cannot exceed 50%)
    if "max_blast_radius" in result:
        if result["max_blast_radius"] > 0.5:
            logger.warning(
                "safe_default.chaos_exceeds_clamping",
                max_blast_radius=result["max_blast_radius"],
            )
            result["max_blast_radius"] = 0.5
        if result["max_blast_radius"] < 0:
            result["max_blast_radius"] = 0.0

    # Force-clamp Failure Rate
    if "failure_rate" in result:
        if result["failure_rate"] > 0.5:
            logger.warning(
                "safe_default.chaos_exceeds_clamping",
                failure_rate=result["failure_rate"],
            )
            result["failure_rate"] = 0.5
        if result["failure_rate"] < 0:
            result["failure_rate"] = 0.0

    # Force dry_run in production environments
    import os

    if os.environ.get("DJANGO_SETTINGS_MODULE", "").endswith(
        "production"
    ) and not result.get("dry_run", True):
        logger.warning("safe_default.chaos_production_forcing_true")
        result["dry_run"] = True

    return result
