# grblHAL for Raspberry Pi Pico (RP2040) — from-scratch `.uf2` build pipeline

This directory contains a self-contained, reproducible pipeline that compiles
**grblHAL** firmware for a **generic Raspberry Pi Pico** board into a flashable
`*.uf2` file — a local replacement for the grblHAL web builder for our target.

The pipeline is provided for both platforms with identical logic and options:

- **Linux** (dev PC or Raspberry Pi): [`build-grblhal.sh`](build-grblhal.sh) — requires `curl git cmake ninja-build tar xz-utils`.
- **Windows** (PowerShell): [`build-grblhal.ps1`](build-grblhal.ps1).

### Linux (bash)

```bash
# 1. First run: download toolchain + SDK + tools, fetch source, build baseline
./build-grblhal.sh --setup-tools --fetch-source

# 2. Subsequent builds (offline). Baseline generic Pico:
./build-grblhal.sh --clean

# 3. With modules + 4 axes:
./build-grblhal.sh --clean --axes 4 --modules eeprom,eeprom_fram,fans,rgb,modbus

# 4. See every selectable module:
./build-grblhal.sh --list-modules
```

### Windows (PowerShell)

```powershell
.\build-grblhal.ps1 -SetupTools -FetchSource
.\build-grblhal.ps1 -Clean
.\build-grblhal.ps1 -Clean -Axes 4 -Modules eeprom,eeprom_fram,fans,rgb,modbus
.\build-grblhal.ps1 -ListModules
```

The two scripts take the same options (PowerShell `-PascalCase` ⇄ bash
`--kebab-case`): `--board`, `--axes`, `--modules`, `--spindles`, `--output`,
`--jobs`, `--homing`, `--homing-order`, `--limit-switches`, `--require-homing`,
`--no-estop`, `--no-control-inputs`, `--invert-control`.

Output lands in `firmware/grblHAL_<board>_<axes>axis[_<modules>].uf2`.
Flash it: hold **BOOTSEL**, plug the Pico in, copy the `.uf2` onto the `RPI-RP2`
USB drive that appears.

---

## 1. Baseline configuration ("generic board" defaults)

These match the web-builder screen we standardized on. **All of them are already
grblHAL's shipped defaults** at compatibility level 0, so the baseline needs *no*
source edits:

| Setting           | Value                | How it is set                              |
|-------------------|----------------------|--------------------------------------------|
| Driver            | RP2040 (Pi Pico)     | `grblHAL/RP2040` driver, `PICO_BOARD=pico` |
| Board             | Generic              | no `BOARD_*` macro → `boards/generic_map.h` |
| Connection        | Native USB           | `USB_SERIAL_CDC 1`                          |
| Number of axes    | 3                    | default `N_AXIS` (generic_map.h)           |
| Probe input       | enabled              | default (`PROBE_PIN` = GP28)               |
| Spindle 1         | PWM                  | default on-board PWM spindle (GP15)        |
| Compatibility     | grblHAL              | `COMPATIBILITY_LEVEL 0`                     |
| Reset as E-Stop   | enabled              | `ESTOP_ENABLE` auto = 1 when compat ≤ 1    |

The script still writes them **explicitly** into `RP2040/my_machine.h` so the
build is self-documenting.

---

## 2. Toolchain (pinned versions)

Matches what `grblHAL/RP2040/CMakeLists.txt` expects:

| Component        | Version       | Source                                              |
|------------------|---------------|-----------------------------------------------------|
| ARM GNU toolchain| 14.2.Rel1     | developer.arm.com (mingw-w64 i686 zip)              |
| Pico SDK         | 2.1.1         | `github.com/raspberrypi/pico-sdk` (tag 2.1.1)       |
| pioasm + picotool| 2.1.1         | `github.com/raspberrypi/pico-sdk-tools` (prebuilt)  |

All are installed locally under `tools\` by `-SetupTools`; nothing is installed
system-wide. Host build deps already present: Git, CMake ≥ 3.13, Ninja, Python 3.

Layout created:

```
tools\
  arm\                       ARM GNU toolchain (arm-none-eabi-gcc, ...)
  pico-sdk\                  Pico SDK 2.1.1 (+ lib/tinyusb)
  pico-sdk-tools-bin\pioasm\ prebuilt pioasm + pioasmConfig.cmake
  picotool-bin\picotool\     prebuilt picotool + picotoolConfig.cmake
RP2040\                      grblHAL driver + submodules (the source tree)
firmware\                    output *.uf2 files
```

---

## 3. Modules

`-Modules a,b,c` enables features. Two kinds:

* **`[define]`** modules become `#define` lines in `my_machine.h`.
* **`[cmake]`** modules become `-D...=ON` CMake options (networking / Bluetooth /
  HPGL); WiFi/Bluetooth also force `PICO_BOARD=pico_w`.

