# EMCO CNC Retrofit — Custom Controller

This file is the source of truth for Claude Code on this project. Read it at the
start of every session. If something here is wrong or out of date, **stop and
ask the user to update this file before continuing** — do not silently work
against stale guidance.

---

## 1. Project Context

We are retrofitting an EMCO CNC mill. The hardware path is already proven:
G-code sent through ioSender drives the motors correctly. The goal of this
codebase is to build a **pure-Python, headless core library** that talks to
grblHAL on the Pico over USB-CDC, and (later) a FastAPI + web UI on top of it.

**ioSender's source code is our reference implementation.** It is not a runtime
dependency. We port its algorithms to idiomatic Python, validate against its
behavior, and improve where Python's strengths make it natural.

**License:** GPL-3.0. This is a non-commercial PoC; we accept any derivative-
work obligations inherited from reading ioSender's source. Do not raise the
licensing question again in this codebase.

**Development host:** Windows, for now.
**Deployment host (later):** Linux on a Raspberry Pi 4/5 acting as the
machine's brain.
**Hard rule:** no platform-specific code paths without an explicit `if
sys.platform` guard, a comment justifying it, and a TODO to remove it.
Cross-platform CI from M0 — see §6.

## 2. Hardware Inventory

- **MCU:** Raspberry Pi Pico (RP2040) running grblHAL generic build.
  Pin map reference: `grblHAL/RP2040/boards/generic_map.h`.
- **Motors:** 3 × 57BHH100 NEMA23 stepper (X, Y, Z).
- **Drivers:** 3 × CWD 556 (STEP/DIR/ENA, microstepping via DIP switches).
- **Host connection:** USB-CDC. Windows shows `COMx`, Linux shows `/dev/ttyACM0`.
- **Pendant / MPG / probe / spindle control:** not yet wired. Reserve hooks but
  do not implement.

**Calibration constants** live in `config/machine.toml` (committed):
- DIP-switch microstepping setting per driver.
- Lead-screw pitch per axis.
- Computed `$100` / `$101` / `$102` (steps/mm) per axis.
- Soft-limit travel per axis (`$130`–`$132`).
- Max rate (`$110`–`$112`) and acceleration (`$120`–`$122`) — established
  empirically during commissioning, not guessed.
- Junction deviation (`$11`) — fed into the motion simulator (see §7 M10).

On controller boot: push these to the device and verify by re-reading `$$`.
Mismatch is an error.

## 3. Architecture Principles

1. **Layered, with hard boundaries.** UI → API → facade → Controller protocol →
   real backend → streamer → parser → transport. No layer reaches around
   another.
2. **The `Controller` protocol exists for testability.** A `FakeController`
   substitutes the real one in tests without a serial port or a machine. The
   protocol is not a forward bet on multiple backends.
3. **Asyncio everywhere.** Transport is async (`pyserial-asyncio`); every
   layer above is async. Blocking calls are a code smell.
4. **Typed messages across boundaries.** Pydantic v2 or `msgspec`. No dict-of-
   strings between modules.
5. **No silent failure modes around machine state.** Lost connections, parse
   errors, alarm transitions, and unexpected responses are typed exceptions,
   surfaced immediately. Never assume the machine is `Idle`.
6. **Algorithmic fidelity to ioSender where it counts.** Streaming, realtime
   command handling, status parsing, and reconnect logic must behave the way
   ioSender behaves. Style is idiomatic Python; behavior is faithful.
7. **Buy don't build for non-core concerns.** G-code parsing, motion
   simulation, and visualization use vetted libraries (§7 M6, M10). Streaming
   and protocol handling are written by us — that's the project.
8. **Pure Python.** No C extensions in our code.

## 4. Module Layout

