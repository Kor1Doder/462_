"""BufferAccount tests: the §8.4 invariant under random reserve/ack sequences.

CLAUDE.md §6 Tier 1: "across 10k random (line-lengths, ack-timings) sequences,
outstanding bytes never exceed buffer size." The accounting is pure, so this is
a fast, exhaustive property test.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cncctl.controller.errors import BufferOverflowError, StreamingError
from cncctl.streamer.character_counter import BufferAccount, line_cost


def test_line_cost_includes_terminator() -> None:
    assert line_cost("G0 X1") == len("G0 X1") + 1
    assert line_cost("") == 1  # just the terminator


def test_initial_state() -> None:
    account = BufferAccount(128)
    assert account.buffer_size == 128
    assert account.bytes_outstanding == 0
    assert account.free == 128
    assert account.pending_count == 0


@pytest.mark.parametrize("size", [0, -1, -128])
def test_rejects_nonpositive_buffer(size: int) -> None:
    with pytest.raises(ValueError, match="buffer_size"):
        BufferAccount(size)


def test_reserve_and_acknowledge_are_fifo() -> None:
    account = BufferAccount(128)
    account.reserve("first", 6)
    account.reserve("second", 7)
    assert account.bytes_outstanding == 13
    assert account.pending_count == 2
    assert account.acknowledge() == "first"  # oldest acked first (§5.1)
    assert account.bytes_outstanding == 7
    assert account.acknowledge() == "second"
    assert account.bytes_outstanding == 0


def test_acknowledge_without_pending_raises() -> None:
    with pytest.raises(StreamingError):
        BufferAccount(128).acknowledge()


def test_reserve_beyond_free_raises() -> None:
    account = BufferAccount(10)
    account.reserve("x" * 8, 9)  # outstanding 9, free 1
    with pytest.raises(BufferOverflowError):
        account.reserve("y", 5)


def test_can_send_boundary_is_inclusive() -> None:
    account = BufferAccount(10)
    assert account.can_send(10)  # exactly fills an empty buffer
    account.reserve("a", 6)
    assert account.can_send(4)  # 4 <= free 4
    assert not account.can_send(5)  # 5 > free 4


def test_fits_at_all() -> None:
    account = BufferAccount(10)
    assert account.fits_at_all(10)
    assert not account.fits_at_all(11)


@settings(max_examples=600)
@given(
    buffer_size=st.integers(min_value=4, max_value=256),
    ops=st.lists(
        st.tuples(st.sampled_from(["reserve", "ack"]), st.integers(min_value=1, max_value=256)),
        max_size=300,
    ),
)
def test_outstanding_never_exceeds_buffer(buffer_size: int, ops: list[tuple[str, int]]) -> None:
    account = BufferAccount(buffer_size)
    model: list[int] = []  # costs of pending lines, in send order
    for op, raw_cost in ops:
        if op == "reserve":
            cost = min(raw_cost, buffer_size)  # clamp so a line can fit an empty buffer
            if account.can_send(cost):
                account.reserve("x" * (cost - 1), cost)
                model.append(cost)
        elif account.pending_count:
            account.acknowledge()
            model.pop(0)

        # SAFETY INVARIANT (§8.4): never over the buffer, ever.
        assert account.bytes_outstanding <= buffer_size
        assert account.bytes_outstanding == sum(model)
        assert account.free == buffer_size - account.bytes_outstanding
        assert account.pending_count == len(model)
