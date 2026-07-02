#!/usr/bin/env bash
# =============================================================================
# build-grblhal.sh — reproducible, from-scratch grblHAL firmware build (Linux)
#
# Linux/bash port of build-grblhal.ps1, same logic and options. Produces a
# flashable .uf2 for the Raspberry Pi Pico / Pico W (RP2040). Downloads the
# toolchain, fetches the source, applies the module selection, configures with
# CMake and builds — offline afterwards.
#
#   Pinned versions (match grblHAL/RP2040 CMakeLists.txt expectations):
#     ARM GNU toolchain : 14.2.Rel1
#     Pico SDK          : 2.1.1
#     pioasm / picotool : 2.1.1  (prebuilt, from raspberrypi/pico-sdk-tools)
#
#   Baseline "generic board" flags (grblHAL's shipped defaults):
#     Native USB CDC, 3 axes, probe on, on-board PWM spindle, grblHAL compat,
#     reset-as-E-Stop. Written explicitly into my_machine.h so the build is
#     self-documenting.
#
# Examples
#   ./build-grblhal.sh --setup-tools --fetch-source   # one-time: tools + source
#   ./build-grblhal.sh --clean                         # baseline 3-axis build
#   ./build-grblhal.sh --axes 4 --modules sdcard,eeprom,eeprom_fram,fans
#   ./build-grblhal.sh --homing --homing-order ZXY --limit-switches NC
#   ./build-grblhal.sh --list-modules
#
# Requires: bash, curl, git, cmake, ninja, tar, xz.
# =============================================================================
set -euo pipefail

# ----- defaults --------------------------------------------------------------
BOARD="pico"
AXES=3
MODULES_CSV=""
SPINDLES_CSV="pwm"
OUTPUT=""
JOBS=0
DO_SETUP_TOOLS=0
DO_FETCH_SOURCE=0
DO_CLEAN=0
DO_LIST=0
HOMING=0
HOMING_ORDER="ZXY"
LIMIT_SWITCHES="NC"
REQUIRE_HOMING=0
NO_ESTOP=0
NO_CONTROL_INPUTS=0
INVERT_CONTROL=0

die() { echo "ERROR: $*" >&2; exit 1; }
step() { printf '\n\033[32m=== %s ===\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }

# ----- arg parsing -----------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --board)          BOARD="$2"; shift 2 ;;
    --axes)           AXES="$2"; shift 2 ;;
    --modules)        MODULES_CSV="$2"; shift 2 ;;
    --spindles)       SPINDLES_CSV="$2"; shift 2 ;;
    --output)         OUTPUT="$2"; shift 2 ;;
    --jobs)           JOBS="$2"; shift 2 ;;
    --setup-tools)    DO_SETUP_TOOLS=1; shift ;;
    --fetch-source)   DO_FETCH_SOURCE=1; shift ;;
    --clean)          DO_CLEAN=1; shift ;;
    --list-modules)   DO_LIST=1; shift ;;
    --homing)         HOMING=1; shift ;;
    --homing-order)   HOMING_ORDER="$2"; shift 2 ;;
    --limit-switches) LIMIT_SWITCHES="$2"; shift 2 ;;
    --require-homing) REQUIRE_HOMING=1; shift ;;
    --no-estop)       NO_ESTOP=1; shift ;;
    --no-control-inputs) NO_CONTROL_INPUTS=1; shift ;;
    --invert-control) INVERT_CONTROL=1; shift ;;
    -h|--help)        sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done
case "$BOARD" in pico|pico_w) ;; *) die "--board must be pico or pico_w" ;; esac
case "$AXES" in 3|4|8) ;; *) die "--axes must be 3, 4 or 8" ;; esac
case "$LIMIT_SWITCHES" in NC|NO) ;; *) die "--limit-switches must be NC or NO" ;; esac

# ----- paths & pinned versions ----------------------------------------------
ROOT="$(cd "$(dirname "$0")" && pwd)"
TOOLS_DIR="$ROOT/tools"
SDK_DIR="$TOOLS_DIR/pico-sdk"
SRC_DIR="$ROOT/RP2040"
BUILD_DIR="$SRC_DIR/build"
[ -n "$OUTPUT" ] || OUTPUT="$ROOT/firmware"