Run `.\build-grblhal.ps1 -ListModules` for the live list. Verified to build on
the **generic** map (3/4/8-axis): `eeprom`, `eeprom_fram`, `fans`, `rgb`,
`pwm_servo`, `bltouch`, `eventout`, `feed_override`, `homing_pulloff`,
`safety_door`, `probe2`, `toolsetter`, `modbus`, `keypad_serial`, `mpg`,
plus the I/O-expander and laser/plasma/embroidery plugins.

### Modules that need a real board map (rejected early by the script)

The bare `generic_map.h` intentionally does not wire the pins these need, so
grblHAL emits a compile-time `#error`. The script blocks them *before* compiling
and tells you why:

| Module                 | Needs                          |
|------------------------|--------------------------------|
| `sdcard`, `sdcard_ymodem` | `SD_CS` + SPI bus pins      |
| `keypad` (I2C)         | `I2C_STROBE` interrupt pin      |
| `trinamic`             | a Trinamic-capable board map    |

To use these, pick a board map that provides the pins (e.g. `BOARD_PICO_CNC`,
`BOARD_BTT_SKR_PICO_10`) or add a custom `boards/my_machine_map.h`.

### Serial-port budget

With Native USB as the primary stream, the generic RP2040 build has room for
**one** additional serial consumer. Selecting more than one of
`modbus`, `keypad_serial`, `mpg`, `esp_at` triggers grblHAL's
*"Too many options that requires a serial port"* `#error`. The script warns when
you do this.

---

## 3a. Machine defaults: homing & alarm (`-Homing`)

Compiled-in defaults for the homing cycle, limit-switch polarity and E-Stop.
These fix the two field issues (boots in `Alarm`/`$X` won't clear; `$HX/$HY/$HZ`
disabled and homing order/Y-skip). Details in [`MODIFICATIONS.md`](MODIFICATIONS.md) §D.

```powershell
.\build-grblhal.ps1 -Clean -Board pico -Axes 3 -Homing
```

| Flag | Default | Effect |
|------|---------|--------|
| `-Homing` | off | enable + configure homing (`$22`, cycle order, `$24/$25/$27`, `$5`) |
| `-HomingOrder ZXY` | `ZXY` | one axis per pass, in this order (Z-first is the safe default; use `XYZ` to home X first) |
| `-LimitSwitches NC` | `NC` | `NC` inverts limit pins (`$5`); use `NO` for normally-open |
| `-RequireHoming` | off | force homing before motion (init-lock). Then clear the boot alarm with **`$H`**, not `$X` |
| `-NoEStop` | off | make the reset input a plain soft-reset (not a latching E-Stop) |
| `-NoControlInputs` | off | stop monitoring reset/feed-hold/cycle-start pins (`CONTROL_ENABLE 0`). Use when GP18 reads asserted and no control buttons are wired; soft-reset over USB still works |
| `-InvertControl` | off | invert reset/feed-hold/cycle-start (`$14=7`) for normally-closed-to-GND control buttons |

> **After flashing, send `$RST=*` once** (then power-cycle). Compiled defaults only
> load when settings are at factory default; a board with stored settings keeps
> them otherwise.

## 4. Networking (Pico W only)

```powershell
.\build-grblhal.ps1 -SetupTools -FetchSource -Board pico_w -Modules wifi,mdns
```

`wifi`/`bluetooth` force `PICO_BOARD=pico_w`; `wifi`/`ethernet` also fetch the
`networking` and `webui` submodules. Telnet/WebSocket (and FTP if SD card) are
auto-enabled in the generated `my_machine.h`. WiFi and Ethernet cannot be
enabled together (Pico SDK limitation).

---

## 5. How the build works (pipeline steps)

1. **Setup tools** (`-SetupTools`): download + extract ARM toolchain, clone Pico
   SDK 2.1.1 (+ tinyusb), extract prebuilt pioasm/picotool.
2. **Fetch source** (`-FetchSource`): clone `grblHAL/RP2040`, init the 13
   submodules that `CMakeLists.txt` `include()`s unconditionally (`grbl`,
   `eeprom`, `sdcard`, `keypad`, `bluetooth`, `motors`, `trinamic`, `spindle`,
   `embroidery`, `fans`, `laser`, `plugins`, `plasma`) — required even when the
   feature is off, because each plugin self-gates internally. `networking`/`webui`
   are added only when a networking module is selected.
3. **Validate** the requested modules (board-map + serial-budget + dependency
   guards) — see §3.
4. **Generate** `RP2040/my_machine.h` from the baseline + selected modules
   (original saved as `my_machine.h.orig`).
5. **Configure** with CMake/Ninja, passing the toolchain, `PICO_BOARD`,
   `pioasm_DIR`, `picotool_DIR`, and any `[cmake]` module options.
6. **Build** with Ninja → `RP2040/build/grblHAL.uf2` (picotool generates it).
7. **Collect**: copy to `firmware\…uf2` and print `picotool info`.

See [`MODIFICATIONS.md`](MODIFICATIONS.md) for the exact list of changes made to
get a clean compile.
