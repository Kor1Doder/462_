# Modifications made to get a clean grblHAL → `.uf2` compile

Goal: compile grblHAL for a **generic Raspberry Pi Pico (RP2040)** into a
flashable `.uf2`, fixing every error encountered, on this Windows machine.

## Headline result

**No grblHAL source code had to be patched.** The baseline "generic board"
configuration (Native USB, 3 axes, probe, PWM spindle, grblHAL compatibility,
reset-as-E-Stop) compiled cleanly on the first attempt and produced a valid
`grblHAL.uf2` (family `rp2040`, load address `0x10000000`).

Every error that *did* appear came later, only when enabling certain optional
modules, and they were all **intentional configuration guards** in grblHAL
(`#error` directives), not bugs. The correct fix for those is configuration, not
source edits — so the pipeline prevents them with early validation instead of
hacking out the guards (which would yield broken firmware with unwired pins).

All of the following are baked into [`build-grblhal.ps1`](build-grblhal.ps1).

---

## A. Environment / build-system modifications (the real work)

1. **Supplied the ARM toolchain explicitly via `PICO_TOOLCHAIN_PATH`**
   instead of relying on it being on `PATH` (it was not installed on this
   machine). Downloaded ARM GNU toolchain **14.2.Rel1** to `tools\arm`.
   *Why:* `arm-none-eabi-gcc` was missing; the Pico SDK locates the cross
   compiler through this variable.

2. **Used prebuilt `pioasm` and `picotool`** (from `raspberrypi/pico-sdk-tools`
   2.1.1) and pointed CMake at them with `-Dpioasm_DIR=…` and
   `-Dpicotool_DIR=…`.
   *Why:* otherwise the Pico SDK tries to compile these host tools itself during
   configure. On Windows + Ninja that sub-build defaults to looking for MSVC
   (`cl`), which is not installed, and would fail. `picotool` is also what
   actually generates the `.uf2`. Providing them prebuilt removes a whole class
   of host-build failures.

3. **Cloned the Pico SDK at the pinned tag 2.1.1 and initialised only
   `lib/tinyusb`** (not all SDK submodules).
   *Why:* the version is pinned in `RP2040/CMakeLists.txt`; TinyUSB is the only
   SDK submodule needed for the USB-CDC (Native USB) build, so this avoids a
   large, slow recursive clone.

4. **Initialised exactly the grblHAL submodules that `CMakeLists.txt`
   `include()`s unconditionally** — `grbl, eeprom, sdcard, keypad, bluetooth,
   motors, trinamic, spindle, embroidery, fans, laser, plugins, plasma` — even
   for a minimal build.
   *Why:* the top-level CMake unconditionally includes each plugin's
   `CMakeLists.txt` and links its library; a missing submodule breaks configure
   even though the feature is disabled (plugins self-gate on their `*_ENABLE`
   macros). `networking`/`webui` are fetched only when a networking module is
   selected.

5. **Generated `RP2040/my_machine.h` from the spec** (original preserved as
   `my_machine.h.orig`), writing the baseline flags explicitly and appending
   `#define`s for selected modules.

6. **Configured with CMake + Ninja in Release**, passing `PICO_SDK_PATH`,
   `PICO_BOARD`, the toolchain, and the tool dirs. (Generator = Ninja, which is
   present; `make` is not.)

---

## B. Configuration guards turned into early, friendly failures

These are the actual compile errors hit while testing module combinations on the
generic map, and how the script handles each **automatically** (no source edits):

