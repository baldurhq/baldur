"""
Config Applier Adapters.

Collection of ConfigApplier Protocol implementations.
Each adapter applies or rolls back one system's settings at runtime.

Available Adapters:
    - ThrottleConfigApplier: dedicated to AdaptiveThrottle SLA settings
    - CompositeConfigApplier: composite combining several ConfigAppliers
"""

from baldur.adapters.config_applier.composite import CompositeConfigApplier
from baldur.adapters.config_applier.throttle import ThrottleConfigApplier

__all__ = [
    "ThrottleConfigApplier",
    "CompositeConfigApplier",
]