SDK_VERSION="2.1.1"
TOOLS_TAG="v2.1.1-0"
case "$(uname -m)" in
  x86_64|amd64)  ARM_HOST="x86_64";  LIN_HOST="x86_64" ;;
  aarch64|arm64) ARM_HOST="aarch64"; LIN_HOST="aarch64" ;;
  *) die "unsupported host arch $(uname -m) (need x86_64 or aarch64)" ;;
esac
ARM_URL="https://developer.arm.com/-/media/Files/downloads/gnu/14.2.rel1/binrel/arm-gnu-toolchain-14.2.rel1-${ARM_HOST}-arm-none-eabi.tar.xz"
SDK_TOOLS_URL="https://github.com/raspberrypi/pico-sdk-tools/releases/download/${TOOLS_TAG}/pico-sdk-tools-${SDK_VERSION}-${LIN_HOST}-lin.tar.gz"
PICOTOOL_URL="https://github.com/raspberrypi/pico-sdk-tools/releases/download/${TOOLS_TAG}/picotool-${SDK_VERSION}-${LIN_HOST}-lin.tar.gz"

# Discovered after extraction (globbed).
ARM_DIR=""; PIOASM_DIR=""; PICOTOOL_DIR=""
resolve_tool_paths() {
  local g
  g=$(ls -d "$TOOLS_DIR"/arm-gnu-toolchain-*-arm-none-eabi 2>/dev/null | head -1 || true); [ -n "$g" ] && ARM_DIR="$g"
  g=$(find "$TOOLS_DIR/pico-sdk-tools-bin" -name pioasmConfig.cmake 2>/dev/null | head -1 || true); [ -n "$g" ] && PIOASM_DIR="$(dirname "$g")"
  g=$(find "$TOOLS_DIR/picotool-bin" -name picotoolConfig.cmake 2>/dev/null | head -1 || true); [ -n "$g" ] && PICOTOOL_DIR="$(dirname "$g")"
  return 0
}

