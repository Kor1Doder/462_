"""G-code tokenizer / Program tests (M6).

Covers all four block types, the modal carryover that motivated writing our own
tokenizer (bare-coordinate moves), both comment styles, and a real program.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from cncctl.gcode.modal import DistanceMode, MotionMode, Units
from cncctl.gcode.parse import (
    Comment,
    Motion,
    Setting,
    ToolChange,
    Word,
    parse_file,
    parse_string,
)
from cncctl.gcode.parse import _tokenize as tokenize


# -- block types -------------------------------------------------------------
def test_motion_block() -> None:
    (block,) = parse_string("G1 X10 Y20 F300").blocks
    assert isinstance(block, Motion)
    assert block.mode is MotionMode.LINEAR
    assert block.words == {"X": 10.0, "Y": 20.0}
    assert block.context.feed == 300.0


def test_setting_block() -> None:
    (block,) = parse_string("G21 G90").blocks
    assert isinstance(block, Setting)
    assert block.words == (Word("G", 21.0), Word("G", 90.0))
    assert block.context.units is Units.MM


def test_tool_change_block() -> None:
    (block,) = parse_string("M6 T3").blocks
    assert isinstance(block, ToolChange)
    assert block.tool == 3


def test_tool_change_without_tool_number() -> None:
    (block,) = parse_string("M6").blocks
    assert isinstance(block, ToolChange)
    assert block.tool is None


def test_paren_comment_only_line() -> None:
    (block,) = parse_string("(Operation: Contour)").blocks
    assert isinstance(block, Comment)
    assert block.text == "Operation: Contour"


def test_semicolon_comment_only_line() -> None:
    (block,) = parse_string("; tool up").blocks
    assert isinstance(block, Comment)
    assert block.text == "tool up"


# -- the reason we wrote our own tokenizer -----------------------------------
def test_modal_carryover_on_bare_coordinate() -> None:
    motions = parse_string("G1 X10 F100\nX20 Y5\nX30").motions()
    assert len(motions) == 3
    assert all(m.mode is MotionMode.LINEAR for m in motions)  # mode carried
    assert motions[1].words == {"X": 20.0, "Y": 5.0}
    assert motions[2].words == {"X": 30.0}
    assert all(m.context.feed == 100.0 for m in motions)  # feed carried


# -- comment handling on code lines ------------------------------------------
def test_trailing_comment_on_code_line_is_dropped() -> None:
    (block,) = parse_string("G1 X5 (plunge)").blocks
    assert isinstance(block, Motion)
    assert block.words == {"X": 5.0}


def test_inline_paren_comment_between_words() -> None:
    (block,) = parse_string("G1 X5 (rapid) Y10").blocks
    assert isinstance(block, Motion)
    assert block.words == {"X": 5.0, "Y": 10.0}


def test_unterminated_paren_comment_on_code_line() -> None:
    (block,) = parse_string("G0 X1 (oops no close").blocks
    assert isinstance(block, Motion)
    assert block.words == {"X": 1.0}


def test_unterminated_paren_comment_only_line() -> None:
    (block,) = parse_string("(no close").blocks
    assert isinstance(block, Comment)
    assert block.text == "no close"


# -- modal context tracking --------------------------------------------------
def test_units_and_distance_tracked() -> None:
    motion = parse_string("G20\nG91\nG1 X1").motions()[0]
    assert motion.context.units is Units.INCH
    assert motion.context.distance is DistanceMode.INCREMENTAL


def test_wcs_tracked() -> None:
    assert parse_string("G55\nG0 X1").motions()[0].context.wcs == "G55"


def test_arc_with_offset_words() -> None:
    (block,) = parse_string("G2 X10 Y0 I5 J0").blocks
    assert isinstance(block, Motion)
    assert block.mode is MotionMode.ARC_CW
    assert block.words == {"X": 10.0, "Y": 0.0, "I": 5.0, "J": 0.0}


def test_signed_and_fractional_values() -> None:
    (block,) = parse_string("G1 X-1.5 Y.5 Z+2").blocks
    assert isinstance(block, Motion)
    assert block.words == {"X": -1.5, "Y": 0.5, "Z": 2.0}


# -- structure ---------------------------------------------------------------
def test_blank_and_demarcation_lines_skipped() -> None:
    program = parse_string("%\nG0 X1\n\n   \nG0 X2\n%")
    assert len(program) == 2


def test_line_indices_preserved() -> None:
    program = parse_string("(c)\nG0 X1\n\nG0 X2")  # source indices 0, 1, 3
    assert [b.line_index for b in program.blocks] == [0, 1, 3]


def test_iter_and_len() -> None:
    program = parse_string("G0 X1\nG0 X2")
    assert len(program) == 2
    assert list(program) == list(program.blocks)


def test_parse_file(tmp_path: Path) -> None:
    program_file = tmp_path / "prog.nc"
    program_file.write_text("G1 X10 F100\nX20\n", encoding="utf-8")
    assert len(parse_file(program_file).motions()) == 2


def test_real_machining_program() -> None:
    src = """%
(1001 contour)
G21 G90 G94
G54
M6 T1
M3 S12000
G0 X0 Y0 Z5
G1 Z-2 F200
X50
Y50
X0
Y0
G0 Z5
M5
M30
%"""
    program = parse_string(src)
    assert any(isinstance(b, ToolChange) for b in program.blocks)
    assert any(isinstance(b, Comment) for b in program.blocks)
    assert any(isinstance(b, Setting) for b in program.blocks)
    motions = program.motions()
    # X50/Y50/X0/Y0 are bare-coordinate LINEAR moves carried from the G1 Z-2.
    linear = [m for m in motions if m.mode is MotionMode.LINEAR]
    assert len(linear) >= 5


# -- tokenizer property test -------------------------------------------------
_word_letters = st.sampled_from("GXYZIJKFS")
_word_values = st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False)


@given(pairs=st.lists(st.tuples(_word_letters, _word_values), min_size=1, max_size=8))
def test_tokenizer_round_trip(pairs: list[tuple[str, float]]) -> None:
    # Fixed-point formatting keeps values inside the tokenizer's numeric grammar
    # (no scientific notation).
    formatted = [(letter, f"{value:.4f}") for letter, value in pairs]
    line = " ".join(f"{letter}{text}" for letter, text in formatted)
    tokens = tokenize(line)
    assert [t.letter for t in tokens] == [letter for letter, _ in formatted]
    assert [t.value for t in tokens] == [float(text) for _, text in formatted]
