"""``cncctl`` command-line interface.

Currently exposes the calibration subcommands::

    cncctl calibrate steps --axis X --commanded 100 --measured 99.2
    cncctl calibrate backlash --axis X

The ``steps`` flow is two-step: it proposes the corrected ``$100``/``$101``/
``$102`` value, asks for confirmation (unless ``--yes``), then writes and
verifies via ``$$``. User-facing output goes to stdout via a small
writer (no library-level ``print``,).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from cncctl.calibration.backlash import run_backlash_guide
from cncctl.calibration.steps_per_mm import StepsProposal, run_steps_calibration
from cncctl.config_io import default_port, load_config
from cncctl.controller.messages import Axis
from cncctl.controller.real import RealController
from cncctl.facade import Facade
from cncctl.transport.serial_transport import SerialTransport

_AXIS_CHOICES = ("X", "Y", "Z")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``cncctl`` argument parser."""
    parser = argparse.ArgumentParser(prog="cncctl", description="EMCO CNC retrofit controller")
    commands = parser.add_subparsers(dest="command", required=True)

    calibrate = commands.add_parser("calibrate", help="machine calibration tools")
    targets = calibrate.add_subparsers(dest="target", required=True)

    steps = targets.add_parser("steps", help="steps-per-mm calibration")
    steps.add_argument("--axis", required=True, choices=_AXIS_CHOICES)
    steps.add_argument("--commanded", type=float, required=True, help="commanded distance (mm)")
    steps.add_argument("--measured", type=float, required=True, help="measured distance (mm)")
    steps.add_argument("--port", default=None, help="serial port (default: from config / OS)")
    steps.add_argument("--config", type=Path, default=Path("config/machine.toml"))
    steps.add_argument("--yes", action="store_true", help="apply without confirmation")

    backlash = targets.add_parser("backlash", help="backlash measurement (guided)")
    backlash.add_argument("--axis", required=True, choices=_AXIS_CHOICES)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "calibrate" and args.target == "steps":
        return asyncio.run(_run_steps(args))
    if args.command == "calibrate" and args.target == "backlash":
        _run_backlash(args)
        return 0
    return 2  # unreachable: subparsers are required


async def _run_steps(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    port = args.port or default_port(config)
    facade = Facade(RealController(SerialTransport()))
    await facade.connect(port)
    try:
        confirm = (lambda _proposal: True) if args.yes else _stdin_confirm
        applied = await run_steps_calibration(
            facade,
            Axis[args.axis],
            commanded=args.commanded,
            measured=args.measured,
            confirm=confirm,
            emit=_emit,
        )
    finally:
        await facade.disconnect()
    return 0 if applied else 1


def _run_backlash(args: argparse.Namespace) -> None:
    run_backlash_guide(Axis[args.axis], measure=_stdin_measure, emit=_emit)


def _emit(message: str) -> None:
    sys.stdout.write(message + "\n")


def _stdin_confirm(proposal: StepsProposal) -> bool:
    answer = input(f"Apply ${proposal.setting_key}={proposal.value}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def _stdin_measure() -> float:
    return float(input("Enter measured backlash (mm): ").strip())


if __name__ == "__main__":
    raise SystemExit(main())
