"""
Baldur System Constants

Framework-agnostic constant definitions for Control API and other components.
"""


class ControlAPIActions:
    """Control API Action constants."""

    ALLOW = "allow"
    BLOCK = "block"
    OVERRIDE = "override"
    RESET = "reset"
    INJECT_FAILURE = "inject_failure"
    INJECT_SUCCESS = "inject_success"

    CHOICES = [
        (ALLOW, "Allow - Enable service operations (CB → CLOSED)"),
        (BLOCK, "Block - Disable service operations (CB → OPEN)"),
        (OVERRIDE, "Override - Temporarily bypass rules"),
        (RESET, "Reset - Revert to default configuration"),
        (INJECT_FAILURE, "Inject Failure - Simulate failures (non-ops only)"),
        (
            INJECT_SUCCESS,
            "Inject Success - Record successes for CB recovery (test only)",
        ),
    ]

    ALL = [ALLOW, BLOCK, OVERRIDE, RESET, INJECT_FAILURE, INJECT_SUCCESS]


class ControlAPIEnvironments:
    """Control API Environment constants."""

    TEST = "test"
    CHAOS = "chaos"
    OPS = "ops"

    CHOICES = [
        (TEST, "Test - CI/CD validation"),
        (CHAOS, "Chaos - Resilience testing"),
        (OPS, "Ops - Production control"),
    ]

    ALL = [TEST, CHAOS, OPS]


class RiskLevels:
    """Risk level constants."""

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"
    FORBIDDEN = "forbidden"