```
cncctl/
├── pyproject.toml
├── CLAUDE.md
├── config/
│   └── machine.toml                  # calibration, pin map, limits
├── reference/
│   └── ioSender/                     # source checkout, reference reading only; not imported
├── src/cncctl/
│   ├── controller/
│   │   ├── protocol.py               # Controller Protocol (the public interface)
│   │   ├── messages.py               # typed: Status, Alarm, Settings, ProbeResult, ProgramProgress
│   │   ├── state.py                  # MachineState enum, transition rules
│   │   ├── errors.py                 # typed exception hierarchy
│   │   ├── real.py                   # composes transport + parser + streamer into a Controller
│   │   └── fake.py                   # in-memory, deterministic Controller for tests
│   ├── transport/
│   │   ├── base.py                   # AsyncTransport Protocol
│   │   ├── serial_transport.py       # pyserial-asyncio over USB-CDC
│   │   └── fake_transport.py         # in-memory, scriptable for tests
│   ├── protocol/
│   │   ├── inbound.py                # parse bytes -> typed messages
│   │   ├── outbound.py               # encode commands -> bytes
│   │   └── realtime.py               # single-byte realtime command constants and helpers
│   ├── streamer/
│   │   ├── character_counter.py      # ports ioSender's character-counting streamer
│   │   └── line_source.py            # iterators over G-code (file, string, async generator)
│   ├── facade.py                     # high-level: connect, jog, home, send_file, hold, resume, reset
│   ├── gcode/
│   │   ├── parse.py                  # in-house tokenizer -> our typed Program (see M6)
│   │   └── modal.py                  # modal-group accounting on top, if needed
│   ├── viz/
│   │   ├── simulate.py               # in-house geometric toolpath -> Trace (see M10)
│   │   ├── analyze.py                # bounding box, travel time, soft-limit check from traces
│   │   ├── workpiece.py             # heightmap material-removal: carve a Trace into a stock (see M10)
│   │   └── render.py                 # 2D matplotlib (Agg) initially; 3D/plotly later
│   ├── calibration/
│   │   ├── steps_per_mm.py
│   │   ├── backlash.py
│   │   └── squaring.py
│   ├── safety/
│   │   ├── invariants.py             # see §8
│   │   └── soft_limits.py
│   └── config_io.py                  # load/validate config/machine.toml
├── tests/
│   ├── unit/                         # fake transport / fake controller; fast
│   ├── integration/                  # real Controller against grblHAL simulator over virtual COM pair
│   └── hil/                          # real hardware, opt-in via env var
└── tools/
    ├── grblhal_sim/                  # protocol-level grblHAL simulator (not a motion simulator)
    └── replay/                       # capture+replay of real serial sessions
```

### The `Controller` protocol

```python
# src/cncctl/controller/protocol.py — sketch, not final
class Controller(Protocol):
    async def connect(self, port: str) -> None: ...
    async def disconnect(self) -> None: ...
    async def soft_reset(self) -> None: ...
    async def home(self, axes: Iterable[Axis] | None = None) -> None: ...
    async def jog(self, axis: Axis, distance_mm: float, feed_mm_min: float) -> None: ...
    async def cancel_jog(self) -> None: ...
    async def feed_hold(self) -> None: ...
    async def resume(self) -> None: ...
    async def read_settings(self) -> Settings: ...
    async def write_setting(self, key: int, value: str) -> None: ...
    async def send_program(self, lines: AsyncIterable[str]) -> AsyncIterator[ProgramProgress]: ...
    def status_stream(self) -> AsyncIterator[Status]: ...
```

Consumers (facade, API, UI) never import from `transport/`, `protocol/`, or
`streamer/`. Only `RealController` knows about those.

## 5. grblHAL Protocol Specification

Ported from ioSender's source; read that for the canonical algorithm. This
section is our derived requirements.

### 5.1 Streaming: character counting

- grblHAL's RX buffer is typically 128 bytes. **Verify on our specific build
  via `$I` and the build configuration** before going live; if it differs,
  update this section and `streamer/character_counter.py`.
- Track `bytes_outstanding = bytes_sent - bytes_acknowledged`. Each `ok\r\n`
  or `error:N\r\n` acks the **oldest** unacknowledged line. Maintain a FIFO
  of `(line_text, line_length_including_lf)`.
- Only send the next line when `len(line) + 1 ≤ buffer_size - bytes_outstanding`.
- ioSender's exact ack accounting is the reference. If there's an ambiguity,
  ioSender's behavior wins.

### 5.2 Real-time commands: bypass the buffer

Single bytes processed immediately by grblHAL:

| Byte    | Meaning                  |
|---------|--------------------------|
| `0x18`  | Soft reset (Ctrl-X)      |
| `?`     | Status report request    |
| `~`     | Cycle start / resume     |
| `!`     | Feed hold                |
| `0x84`  | Safety door              |
| `0x85`  | Jog cancel               |
| `0x90`+ | Feed/spindle overrides   |

