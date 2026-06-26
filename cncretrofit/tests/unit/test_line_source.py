"""Line-source tests (M4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from cncctl.streamer import line_source


async def _collect(source: AsyncIterator[str]) -> list[str]:
    return [line async for line in source]


async def test_from_lines_strips_and_skips_blanks() -> None:
    out = await _collect(line_source.from_lines(["G0 X1  ", "", "   ", "\tG1 Y2"]))
    assert out == ["G0 X1", "G1 Y2"]


async def test_from_string_splits_and_skips_blanks() -> None:
    out = await _collect(line_source.from_string("G0 X1\nG1 Y2\n\nM30\n"))
    assert out == ["G0 X1", "G1 Y2", "M30"]


async def test_from_string_keeps_comments() -> None:
    out = await _collect(line_source.from_string("(start)\nG0 X1 ; rapid\n"))
    assert out == ["(start)", "G0 X1 ; rapid"]


async def test_from_file(tmp_path: Path) -> None:
    program = tmp_path / "prog.nc"
    program.write_text("G0 X1\n\nG1 Y2\nM30\n", encoding="utf-8")
    out = await _collect(line_source.from_file(program))
    assert out == ["G0 X1", "G1 Y2", "M30"]
