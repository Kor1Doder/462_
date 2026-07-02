"""FakeTransport behavior, AsyncTransport conformance, and backoff schedule (M2).

the design done-criteria: "fake-transport tests cover the abstraction."
"""

from __future__ import annotations

import pytest

from cncctl.controller.errors import NotConnectedError
from cncctl.transport.base import AsyncTransport
from cncctl.transport.fake_transport import FakeTransport
from cncctl.transport.serial_transport import ReconnectPolicy, SerialTransport, backoff_delay


# -- conformance -------------------------------------------------------------
def test_fake_satisfies_transport_protocol() -> None:
    assert isinstance(FakeTransport(), AsyncTransport)


def test_serial_satisfies_transport_protocol() -> None:
    assert isinstance(SerialTransport(), AsyncTransport)


# -- open / close ------------------------------------------------------------
async def test_open_then_close() -> None:
    t = FakeTransport()
    assert not t.is_open
    await t.open("COM-TEST")
    assert t.is_open
    assert t.opened_port == "COM-TEST"
    await t.close()
    assert not t.is_open


async def test_close_is_idempotent() -> None:
    t = FakeTransport()
    await t.open("p")
    await t.close()
    await t.close()  # no raise
    assert not t.is_open


# -- write paths -------------------------------------------------------------
async def test_send_line_records_in_order() -> None:
    t = FakeTransport()
    await t.open("p")
    await t.send_line("G0 X1")
    await t.send_line("$$")
    assert t.sent_lines == ["G0 X1", "$$"]


async def test_send_realtime_records_and_validates() -> None:
    t = FakeTransport()
    await t.open("p")
    await t.send_realtime(0x18)
    await t.send_realtime(ord("?"))
    assert t.sent_realtime == [0x18, ord("?")]
    with pytest.raises(ValueError, match="range"):
        await t.send_realtime(999)


async def test_writes_before_open_raise() -> None:
    t = FakeTransport()
    with pytest.raises(NotConnectedError):
        await t.send_line("x")
    with pytest.raises(NotConnectedError):
        await t.send_realtime(1)


# -- read path ---------------------------------------------------------------
async def test_read_lines_frames_scripted_bytes() -> None:
    t = FakeTransport()
    await t.open("p")
    t.feed_line("GrblHAL 1.1f")
    t.feed_bytes(b"ok\r\n<Idle|MPos:0.000,0.000,0.000>\r\n")
    t.close_inbound()
    got = [line async for line in t.read_lines()]
    assert got == [b"GrblHAL 1.1f", b"ok", b"<Idle|MPos:0.000,0.000,0.000>"]


async def test_read_lines_drops_empty_lines() -> None:
    t = FakeTransport()
    await t.open("p")
    t.feed_bytes(b"\r\nok\r\n\r\n")
    t.close_inbound()
    got = [line async for line in t.read_lines()]
    assert got == [b"ok"]


async def test_read_lines_ends_on_close() -> None:
    t = FakeTransport()
    await t.open("p")
    t.feed_line("ok")
    stream = t.read_lines()
    assert await anext(stream) == b"ok"
    await t.close()  # sentinel unblocks the pending get
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_read_before_open_raises() -> None:
    t = FakeTransport()
    with pytest.raises(NotConnectedError):
        _ = [line async for line in t.read_lines()]


# -- backoff schedule --------------------------------------------------------
def test_backoff_doubles_then_caps() -> None:
    policy = ReconnectPolicy(base_delay=0.5, max_delay=5.0, max_attempts=10)
    assert backoff_delay(0, policy) == 0.5
    assert backoff_delay(1, policy) == 1.0
    assert backoff_delay(2, policy) == 2.0
    assert backoff_delay(3, policy) == 4.0
    assert backoff_delay(4, policy) == 5.0  # capped at max_delay
    assert backoff_delay(20, policy) == 5.0


def test_backoff_rejects_negative_attempt() -> None:
    with pytest.raises(ValueError, match="attempt"):
        backoff_delay(-1, ReconnectPolicy())


# -- serial transport guards (no hardware) -----------------------------------
async def test_serial_writes_before_open_raise() -> None:
    t = SerialTransport()
    with pytest.raises(NotConnectedError):
        await t.send_line("x")
    with pytest.raises(NotConnectedError):
        await t.send_realtime(1)
    with pytest.raises(NotConnectedError):
        _ = [line async for line in t.read_lines()]
