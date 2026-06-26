"""Keyboard jog pendant - drive the tool tip like a remote control.

    A / D   ->  X-  / X+     (left / right)
    W / S   ->  Y+  / Y-     (up / down)
    X / Z   ->  Z+  / Z-     (up / down)
    [ / ]   ->  smaller / larger jog step
    , / .   ->  slower / faster feed
    H       ->  home          R -> soft reset (clears alarm via $H/reset)
    Q / Esc ->  quit

Each keypress is one *step* jog (grbl ``$J=`` incremental). Holding a key
repeats it via the terminal's key-repeat, so it feels continuous.

Try it safely with no hardware (uses the in-memory simulator):

    uv run python examples/pendant.py

Drive the real machine (table clear, spindle off, e-stop in hand!):

    uv run python examples/pendant.py --port /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cncctl.controller.errors import CncError, MachineNotReadyError
from cncctl.controller.messages import Axis
from cncctl.controller.real import RealController
from cncctl.facade import Facade

# key -> (axis, direction)
_JOG = {
    "a": (Axis.X, -1.0),
    "d": (Axis.X, 1.0),
    "w": (Axis.Y, 1.0),
    "s": (Axis.Y, -1.0),
    "z": (Axis.Z, -1.0),
    "x": (Axis.Z, 1.0),
}
_STEPS = [0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 50.0]
_QUIT = {"q", "\x1b", "\x03"}  # q, Esc, Ctrl-C

_HELP = __doc__.split("Try it")[0].rstrip()


class _Keyboard:
    """Read single keypresses without Enter (POSIX / Raspberry Pi terminal)."""

    def __enter__(self) -> _Keyboard:
        # cbreak mode reads one char at a time but keeps Ctrl-C working.
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *_exc: object) -> None:
        import termios

        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read(self) -> str:
        return sys.stdin.read(1)


def _build(port: str | None) -> tuple[Facade, RealController, str]:
    if port:
        controller = RealController(SerialTransport(), status_rate_hz=20)
        return Facade(controller), controller, port
    # No port: drive the in-memory simulator so the demo runs with no hardware.
    from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

    transport = SimulatedTransport(GrblHalSimulator(), status_interval=0.05)
    controller = RealController(transport, status_rate_hz=20)
    return Facade(controller), controller, "sim"


async def _attempt(coro: object) -> None:
    try:
        await coro  # type: ignore[misc]
    except MachineNotReadyError as exc:
        print(f"  ! not ready: {exc}  (try H to home, or R to reset)")
    except CncError as exc:
        print(f"  ! {type(exc).__name__}: {exc}")


def _show(controller: RealController, step: float, feed: float) -> None:
    status = controller.last_status
    if status is not None and status.mpos is not None:
        position = f"X{status.mpos.x:8.2f} Y{status.mpos.y:8.2f} Z{status.mpos.z:8.2f}"
    else:
        position = "(awaiting status)"
    print(f"  {position}   step={step:g}mm  feed={feed:g}mm/min  [{controller.state.value}]")


async def run(port: str | None) -> None:
    facade, controller, target = _build(port)
    where = "the SIMULATOR (no hardware)" if port is None else f"port {target}"
    await facade.connect(target)
    print(_HELP)
    print(f"\nconnected to {where}. Jog away - Q to quit.\n")

    step, feed = 1.0, 500.0
    try:
        with _Keyboard() as keyboard:
            while True:
                key = (await asyncio.to_thread(keyboard.read)).lower()
                if key in _QUIT:
                    break
                if key in _JOG:
                    axis, direction = _JOG[key]
                    await _attempt(facade.jog(axis, direction * step, feed))
                elif key == "[":
                    step = _STEPS[max(0, _STEPS.index(step) - 1)] if step in _STEPS else 1.0
                elif key == "]":
                    step = _STEPS[min(len(_STEPS) - 1, _STEPS.index(step) + 1)] if step in _STEPS else 1.0
                elif key == ",":
                    feed = max(10.0, feed - 100.0)
                elif key == ".":
                    feed += 100.0
                elif key == "h":
                    await _attempt(facade.home())
                elif key == "r":
                    await _attempt(facade.reset())
                else:
                    continue
                await asyncio.sleep(0.06)  # let a status poll reflect the move
                _show(controller, step, feed)
    finally:
        await facade.disconnect()
        print("\ndisconnected. bye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Keyboard jog pendant for cncctl")
    parser.add_argument("--port", default=None, help="serial port (omit to use the simulator)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.port))
    except KeyboardInterrupt:
        print("\ninterrupted.")


# Imported lazily only when --port is given, so the simulator demo needs no serial stack.
from cncctl.transport.serial_transport import SerialTransport  # noqa: E402

if __name__ == "__main__":
    main()
