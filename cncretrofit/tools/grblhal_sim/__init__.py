"""grblHAL response simulator — line-level protocol fake used by Tier-2 tests (M7).

Not a motion simulator (that is grblHAL's job). See CLAUDE.md §6, §7 M7.
"""

from .loopback import AckDelay, SimulatedTransport
from .simulator import DEFAULT_SETTINGS, GrblHalSimulator, SimulatorConfig

__all__ = [
    "DEFAULT_SETTINGS",
    "AckDelay",
    "GrblHalSimulator",
    "SimulatedTransport",
    "SimulatorConfig",
]
