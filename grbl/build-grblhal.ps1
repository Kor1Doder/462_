<#
.SYNOPSIS
    Reproducible, from-scratch compile pipeline for grblHAL firmware on the
    Raspberry Pi Pico / Pico W (RP2040), producing a flashable *.uf2 file.

.DESCRIPTION
    This script replaces the grblHAL web builder for our "generic board" target.
    It can (optionally) download the complete toolchain, fetch the source, apply
    the module selection, configure with CMake and build the .uf2 - all offline
    afterwards.

    Pinned versions (match grblHAL/RP2040 CMakeLists.txt expectations):
        ARM GNU toolchain : 14.2.Rel1
        Pico SDK          : 2.1.1
        pioasm / picotool : 2.1.1   (prebuilt, from raspberrypi/pico-sdk-tools)

    Default compilation flags (the "generic board" baseline) match the web-builder
    screen we standardized on:
        Connection      : Native USB  (USB_SERIAL_CDC = 1)
        Number of axes  : 3
        Probe input     : enabled
        Spindle 1       : PWM (default driver spindle)
        Compatibility   : grblHAL (COMPATIBILITY_LEVEL = 0)
        Reset as E-Stop : enabled (ESTOP_ENABLE = 1, implied by compat level 0)
    None of these require edits - they are grblHAL's shipped defaults - but the
    script writes them explicitly into my_machine.h so the build is self-documenting.

.EXAMPLE
    # One-time: download toolchain + SDK + tools, fetch source, build baseline
    .\build-grblhal.ps1 -SetupTools -FetchSource

.EXAMPLE
    # Baseline generic Pico build (tools already present)
    .\build-grblhal.ps1 -Clean

.EXAMPLE
    # Generic Pico with SD card + I2C FRAM + fans + 4 axes
    .\build-grblhal.ps1 -Axes 4 -Modules sdcard,eeprom,eeprom_fram,fans

.EXAMPLE
    # Show the full module catalog
    .\build-grblhal.ps1 -ListModules
#>

