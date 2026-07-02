# Control software

Host-side software for driving the retrofitted machines over USB-CDC. Two
self-contained Python project trees share the same core package (`cncctl`):

| Tree | What it is | Use it for |
|------|------------|------------|
| **[`pitrofit/`](pitrofit/)** | The **current** operator application: the `cncctl` core plus a touch-friendly PySide6 GUI (jog, homing, G-code sender, 2.5D CAD/CAM, PCB isolation, camera monitoring, 3D workpiece simulation) and a Raspberry Pi kiosk deployment. | Running a machine. |
| **[`cncretrofit/`](cncretrofit/)** | The **base** controller library: the clean, headless `cncctl` core (transport, grblHAL protocol, streamer, facade, G-code + visualization) with its full test suite. `pitrofit` is built on top of this. | Reading the core library and running the tests. |

Both projects manage their own dependencies (`pyproject.toml` + `uv.lock`) and
can be built independently. Start with **`pitrofit/`** to operate a machine; see
**`cncretrofit/`** for the core library architecture and tests.
