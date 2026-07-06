"""
Coordination unit test isolation.

The RedisLeaderElector daemon-worker tracking fixture moved to
``tests/pro/unit/coordination/conftest.py`` with the elector itself
(599 D2/D14 — the concrete implementation lives in
``baldur_pro.coordination.redis_elector``). The OSS chassis tests in this
package construct only ``NoOpLeaderElector`` / mocks, which spawn no
threads, so no live-worker teardown is needed here.

Reference:
    the unit test guidelines §6.5 (xdist parallel isolation).
"""

from __future__ import annotations