[CmdletBinding()]
param(
    [ValidateSet("pico", "pico_w")]
    [string]$Board = "pico",

    [ValidateSet(3, 4, 8)]
    [int]$Axes = 3,

    [string[]]$Modules = @(),

    # Spindle drivers. "pwm" = on-board default PWM spindle. Otherwise pass one or
    # more grblHAL spindle symbols, e.g. -Spindles SPINDLE_PWM0,SPINDLE_HUANYANG1
    [string[]]$Spindles = @("pwm"),

    [string]$Output = "$PSScriptRoot\firmware",

    [switch]$SetupTools,   # download + extract ARM toolchain, Pico SDK, pioasm/picotool
    [switch]$FetchSource,  # clone grblHAL/RP2040 + required submodules
    [switch]$Clean,        # wipe the build directory first
    [switch]$ListModules,  # print the module catalog and exit
    [int]$Jobs = 0,        # parallel build jobs (0 = auto)

    # --- machine defaults (compiled-in; applied on a fresh board or after $RST=*) ---
    [switch]$Homing,                       # enable + configure the homing cycle
    [string]$HomingOrder = "ZXY",          # order of homing passes, one axis per pass (Z-first = safe default)
    [ValidateSet("NC", "NO")]
    [string]$LimitSwitches = "NC",         # NC -> invert limit pins ($5); NO -> no invert
    [switch]$RequireHoming,                # force homing before motion (init lock). Use $H, not $X, to clear boot alarm
    [switch]$NoEStop,                      # make the reset input a plain soft-reset (not a latching E-Stop)
    [switch]$NoControlInputs,              # stop monitoring reset/feed-hold/cycle-start pins (still soft-reset over USB)
    [switch]$InvertControl                 # invert reset/feed-hold/cycle-start inputs (for NC-to-GND control buttons)
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Paths & pinned versions
# ---------------------------------------------------------------------------
$Root        = $PSScriptRoot
$ToolsDir    = Join-Path $Root "tools"
$ArmDir      = Join-Path $ToolsDir "arm"
$SdkDir      = Join-Path $ToolsDir "pico-sdk"
$PioasmDir   = Join-Path $ToolsDir "pico-sdk-tools-bin\pioasm"
$PicotoolDir = Join-Path $ToolsDir "picotool-bin\picotool"
$SrcDir      = Join-Path $Root "RP2040"
$BuildDir    = Join-Path $SrcDir "build"

$SdkVersion  = "2.1.1"
$ToolsTag    = "v2.1.1-0"
$ArmZipUrl   = "https://developer.arm.com/-/media/Files/downloads/gnu/14.2.rel1/binrel/arm-gnu-toolchain-14.2.rel1-mingw-w64-i686-arm-none-eabi.zip"
$SdkToolsUrl = "https://github.com/raspberrypi/pico-sdk-tools/releases/download/$ToolsTag/pico-sdk-tools-$SdkVersion-x64-win.zip"
$PicotoolUrl = "https://github.com/raspberrypi/pico-sdk-tools/releases/download/$ToolsTag/picotool-$SdkVersion-x64-win.zip"

# ---------------------------------------------------------------------------
# Module catalog  (key -> my_machine.h #define lines)
# Keys are case-insensitive. See -ListModules.
# ---------------------------------------------------------------------------
$Catalog = [ordered]@{
    # --- storage / settings ------------------------------------------------
    "sdcard"        = @{ Defines = @("SDCARD_ENABLE 1");       NeedsBoard = $true; Desc = "Run g-code from SD card (SPI). Needs SD_CS + SPI pins -> board map only." }
    "sdcard_ymodem" = @{ Defines = @("SDCARD_ENABLE 2");       NeedsBoard = $true; Desc = "SD card + YModem upload. Needs SD_CS + SPI pins -> board map only." }
    "eeprom"        = @{ Defines = @("EEPROM_ENABLE 32");      Desc = "I2C EEPROM/FRAM settings storage (32 = 4K)." }
    "eeprom_fram"   = @{ Defines = @("EEPROM_IS_FRAM 1");      Desc = "Mark the EEPROM chip as FRAM (no write delay). Requires 'eeprom'." }
    # --- human interface ---------------------------------------------------
    "keypad"        = @{ Defines = @("KEYPAD_ENABLE 1");       NeedsBoard = $true; Desc = "I2C keypad. Needs I2C_STROBE pin -> board map only." }
    "keypad_serial" = @{ Defines = @("KEYPAD_ENABLE 2");       Serial = $true; Desc = "Serial-stream keypad (shares MPG stream if MPG enabled)." }
    "mpg"           = @{ Defines = @("MPG_ENABLE 2");          Serial = $true; Desc = "MPG handwheel interface (mode toggle via 0x8B)." }
    "display"       = @{ Defines = @("DISPLAY_ENABLE 9");      Desc = "I2C display protocol." }
    "rgb"           = @{ Defines = @("RGB_LED_ENABLE 2");      Desc = 'RGB/NeoPixel strip ($536/$537 + M150).' }
    # --- motion / probing extras ------------------------------------------
    "safety_door"   = @{ Defines = @("SAFETY_DOOR_ENABLE 1");  Desc = "Safety door input." }
    "probe2"        = @{ Defines = @("PROBE2_ENABLE 1");       Desc = "Second probe input." }
    "toolsetter"    = @{ Defines = @("TOOLSETTER_ENABLE 1");   Desc = "Toolsetter input." }
    "motor_fault"   = @{ Defines = @("MOTOR_FAULT_ENABLE 1");  Desc = "Motor fault input (conflicts with I2C keypad strobe)." }
    "homing_pulloff"= @{ Defines = @("HOMING_PULLOFF_ENABLE 1"); Desc = "Per-axis homing pulloff settings." }
    "feed_override" = @{ Defines = @("FEED_OVERRIDE_ENABLE 1"); Desc = "M220 feed-rate override." }
    "step_inject"   = @{ Defines = @("STEP_INJECT_ENABLE 1");  Desc = "Step injection support." }
    # --- spindle / tooling -------------------------------------------------
    "modbus"        = @{ Defines = @("MODBUS_ENABLE 1");       Serial = $true; Desc = "ModBus RTU (1 = auto direction). Consumes a serial port." }
    "pwm_servo"     = @{ Defines = @("PWM_SERVO_ENABLE 1");    Desc = "M280 PWM servo (needs a PWM-capable aux output)." }
    "bltouch"       = @{ Defines = @("BLTOUCH_ENABLE 1");      Desc = "M401/M402 BLTouch (claims one PWM servo output)." }
    "eventout"      = @{ Defines = @("EVENTOUT_ENABLE 1");     Desc = "Bind events/triggers to aux outputs." }
    # --- application plugins ----------------------------------------------
    "fans"          = @{ Defines = @("FANS_ENABLE 1");         Desc = "M106/M107 fan control." }
    "plasma"        = @{ Defines = @("PLASMA_ENABLE 1");       Desc = "Plasma / THC plugin." }
    "laser_coolant" = @{ Defines = @("LASER_COOLANT_ENABLE 1"); Desc = "Laser coolant plugin." }
    "laser_ovd"     = @{ Defines = @("LASER_OVD_ENABLE 1");    Desc = "Laser overdrive PWM M-code." }
    "lb_clusters"   = @{ Defines = @("LB_CLUSTERS_ENABLE 1");  Desc = "LaserBurn cluster support." }
    "embroidery"    = @{ Defines = @("EMBROIDERY_ENABLE 1");   Desc = "Embroidery plugin." }
    "odometer"      = @{ Defines = @("ODOMETER_ENABLE 1");     Desc = "Odometer plugin." }
    "esp_at"        = @{ Defines = @("ESP_AT_ENABLE 1");       Serial = $true; Desc = "Telnet via UART-connected ESP32 (ESP-AT). Consumes a serial port." }
    # --- I/O expanders -----------------------------------------------------
    "mcp3221"       = @{ Defines = @("MCP3221_ENABLE 1");      Desc = "MCP3221 I2C 12-bit ADC input." }
    "mcp4725"       = @{ Defines = @("MCP4725_ENABLE 1");      Desc = "MCP4725 I2C 12-bit DAC output." }
    "mcp23017"      = @{ Defines = @("MCP23017_ENABLE 1");     Desc = "MCP23017 I2C 16-ch digital I/O." }
    "pca9654e"      = @{ Defines = @("PCA9654E_ENABLE 1");     Desc = "PCA9654E I2C 8-ch digital out." }
    # --- stepper drivers ---------------------------------------------------
    "trinamic"      = @{ Defines = @("TRINAMIC_ENABLE 1");     NeedsBoard = $true; Desc = "Trinamic TMC drivers. generic_map.h rejects this (#error). Use a Trinamic-capable board map." }
    # --- networking / wireless (also set CMake options + may force pico_w) --
    "wifi"          = @{ Cmake = @("ADD_WIFI=ON");      ForceBoard = "pico_w"; NeedsSub = @("networking","webui"); Desc = "WiFi networking (Pico W only)." }
    "ethernet"      = @{ Cmake = @("ADD_ETHERNET=ON");  NeedsSub = @("networking","webui"); Desc = "Wiznet W5500/W5100S Ethernet (SPI)." }
    "bluetooth"     = @{ Cmake = @("ADD_BLUETOOTH=ON"); ForceBoard = "pico_w"; Desc = "Bluetooth SPP (Pico W only)." }
    "mdns"          = @{ Cmake = @("ADD_mDNS=ON");      Desc = "mDNS responder (requires wifi or ethernet)." }
    "mqtt"          = @{ Cmake = @("ADD_MQTT=ON");      Desc = "MQTT client (requires wifi or ethernet)." }
    "hpgl"          = @{ Cmake = @("ADD_HPGL=ON");      Desc = "HPGL plotter plugin (C.ITOH CX-6000)." }
}

function Show-Catalog {
    Write-Host "`nAvailable modules (-Modules key1,key2,...):`n" -ForegroundColor Cyan
    foreach ($k in $Catalog.Keys) {
        $m = $Catalog[$k]
        $tag = if ($m.ContainsKey("Cmake")) { "[cmake]" } else { "[define]" }
        $warn = if ($m.ContainsKey("NeedsBoard")) { " (needs board map)" }
                elseif ($m.ContainsKey("Serial")) { " (uses a serial port)" } else { "" }
        "{0,-16} {1,-8} {2}{3}" -f $k, $tag, $m.Desc, $warn | Write-Host
    }
    Write-Host "`nBoards: pico (default), pico_w   |   Axes: 3 (default), 4, 8" -ForegroundColor Cyan
    Write-Host "Spindles: pwm (default) or grblHAL spindle symbols (SPINDLE_PWM0, SPINDLE_HUANYANG1, ...)`n"
}

if ($ListModules) { Show-Catalog; return }

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Green }
function Get-Web($url, $dest) {
    Write-Host "  downloading $url"
    & curl.exe -L -s -o $dest $url
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dest)) { throw "download failed: $url" }
}
function Expand-Zip($zip, $dir) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    & cmake -E tar xf $zip --format=zip | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "extract failed: $zip" }
}

