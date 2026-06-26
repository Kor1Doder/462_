"""Toolpath simulation, soft-limit analysis, and rendering (M10).

The simulate/analyze core is pure Python and is re-exported here. ``render``
(and its matplotlib import) is intentionally **not** re-exported, so importing
``cncctl.viz`` for the safety-critical analysis does not pull in matplotlib —
import it explicitly with ``from cncctl.viz.render import render``.
See ``viz/README.md`` for the M10 design rationale.
"""

from cncctl.viz.analyze import AnalysisResult, SoftLimits, analyze
from cncctl.viz.simulate import Kinematics, Trace, TracePoint, simulate

__all__ = [
    "AnalysisResult",
    "Kinematics",
    "SoftLimits",
    "Trace",
    "TracePoint",
    "analyze",
    "simulate",
]
