# cncctl — EMCO CNC mill retrofit (custom controller)

Headless Python core library for talking to [grblHAL](https://github.com/grblHAL)
running on an RP2040 Pico over USB-CDC. The hardware path is already proven
via ioSender; this codebase is a clean-room Python port of ioSender's
algorithms (streamer, parser, protocol handling) plus an asyncio-native facade
on top.

**The source of truth for this project is [`CLAUDE.md`](CLAUDE.md).** Read it
before doing anything. It defines the architecture, the protocol contract,
the safety invariants (§8 — never violate), and the milestone-by-milestone
build plan (§7).

## Status

**M0 — Bootstrap.** Repository scaffold and tooling only. No functional code
yet; subpackages under `src/cncctl/` are placeholders that name their owning
milestone. See `CLAUDE.md §7` for the roadmap.

## Quick start (development)

Requires [uv](https://docs.astral.sh/uv/). uv pins Python 3.12 automatically
per `pyproject.toml`.

```bash
uv sync                                # create .venv, install dev deps
uv run pytest                          # full test suite (unit + skipped placeholders)
uv run pytest tests/unit               # Tier-1 only (fast)
uv run ruff check
uv run ruff format --check
uv run mypy --strict src/cncctl
```

Pre-commit hooks (recommended once per clone):

```bash
uv run pre-commit install
```

## Deploy on a Raspberry Pi 5

The deployment target is a Raspberry Pi 5 running Raspberry Pi OS (Trixie,
64-bit, Desktop), wired to the grblHAL Pico over USB and launching the operator
GUI fullscreen on boot. See [`deploy/README.md`](deploy/README.md) for the full
guide; the short version:

```bash
git clone <this-repo-url> ~/cncctl
cd ~/cncctl
sudo deploy/install.sh        # deps + venv + udev + labwc autostart + autologin
sudo reboot
```

If you prefer a plain `pip` environment instead of `uv` (Python 3.12+ required):

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # third-party runtime + GUI deps
pip install -e . --no-deps               # the cncctl package itself
python examples/gui.py --config config/machine.toml
```

## Reference checkouts

Two external source trees are read-only references — they are **not imported
at runtime** and are intentionally `.gitignore`d to keep the repo small.
They must be present locally before working on streamer / parser / protocol
code:

- `reference/ioSender/` — canonical algorithm reference for everything in
  `src/cncctl/{protocol,streamer,transport}/`. Read it before implementing.
- `reference/grblHAL/` — firmware source, especially `RP2040/boards/generic_map.h`
  for our pin map.

If you are starting fresh, clone the upstream repositories into those paths.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE). The license is inherited from
the obligation to read ioSender's source (CLAUDE.md §1).