# ----- module catalog  (key -> my_machine.h #define lines or CMake options) --
# Parallel associative arrays keep the per-module attributes.
declare -a CAT_KEYS
declare -A CAT_DEFINES CAT_CMAKE CAT_DESC CAT_NEEDSBOARD CAT_SERIAL CAT_FORCEBOARD CAT_NEEDSSUB
mod() { # key  "defines(; sep)"  "cmake"  desc  needsboard serial forceboard "needssub"
  CAT_KEYS+=("$1"); CAT_DEFINES["$1"]="$2"; CAT_CMAKE["$1"]="$3"; CAT_DESC["$1"]="$4"
  [ -n "$5" ] && CAT_NEEDSBOARD["$1"]=1; [ -n "$6" ] && CAT_SERIAL["$1"]=1
  [ -n "$7" ] && CAT_FORCEBOARD["$1"]="$7"; [ -n "$8" ] && CAT_NEEDSSUB["$1"]="$8"; return 0
}
#   key            defines                         cmake          description                                                          nb se force   subs
mod sdcard         "SDCARD_ENABLE 1"               ""             "Run g-code from SD card (SPI). Needs SD_CS+SPI pins -> board map."    1  ""  ""      ""
mod sdcard_ymodem  "SDCARD_ENABLE 2"               ""             "SD card + YModem upload. Needs SD_CS+SPI pins -> board map."          1  ""  ""      ""
mod eeprom         "EEPROM_ENABLE 32"              ""             "I2C EEPROM/FRAM settings storage (32 = 4K)."                          "" ""  ""      ""
mod eeprom_fram    "EEPROM_IS_FRAM 1"              ""             "Mark the EEPROM as FRAM (no write delay). Requires 'eeprom'."        "" ""  ""      ""
mod keypad         "KEYPAD_ENABLE 1"               ""             "I2C keypad. Needs I2C_STROBE pin -> board map."                       1  ""  ""      ""
mod keypad_serial  "KEYPAD_ENABLE 2"               ""             "Serial-stream keypad (shares MPG stream if MPG enabled)."             "" 1   ""      ""
mod mpg            "MPG_ENABLE 2"                  ""             "MPG handwheel interface (mode toggle via 0x8B)."                      "" 1   ""      ""
mod display        "DISPLAY_ENABLE 9"              ""             "I2C display protocol."                                                "" ""  ""      ""
mod rgb            "RGB_LED_ENABLE 2"              ""             "RGB/NeoPixel strip (\$536/\$537 + M150)."                             "" ""  ""      ""
mod safety_door    "SAFETY_DOOR_ENABLE 1"          ""             "Safety door input."                                                   "" ""  ""      ""
mod probe2         "PROBE2_ENABLE 1"               ""             "Second probe input."                                                  "" ""  ""      ""
mod toolsetter     "TOOLSETTER_ENABLE 1"           ""             "Toolsetter input."                                                    "" ""  ""      ""
mod motor_fault    "MOTOR_FAULT_ENABLE 1"          ""             "Motor fault input (conflicts with I2C keypad strobe)."                "" ""  ""      ""
mod homing_pulloff "HOMING_PULLOFF_ENABLE 1"       ""             "Per-axis homing pulloff settings."                                    "" ""  ""      ""
mod feed_override  "FEED_OVERRIDE_ENABLE 1"        ""             "M220 feed-rate override."                                             "" ""  ""      ""
mod step_inject    "STEP_INJECT_ENABLE 1"          ""             "Step injection support."                                             "" ""  ""      ""
mod modbus         "MODBUS_ENABLE 1"               ""             "ModBus RTU (1 = auto direction). Consumes a serial port."             "" 1   ""      ""
mod pwm_servo      "PWM_SERVO_ENABLE 1"            ""             "M280 PWM servo (needs a PWM-capable aux output)."                     "" ""  ""      ""
mod bltouch        "BLTOUCH_ENABLE 1"              ""             "M401/M402 BLTouch (claims one PWM servo output)."                     "" ""  ""      ""
mod eventout       "EVENTOUT_ENABLE 1"             ""             "Bind events/triggers to aux outputs."                                 "" ""  ""      ""
mod fans           "FANS_ENABLE 1"                 ""             "M106/M107 fan control."                                              "" ""  ""      ""
mod plasma         "PLASMA_ENABLE 1"               ""             "Plasma / THC plugin."                                                 "" ""  ""      ""
mod laser_coolant  "LASER_COOLANT_ENABLE 1"        ""             "Laser coolant plugin."                                               "" ""  ""      ""
mod laser_ovd      "LASER_OVD_ENABLE 1"            ""             "Laser overdrive PWM M-code."                                          "" ""  ""      ""
mod lb_clusters    "LB_CLUSTERS_ENABLE 1"          ""             "LaserBurn cluster support."                                          "" ""  ""      ""
mod embroidery     "EMBROIDERY_ENABLE 1"           ""             "Embroidery plugin."                                                  "" ""  ""      ""
mod odometer       "ODOMETER_ENABLE 1"             ""             "Odometer plugin."                                                    "" ""  ""      ""
mod esp_at         "ESP_AT_ENABLE 1"               ""             "Telnet via UART ESP32 (ESP-AT). Consumes a serial port."             "" 1   ""      ""
mod mcp3221        "MCP3221_ENABLE 1"              ""             "MCP3221 I2C 12-bit ADC input."                                        "" ""  ""      ""
mod mcp4725        "MCP4725_ENABLE 1"              ""             "MCP4725 I2C 12-bit DAC output."                                       "" ""  ""      ""
mod mcp23017       "MCP23017_ENABLE 1"             ""             "MCP23017 I2C 16-ch digital I/O."                                      "" ""  ""      ""
mod pca9654e       "PCA9654E_ENABLE 1"             ""             "PCA9654E I2C 8-ch digital out."                                       "" ""  ""      ""
mod trinamic       "TRINAMIC_ENABLE 1"             ""             "Trinamic TMC drivers. generic_map.h rejects this. Board map only."    1  ""  ""      ""
mod wifi           ""                              "ADD_WIFI=ON"  "WiFi networking (Pico W only)."                                       "" ""  "pico_w" "networking webui"
mod ethernet       ""                              "ADD_ETHERNET=ON" "Wiznet W5500/W5100S Ethernet (SPI)."                              "" ""  ""      "networking webui"
mod bluetooth      ""                              "ADD_BLUETOOTH=ON" "Bluetooth SPP (Pico W only)."                                     "" ""  "pico_w" ""
mod mdns           ""                              "ADD_mDNS=ON"  "mDNS responder (requires wifi or ethernet)."                          "" ""  ""      ""
mod mqtt           ""                              "ADD_MQTT=ON"  "MQTT client (requires wifi or ethernet)."                             "" ""  ""      ""
mod hpgl           ""                              "ADD_HPGL=ON"  "HPGL plotter plugin (C.ITOH CX-6000)."                                "" ""  ""      ""

