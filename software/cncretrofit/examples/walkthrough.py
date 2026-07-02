"""End-to-end walkthrough of cncctl — no hardware required.

Drives the whole stack against the in-memory grblHAL simulator
(``tools/grblhal_sim``), exercising connect -> bootstrap -> pre-flight ->
stream -> jog -> calibrate -> render.

Run from the repo root:

    uv run python examples/walkthrough.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

# Make the repo root importable so the demo can use tools/grblhal_sim
# (cncctl itself is installed, so it imports fine either way).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cncctl.calibration.steps_per_mm import corrected_steps_per_mm, run_steps_calibration
from cncctl.config_io import (
    AxesConfig,
    AxisConfig,
    Config,
    MachineConfig,
    MotionConfig,
    TransportConfig,
)
from cncctl.controller.errors import SoftLimitError
from cncctl.controller.messages import Axis
from cncctl.controller.real import RealController
from cncctl.facade import Facade, MachineProfile
from cncctl.gcode.parse import parse_string
from cncctl.viz.analyze import SoftLimits
from cncctl.viz.render import render
from cncctl.viz.simulate import Kinematics, simulate
from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

CONTOUR = """(demo: in-bounds contour with an arc)
G21 G90
G0 X5 Y5
G1 Z-1 F150
G1 X45 F600
G2 X55 Y15 I0 J10
G1 Y45
G1 X5
G1 Y5
G0 Z5
M30
"""

TOO_FAR = """(demo: travels past the X soft limit)
G21 G90
G0 X0 Y0
G1 X250 F600
"""


def banner(title: str) -> None:
    print(f"\n{'=' * 66}\n  {title}\n{'=' * 66}")


def demo_config() -> Config:
    xy = AxisConfig(
        microsteps=8,
        lead_screw_mm=5.0,
        steps_per_mm=200.0,
        max_rate_mm_min=3000.0,
        acceleration_mm_s2=150.0,
        soft_limit_mm=200.0,
    )
    z = AxisConfig(
        microsteps=8,
        lead_screw_mm=2.0,
        steps_per_mm=400.0,
        max_rate_mm_min=1500.0,
        acceleration_mm_s2=100.0,
        soft_limit_mm=80.0,
    )
    return Config(
        machine=MachineConfig(name="EMCO demo mill"),
        transport=TransportConfig(default_port_windows="COM3", default_port_linux="/dev/ttyACM0"),
        axes=AxesConfig(x=xy, y=xy, z=z),
        motion=MotionConfig(junction_deviation_mm=0.01),
    )


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="cncctl-demo-"))
    contour = work / "contour.nc"
    contour.write_text(CONTOUR, encoding="utf-8")
    too_far = work / "too_far.nc"
    too_far.write_text(TOO_FAR, encoding="utf-8")

    banner("1. Wire a controller to a simulated grblHAL machine")
    sim = GrblHalSimulator()
    transport = SimulatedTransport(sim, ack_delay=0.0005, status_interval=0.05)
    controller = RealController(transport, status_rate_hz=20)
    profile = MachineProfile(
        soft_limits=SoftLimits(x=(0.0, 200.0), y=(0.0, 200.0), z=(-50.0, 10.0)),
        kinematics=Kinematics(max_rate_mm_min=3000.0),
    )
    facade = Facade(controller, profile=profile)
    print("   built Facade over RealController over SimulatedTransport")

    banner("2. Bootstrap - push the machine config, verify via $$ (safety inv. 8.7)")
    await facade.bootstrap(demo_config(), port="sim")
    settings = await facade.read_settings()
    print(f"   connected: {controller.is_connected}   state: {facade.state.value}")
    print(f"   $100 (X steps/mm) = {settings.get(100)}")
    print(f"   $102 (Z steps/mm) = {settings.get(102)}")

    banner("3. Pre-flight the contour against soft limits (host-side, safety inv. 8.2)")
    result = await facade.analyze_file(contour)
    lo, hi = result.bounding_box
    print(f"   bounding box: ({lo.x:.0f},{lo.y:.0f},{lo.z:.0f}) -> ({hi.x:.0f},{hi.y:.0f},{hi.z:.0f}) mm")
    print(f"   (note X max = {hi.x:.0f}: the G2 arc bulges past its endpoints)")
    print(f"   travel: {result.total_travel_mm:.1f} mm   est. duration: {result.duration_s:.1f} s")
    print(f"   within soft limits: {result.in_bounds}")

    banner("4. Pre-flight REFUSES an out-of-bounds program")
    bad = await facade.analyze_file(too_far)
    print(f"   within soft limits: {bad.in_bounds}")
    print(f"   violations: {bad.violations}")
    try:
        async for _ in facade.send_program(too_far):
            pass
    except SoftLimitError as exc:
        print(f"   send_program raised {type(exc).__name__}: {exc}")
    sent_bad = sum(1 for line in sim.received_lines if line.startswith("G1 X250"))
    print(f"   out-of-bounds lines actually sent to the device: {sent_bad}")

    banner("5. Stream the contour (character-counted back-pressure)")
    last = None
    async for progress in facade.send_program(contour):
        last = progress
    assert last is not None
    # send_program returns only after the streamer drains (all lines acked).
    print(f"   streamed {last.sent}/{last.total} lines; returned after full drain (all acked)")
    print(f"   final state: {facade.state.value}   device's last line: {sim.received_lines[-1]!r}")

    banner("6. Manual jog")
    await facade.jog(Axis.X, 10.0, 500.0)
    jog_line = next(line for line in sim.received_lines if line.startswith("$J="))
    print(f"   facade.jog(X, 10mm, 500mm/min) -> sent {jog_line!r}")

    banner("7. Steps-per-mm calibration (commanded 100mm, measured 99.2mm)")
    corrected = corrected_steps_per_mm(200.0, 100.0, 99.2)
    print(f"   corrected = 200.0 * 100 / 99.2 = {corrected:.3f} steps/mm")
    await run_steps_calibration(
        facade,
        Axis.X,
        commanded=100.0,
        measured=99.2,
        confirm=lambda _proposal: True,
        emit=lambda message: print(f"   | {message}"),
    )
    print(f"   $100 is now {(await facade.read_settings()).get(100)}")

    banner("8. Render the toolpath to a PNG (2D, headless)")
    trace = simulate(parse_string(CONTOUR), Kinematics(max_rate_mm_min=3000.0))
    png = work / "toolpath.png"
    render(trace).savefig(png, dpi=110)
    print(f"   simulated {len(trace.points)} trace points")
    print(f"   wrote {png} ({png.stat().st_size} bytes)")

    await facade.disconnect()
    banner("Done - no hardware was harmed")
    print(f"   artifacts in {work}")


if __name__ == "__main__":
    asyncio.run(main())
