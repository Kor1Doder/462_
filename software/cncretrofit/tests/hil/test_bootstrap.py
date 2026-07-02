"""M8 HIL smoke: bootstrap a commissioned config against the real machine.

Fulfills the M8 done-criterion "bootstrap runs against the real machine in HIL
smoke". Opt-in and hardware-gated per CLAUDE.md §6 Tier 3:

* requires ``CNCCTL_HIL=1`` AND ``CNCCTL_PORT`` (no port is ever assumed, §11);
* ``CNCCTL_CONFIG`` may point at a *commissioned* machine.toml (the committed
  one ships placeholder zeros, so the test skips unless a real config is given);
* prints a 5-second "abort now" message before touching the machine.

Run, e.g.:
  CNCCTL_HIL=1 CNCCTL_PORT=/dev/ttyACM0 CNCCTL_CONFIG=~/mymachine.toml \
    uv run pytest tests/hil/test_bootstrap.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from cncctl.config_io import load_config, require_commissioned, settings_from_config
from cncctl.controller.errors import ConfigError
from cncctl.controller.real import RealController
from cncctl.facade import Facade
from cncctl.transport.serial_transport import SerialTransport

pytestmark = pytest.mark.hil

_ABORT_SECONDS = 5


async def test_bootstrap_against_real_machine() -> None:
    if os.environ.get("CNCCTL_HIL") != "1":
        pytest.skip("HIL tests require CNCCTL_HIL=1 and real hardware connected.")
    port = os.environ.get("CNCCTL_PORT")
    if not port:
        pytest.skip("Set CNCCTL_PORT to the grblHAL device (e.g. COM3 or /dev/ttyACM0).")

    config_path = Path(os.environ.get("CNCCTL_CONFIG", "config/machine.toml"))
    config = load_config(config_path)
    try:
        require_commissioned(config)
    except ConfigError:
        pytest.skip(f"{config_path} is not commissioned; set CNCCTL_CONFIG to a real config.")

    print(f"\n*** HIL: bootstrapping {port} from {config_path}")
    print(f"*** in {_ABORT_SECONDS}s — Ctrl-C to abort. ***")
    await asyncio.sleep(_ABORT_SECONDS)

    facade = Facade(RealController(SerialTransport()))
    await facade.bootstrap(config, port)
    try:
        settings = await facade.read_settings()
        expected = settings_from_config(config)
        assert settings.get(100) == expected[100]  # steps/mm round-tripped
    finally:
        await facade.disconnect()
