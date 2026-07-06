"""
Statistics Adapters Package.

This package provides adapters for the StatisticsRepositoryInterface.
"""

from baldur.adapters.statistics.null import NullStatisticsRepository

__all__ = [
    "NullStatisticsRepository",
]


def get_django_adapter():
    """Get Django statistics adapter (requires Django)."""
    from baldur.adapters.django.statistics import DjangoStatisticsAdapter

    return DjangoStatisticsAdapter
