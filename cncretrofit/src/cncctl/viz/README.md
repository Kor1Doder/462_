# `cncctl.viz` — toolpath simulation & visualization

This package turns a parsed `cncctl.gcode.Program` into a typed `Trace`,
analyzes it (bounding box, soft-limit pre-flight, duration), and renders it.

## M10 spike: why this is in-house, not `pyGCodeDecode`

CLAUDE.md §7 M10 originally named `pyGCodeDecode` (primary) and
`gcode-simulator` (fallback). The mandated spike rejected both:

### `pyGCodeDecode` — functional but a poor fit
- **Functionally OK:** it parses and simulates plain XYZ G-code (no extruder)
  without error.
- **Heavy native dependency tree:** installing it pulls `vtk` (the C++
  Visualization Toolkit, hundreds of MB), `pyvista`, and `pooch` + `requests`
  (runtime data downloaders). For the **headless Raspberry Pi / ARM
  deployment target (CLAUDE.md §1)** this is a serious liability: vtk aarch64
  wheels are large and heavy to import, and `pooch` implies network fetches
  that fail offline / on first run.
- **3D-printer-oriented API:** machine configs are *printer presets*
  (`prusa_mini`, `ultimaker_2plus`, …) and the result surface is extrusion-
  centric (`extrusion_extent`, `extrusion_max_vel`). It is file + YAML based,
  and `get_values(t)` evaluates at a single time rather than returning a trace
  — exactly the "3D-printing-oriented API gets in the way" risk CLAUDE.md
  flagged.

### `gcode-simulator` (the documented fallback) — unusable
- Its CLI/model is **X/Y only**. A 3-axis mill needs **Z** to bound plunge
  depth for the soft-limit check (§8.2), so the fallback is a dead end.

### Decision
The safety-critical part — the soft-limit **bounding box** (§8.2, M9's
pre-flight) — is **pure geometry**, computable directly from the M6 `Program`.
It needs no motion planner. So we compute it ourselves:

- `simulate.py` — walks the `Program` (absolute/incremental positions, linear
  segments, plane-aware G2/G3 arcs sampled to a chord tolerance) into a typed
  `Trace`. Pure Python (`math` only). Times are a **feed-limited estimate**;
  acceleration- and junction-deviation-accurate velocity profiles are a future
  opt-in backend (where `pyGCodeDecode` could return, behind this interface).
- `analyze.py` — bounding box, per-axis min/max, total travel, duration, and
  soft-limit violations. Pure Python. This is the §8.2 safety check.
- `render.py` — 2D plot via `matplotlib` (Agg / headless). Much lighter than
  vtk and ships aarch64 wheels.

The `viz` interface is stable, so a vetted motion-planner backend can slot in
later without consumers changing.