list_modules() {
  printf '\n\033[36mAvailable modules (--modules key1,key2,...):\033[0m\n\n'
  for k in "${CAT_KEYS[@]}"; do
    local tag warn=""
    [ -n "${CAT_CMAKE[$k]}" ] && tag="[cmake]" || tag="[define]"
    [ -n "${CAT_NEEDSBOARD[$k]:-}" ] && warn=" (needs board map)"
    [ -n "${CAT_SERIAL[$k]:-}" ] && warn=" (uses a serial port)"
    printf '%-16s %-8s %s%s\n' "$k" "$tag" "${CAT_DESC[$k]}" "$warn"
  done
  printf '\n\033[36mBoards: pico (default), pico_w   |   Axes: 3 (default), 4, 8\033[0m\n'
  printf 'Spindles: pwm (default) or grblHAL spindle symbols (SPINDLE_PWM0, SPINDLE_HUANYANG1, ...)\n\n'
}
[ "$DO_LIST" -eq 1 ] && { list_modules; exit 0; }

# split CSVs into arrays
IFS=',' read -r -a MODULES <<< "$MODULES_CSV"; [ -z "$MODULES_CSV" ] && MODULES=()
IFS=',' read -r -a SPINDLES <<< "$SPINDLES_CSV"

get_web() { echo "  downloading $1"; curl -Ls -o "$2" "$1" || die "download failed: $1"; [ -s "$2" ] || die "download empty: $1"; }

# ----- 1. toolchain / SDK / tools -------------------------------------------
setup_tools() {
  step "Setting up toolchain, SDK and host tools"
  mkdir -p "$TOOLS_DIR"
  if ! ls "$TOOLS_DIR"/arm-gnu-toolchain-*-arm-none-eabi/bin/arm-none-eabi-gcc >/dev/null 2>&1; then
    get_web "$ARM_URL" "$TOOLS_DIR/arm-toolchain.tar.xz"
    echo "  extracting ARM toolchain..."; tar -xf "$TOOLS_DIR/arm-toolchain.tar.xz" -C "$TOOLS_DIR"
  else echo "  ARM toolchain present."; fi
  if [ ! -f "$SDK_DIR/pico_sdk_init.cmake" ]; then
    echo "  cloning Pico SDK $SDK_VERSION..."
    git clone --depth 1 -b "$SDK_VERSION" https://github.com/raspberrypi/pico-sdk.git "$SDK_DIR"
    ( cd "$SDK_DIR" && git submodule update --init --depth 1 lib/tinyusb )
  else echo "  Pico SDK present."; fi
  if ! find "$TOOLS_DIR/pico-sdk-tools-bin" -name pioasmConfig.cmake >/dev/null 2>&1; then
    get_web "$SDK_TOOLS_URL" "$TOOLS_DIR/pico-sdk-tools.tar.gz"
    mkdir -p "$TOOLS_DIR/pico-sdk-tools-bin"; tar -xf "$TOOLS_DIR/pico-sdk-tools.tar.gz" -C "$TOOLS_DIR/pico-sdk-tools-bin"
  else echo "  pioasm present."; fi
  if ! find "$TOOLS_DIR/picotool-bin" -name picotoolConfig.cmake >/dev/null 2>&1; then
    get_web "$PICOTOOL_URL" "$TOOLS_DIR/picotool.tar.gz"
    mkdir -p "$TOOLS_DIR/picotool-bin"; tar -xf "$TOOLS_DIR/picotool.tar.gz" -C "$TOOLS_DIR/picotool-bin"
  else echo "  picotool present."; fi
}

