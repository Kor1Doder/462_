"""CharacterCountingStreamer async-orchestration tests (M4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from cncctl.controller.errors import BufferOverflowError, StreamingError
from cncctl.streamer.character_counter import CharacterCountingStreamer, StreamProgress


async def _aiter(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


def _recorder() -> tuple[list[str], Callable[[str], Awaitable[None]]]:
    sent: list[str] = []

    async def send(line: str) -> None:
        sent.append(line)

    return sent, send


async def test_streams_all_lines_then_drains() -> None:
    sent, send = _recorder()
    streamer = CharacterCountingStreamer(buffer_size=128, send_line=send)
    assert streamer.buffer_size == 128
    progress: list[StreamProgress] = []

    async def run() -> None:
        async for item in streamer.stream(_aiter(["G0 X1", "G1 Y2", "M30"])):
            progress.append(item)

    task = asyncio.create_task(run())
    await asyncio.sleep(0)  # let all three send and block in drain
    assert sent == ["G0 X1", "G1 Y2", "M30"]
    assert streamer.pending_count == 3

    for _ in range(3):
        await streamer.acknowledge()
    await task

    assert [p.sent for p in progress] == [1, 2, 3]
    assert progress[-1].line == "M30"


async def test_blocks_until_ack_frees_room() -> None:
    sent, send = _recorder()
    # buffer 10; "AAAAA" costs 6, so two cannot be outstanding at once (12 > 10).
    streamer = CharacterCountingStreamer(buffer_size=10, send_line=send)
    gen = streamer.stream(_aiter(["AAAAA", "BBBBB"]))

    await anext(gen)  # sends AAAAA
    assert sent == ["AAAAA"]
    assert streamer.bytes_outstanding == 6

    pending = asyncio.create_task(anext(gen))  # wants BBBBB, must wait for room
    await asyncio.sleep(0)
    assert not pending.done()
    assert sent == ["AAAAA"]

    assert await streamer.acknowledge() == "AAAAA"  # frees 6
    progress = await pending
    assert sent == ["AAAAA", "BBBBB"]
    assert progress.line == "BBBBB"

    drain = asyncio.create_task(anext(gen))  # enters drain, waits for last ack
    await asyncio.sleep(0)
    assert not drain.done()
    await streamer.acknowledge()
    with pytest.raises(StopAsyncIteration):
        await drain


async def test_line_larger_than_buffer_raises() -> None:
    sent, send = _recorder()
    streamer = CharacterCountingStreamer(buffer_size=8, send_line=send)
    gen = streamer.stream(_aiter(["X" * 20]))  # cost 21 > 8, can never fit
    with pytest.raises(BufferOverflowError):
        await anext(gen)
    assert sent == []


async def test_acknowledge_without_outstanding_raises() -> None:
    _sent, send = _recorder()
    streamer = CharacterCountingStreamer(buffer_size=128, send_line=send)
    with pytest.raises(StreamingError):
        await streamer.acknowledge()


async def test_empty_program_completes_immediately() -> None:
    sent, send = _recorder()
    streamer = CharacterCountingStreamer(buffer_size=128, send_line=send)
    progress = [p async for p in streamer.stream(_aiter([]))]
    assert progress == []
    assert sent == []