| # | Error seen (grblHAL `#error`)                                   | Trigger                                   | Automated handling in the script |
|---|----------------------------------------------------------------|-------------------------------------------|----------------------------------|
| 1 | `SD card plugin not supported!` (`driver_opts2.h:95`, `driver.h:246`) | `sdcard` on generic map (no `SD_CS_PIN`)  | Module flagged `NeedsBoard`; rejected **before** compiling with guidance to use a board map. |
| 2 | `Keypad plugin not supported!` (`driver_opts2.h:65`) + `I2C keypad/strobe is not supported…` (`pin_bits_masks.h:45`) | `keypad` (I2C) on generic map (no `I2C_STROBE_PIN`) | Module flagged `NeedsBoard`; rejected early. (Serial keypad `keypad_serial` is allowed.) |
| 3 | `Trinamic plugin not supported!` (`generic_map.h:24`)          | `trinamic` on generic map                 | Module flagged `NeedsBoard`; rejected early. |
| 4 | `Too many options that requires a serial port are enabled!` (`driver_opts2.h:386`) | e.g. `modbus` + `keypad_serial` together  | Serial-consuming modules counted; script **warns** when >1 is selected (USB build has room for ~1). |

Plus two dependency guards the script enforces up front:

* `eeprom_fram` requires `eeprom`.
* `mdns` / `mqtt` require `wifi` or `ethernet`.

---

## C. Verified builds (produced `.uf2` files in `firmware\`)

| Configuration | Result |
|---------------|--------|
| Baseline, generic Pico, 3 axis | `grblHAL_pico_3axis.uf2` — **471 KB**, valid `rp2040` UF2 |
| 4 axis + eeprom+fram, fans, rgb, pwm_servo, eventout, feed_override, homing_pulloff, safety_door, modbus | `grblHAL_pico_4axis_…uf2` — **~512 KB**, valid `rp2040` UF2 |

Both validated with `picotool info` (correct UF2 magic, family `rp2040`,
program name `grblHAL`, load address `0x10000000`).

---

## D. Alarm-state and homing fixes (firmware config)

Two field issues were traced to grblHAL **default settings**, fixed by compiling
in the correct defaults. New script flags: `-Homing`, `-HomingOrder`,
`-LimitSwitches NC|NO`, `-RequireHoming`, `-NoEStop`.

### Why these go in as global compiler `-D`, not my_machine.h
The core file that builds the default settings table, `grbl/settings.c`, includes
`config.h` but **not** `my_machine.h` (verified). So `DEFAULT_*` overrides placed
in `my_machine.h` never reach it. The script instead injects
`add_compile_definitions(...)` into `CMakeLists.txt` (regenerated from
`CMakeLists.txt.orig` each run) so the defaults apply to every translation unit.
We do **not** use `-DCMAKE_C_FLAGS=...` for this — that replaces the Pico SDK's
`-mcpu=cortex-m0plus -mthumb` flags and breaks CMSIS (`"Unknown Arm architecture
profile"`); found and corrected during testing.

### Issue 1 — boots in `Alarm`, `$X` won't reach `Idle`
Root cause (firmware): `$X` (`system.c` `check_status`) refuses to clear the lock
while `limits_homing_required()` is true, and that function
(`machine_limits.c:669`) is true only when **`homing.flags.init_lock`** is set.
With homing enabled + init-lock, the board powers up in `ALARM:11` (homing
required) and `$X` is rejected with `Status_HomingRequired` — only `$H` clears it.

Fix: compile `DEFAULT_HOMING_INIT_LOCK = 0`. The board boots `Idle` (or is
`$X`-clearable) while homing stays available. Use `-RequireHoming` to force homing
instead — then clear the boot alarm with `$H`, not `$X`.
Secondary cause covered by `-NoEStop`: with reset-as-E-Stop (`ESTOP_ENABLE=1`,
our spec default), an asserted/wired reset line raises `Alarm_EStop`, which `$X`
also cannot clear (`check_status` blocks on `e_stop`/`reset`). `-NoEStop`
(`#define ESTOP_ENABLE 0` in my_machine.h) makes the reset input a plain
soft-reset so it cannot wedge the board.

### Issue 2 — `$HX/$HY/$HZ` disabled; homing order; Y-axis skip
* **Single-axis homing disabled:** `go_home()` (`system.c:483`) returns
  `Status_HomingDisabled` for `$HX/$HY/$HZ` unless
  `homing.flags.single_axis_commands` is set. Fix: compile
  `DEFAULT_HOMING_SINGLE_AXIS_COMMANDS = 1`.
