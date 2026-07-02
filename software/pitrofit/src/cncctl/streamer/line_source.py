"""G-code line sources for the streamer.

Async iterators over program lines from various inputs. Each yields lines with
trailing whitespace removed and blank lines skipped (sending a blank line only
wastes an RX-buffer slot). Comments are left intact — grbl handles them, and
stripping them would change what reaches the controller.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from pathlib import Path


async def from_lines(lines: Iterable[str]) -> AsyncIterator[str]:
    """Yield cleaned, non-blank lines from any synchronous iterable of strings."""
    for raw in lines:
        line = raw.strip()
        if line:
            yield line


async def from_string(text: str) -> AsyncIterator[str]:
    """Yield cleaned, non-blank lines from a multi-line string."""
    async for line in from_lines(text.splitlines()):
        yield line


async def from_file(path: Path) -> AsyncIterator[str]:
    """Yield cleaned, non-blank lines from a G-code file.

    The file is read off the event loop via ``asyncio.to_thread`` so the read
    does not block other transport/streamer work.
    """
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    async for line in from_string(text):
        yield line


__all__ = ["from_file", "from_lines", "from_string"]