# ---------------------------------------------------------------------------
# 1. Toolchain / SDK / tools
# ---------------------------------------------------------------------------
function Setup-Tools {
    Write-Step "Setting up toolchain, SDK and host tools"
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

    if (-not (Test-Path (Join-Path $ArmDir "bin\arm-none-eabi-gcc.exe"))) {
        $zip = Join-Path $ToolsDir "arm-toolchain.zip"
        if (-not (Test-Path $zip)) { Get-Web $ArmZipUrl $zip }
        Write-Host "  extracting ARM toolchain..."
        New-Item -ItemType Directory -Force -Path $ArmDir | Out-Null
        Push-Location $ArmDir; & cmake -E tar xf $zip --format=zip | Out-Null; Pop-Location
    } else { Write-Host "  ARM toolchain present." }

    if (-not (Test-Path (Join-Path $SdkDir "pico_sdk_init.cmake"))) {
        Write-Host "  cloning Pico SDK $SdkVersion..."
        & git clone --depth 1 -b $SdkVersion https://github.com/raspberrypi/pico-sdk.git $SdkDir
        Push-Location $SdkDir; & git submodule update --init --depth 1 lib/tinyusb; Pop-Location
    } else { Write-Host "  Pico SDK present." }

    if (-not (Test-Path (Join-Path $PioasmDir "pioasmConfig.cmake"))) {
        $zip = Join-Path $ToolsDir "pico-sdk-tools.zip"
        if (-not (Test-Path $zip)) { Get-Web $SdkToolsUrl $zip }
        $out = Join-Path $ToolsDir "pico-sdk-tools-bin"
        New-Item -ItemType Directory -Force -Path $out | Out-Null
        Push-Location $out; & cmake -E tar xf $zip --format=zip | Out-Null; Pop-Location
    } else { Write-Host "  pioasm present." }

    if (-not (Test-Path (Join-Path $PicotoolDir "picotoolConfig.cmake"))) {
        $zip = Join-Path $ToolsDir "picotool.zip"
        if (-not (Test-Path $zip)) { Get-Web $PicotoolUrl $zip }
        $out = Join-Path $ToolsDir "picotool-bin"
        New-Item -ItemType Directory -Force -Path $out | Out-Null
        Push-Location $out; & cmake -E tar xf $zip --format=zip | Out-Null; Pop-Location
    } else { Write-Host "  picotool present." }
}