Transport exposes:
- `send_line(s: str)` — character-counted via the streamer.
- `send_realtime(b: int)` — bypasses the streamer's queue, writes directly.

Both must be safely callable concurrently; they share the underlying serial
writer with byte-write-granular locking only.

### 5.3 Inbound parser

Line-shape dispatcher; each shape produces a typed message:

| Shape                                  | Message       |
|----------------------------------------|---------------|
| `ok`                                   | `Ok`          |
| `error:N`                              | `Error(N)`    |
| `ALARM:N`                              | `Alarm(N)`    |
| `<state\|MPos:...\|FS:...\|...>`       | `Status(...)` |
| `[MSG:...]`                            | `Feedback`    |
| `[GC:...]`                             | `ModalState`  |
| `[G54:...]` etc.                       | `WCSReport`   |
| `[PRB:...]`                            | `ProbeResult` |
| `[VER:...]` / `[OPT:...]`              | `BuildInfo`   |
| `$N=value`                             | `SettingLine` |
| `GrblHAL X.YY ...`                     | `Welcome`     |

Status reports arrive **asynchronously w.r.t. acks**. Dispatch by line shape,
not by what was last sent.

The `Status` carries the raw `Pn:` pin string; `Status.signals` decodes it into
a typed `InputSignals` ("switch logic": per-axis limit switches, probe, door,
e-stop, reset/hold/cycle-start, with any unnamed letter preserved). grblHAL
reports either `MPos` *or* `WPos` (per `$10`) plus `WCO` only periodically — the
controller caches `WCO` and derives the missing position so consumers always see
both (this is implemented in `RealController._on_status`, not just intended).

### 5.4 Welcome and reset

Reception of a `Welcome` line is a hard state reset: drop the ack queue,
clear modal state, re-poll settings, emit a state-changed event.

### 5.5 Alarm is sticky

After `ALARM:N`, the machine ignores motion until `$X` (unlock) or `$H`
(home). The facade enforces this client-side too — motion calls in Alarm
raise `MachineNotReadyError` without reaching the streamer.

### 5.6 Settings

`$$` emits every `$N=value` line followed by `ok`. Cache the parsed map
after every connect and after every successful `$N=value` write. After every
write, re-read `$$` and diff; mismatch is an error.

## 6. Testing Strategy

Three tiers. Tier 1 every commit (fast). Tier 2 on PR. Tier 3 hand-run before
merging anything touching the streamer, parser, or safety layer.

**Cross-platform CI from M0.** GitHub Actions matrix: `windows-latest` and
`ubuntu-latest`, Python 3.12. Tier 1 must pass on both. Tier 2 needs a
virtual COM pair (`com0com` on Windows, `socat` on Linux); document install
in `tools/`.

### Tier 1 — Unit tests
- Run against `FakeTransport` / `FakeController`. No serial, no hardware.
- Cover:
  - Inbound parser: Hypothesis round-trips of every well-formed line shape.
  - Outbound encoder: bytes match expected output.
  - Character-counting streamer: across 10k random `(line-lengths, ack-timings)`
    sequences, outstanding bytes never exceed buffer size.
  - State machine: exhaustive transitions including illegal ones (must raise).
  - Facade: behavior against `FakeController`.
  - G-code parse wrapper: parse known programs, verify typed output.
  - Visualization analyze: bounding-box / travel-time on known inputs.
- Coverage target: ≥ 90% statements on `src/cncctl/`.

### Tier 2 — Integration tests (`tools/grblhal_sim`)
- Python coroutine imitating a grblHAL device on a virtual COM port.
  Configurable: per-line ack delay, status report rate, alarm injection,
  settings dictionary, welcome on reset.
- Not a motion simulator — that's grblHAL's job.
- Suite: connect → soft reset → settings round-trip → 1000-line program →
  assert no buffer overflow, all acks received, final state `Idle`, continuous
  status reports.

### Tier 3 — HIL
- Gated by `CNCCTL_HIL=1`.
- First action of every test: print a 5-second "abort now" message.
- Curated: connect → soft reset → settings round-trip → jog and jog-cancel →
  feed hold / resume → known short program, assert final `MPos`.
- Assumes spindle off, table clear.

## 7. Incremental Milestones — Must-Haves Only

