"""In-house G-code tokenizer producing a typed :class:`Program` (CLAUDE.md §7 M6).

This replaces ``gcodeparser`` (see M6 in CLAUDE.md): a line-oriented tokenizer
that correctly handles **modal moves** (bare-coordinate lines such as ``X30 Y40``
that continue the previously commanded motion) and both comment styles
(``( … )`` and ``;``). It is deliberately a thin, swappable layer — consumers
depend on the typed blocks here, not on any particular parser.

Each non-blank source line becomes exactly one block:

* :class:`Motion`    — the line moves an axis (or arc offset). Its ``mode``,
  ``feed``, units, and distance mode come from the modal context (§7 M6
  "modal carryover applied"), so a bare ``X30`` is a complete motion block.
* :class:`ToolChange` — the line contains ``M6`` (with the selected ``T`` tool).
* :class:`Setting`   — any other state/config line (units, distance mode, plane,
  WCS, feed/spindle set, spindle/coolant M-codes, program control).
* :class:`Comment`   — a comment-only line.

Trailing comments on code lines are not retained on the block (the code is what
matters for analysis); comment-only lines are preserved as :class:`Comment`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import msgspec

from cncctl.gcode.modal import ModalContext, ModalState, MotionMode

# Letters that indicate the line performs a move (linear axes, rotary axes, and
# arc offset/radius words).
_MOTION_WORD_LETTERS = frozenset("XYZABCUVWIJKR")
_TOOLCHANGE_M_CODE = 6
_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


class Word(msgspec.Struct, frozen=True):
    """A single G-code word: a letter plus its numeric value (e.g. ``G`` ``21``)."""

    letter: str
    value: float


class Motion(msgspec.Struct, frozen=True):
    """A line that commands motion, with the modal context resolved."""

    line_index: int
    raw: str
    mode: MotionMode
    words: dict[str, float]  # the axis/offset words present on the line
    context: ModalContext


class Setting(msgspec.Struct, frozen=True):
    """A non-motion state/config line (units, distance mode, feed set, M-codes…)."""

    line_index: int
    raw: str
    words: tuple[Word, ...]
    context: ModalContext


class ToolChange(msgspec.Struct, frozen=True):
    """An ``M6`` tool change; ``tool`` is the selected ``T`` number if present."""

    line_index: int
    raw: str
    tool: int | None
    context: ModalContext


class Comment(msgspec.Struct, frozen=True):
    """A comment-only line."""

    line_index: int
    raw: str
    text: str


Block = Motion | Setting | ToolChange | Comment


class Program(msgspec.Struct, frozen=True):
    """A parsed program: an ordered sequence of typed blocks."""

    blocks: tuple[Block, ...]

    def __iter__(self) -> Iterator[Block]:
        return iter(self.blocks)

    def __len__(self) -> int:
        return len(self.blocks)

    def motions(self) -> tuple[Motion, ...]:
        """Return just the :class:`Motion` blocks, in order (for M10 analysis)."""
        return tuple(block for block in self.blocks if isinstance(block, Motion))


def parse_string(text: str) -> Program:
    """Parse G-code from a string into a :class:`Program`."""
    modal = ModalState()
    blocks: list[Block] = []
    for index, raw in enumerate(text.splitlines()):
        block = _parse_line(index, raw, modal)
        if block is not None:
            blocks.append(block)
    return Program(blocks=tuple(blocks))


def parse_file(path: Path) -> Program:
    """Parse G-code from a file into a :class:`Program`."""
    return parse_string(path.read_text(encoding="utf-8"))


def _parse_line(index: int, raw: str, modal: ModalState) -> Block | None:
    code, comments = _strip_comments(raw)
    words = _tokenize(code)
    if not words:
        if comments:  # comment-only (or empty-paren) line
            return Comment(line_index=index, raw=raw.strip(), text=" ".join(filter(None, comments)))
        return None  # blank line

    m_codes: list[int] = []
    tool: int | None = None
    axis_words: dict[str, float] = {}
    for word in words:
        modal.apply(word.letter, word.value)
        if word.letter == "M":
            m_codes.append(int(word.value))
        elif word.letter == "T":
            tool = int(word.value)
        elif word.letter in _MOTION_WORD_LETTERS:
            axis_words[word.letter] = word.value

    context = modal.snapshot()
    stripped = raw.strip()
    if _TOOLCHANGE_M_CODE in m_codes:
        return ToolChange(line_index=index, raw=stripped, tool=tool, context=context)
    if axis_words:
        return Motion(
            line_index=index, raw=stripped, mode=modal.motion, words=axis_words, context=context
        )
    return Setting(line_index=index, raw=stripped, words=tuple(words), context=context)


def _strip_comments(line: str) -> tuple[str, list[str]]:
    """Split a line into its code and its comments (``( … )`` and ``;`` styles)."""
    code: list[str] = []
    comments: list[str] = []
    i = 0
    while i < len(line):
        char = line[i]
        if char == ";":
            comments.append(line[i + 1 :].strip())
            break
        if char == "(":
            end = line.find(")", i + 1)
            if end == -1:  # unterminated comment runs to end of line
                comments.append(line[i + 1 :].strip())
                break
            comments.append(line[i + 1 : end].strip())
            i = end + 1
            continue
        code.append(char)
        i += 1
    return "".join(code).strip(), comments


def _tokenize(code: str) -> list[Word]:
    return [
        Word(letter=m.group(1).upper(), value=float(m.group(2))) for m in _WORD_RE.finditer(code)
    ]


__all__ = [
    "Block",
    "Comment",
    "Motion",
    "Program",
    "Setting",
    "ToolChange",
    "Word",
    "parse_file",
    "parse_string",
]
