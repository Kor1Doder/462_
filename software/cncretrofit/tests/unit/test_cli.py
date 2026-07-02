"""CLI argument-parsing tests (M11)."""

from __future__ import annotations

import pytest

from cncctl.calibration.steps_per_mm import StepsProposal
from cncctl.cli import _emit, _stdin_confirm, _stdin_measure, build_parser, main
from cncctl.controller.messages import Axis


def test_parser_steps_defaults() -> None:
    args = build_parser().parse_args(
        ["calibrate", "steps", "--axis", "X", "--commanded", "100", "--measured", "99.2"]
    )
    assert args.command == "calibrate"
    assert args.target == "steps"
    assert args.axis == "X"
    assert args.commanded == 100.0
    assert args.measured == 99.2
    assert args.yes is False
    assert args.port is None


def test_parser_steps_with_yes_and_port() -> None:
    args = build_parser().parse_args(
        [
            "calibrate",
            "steps",
            "--axis",
            "Z",
            "--commanded",
            "50",
            "--measured",
            "50.1",
            "--yes",
            "--port",
            "COM9",
        ]
    )
    assert args.axis == "Z"
    assert args.yes is True
    assert args.port == "COM9"


def test_parser_backlash() -> None:
    args = build_parser().parse_args(["calibrate", "backlash", "--axis", "Y"])
    assert args.target == "backlash"
    assert args.axis == "Y"


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_parser_rejects_unknown_axis() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["calibrate", "steps", "--axis", "Q", "--commanded", "100", "--measured", "99"]
        )


# -- I/O helpers -------------------------------------------------------------
def test_emit_writes_a_line_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _emit("hello")
    assert capsys.readouterr().out == "hello\n"


_PROPOSAL = StepsProposal(
    axis=Axis.X, setting_key=100, current=100.0, corrected=100.8, commanded=100.0, measured=99.2
)


@pytest.mark.parametrize(
    ("answer", "expected"), [("y", True), ("yes", True), ("n", False), ("", False)]
)
def test_stdin_confirm(monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert _stdin_confirm(_PROPOSAL) is expected


def test_stdin_measure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "0.08")
    assert _stdin_measure() == 0.08


def test_main_backlash_runs_the_guide(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "0.05")
    code = main(["calibrate", "backlash", "--axis", "X"])
    assert code == 0
    assert "backlash" in capsys.readouterr().out.lower()