Each milestone produces something runnable and testable. Do not start N+1
until N's tests are green on both Windows and Linux CI.

### M0 — Bootstrap
- `pyproject.toml`, `ruff`, `mypy --strict`, `pytest`, `pytest-asyncio`,
  `hypothesis`, `pre-commit`.
- GitHub Actions Windows + Linux matrix.
- `CLAUDE.md`, `README.md`, `LICENSE` (GPL-3.0).
- Clone ioSender source into `reference/ioSender/`.
- **Done when:** empty test suite runs green on both OSes.

### M1 — Controller protocol + fake
- Define `Controller` protocol, typed messages, error hierarchy, state machine.
- `FakeController`: in-memory, deterministic, scriptable.
- **Done when:** Tier 1 tests exercise every method of the protocol against
  the fake.

### M2 — Transport layer
- `AsyncTransport` protocol.
- `SerialTransport` over `pyserial-asyncio` with reconnect-and-backoff.
- `FakeTransport` for tests.
- Two write paths: `send_line` (buffered) and `send_realtime` (immediate).
- **Done when:** opens the real port, receives the welcome line, logs it on
  both OSes; fake-transport tests cover the abstraction.

### M3 — Inbound parser and outbound encoder
- Parse every line shape in §5.3 with Hypothesis round-trip tests.
- Encoder for line commands and realtime bytes.
- **Done when:** parser handles every example in the grblHAL docs and the
  test corpus; coverage ≥ 95% on `protocol/`.

### M4 — Character-counting streamer
- Port from `reference/ioSender/CNC Core/`. Read it; write idiomatic Python.
- Property test: outstanding bytes never exceed configured buffer size.
- **Done when:** integration test against `grblhal_sim` (M7) streams a
  1000-line program with randomized ack delays, no buffer violations.

### M5 — Real Controller composition
- `RealController` composes transport + parser + streamer.
- Status polling task at configurable rate (default 10 Hz).
- Three consecutive missed status reports → disconnect signal.
- **Done when:** Tier 2 integration suite passes against the simulator on
  both OSes.

### M6 — G-code parsing wrapper
- **Parser:** in-house tokenizer (no third-party dependency). `gcodeparser`
  (the originally-planned library) was evaluated and **rejected**: its line
  regex requires every line to begin with `G`/`M`/`T`, so it silently drops
  bare-coordinate *modal moves* (e.g. `X30 Y40` after a prior `G1`) and
  `( … )` comments. Losing moves would under-report the toolpath and defeat
  M10's host-side soft-limit pre-flight (§8.2) — a safety regression — so we
  do not build on it. The decision and its rationale live in
  `pyproject.toml` and `src/cncctl/gcode/`.
- Implement `cncctl.gcode.parse` behind a stable wrapper interface: takes
  file/string input, returns our typed `Program` (a sequence of typed blocks:
  `Motion`, `Setting`, `ToolChange`, `Comment`, modal carryover applied via
  `cncctl.gcode.modal`).
- The wrapper boundary is preserved so a vetted library can replace the
  in-house tokenizer later without consumers changing.
- **Done when:** parsing a real machining program — including modal moves and
  both comment styles — yields a typed `Program` we can iterate over; unit
  tests cover all block types.

### M7 — grblHAL response simulator (`tools/grblhal_sim`)
- Standalone Python coroutine simulating grblHAL's line-level behavior.
- Used by Tier 2 tests.
- Configurable: ack delay distribution, status report rate, alarm injection.
- **Done when:** the M5 integration suite passes against it.

### M8 — Facade + config
- `Facade` exposes operator-meaningful operations using only the `Controller`
  protocol.
- `config_io.py` loads/validates `config/machine.toml`. Bootstrap: load
  config → connect → push settings → verify via `$$`.
- Safety invariants (§8) live in the facade and are unit-tested.
- **Done when:** facade tests pass against `FakeController`; bootstrap runs
  against the real machine in HIL smoke.

### M9 — G-code file sender
- `facade.send_program(path: Path) -> AsyncIterator[ProgramProgress]`.
- Streams from disk; exposes line/total progress, elapsed time, current MPos.
- **Pre-flight:** runs the program through `viz.analyze` (M10) before
  sending. Refuses to send if soft limits would be violated.