# ----- 2. source + submodules ------------------------------------------------
REQUIRED_SUBS=(grbl eeprom sdcard keypad bluetooth motors trinamic spindle embroidery fans laser plugins plasma)
setup_source() {  # $@ = extra submodules
  step "Fetching grblHAL source + submodules"
  [ -d "$SRC_DIR/.git" ] || git clone https://github.com/grblHAL/RP2040.git "$SRC_DIR"
  local subs; subs=$(printf '%s\n' "${REQUIRED_SUBS[@]}" "$@" | awk 'NF' | sort -u)
  ( cd "$SRC_DIR"
    for s in $subs; do
      if [ ! -f "$SRC_DIR/$s/CMakeLists.txt" ] && [ ! -e "$SRC_DIR/$s/.git" ]; then
        echo "  submodule: $s"; git submodule update --init --depth 1 "$s" || true
      fi
    done )
}

# ----- 3. generate my_machine.h ---------------------------------------------
write_my_machine() {  # uses BOARD AXES MODULES SPINDLES EXTRA_DEFINES[]
  step "Generating my_machine.h"
  local path="$SRC_DIR/my_machine.h"
  [ -f "$path" ] && [ ! -f "$path.orig" ] && cp "$path" "$path.orig"
  {
    echo "/* my_machine.h - GENERATED by build-grblhal.sh - do not edit by hand. */"
    echo "/* Baseline = grblHAL 'generic board' defaults (Native USB, probe, PWM spindle, grblHAL compat). */"
    echo ""
    [ "$AXES" -eq 4 ] && echo "#define BOARD_GENERIC_4AXIS"
    [ "$AXES" -eq 8 ] && echo "#define BOARD_GENERIC_8AXIS"
    echo ""
    echo "// --- baseline (default) flags ---"
    echo "#define USB_SERIAL_CDC      1   // Native USB CDC"
    echo "#define COMPATIBILITY_LEVEL 0   // grblHAL native -> ESTOP_ENABLE defaults to 1"
    echo ""
    echo "// --- spindle(s) ---"
    if [ "${#SPINDLES[@]}" -eq 1 ] && [ "${SPINDLES[0]}" = "pwm" ]; then
      echo "// (none specified -> default on-board PWM spindle is instantiated)"
    else
      local i=0; for s in "${SPINDLES[@]}"; do echo "#define SPINDLE${i}_ENABLE $s"; i=$((i+1)); done
    fi
    # selected modules (define-type only)
    local defout=""
    for key in "${MODULES[@]}"; do
      local k; k=$(echo "$key" | tr '[:upper:]' '[:lower:]' | xargs)
      [ -z "$k" ] && continue
      [ -n "${CAT_DEFINES[$k]+x}" ] || die "Unknown module '$key'. Run --list-modules."
      if [ -n "${CAT_DEFINES[$k]}" ]; then
        IFS=';' read -r -a ds <<< "${CAT_DEFINES[$k]}"
        for d in "${ds[@]}"; do defout+="#define $d"$'\n'; done
      fi
    done
    if [ -n "$defout" ]; then echo ""; echo "// --- selected modules ---"; printf '%s' "$defout"; fi
    # driver-level tuning defines (e.g. ESTOP_ENABLE)
    if [ "${#EXTRA_DEFINES[@]}" -gt 0 ]; then
      echo ""; echo "// --- machine tuning (driver-level) ---"
      for d in "${EXTRA_DEFINES[@]}"; do echo "#define $d"; done
    fi
    # networking daemons auto-enable when WIFI/ETHERNET/WEBUI turned on (via CMake)
    cat <<'EOF'

// --- networking daemons (auto, keyed off CMake-set WIFI/ETHERNET) ---
#if WIFI_ENABLE || ETHERNET_ENABLE || WEBUI_ENABLE
#define TELNET_ENABLE        1
#define WEBSOCKET_ENABLE     1
#if SDCARD_ENABLE || WEBUI_ENABLE
#define FTP_ENABLE           1
#endif
#endif

/**/
EOF
  } > "$path"
  echo "  wrote $path"
}

# ----- inject global compile definitions into CMakeLists.txt -----------------
apply_cmake_defs() {  # uses CDEFS[]
  local cmk="$SRC_DIR/CMakeLists.txt" orig="$SRC_DIR/CMakeLists.txt.orig"
  [ -f "$orig" ] || cp "$cmk" "$orig"
  if [ "${#CDEFS[@]}" -gt 0 ]; then
    local body=""; for d in "${CDEFS[@]}"; do body+="    $d"$'\n'; done
    local block; block=$'\n# >>> build-grblhal.sh machine defaults >>>\nadd_compile_definitions(\n'"$body"$')\n# <<< build-grblhal.sh machine defaults <<<\n'
    awk -v blk="$block" '{print} /cmake_minimum_required\(/ && !done {printf "%s", blk; done=1}' "$orig" > "$cmk"
  else
    cp "$orig" "$cmk"
  fi
}