# ---------------------------------------------------------------------------
# 2. Source + submodules
# ---------------------------------------------------------------------------
# Submodules that CMakeLists.txt include()s unconditionally - all must exist
# even when the corresponding feature is disabled (the plugins self-gate
# internally on their *_ENABLE macros).
$RequiredSubs = @("grbl","eeprom","sdcard","keypad","bluetooth","motors",
                  "trinamic","spindle","embroidery","fans","laser","plugins","plasma")

function Setup-Source([string[]]$extraSubs) {
    Write-Step "Fetching grblHAL source + submodules"
    if (-not (Test-Path (Join-Path $SrcDir ".git"))) {
        & git clone https://github.com/grblHAL/RP2040.git $SrcDir
    }
    $subs = $RequiredSubs + $extraSubs | Select-Object -Unique
    Push-Location $SrcDir
    foreach ($s in $subs) {
        if (-not (Test-Path (Join-Path $SrcDir "$s\CMakeLists.txt")) -and
            -not (Test-Path (Join-Path $SrcDir "$s\.git"))) {
            Write-Host "  submodule: $s"
            & git submodule update --init --depth 1 $s
        }
    }
    Pop-Location
}

# ---------------------------------------------------------------------------
# 3. Generate my_machine.h from the selected options
# ---------------------------------------------------------------------------
function Write-MyMachine([string]$board, [int]$axes, [string[]]$mods, [string[]]$spindles, [string[]]$extraDefines) {
    Write-Step "Generating my_machine.h"
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("/* my_machine.h - GENERATED by build-grblhal.ps1 - do not edit by hand. */")
    $lines.Add("/* Baseline = grblHAL 'generic board' defaults (Native USB, probe, PWM spindle, grblHAL compat). */")
    $lines.Add("")

    # Board map selection (generic family). 3-axis uses the default generic_map.h.
    if     ($axes -eq 4) { $lines.Add("#define BOARD_GENERIC_4AXIS") }
    elseif ($axes -eq 8) { $lines.Add("#define BOARD_GENERIC_8AXIS") }
    # else: no board macro -> driver.h falls back to boards/generic_map.h (3 axis)

    $lines.Add("")
    $lines.Add("// --- baseline (default) flags ---")
    $lines.Add("#define USB_SERIAL_CDC      1   // Native USB CDC")
    $lines.Add("#define COMPATIBILITY_LEVEL 0   // grblHAL native -> ESTOP_ENABLE defaults to 1")

    # Spindles
    $lines.Add("")
    $lines.Add("// --- spindle(s) ---")
    if (-not ($spindles.Count -eq 1 -and $spindles[0] -eq "pwm")) {
        $i = 0
        foreach ($s in $spindles) {
            $lines.Add("#define SPINDLE$($i)_ENABLE $s")
            $i++
        }
    } else {
        $lines.Add("// (none specified -> default on-board PWM spindle is instantiated)")
    }

    # Modules (my_machine.h #define type only; cmake-type handled at configure)
    $defLines = New-Object System.Collections.Generic.List[string]
    foreach ($key in $mods) {
        $k = $key.Trim().ToLower()
        if (-not $Catalog.Contains($k)) { throw "Unknown module '$key'. Run -ListModules." }
        $m = $Catalog[$k]
        if ($m.ContainsKey("Defines")) { foreach ($d in $m.Defines) { $defLines.Add("#define $d") } }
    }
    if ($defLines.Count -gt 0) {
        $lines.Add("")
        $lines.Add("// --- selected modules ---")
        $lines.AddRange([string[]]$defLines)
    }

    # Driver-level tuning defines (e.g. ESTOP_ENABLE) - these ARE read in the
    # driver translation units (driver.c / grbl/driver_opts.h include my_machine.h).
    if ($extraDefines -and $extraDefines.Count -gt 0) {
        $lines.Add("")
        $lines.Add("// --- machine tuning (driver-level) ---")
        foreach ($d in $extraDefines) { $lines.Add("#define $d") }
    }

    # Networking conditional tail (verbatim behaviour from upstream my_machine.h):
    # daemons auto-enable when WIFI/ETHERNET/WEBUI are turned on (via CMake).
    $tail = @'

// --- networking daemons (auto, keyed off CMake-set WIFI/ETHERNET) ---
#if WIFI_ENABLE || ETHERNET_ENABLE || WEBUI_ENABLE
#define TELNET_ENABLE        1
#define WEBSOCKET_ENABLE     1
#if SDCARD_ENABLE || WEBUI_ENABLE
#define FTP_ENABLE           1
#endif
#endif

/**/
'@
    $lines.Add($tail)

    $path = Join-Path $SrcDir "my_machine.h"
    if ((Test-Path $path) -and -not (Test-Path "$path.orig")) { Copy-Item $path "$path.orig" }
    Set-Content -Path $path -Value ($lines -join "`r`n") -Encoding ASCII
    Write-Host "  wrote $path ($($lines.Count) lines)"
}

