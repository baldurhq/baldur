"""Rate-limit adapter test fixtures.

The posture fixture is re-exported here rather than imported by each module:
imported into a test module, the name shadows itself at every parameter that
requests it.
"""

from tests.factories.redis_posture import (
    no_redis_posture,  # noqa: F401 - fixture registration
)
