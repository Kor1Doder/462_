"""3D G-code workpiece simulator — launcher.

The interactive view now lives in the main GUI (``examples/gui.py`` -> the
"Workpiece 3D" tab) and in the standalone widget ``examples/workpiece_view.py``;
the carve engine is ``cncctl.viz.workpiece``. This thin launcher just dispatches:

* no ``--render``  -> open the GPU window (needs the GUI extra: PySide6 +
  pyqtgraph + PyOpenGL); ``uv sync --extra gui``.
* ``--render P.png`` -> headless matplotlib export, no Qt / display (Pi or CI).

Improves on ``reference/gcode_workpiece_simulator_pyqt6.py`` with real 3D, actual
material removal (a carved Z-heightmap, not just the toolpath), tool geometry
(flat / ball), and metrics + CAM collision checks — all built on the real cncctl
parse -> simulate -> carve pipeline.

    uv run python examples/workpiece_sim.py                       # GPU window
    uv run python examples/workpiece_sim.py program.nc           # ... with a file
    uv run python examples/workpiece_sim.py --render out.png --gcode program.nc
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # examples dir -> workpiece_core/view

from workpiece_core import DEMO_GCODE, PRESETS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="3D G-code workpiece simulator")
    parser.add_argument("gcode", nargs="?", type=Path, help="G-code file (omit for the demo)")
    parser.add_argument("--gcode", dest="gcode_opt", type=Path, help="G-code file (alternative)")
    parser.add_argument("--render", type=Path, help="headless: write a PNG and exit (no Qt)")
    parser.add_argument("--size-x", type=float, default=80.0)
    parser.add_argument("--size-y", type=float, default=50.0)
    parser.add_argument("--size-z", type=float, default=10.0)
    parser.add_argument(
        "--origin", type=int, default=0, choices=range(len(PRESETS)), help="origin preset index"
    )
    parser.add_argument("--tool", type=float, default=6.0, help="tool diameter (mm)")
    parser.add_argument("--ball", action="store_true", help="ball-nose tool")
    parser.add_argument("--resolution", type=int, default=220, help="grid cells on the long side")
    parser.add_argument("--no-path", action="store_true", help="omit the toolpath overlay")
    parser.add_argument("--mirror-x", action="store_true", help="mirror the toolpath on X (preview)")
    parser.add_argument("--mirror-y", action="store_true", help="mirror the toolpath on Y (preview)")
    parser.add_argument("--fit", action="store_true", help="size/place the stock to the toolpath")
    parser.add_argument(
        "--engrave-depth", type=float, default=0.0, help="preview: sink Z0 cuts this deep (mm)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path: Path | None = args.gcode or args.gcode_opt
    gcode: str | None = None
    if path is not None:
        try:
            gcode = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 2

    if args.render is not None:
        from workpiece_core import render_png  # lazy: keeps the headless path Qt-free

        report = render_png(
            args.render,
            gcode or DEMO_GCODE,
            size_x=args.size_x,
            size_y=args.size_y,
            size_z=args.size_z,
            preset=PRESETS[args.origin][1],
            diameter=args.tool,
            ball=args.ball,
            resolution=args.resolution,
            show_path=not args.no_path,
            mirror_x=args.mirror_x,
            mirror_y=args.mirror_y,
            fit=args.fit,
            engrave_depth=args.engrave_depth,
        )
        print(report)
        print(f"\nWrote {args.render}")
        return 0

    from workpiece_view import main as gui_main  # lazy: only import Qt/pyqtgraph for the window

    forwarded = ["workpiece_sim", *([str(path)] if path is not None else [])]
    return gui_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