# ---------------------------------------------------------------------------
# Inject global compile definitions (machine defaults) into CMakeLists.txt.
# Regenerated from CMakeLists.txt.orig every run, so it stays clean when no
# defaults are requested.
# ---------------------------------------------------------------------------
function Apply-CMakeDefs([string[]]$cDefs) {
    $cmk  = Join-Path $SrcDir "CMakeLists.txt"
    $orig = "$cmk.orig"
    if (-not (Test-Path $orig)) { Copy-Item $cmk $orig }
    $text = Get-Content $orig -Raw
    if ($cDefs -and $cDefs.Count -gt 0) {
        $body = ($cDefs | ForEach-Object { "    $_" }) -join "`r`n"
        $block = "`r`n# >>> build-grblhal.ps1 machine defaults >>>`r`n" +
                 "add_compile_definitions(`r`n$body`r`n)`r`n" +
                 "# <<< build-grblhal.ps1 machine defaults <<<`r`n"
        # Inject right after cmake_minimum_required(), i.e. before the first
        # include(grbl/CMakeLists.txt) so the grbl library picks the defs up.
        $text = $text -replace "(cmake_minimum_required\([^\)]*\))", "`$1`r`n$block"
    }
    Set-Content -Path $cmk -Value $text -Encoding ASCII
}