* **Order (one axis per pass):** the stock default homes Z first, then **X and Y
  together** (`config.h:1702`, `DEFAULT_HOMING_CYCLE_1 = X_AXIS_BIT|Y_AXIS_BIT`).
  The script sets one axis per pass via `DEFAULT_HOMING_CYCLE_0/1/2` and zeroes the
  unused passes. Default order is **Z -> X -> Y** (`-HomingOrder ZXY`): Z lifts
  first to clear the workspace, then X, then Y - the safe CNC convention. Pass
  `-HomingOrder XYZ` for strict X,Y,Z.
* **Y-skip:** because the stock map homes X+Y in one pass, a shared/mis-mapped Y
  limit (the failure in the other firmware) makes the pass finish on X's switch
  and skip Y. Our generic map has separate limit pins (GP9/10/11), so the default
  would not skip Y — but giving Y its **own** homing pass removes the risk
  entirely. **Answer: no, this build does not skip Y, and the per-axis sequence
  guarantees it.**
* NC limit switches: `DEFAULT_LIMIT_SIGNALS_INVERT_MASK = (1<<axes)-1` (=`$5`) so
  switches don't read permanently triggered (else homing won't start). Use
  `-LimitSwitches NO` for normally-open switches.
* Commissioning rates matched to the GUI's safe preset: `$24=50`, `$25=300`,
  `$27=2.0`.

Verified the defines reach the grbl library (`build.ninja`) and map to the
runtime flags (`settings.c:132-150,184`).

### IMPORTANT — applying the new defaults
Compiled `DEFAULT_*` values only load when settings are at factory default. A
board that already has stored settings keeps them after flashing. After flashing
this firmware, send **`$RST=*`** once (restore all settings) so the new homing /
limit / lock defaults take effect, then power-cycle.

### Issue 1b — `$X` rejected with status 79, then 18 (reset/control input asserted)
Field follow-up: `$X` returned **79** (`Status_NotAllowedCriticalEvent`,
`system.c:1152`) - raised when `sys.blocking_event` is set, which happens for a
*critical* alarm (`alarms.h:74`: hard/soft limit, **E-Stop**, motor fault).
After `-NoEStop` the same input instead returned **18** (`Status_Reset`,
`check_status` `system.c:429`) and Soft Reset cleared it only for an instant
before the main loop re-read the pin and re-alarmed.

Root cause: the **reset/E-Stop input GP18** (`AUXINPUT3`, generic_map.h) is read
as *asserted* (wired to GND / floating low / NC-to-GND button). `$X` cannot clear
a control signal that is still physically active - by design.

Fixes (firmware):
* `-NoControlInputs` -> `#define CONTROL_ENABLE 0` in my_machine.h. `driver.h:160`
  defines `CONTROL_ENABLE` with `#ifndef`, after `my_machine.h` is included, so
  this override wins; generic_map.h then does not assign RESET/FEED_HOLD/
  CYCLE_START pins and they are no longer monitored. Soft reset over USB (Ctrl-X)
  still works. Best when no control buttons are wired.
* `-InvertControl` -> `DEFAULT_CONTROL_SIGNALS_INVERT_MASK=7` (reset|feedhold|
  cyclestart). Use when NC control/E-Stop buttons ARE wired to GND, so 'closed'
  reads inactive (the `$14` analog of `$5` for limits).

Hardware confirmation: jumper GP18 (physical pin 24) to 3V3 OUT (physical pin 36)
- if the alarm clears, GP18 was the culprit.

### Build command for the fixed firmware
```powershell
.\build-grblhal.ps1 -Clean -Board pico -Axes 3 -Homing
# default homing order is Z->X->Y (safe);   add -HomingOrder XYZ for strict X,Y,Z
# enforced homing instead of free unlock:   add -RequireHoming  (then use $H)
# normally-open limit switches:              add -LimitSwitches NO
# also neutralize reset-as-E-Stop:           add -NoEStop
```
Output: `firmware\grblHAL_pico_3axis_homing.uf2`.
