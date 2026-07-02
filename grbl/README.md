# grblHAL firmware (Raspberry Pi Pico / RP2040)

The motion firmware for the retrofit: [grblHAL](https://github.com/grblHAL)
built for the RP2040 Pico as a **3-axis controller with homing**, talking to the
host over USB-CDC.

## Contents

| Path | What it is |
|------|------------|
| `firmware/grblHAL_pico_3axis_homing.uf2` | Prebuilt, flashable firmware image. |
| `RP2040/my_machine.h` | Board / module configuration (axes, USB serial, probe, spindle). |
| `RP2040/CMakeLists.txt` | CMake build configuration. |
| `build-grblhal.sh` | Reproducible from-scratch build pipeline for **Linux** (bash). |
| `build-grblhal.ps1` | The same pipeline for **Windows** (PowerShell). |
| `README-build.md` | Full build instructions and options. |
| `MODIFICATIONS.md` | What we changed from stock grblHAL, and why. |

## Flash the prebuilt firmware (quickest)

1. Hold **BOOTSEL** on the Pico and plug it into USB — it mounts as a drive named `RPI-RP2`.
2. Copy `firmware/grblHAL_pico_3axis_homing.uf2` onto that drive.
3. The Pico reboots into grblHAL and appears as a serial port (`/dev/ttyACM0` on Linux, `COMx` on Windows).

## Build from source

Full details in **[`README-build.md`](README-build.md)**. In short:

```bash
# Linux
./build-grblhal.sh --setup-tools --fetch-source   # one-time: toolchain + Pico SDK + source
./build-grblhal.sh --clean                         # build the baseline 3-axis .uf2
```

```powershell
# Windows
.\build-grblhal.ps1 -SetupTools -FetchSource
.\build-grblhal.ps1 -Clean
```

The build writes the module/axis/spindle selection into `RP2040/my_machine.h`
so the resulting firmware is self-documenting; the output `.uf2` lands in
`firmware/`.