# ---------------------------------------------------------------------------
# 4 + 5. Configure & build
# ---------------------------------------------------------------------------
function Invoke-Build([string]$board, [string[]]$mods, [string[]]$cDefs) {
    Write-Step "Configuring (CMake / Ninja)"

    $env:PICO_SDK_PATH       = $SdkDir
    $env:PICO_TOOLCHAIN_PATH = $ArmDir

    # Collect CMake-type options from selected modules
    $cmakeOpts = @()
    $effBoard  = $board
    foreach ($key in $mods) {
        $m = $Catalog[$key.Trim().ToLower()]
        if ($m.ContainsKey("Cmake")) { $cmakeOpts += $m.Cmake }
        if ($m.ContainsKey("ForceBoard")) { $effBoard = $m.ForceBoard }
    }

    if ($Clean -and (Test-Path $BuildDir)) { Remove-Item -Recurse -Force $BuildDir }

    $args = @(
        "-G", "Ninja", "-S", $SrcDir, "-B", $BuildDir,
        "-DCMAKE_BUILD_TYPE=Release",
        "-DPICO_BOARD=$effBoard",
        "-DPICO_TOOLCHAIN_PATH=$ArmDir",
        "-Dpioasm_DIR=$PioasmDir",
        "-Dpicotool_DIR=$PicotoolDir"
    )
    foreach ($o in $cmakeOpts) { $args += "-D$o" }

    # Machine-default overrides are injected into CMakeLists.txt as
    # add_compile_definitions() (global), because the core file that consumes them
    # (grbl/settings.c) includes config.h but NOT my_machine.h. We inject via the
    # CMake project (not CMAKE_C_FLAGS) so the SDK's -mcpu/-mthumb arch flags stay
    # intact - overriding CMAKE_C_FLAGS would break CMSIS architecture detection.
    Apply-CMakeDefs $cDefs
    Write-Host "  cmake $($args -join ' ')"
    & cmake @args
    if ($LASTEXITCODE -ne 0) { throw "CMake configure failed." }

    Write-Step "Building"
    $buildArgs = @("--build", $BuildDir)
    if ($Jobs -gt 0) { $buildArgs += @("-j", "$Jobs") }
    & cmake @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "Build failed." }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# --- Pre-flight validation: turn grblHAL's compile-time #error guards into
