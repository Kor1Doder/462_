# cncctl — headless CNC controller core

A pure-Python, asyncio controller library for the EMCO CNC mill running
[grblHAL](https://github.com/grblHAL) on a Raspberry Pi Pico over USB-CDC. This
is the clean core: ioSender's proven streaming / parsing / protocol algorithms
ported to idiomatic Python, with an asyncio-native facade on top. The
operator application in [`../pitrofit`](../pitrofit) builds its GUI on this
library.

## Architecture

Layered, with hard boundaries — each layer talks only to the one below it:

```
facade  ─►  Controller protocol  ─►  real controller  ─►  streamer / parser  ─►  transport (USB serial)
```

| Package | Responsibility |
|---------|----------------|
| `transport/` | Async serial over USB-CDC (`pyserial-asyncio`) with reconnect/backoff; a fake transport for tests. |
| `protocol/` | Encode commands → bytes; parse grblHAL lines (`ok`, `error:N`, `ALARM:N`, `<status>`, `[messages]`, settings, welcome) → typed messages. |
| `streamer/` | The character-counting streamer (ported from ioSender): never lets outstanding bytes exceed the Pico's RX buffer. |
| `controller/` | Composes the above into a `Controller` with status polling and reconnect; a deterministic `FakeController` for tests. |
| `facade.py` | Operator-meaningful operations (connect, jog, home, `send_program`, hold/resume/reset, set work-zero, calibrate) — the only surface the GUI/API uses. |
| `gcode/` | In-house G-code tokenizer → typed program (handles modal moves and both comment styles). |
| `viz/` | Geometric toolpath simulation, soft-limit analysis, 2D render, and heightmap material-removal — see [`src/cncctl/viz/README.md`](src/cncctl/viz/README.md). |
| `calibration/`, `safety/`, `config_io.py` | Steps-per-mm & backlash flows, the safety invariants, and `machine.toml` loading. |

### grblHAL protocol notes

- **Streaming is character-counted:** track bytes outstanding vs. the RX buffer (typically 128 B); each `ok`/`error:N` acknowledges the oldest unacknowledged line.
- **Real-time bytes bypass the queue:** `0x18` reset, `?` status, `~` resume, `!` feed-hold, `0x85` jog-cancel, feed/spindle overrides.
- **Status reports** (`<State|MPos:…|Pn:…|FS:…>`) arrive asynchronously; the `Pn` field decodes into typed input signals (per-axis limit switches, probe, door, e-stop).

### Safety invariants (never violated)

- No motion command is sent in `Alarm`/`Door` state.
- Soft limits are validated host-side before a program is streamed.
- Soft reset is always available, regardless of state or queue.
- The streamer never sends a line that would exceed the known RX buffer.
- Loss of consecutive status reports is treated as a disconnect; a program never auto-resumes after a drop.
- Calibration writes are verified by re-reading `$$`.

## Install & run

Requires [uv](https://docs.astral.sh/uv/) (pins Python 3.12 automatically).

```bash
uv sync                                # create .venv, install deps
uv run pytest tests/unit               # fast unit tests
uv run ruff check
uv run mypy --strict src/cncctl
```

`config/machine.toml` holds the machine calibration and limits (steps/mm,
max rate, acceleration, travel, junction deviation). Commission it with real
measured values before driving hardware.

## License

GPL-3.0-or-later — see [`LICENSE`](LICENSE).