# ----- 4 + 5. configure & build ---------------------------------------------
invoke_build() {  # uses BOARD MODULES CDEFS[]
  step "Configuring (CMake / Ninja)"
  export PICO_SDK_PATH="$SDK_DIR"
  export PICO_TOOLCHAIN_PATH="$ARM_DIR"
  local cmake_opts=() eff_board="$BOARD"
  for key in "${MODULES[@]}"; do
    local k; k=$(echo "$key" | tr '[:upper:]' '[:lower:]' | xargs); [ -z "$k" ] && continue
    [ -n "${CAT_CMAKE[$k]}" ] && cmake_opts+=("${CAT_CMAKE[$k]}")
    [ -n "${CAT_FORCEBOARD[$k]:-}" ] && eff_board="${CAT_FORCEBOARD[$k]}"
  done
  [ "$DO_CLEAN" -eq 1 ] && [ -d "$BUILD_DIR" ] && rm -rf "$BUILD_DIR"
  local args=(-G Ninja -S "$SRC_DIR" -B "$BUILD_DIR"
    -DCMAKE_BUILD_TYPE=Release -DPICO_BOARD="$eff_board"
    -DPICO_TOOLCHAIN_PATH="$ARM_DIR" -Dpioasm_DIR="$PIOASM_DIR" -Dpicotool_DIR="$PICOTOOL_DIR")
  for o in "${cmake_opts[@]}"; do args+=("-D$o"); done
  apply_cmake_defs
  echo "  cmake ${args[*]}"
  cmake "${args[@]}" || die "CMake configure failed."
  step "Building"
  local bargs=(--build "$BUILD_DIR"); [ "$JOBS" -gt 0 ] && bargs+=(-j "$JOBS")
  cmake "${bargs[@]}" || die "Build failed."
}

# ============================================================================
# Main
# ============================================================================
declare -a MODS_LOWER=()
for m in "${MODULES[@]}"; do k=$(echo "$m" | tr '[:upper:]' '[:lower:]' | xargs); [ -n "$k" ] && MODS_LOWER+=("$k"); done
has() { local x; for x in "${MODS_LOWER[@]}"; do [ "$x" = "$1" ] && return 0; done; return 1; }

# 1) modules that require pins the generic board maps do not define
needs_board=()
for k in "${MODS_LOWER[@]}"; do [ -n "${CAT_NEEDSBOARD[$k]:-}" ] && needs_board+=("$k"); done
if [ "${#needs_board[@]}" -gt 0 ]; then
  die "These modules are not supported on the generic board map: ${needs_board[*]}.
  They need board-specific pins (SD_CS/SPI, I2C strobe, Trinamic, ...).
  Use a board map that provides them (e.g. BOARD_PICO_CNC) or a custom
  boards/my_machine_map.h. See README-build.md."
fi
# 2) eeprom_fram modifies eeprom
has eeprom_fram && ! has eeprom && die "'eeprom_fram' requires 'eeprom'."
# 3) serial-port budget
serial_mods=(); for k in "${MODS_LOWER[@]}"; do [ -n "${CAT_SERIAL[$k]:-}" ] && serial_mods+=("$k"); done
if [ "${#serial_mods[@]}" -gt 1 ]; then
  warn "Multiple serial-port consumers selected (${serial_mods[*]}). The generic RP2040 build"
  warn "  typically supports only one; the build may fail with 'Too many options that requires a serial port'."
fi
# 4) mdns/mqtt need networking
if { has mdns || has mqtt; } && ! { has wifi || has ethernet; }; then die "'mdns'/'mqtt' require 'wifi' or 'ethernet'."; fi

# extra submodules required by networking modules
extra_subs=()
for k in "${MODS_LOWER[@]}"; do [ -n "${CAT_NEEDSSUB[$k]:-}" ] && extra_subs+=(${CAT_NEEDSSUB[$k]}); done

