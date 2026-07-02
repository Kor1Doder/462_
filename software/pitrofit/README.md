# pitrofit — CNC operator application

The full control application for the retrofitted machines: the `cncctl` core
(see [`../cncretrofit`](../cncretrofit/README.md) for the library architecture)
plus a touch-friendly **PySide6 GUI** and a Raspberry Pi kiosk deployment.

## Features

- **Machine control** — auto-discovering USB connection with graceful reconnect, jog, homing, G54 work-zero, feed-hold / resume / soft-reset, a live status dashboard, and a *Switch Doctor* for diagnosing limit switches.
- **G-code sender** — host-side soft-limit pre-flight, streaming with progress, and hold / resume / cancel.
- **3D workpiece simulator** — GPU-accelerated (pyqtgraph / OpenGL) material-removal preview with line-by-line playback and mirror / fit-to-stock / engrave-depth controls.
- **2.5D CAD/CAM** (`examples/cad_cam.py`) — draw shapes, set stock + origin, emit grblHAL G-code.
- **PCB isolation** (`examples/pcb.py`) — draw traces, pads and a board outline, emit single-sided isolation G-code.
- **Camera monitoring** (`examples/camera.py`) and an experimental click-to-drive **visual aligner** (`examples/visual_align.py`).
- **Touch numpad** (`examples/touch_input.py`) for the keyboard-less Pi panel.

## Run it

**Quickest (Raspberry Pi or dev PC) — no `uv` needed:**

```bash
./run-gui.sh                 # windowed
./run-gui.sh --fullscreen    # kiosk / touch panel
```

The launcher builds a local `.venv` on first run, then just launches the GUI.
Tick **Use simulator** in the top bar to run with no hardware; with a Pico
plugged in, it auto-discovers the serial port and connects.

**With uv (development):**

```bash
uv sync --extra gui
uv run python examples/gui.py
```

## Deploy on a Raspberry Pi 5

Target: a Raspberry Pi 5 (or 4) running Raspberry Pi OS *Trixie* (64-bit),
wired to the grblHAL Pico over USB and launching the GUI fullscreen on boot.
Full guide in [`deploy/README.md`](deploy/README.md); the short version:

```bash
sudo deploy/install.sh        # deps + venv + udev rules + labwc autostart + autologin
sudo reboot
```

## Tests

```bash
uv sync
uv run pytest tests/unit
uv run ruff check && uv run mypy --strict src/cncctl
```

## Architecture

The control core is `cncctl`, layered facade → controller protocol →
streamer / parser → USB transport. See
[`../cncretrofit/README.md`](../cncretrofit/README.md#architecture) for the full
description plus grblHAL protocol and safety notes;
[`src/cncctl/viz/README.md`](src/cncctl/viz/README.md) covers the
simulation / visualization engine.

## License

GPL-3.0-or-later — see [`LICENSE`](LICENSE).
