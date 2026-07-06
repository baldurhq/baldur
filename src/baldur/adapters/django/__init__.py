"""
Django Adapters Package for Baldur System.

This package provides Django-specific adapters for statistics and dashboards.
Note: Runtime repositories use Redis (not Django ORM) for performance.

Provides:
- BaldurConfig: Django AppConfig for Baldur system
- create_baldur_groups: RBAC group creation signal handler
- AbstractFailedOperation: Domain-free abstract model for DLQ entries
- AbstractPostmortemRecord: Domain-free abstract model for Postmortem records
- BasePostmortemRecordAdmin: Base Admin class for Postmortem records
- BaseDLQEntryAdmin: Base Admin class for DLQ (FailedOperation) entries
- BaseCircuitBreakerStateAdmin: Base Admin class for Circuit Breaker states
- DjangoStatisticsAdapter: Statistics adapter using Django ORM
- connect_session_signals: Wire up Django session signal handlers
- disconnect_session_signals: Detach Django session signal handlers (test-only)
- configure_baldur: Invoked from consumer settings.py to auto-wrap configuration

Status: Public
"""

from typing import TYPE_CHECKING

from baldur.adapters.django.apps import BaldurConfig
from baldur.adapters.django.auto_config import configure_baldur
from baldur.adapters.django.startup.rbac_initializer import (
    BALDUR_GROUPS,
    create_baldur_groups,
)
from baldur.adapters.django.statistics import DjangoStatisticsAdapter

if TYPE_CHECKING:
    from baldur.adapters.django.admin import (
        BaseCircuitBreakerStateAdmin,
        BaseDLQEntryAdmin,
        BasePostmortemRecordAdmin,
    )
    from baldur.adapters.django.models import (
        AbstractFailedExternalRequest,
        AbstractFailedOperation,
        AbstractPostmortemRecord,
        AbstractSecurityIncident,
    )


# Lazy import for AbstractFailedOperation (requires Django)
def get_abstract_failed_operation() -> "type[AbstractFailedOperation]":
    """
    Get AbstractFailedOperation model class.

    This is a lazy import to avoid Django dependency at module load time.

    Returns:
        AbstractFailedOperation class

    Raises:
        ImportError: If Django is not installed
    """
    from baldur.adapters.django.models import AbstractFailedOperation

    return AbstractFailedOperation


def get_abstract_postmortem_record() -> "type[AbstractPostmortemRecord]":
    """
    Get AbstractPostmortemRecord model class.

    This is a lazy import to avoid Django dependency at module load time.

    Returns:
        AbstractPostmortemRecord class

    Raises:
        ImportError: If Django is not installed
    """
    from baldur.adapters.django.models import AbstractPostmortemRecord

    return AbstractPostmortemRecord


def get_base_postmortem_admin() -> "type[BasePostmortemRecordAdmin]":
    """
    Get BasePostmortemRecordAdmin class.

    This is a lazy import to avoid Django dependency at module load time.

    Returns:
        BasePostmortemRecordAdmin class

    Raises:
        ImportError: If Django is not installed
    """
    from baldur.adapters.django.admin import BasePostmortemRecordAdmin

    return BasePostmortemRecordAdmin


def get_base_dlq_admin() -> "type[BaseDLQEntryAdmin]":
    """
    Get BaseDLQEntryAdmin class.

    This is a lazy import to avoid Django dependency at module load time.

    Returns:
        BaseDLQEntryAdmin class

    Raises:
        ImportError: If Django is not installed
    """
    from baldur.adapters.django.admin import BaseDLQEntryAdmin

    return BaseDLQEntryAdmin


def get_base_circuit_breaker_admin() -> "type[BaseCircuitBreakerStateAdmin]":
    """
    Get BaseCircuitBreakerStateAdmin class.

    This is a lazy import to avoid Django dependency at module load time.

    Returns:
        BaseCircuitBreakerStateAdmin class

    Raises:
        ImportError: If Django is not installed
    """
    from baldur.adapters.django.admin import BaseCircuitBreakerStateAdmin

    return BaseCircuitBreakerStateAdmin


def get_abstract_failed_external_request() -> "type[AbstractFailedExternalRequest]":
    """
    Get AbstractFailedExternalRequest model class (223 Host App Decoupling).

    Returns:
        AbstractFailedExternalRequest class
    """
    from baldur.adapters.django.models import AbstractFailedExternalRequest

    return AbstractFailedExternalRequest


def get_abstract_security_incident() -> "type[AbstractSecurityIncident]":
    """
    Get AbstractSecurityIncident model class (223 Host App Decoupling).

    Returns:
        AbstractSecurityIncident class
    """
    from baldur.adapters.django.models import AbstractSecurityIncident

    return AbstractSecurityIncident


__all__ = [
    # AppConfig
    "BaldurConfig",
    "create_baldur_groups",
    "BALDUR_GROUPS",
    # Adapters
    "DjangoStatisticsAdapter",
    # Lazy imports for models
    "get_abstract_failed_operation",
    "get_abstract_postmortem_record",
    "get_abstract_failed_external_request",
    "get_abstract_security_incident",
    # Lazy imports for admin
    "get_base_postmortem_admin",
    "get_base_dlq_admin",
    "get_base_circuit_breaker_admin",
    # Auto-configuration
    "configure_baldur",
]
