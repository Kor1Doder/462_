"""Interactive G-code console (MDI) — type a line, run it, see the response.

Prompts '> ' until you type Q / quit / exit, or press Ctrl-C / Ctrl-D.

Console commands (handled locally, not sent verbatim):
    ?       show machine status (live MPos on real hardware)
    !       feed hold       ~  resume       reset  soft reset
    $$      show settings   help  show this text
Anything else is sent to the machine as a G-code / $ line. Motion lines are
soft-limit pre-flighted first and refused if they'd leave the envelope (§8.2).

Try without hardware (the simulator acks lines; it is not a motion simulator,
so MPos only moves on jogs):
    uv run python examples/console.py
Real machine (table clear, spindle off, e-stop in hand):
    uv run python examples/console.py --port /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cncctl.controller.errors import CncError, CommandRejectedError, UnsupportedGcodeError
from cncctl.controller.real import RealController
from cncctl.facade import Facade
from cncctl.gcode.parse import parse_string
from cncctl.transport.serial_transport import SerialTransport
from cncctl.viz.analyze import SoftLimits, analyze
from cncctl.viz.simulate import Kinematics, simulate

# A demo soft-limit envelope + rate for the host-side pre-flight. On a real
# machine you'd derive this from config/machine.toml (MachineProfile.from_config).
_LIMITS = SoftLimits(x=(-1.0, 200.0), y=(-1.0, 200.0), z=(-110.0, 10.0))
_KINEMATICS = Kinematics(max_rate_mm_min=3000.0)

_HELP = __doc__.split("Try without")[0].rstrip() if __doc__ else ""


def _build(port: str | None) -> tuple[Facade, RealController, str]:
    if port:
        controller = RealController(SerialTransport(), status_rate_hz=20)
        return Facade(controller), controller, port
    from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

    transport = SimulatedTransport(GrblHalSimulator(), status_interval=0.05)
    controller = RealController(transport, status_rate_hz=20)
    return Facade(controller), controller, "sim"


def preflight(accepted: list[str], line: str) -> list[str] | None:
    """Soft-limit-check the accumulated program plus ``line``.

    Returns the violations (if any), or ``None`` if in-bounds or the line can't
    be modelled (e.g. a G18 arc) — in which case grbl's own soft limits apply.
    """
    try:
        trace = simulate(parse_string("\n".join([*accepted, line])), _KINEMATICS)
    except UnsupportedGcodeError:
        return None
    return list(analyze(trace, _LIMITS).violations) or None


def _status(controller: RealController) -> str:
    status = controller.last_status
    if status is not None and status.mpos is not None:
        return f"X{status.mpos.x:.2f} Y{status.mpos.y:.2f} Z{status.mpos.z:.2f}  [{controller.state.value}]"
    return f"[{controller.state.value}]"


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive G-code console for cncctl")
    parser.add_argument("--port", default=None, help="serial port (omit to use the simulator)")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def call(coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    facade, controller, target = _build(args.port)
    call(facade.connect(target))
    where = "the SIMULATOR (no hardware)" if args.port is None else f"port {target}"
    print(_HELP)
    print(f"\nconnected to {where}. Type G-code, or 'help'.\n")

    accepted: list[str] = []
    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue
            low = line.lower()
            if low in ("q", "quit", "exit"):
                break
            if low == "help":
                print(_HELP)
            elif line == "?":
                print(" ", _status(controller))
            elif line == "!":
                call(facade.hold())
                print("  feed hold")
            elif line == "~":
                call(facade.resume())
                print("  resume")
            elif low == "reset":
                call(facade.reset())
                print("  soft reset")
            elif line == "$$":
                settings = call(facade.read_settings())
                for key in sorted(settings.values):
                    print(f"  ${key}={settings.values[key]}")
            else:
                _run(call, facade, controller, accepted, line)
    except KeyboardInterrupt:
        print()
    finally:
        call(facade.disconnect())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
    print("bye.")


def _run(
    call: Any, facade: Facade, controller: RealController, accepted: list[str], line: str
) -> None:
    violations = preflight(accepted, line)
    if violations:
        print(f"  REFUSED (soft limits): {'; '.join(violations)}")
        return
    try:
        call(facade.run_line(line))
    except CommandRejectedError as exc:
        print(f"  error:{exc.code}")
        return
    except CncError as exc:
        print(f"  ! {type(exc).__name__}: {exc}")
        return
    accepted.append(line)
    print("  ok  ", _status(controller))


if __name__ == "__main__":
    main()
