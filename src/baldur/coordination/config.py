"""
Leader Election settings (backward-compatible re-export).

Actual definitions: baldur.settings.leader_election
"""

from baldur.settings.leader_election import (  # noqa: F401
    LeaderElectionSettings,
    get_leader_election_settings,
    reset_leader_election_settings,
)

__all__ = [
    "LeaderElectionSettings",
    "get_leader_election_settings",
    "reset_leader_election_settings",
]