[ "$DO_SETUP_TOOLS" -eq 1 ] && setup_tools
[ "$DO_FETCH_SOURCE" -eq 1 ] && setup_source "${extra_subs[@]}"
resolve_tool_paths

# verify prerequisites
[ -n "$ARM_DIR" ] && [ -x "$ARM_DIR/bin/arm-none-eabi-gcc" ] || die "Missing ARM gcc. Run with --setup-tools."
[ -f "$SDK_DIR/pico_sdk_init.cmake" ] || die "Missing Pico SDK. Run with --setup-tools."
[ -n "$PIOASM_DIR" ]   || die "Missing pioasm. Run with --setup-tools."
[ -n "$PICOTOOL_DIR" ] || die "Missing picotool. Run with --setup-tools."
[ -f "$SRC_DIR/CMakeLists.txt" ] || die "Missing source. Run with --fetch-source."

# ----- machine defaults: homing, limit polarity, E-Stop ----------------------
CDEFS=(); EXTRA_DEFINES=()
declare -A AXIS_BIT=([X]=1 [Y]=2 [Z]=4 [A]=8 [B]=16 [C]=32)

[ "$NO_ESTOP" -eq 1 ] && EXTRA_DEFINES+=("ESTOP_ENABLE 0")
[ "$NO_CONTROL_INPUTS" -eq 1 ] && EXTRA_DEFINES+=("CONTROL_ENABLE 0")
[ "$INVERT_CONTROL" -eq 1 ] && CDEFS+=("DEFAULT_CONTROL_SIGNALS_INVERT_MASK=7")

if [ "$HOMING" -eq 1 ]; then
  echo "  homing: order=$HOMING_ORDER, switches=$LIMIT_SWITCHES, requireHoming=$REQUIRE_HOMING"
  order=$(echo "$HOMING_ORDER" | tr '[:lower:]' '[:upper:]')
  CDEFS+=("DEFAULT_HOMING_ENABLE=1" "DEFAULT_HOMING_SINGLE_AXIS_COMMANDS=1")
  [ "$REQUIRE_HOMING" -eq 1 ] && CDEFS+=("DEFAULT_HOMING_INIT_LOCK=1") || CDEFS+=("DEFAULT_HOMING_INIT_LOCK=0")
  i=0
  for (( c=0; c<${#order}; c++ )); do
    ax="${order:$c:1}"; [ -n "${AXIS_BIT[$ax]:-}" ] || die "Bad --homing-order '$HOMING_ORDER' (use axis letters like XYZ)."
    CDEFS+=("DEFAULT_HOMING_CYCLE_${i}=${AXIS_BIT[$ax]}"); i=$((i+1))
  done
  while [ "$i" -le 5 ]; do CDEFS+=("DEFAULT_HOMING_CYCLE_${i}=0"); i=$((i+1)); done
  if [ "$LIMIT_SWITCHES" = "NC" ]; then
    mask=$(( (1 << AXES) - 1 )); CDEFS+=("DEFAULT_LIMIT_SIGNALS_INVERT_MASK=$mask")
  fi
  CDEFS+=("DEFAULT_HOMING_FEED_RATE=50.0f" "DEFAULT_HOMING_SEEK_RATE=300.0f" "DEFAULT_HOMING_PULLOFF=2.0f")
fi

write_my_machine
invoke_build

# ----- collect output --------------------------------------------------------
step "Collecting firmware"
uf2="$BUILD_DIR/grblHAL.uf2"
[ -f "$uf2" ] || die "Expected $uf2 was not produced."
mkdir -p "$OUTPUT"
name="grblHAL_${BOARD}_${AXES}axis"
[ "$HOMING" -eq 1 ] && name+="_homing"
if [ "${#MODS_LOWER[@]}" -gt 0 ]; then name+="_$(IFS=-; echo "${MODS_LOWER[*]}")"; fi
dest="$OUTPUT/$name.uf2"
cp -f "$uf2" "$dest"
size=$(awk "BEGIN{printf \"%.1f\", $(stat -c%s "$dest")/1024}")
printf '\n\033[32mSUCCESS\033[0m\n'
echo "  firmware : $dest  (${size} KB)"
echo "  flash    : hold BOOTSEL, plug in the Pico, copy the .uf2 onto the RPI-RP2 drive."
"$PICOTOOL_DIR/picotool" info "$uf2" 2>/dev/null || true
