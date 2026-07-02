"""M2 HIL smoke: open the real port and read/log the grblHAL welcome line.

Fulfills the M2 done-criterion "opens the real port, receives the welcome line,
logs it". Opt-in and hardware-gated:

* requires ``CNCCTL_HIL=1`` AND ``CNCCTL_PORT`` (no port is ever assumed,);
* prints a 5-second "abort now" message before touching the machine.

Run, e.g.:  ``CNCCTL_HIL=1 CNCCTL_PORT=/dev/ttyACM0 uv run pytest tests/hil``
"""

from __future__ import annotations

import asyncio
import os

import pytest

from cncctl.log import configure_logging, get_logger
from cncctl.transport.serial_transport import ReconnectPolicy, SerialTransport

pytestmark = pytest.mark.hil

_ABORT_SECONDS = 5


async def test_serial_welcome_line() -> None:
    if os.environ.get("CNCCTL_HIL") != "1":
        pytest.skip("HIL tests require CNCCTL_HIL=1 and real hardware connected.")
    port = os.environ.get("CNCCTL_PORT")
    if not port:
        pytest.skip("Set CNCCTL_PORT to the grblHAL device (e.g. COM3 or /dev/ttyACM0).")

    #: give the operator a chance to abort before any I/O.
    print(f"\n*** HIL: opening {port} in {_ABORT_SECONDS}s — Ctrl-C to abort. ***")
    await asyncio.sleep(_ABORT_SECONDS)

    configure_logging(json=False)
    log = get_logger("tests.hil.serial")
    transport = SerialTransport(reconnect=ReconnectPolicy(max_attempts=3))

    await transport.open(port)
    try:
        stream = transport.read_lines()
        welcome = await asyncio.wait_for(anext(stream), timeout=10.0)
    finally:
        await transport.close()

    text = welcome.decode("utf-8", errors="replace")
    log.info("welcome_received", port=port, welcome=text)
    assert "grbl" in text.lower()