#     clear, early failures instead of cryptic build errors. -----------------
$modsLower = @($Modules | ForEach-Object { $_.Trim().ToLower() })

# 1) Modules that require pins/peripherals the generic board maps do not define.
#    (generic_map.h / driver_opts2.h emit #error for these.)
$needsBoard = @($modsLower | Where-Object { $Catalog.Contains($_) -and $Catalog[$_].ContainsKey("NeedsBoard") })
if ($needsBoard.Count -gt 0) {
    throw ("These modules are not supported on the generic board map: {0}.`n" +
           "  They need board-specific pins (SD_CS/SPI, I2C strobe, Trinamic, ...).`n" +
           "  Use a board map that provides them (e.g. BOARD_PICO_CNC, BOARD_BTT_SKR_PICO_10)`n" +
           "  or add a custom boards/my_machine_map.h. See README-build.md.") -f ($needsBoard -join ", ")
}

# 2) eeprom_fram is a modifier on eeprom.
if (($modsLower -contains "eeprom_fram") -and ($modsLower -notcontains "eeprom")) {
    throw "'eeprom_fram' requires 'eeprom'."
}

# 3) Serial-port budget. With Native USB as the primary stream the generic Pico
#    has room for ~1 extra serial consumer (driver_opts2.h:385). Warn if exceeded.
$serialMods = @($modsLower | Where-Object { $Catalog.Contains($_) -and $Catalog[$_].ContainsKey("Serial") })
if ($serialMods.Count -gt 1) {
    Write-Warning ("Multiple serial-port consumers selected ({0}). The generic RP2040 build" -f ($serialMods -join ", "))
    Write-Warning "  typically supports only one; the build may fail with 'Too many options that requires a serial port'."
}

# 4) mdns/mqtt require networking.
if (($modsLower -contains "mdns" -or $modsLower -contains "mqtt") -and
    -not ($modsLower -contains "wifi" -or $modsLower -contains "ethernet")) {
    throw "'mdns'/'mqtt' require 'wifi' or 'ethernet'."
}

# extra submodules required by networking modules
$extraSubs = @()
foreach ($k in $modsLower) {
    if ($Catalog.Contains($k) -and $Catalog[$k].ContainsKey("NeedsSub")) { $extraSubs += $Catalog[$k].NeedsSub }
}

if ($SetupTools)  { Setup-Tools }
if ($FetchSource) { Setup-Source $extraSubs }

# Verify prerequisites exist
foreach ($p in @(
    @{n="ARM gcc";   p=(Join-Path $ArmDir "bin\arm-none-eabi-gcc.exe")},
    @{n="Pico SDK";  p=(Join-Path $SdkDir "pico_sdk_init.cmake")},
    @{n="pioasm";    p=(Join-Path $PioasmDir "pioasmConfig.cmake")},
    @{n="picotool";  p=(Join-Path $PicotoolDir "picotoolConfig.cmake")},
    @{n="source";    p=(Join-Path $SrcDir "CMakeLists.txt")}
)) {
    if (-not (Test-Path $p.p)) { throw "Missing prerequisite '$($p.n)' at $($p.p). Run with -SetupTools and/or -FetchSource." }
}

# ---------------------------------------------------------------------------
# Machine defaults: homing, limit-switch polarity, E-Stop.
#   - homing/limit defaults -> global compiler -D (consumed by grbl/settings.c)
#   - ESTOP_ENABLE          -> my_machine.h (consumed by the driver)
# These are *defaults*; a board with stored settings only adopts them after $RST=*.
# ---------------------------------------------------------------------------
$cDefs      = @()
$extraDefs  = @()
$AXIS_BIT   = @{ "X" = 1; "Y" = 2; "Z" = 4; "A" = 8; "B" = 16; "C" = 32 }

if ($NoEStop) {
    # Reset input acts as a normal soft-reset instead of a latching E-Stop, so a
    # floating/asserted reset line cannot wedge the board in an unclearable alarm.
    $extraDefs += "ESTOP_ENABLE 0"
}