- Cancel: a single call issuing feed hold then soft reset.
- **Done when:** can send a real program on both OSes; pre-flight catches a
  deliberately-out-of-bounds program.

### M10 — Toolpath simulation + visualization
- **Implementation:** in-house geometric simulator (no motion-planner
  dependency). The spike (see `src/cncctl/viz/README.md`) rejected both named
  libraries: `pyGCodeDecode` functionally parses plain XYZ but drags in `vtk`
  + `pyvista` + `pooch`/`requests` (a heavy native tree with runtime
  downloads, poor for the headless Pi/ARM target, §1) and is entirely
  3D-printer-oriented; the documented fallback `gcode-simulator` is X/Y-only
  (no Z), so it cannot bound plunge depth on a 3-axis mill. The
  safety-critical need (the soft-limit bounding box, §8.2) is pure geometry
  computable from the M6 `Program`, so we compute it ourselves.
- Implement:
  - `viz.simulate(program, kinematics) -> Trace` — walks the `Program`
    (absolute/incremental positions, linear segments, plane-aware G2/G3 arcs
    sampled to a chord tolerance) into a typed `Trace` (positions, times,
    per-segment velocity). Pure Python (`math` only). Time is a feed-limited
    estimate (acceleration/junction-deviation-accurate profiles are a future
    opt-in backend, e.g. `pyGCodeDecode`).
  - `viz.analyze(trace, soft_limits) -> AnalysisResult` — bounding box, total
    travel, dry-run duration estimate, per-axis min/max, soft-limit
    violations. **This is what M9's pre-flight depends on.** Pure Python.
  - `viz.render(trace) -> matplotlib.Figure` — 2D (matplotlib, Agg/headless;
    far lighter than vtk, ships aarch64 wheels). 3D/plotly when the web UI lands.
  - `viz.workpiece.carve(trace, stock, tool) -> CarveResult` — material-removal
    ("workpiece") simulation: models the stock as a Z-heightmap and lowers it
    wherever a flat/ball tool sweeps below the surface, yielding the carved top
    surface + removed-volume/max-depth metrics. `cam_warnings(trace, stock)`
    adds the geometric collision checks (rapid into stock, cutting outside the
    footprint, cut-through). The standard cheap 3-axis (no-undercut) model;
    vectorised with `numpy` (the one place the otherwise-pure `viz` core uses
    it — `simulate`/`analyze`, the §8.2 safety path, stay pure Python). Drives
    the live GPU 3D view in the main GUI (`examples/gui.py` -> "Workpiece 3D"
    tab; `pyqtgraph`/OpenGL surface, carve run off the UI thread so orbit stays
    smooth) — a rewrite of the 2.5D, path-only reference previewer that now
    actually removes material — plus a headless matplotlib PNG export
    (`examples/workpiece_sim.py --render`, Qt-free for the Pi/CI).
  - `viz.workpiece.HeightMapCarver` — the stateful, incremental form of `carve`
    (carve one segment at a time); the GUI uses it for **line-by-line playback**
    (Play/Step/Restart/seek) with live material removal, a moving tool marker,
    and a running readout (current G-code line + text, elapsed/total machining
    time, progress, removed volume). `Trace`/`TracePoint` carry the source
    `line` index so playback can name the line being cut.
- **Done when:** simulating a known program (incl. an arc) produces a bounding
  box matching ioSender's preview within tolerance; analyze catches a
  deliberately-out-of-bounds program; render produces a sensible 2D plot;
  carve reproduces a slot/pocket's depth and flags a deliberately
  out-of-stock / cut-through program.

### M11 — Calibration tools
- CLI subcommands invoking the facade:
  - `cncctl calibrate steps --axis X --measured 99.2 --commanded 100.0`
    → computes corrected `$100`.
  - `cncctl calibrate backlash --axis X` → guided measurement protocol.
- Two-step: print proposed `$N=value`, require confirmation, write, verify
  via `$$`.
- **Done when:** steps-per-mm flow runs end-to-end against the real machine
  and the new value persists across power cycles.

### Hooks reserved for later (do not implement)
- WCS management UI (G54–G59).
- Probing cycles, tool-length sensor.
- Pendant / MPG input handler.
- Macro system.
- Multi-job queue.
- 3D visualization in a web UI (the simulator already produces the data; UI
  rendering is the only missing piece).
