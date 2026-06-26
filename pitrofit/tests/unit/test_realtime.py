"""Realtime command byte tests (M3, §5.2)."""

from __future__ import annotations

from cncctl.protocol.realtime import REALTIME_BYTES, Realtime, is_realtime, to_byte


def test_key_realtime_byte_values() -> None:
    # Ported from ioSender GrblConstants (Grbl.cs:65).
    assert Realtime.SOFT_RESET == 0x18
    assert ord("?") == Realtime.STATUS_REPORT
    assert ord("~") == Realtime.CYCLE_START
    assert ord("!") == Realtime.FEED_HOLD
    assert Realtime.SAFETY_DOOR == 0x84
    assert Realtime.JOG_CANCEL == 0x85
    assert Realtime.FEED_OVR_RESET == 0x90


def test_realtime_members_are_ints() -> None:
    # IntEnum members pass straight to AsyncTransport.send_realtime(int).
    assert int(Realtime.SOFT_RESET) == 24
    assert to_byte(Realtime.FEED_HOLD) == 0x21


def test_is_realtime_membership() -> None:
    assert is_realtime(0x18)
    assert is_realtime(ord("?"))
    assert not is_realtime(ord("A"))
    assert not is_realtime(0x00)


def test_realtime_bytes_set_matches_members() -> None:
    assert {member.value for member in Realtime} == REALTIME_BYTES
    assert all(is_realtime(b) for b in REALTIME_BYTES)


def test_override_bytes_are_distinct() -> None:
    values = [member.value for member in Realtime]
    assert len(values) == len(set(values))  # no duplicate byte assignments