if ($NoControlInputs) {
    # Do not monitor the reset / feed-hold / cycle-start input pins at all. Use
    # this when no control buttons are wired and a floating/asserted reset line
    # (GP18) keeps the board in an unclearable alarm (status 18/79). Soft reset
    # over USB still works. Overrides driver.h's default CONTROL_ENABLE.
    $extraDefs += "CONTROL_ENABLE 0"
}

if ($InvertControl) {
    # Invert reset+feed-hold+cycle-start so normally-closed-to-GND buttons read
    # 'inactive' at rest. (1|2|4 = reset|feedhold|cyclestart). Use this instead of
    # -NoControlInputs when you DO have NC control buttons wired.
    $cDefs += "DEFAULT_CONTROL_SIGNALS_INVERT_MASK=7"
}

if ($Homing) {
    Write-Host "  homing: order=$HomingOrder, switches=$LimitSwitches, requireHoming=$([bool]$RequireHoming)"
    $order = $HomingOrder.ToUpper().ToCharArray() | ForEach-Object { "$_" }
    foreach ($ax in $order) {
        if (-not $AXIS_BIT.ContainsKey($ax)) { throw "Bad -HomingOrder '$HomingOrder' (use axis letters like XYZ)." }
    }
    $cDefs += "DEFAULT_HOMING_ENABLE=1"
    $cDefs += "DEFAULT_HOMING_SINGLE_AXIS_COMMANDS=1"   # enables `$HX/`$HY/`$HZ
    $cDefs += ("DEFAULT_HOMING_INIT_LOCK=" + $(if ($RequireHoming) { "1" } else { "0" }))

    # One axis per homing pass, in the requested order -> deterministic sequence,
    # and Y can never be skipped (it is not lumped together with X).
    $i = 0
    foreach ($ax in $order) { $cDefs += "DEFAULT_HOMING_CYCLE_$i=$($AXIS_BIT[$ax])"; $i++ }
    while ($i -le 5) { $cDefs += "DEFAULT_HOMING_CYCLE_$i=0"; $i++ }  # clear stock X|Y default etc.

    # NC limit switches read 'triggered' at rest unless the limit pins are inverted.
    if ($LimitSwitches -eq "NC") {
        $mask = (1 -shl $Axes) - 1
        $cDefs += "DEFAULT_LIMIT_SIGNALS_INVERT_MASK=$mask"
    }

    # Conservative first-commissioning rates (match the GUI's safe-homing preset).
    $cDefs += "DEFAULT_HOMING_FEED_RATE=50.0f"
    $cDefs += "DEFAULT_HOMING_SEEK_RATE=300.0f"
    $cDefs += "DEFAULT_HOMING_PULLOFF=2.0f"
}

Write-MyMachine -board $Board -axes $Axes -mods $Modules -spindles $Spindles -extraDefines $extraDefs
Invoke-Build  -board $Board -mods $Modules -cDefs $cDefs

# ---------------------------------------------------------------------------
# Collect output
# ---------------------------------------------------------------------------
Write-Step "Collecting firmware"
$uf2 = Join-Path $BuildDir "grblHAL.uf2"
if (-not (Test-Path $uf2)) { throw "Expected $uf2 was not produced." }
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$stamp = (Get-Item $uf2).LastWriteTime.ToString("yyyyMMdd-HHmmss")
$name  = "grblHAL_${Board}_${Axes}axis"
if ($Homing)        { $name += "_homing" }
if ($Modules.Count) { $name += "_" + (($Modules | ForEach-Object { $_.ToLower() }) -join "-") }
$destUf2 = Join-Path $Output "$name.uf2"
Copy-Item $uf2 $destUf2 -Force

$size = [math]::Round((Get-Item $destUf2).Length / 1KB, 1)
Write-Host "`nSUCCESS" -ForegroundColor Green
Write-Host "  firmware : $destUf2  ($size KB)"
Write-Host "  flash    : hold BOOTSEL, plug in the Pico, copy the .uf2 onto the RPI-RP2 drive."
& (Join-Path $PicotoolDir "picotool.exe") info $uf2 2>$null
