"""
Startup sub-package for Django app configuration.

Extracts startup responsibilities from ``BaldurConfig.ready()`` into
focused, single-responsibility classes.
"""

from __future__ import annotations

from baldur.adapters.django.startup.env_auditor import EnvironmentAuditor
from baldur.adapters.django.startup.metric_hydrator import MetricHydrator
from baldur.adapters.django.startup.rbac_initializer import (
    BALDUR_GROUPS,
    RBACInitializer,
    create_baldur_groups,
)

__all__ = [
    "EnvironmentAuditor",
    "MetricHydrator",
    "RBACInitializer",
    "BALDUR_GROUPS",
    "create_baldur_groups",
]