- FastAPI + web UI layer (deferred until M11 is solid).

## 8. Safety Invariants (NEVER VIOLATE)

Asserted in code and tested. If a change appears to require violating one of
these, **stop and surface the design conflict** — do not relax.

1. **No motion command is sent in `Alarm` or `Door` state.** Rejected at the
   facade with `MachineNotReadyError` before reaching the streamer.
2. **Soft limits are validated host-side** (via `viz.analyze`) before
   sending, in addition to the controller's enforcement.
3. **Soft reset is always available**, regardless of state or queue.
4. **The streamer never sends a line that would exceed the known RX buffer
   size.** No "probably fine" margins.
5. **Status polling continues during programs.** Loss of N consecutive
   status reports (default 3) → treat as disconnect, halt streaming, surface
   to UI.
6. **Disconnect mid-program does not auto-resume.** Operator must reconnect
   explicitly and acknowledge state.
7. **Calibration writes are verified by re-reading `$$`.** Mismatch is an
   error, not a warning.

## 9. Code Conventions

- Python 3.12+. `mypy --strict`. `ruff` (lint + format). `pre-commit` enforces both.
- No bare `except`. Catch specific exceptions; re-raise as typed errors at
  layer boundaries.
- Every public async function documents what it awaits, what it raises, and
  what cancels it.
- Logging: `structlog`, JSON output. Required fields: `event`, `module`. Add
  `mpos`, `state`, `line` where relevant. No `print` in `src/`.
- Paths via `pathlib.Path`. No hardcoded `COM1` or `/dev/ttyACM0` outside
  one defaults table.
- Tests use `pytest`, `pytest-asyncio`, `hypothesis`. No `unittest.TestCase`.
- Multi-platform: any code path with `if sys.platform` needs a comment.

## 10. Definition of Done (per change)

A change is not done until:

1. `ruff check` and `ruff format --check` pass.
2. `mypy --strict src/` passes.
3. `pytest tests/unit` passes on both Windows and Linux CI.
4. If the change touches `transport/`, `protocol/`, `streamer/`, or
   `facade.py`: `pytest tests/integration` passes on both OSes.
5. New behavior has tests at the appropriate tier.
6. If the change touches §5 (protocol), §6 (testing), §7 (milestones), or §8
   (safety), this `CLAUDE.md` is updated in the same commit.
7. If the change touches streamer, transport, or safety: HIL suite is run
   and the commit message includes "HIL: pass" with the date.

## 11. Working With Claude Code on This Repo

- **Read this file first**, every session.
- **One milestone at a time.** No M5 work while M3 is red.
- **`reference/ioSender/` is the canonical algorithm reference** for streamer,
  parser, and protocol logic. Read the corresponding code before
  implementing; cite the path in commit messages.
- **Algorithmic fidelity, not transliteration.** Match ioSender's behavior;
  write idiomatic Python. Pythonic patterns (asyncio, context managers,
  typed dataclasses) are encouraged where they yield a cleaner result — note
  the deviation in the code or PR.
- **Ask before assuming hardware specifics.** Lead-screw pitch, microstepping,
  port name, RX buffer size — these come from the user. No guessing.
- **Third-party libraries are pre-approved for their specific purpose**
  (`pyserial-asyncio` for transport, `matplotlib` for 2D rendering, `numpy` for
  the M10 heightmap material-removal engine in `viz/workpiece.py` — already
  pulled in transitively by matplotlib, ships aarch64 wheels, and only the
  workpiece engine uses it; `PySide6` (LGPL) for the optional desktop GUI, with
  `pyqtgraph` + `PyOpenGL` for its GPU-accelerated 3D workpiece view — all three
  live only in the `gui` optional extra, so the headless Pi/core never pulls
  them). Two originally-named libraries proved unfit and were replaced by
  in-house code:
  `gcodeparser` (drops modal moves; see M6) and `pyGCodeDecode` (heavy
  `vtk`/`pooch` tree, 3D-printer-oriented; see M10). Adding other
  libraries requires PR justification: what it does, why we use it instead of
  writing it, license, maintenance status, last release date.
- **Prefer narrow PRs.** Multi-module changes are hard to review.
- **Behavioral disagreements with ioSender are bugs in our code by default.**
  If our behavior diverges and we believe ours is correct, document why
  before merging.
