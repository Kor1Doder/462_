"""Inbound line parser: bytes/str -> typed message.

A line-shape dispatcher. Dispatch is by the *shape* of the line, never by what
was last sent (status reports arrive asynchronously w.r.t. acks,). The
parser is stateless: it fills whatever a single line carries. Cross-report
state (e.g. deriving ``MPos`` from a cached ``WCO``) is the controller's job.

Field handling for the ``<...>`` status report is ported from ioSender's
``GrblViewModel.ParseStatus`` / ``Set`` (``reference/ioSender/CNC Core/CNC
Core/GrblViewModel.cs:866``).

Robustness: malformed lines (no recognizable shape) raise ``ParseError`` so the
surprise surfaces. Recognized-but-unknown *bracket* messages — grblHAL
has an open-ended set of them — fall back to ``Feedback`` rather than crashing
the read loop.
"""

from __future__ import annotations

from cncctl.controller.errors import ParseError
from cncctl.controller.messages import (
    Alarm,
    BuildInfo,
    Error,
    Feedback,
    ModalState,
    Ok,
    Overrides,
    Position,
    ProbeResult,
    SettingLine,
    Status,
    WCSReport,
    Welcome,
)
from cncctl.controller.state import MachineState

#: The union of every message the parser can produce.
InboundMessage = (
    Ok
    | Error
    | Alarm
    | Status
    | Feedback
    | ModalState
    | WCSReport
    | ProbeResult
    | BuildInfo
    | SettingLine
    | Welcome
)

#: Bracket keys that carry an x,y,z coordinate offset -> WCSReport.
_COORDINATE_KEYS = frozenset({"G54", "G55", "G56", "G57", "G58", "G59", "G28", "G30", "G92"})


def parse_line(line: bytes | str) -> InboundMessage:  # noqa: PLR0911
    """Parse a single inbound line into a typed message.

    Args:
        line: one line, terminator already stripped (bytes from the transport
            or str). Bytes are decoded as UTF-8.

    Raises:
        ParseError: the bytes are undecodable, or the line matches no shape.
    """
    text = _decode(line).strip()
    if not text:
        raise ParseError("empty line")
    if text == "ok":
        return Ok()
    if text.startswith("error:"):
        return Error(code=_to_int(text[len("error:") :], "error code"))
    if text.startswith("ALARM:"):
        return Alarm(code=_to_int(text[len("ALARM:") :], "alarm code"))
    if text.startswith("<") and text.endswith(">"):
        return _parse_status(text[1:-1])
    if text.startswith("[") and text.endswith("]"):
        return _parse_bracket(text[1:-1])
    if text.startswith("$"):
        return _parse_setting(text)
    if text.lower().startswith("grbl"):
        return _parse_welcome(text)
    raise ParseError(f"unrecognized line: {text!r}")


# --- status report ----------------------------------------------------------
def _parse_status(body: str) -> Status:
    parts = body.split("|")
    state, substate = _parse_state(parts[0])

    mpos: Position | None = None
    wpos: Position | None = None
    wco: Position | None = None
    feed: float | None = None
    spindle: float | None = None
    overrides: Overrides | None = None
    buffer_planner: int | None = None
    buffer_rx: int | None = None
    line_number: int | None = None
    pins: str | None = None
    accessory: str | None = None

    for field in parts[1:]:
        key, sep, value = field.partition(":")
        if not sep:
            continue  # malformed field without a colon; ignore leniently
        if key == "MPos":
            mpos = _parse_position(value)
        elif key == "WPos":
            wpos = _parse_position(value)
        elif key == "WCO":
            wco = _parse_position(value)
        elif key == "FS":
            feed, spindle = _parse_fs(value)
        elif key == "F":
            feed = _to_float(value, "feed")
        elif key == "Bf":
            buffer_planner, buffer_rx = _parse_buffer(value)
        elif key == "Ln":
            line_number = _to_int(value, "line number")
        elif key == "Ov":
            overrides = _parse_overrides(value)
        elif key == "Pn":
            pins = value
        elif key == "A":
            accessory = value
        # other keys (WCS, FW, PWM, P, ...) are not part of the Status shape yet.

    return Status(
        state=state,
        substate=substate,
        mpos=mpos,
        wpos=wpos,
        wco=wco,
        feed=feed,
        spindle=spindle,
        overrides=overrides,
        buffer_planner=buffer_planner,
        buffer_rx=buffer_rx,
        line_number=line_number,
        pins=pins,
        accessory=accessory,
    )


def _parse_state(token: str) -> tuple[MachineState, int | None]:
    name, _, sub = token.partition(":")
    try:
        state = MachineState(name)
    except ValueError as exc:
        raise ParseError(f"unknown machine state: {name!r}") from exc
    substate = _to_int(sub, "substate") if sub else None
    return state, substate


def _parse_position(value: str) -> Position:
    parts = value.split(",")
    if len(parts) < 3:  # noqa: PLR2004 - a Cartesian position needs 3 components
        raise ParseError(f"position needs >= 3 components: {value!r}")
    x, y, z = (_to_float(parts[i], "position") for i in range(3))
    return Position(x, y, z)


def _parse_fs(value: str) -> tuple[float, float | None]:
    parts = value.split(",")
    feed = _to_float(parts[0], "feed")
    spindle = _to_float(parts[1], "spindle") if len(parts) > 1 else None
    return feed, spindle


def _parse_buffer(value: str) -> tuple[int, int]:
    parts = value.split(",")
    if len(parts) != 2:  # noqa: PLR2004 - Bf is exactly planner,rx
        raise ParseError(f"Bf needs 2 components: {value!r}")
    return _to_int(parts[0], "planner buffer"), _to_int(parts[1], "rx buffer")


def _parse_overrides(value: str) -> Overrides:
    parts = value.split(",")
    if len(parts) != 3:  # noqa: PLR2004 - Ov is feed,rapid,spindle
        raise ParseError(f"Ov needs 3 components: {value!r}")
    return Overrides(
        feed=_to_int(parts[0], "feed override"),
        rapid=_to_int(parts[1], "rapid override"),
        spindle=_to_int(parts[2], "spindle override"),
    )


# --- bracket messages -------------------------------------------------------
def _parse_bracket(inner: str) -> InboundMessage:  # noqa: PLR0911
    key, sep, value = inner.partition(":")
    if not sep:
        return Feedback(text=inner)  # e.g. "[enabled]"
    if key == "MSG":
        return Feedback(text=value)
    if key == "GC":
        return ModalState(modes=tuple(value.split()))
    if key == "PRB":
        return _parse_probe(value)
    if key == "VER":
        return BuildInfo(version=value, raw=f"[{inner}]")
    if key == "OPT":
        return BuildInfo(version="", options=value, raw=f"[{inner}]")
    if key in _COORDINATE_KEYS or key.startswith("G59."):
        return WCSReport(wcs=key, offset=_parse_position(value))
    return Feedback(text=inner)  # unknown but well-formed bracket: stay lenient


def _parse_probe(value: str) -> ProbeResult:
    coords, sep, success = value.rpartition(":")
    if not sep:
        raise ParseError(f"PRB needs position:success: {value!r}")
    return ProbeResult(position=_parse_position(coords), success=success == "1")


# --- settings and welcome ---------------------------------------------------
def _parse_setting(text: str) -> SettingLine:
    key_str, sep, value = text[1:].partition("=")
    if not sep or not key_str.isdigit():
        raise ParseError(f"unrecognized $-line: {text!r}")
    return SettingLine(key=int(key_str), value=value)


def _parse_welcome(text: str) -> Welcome:
    tokens = text.split()
    version = tokens[1] if len(tokens) > 1 else None
    return Welcome(raw=text, version=version)


# --- primitives -------------------------------------------------------------
def _decode(line: bytes | str) -> str:
    if isinstance(line, str):
        return line
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"undecodable line bytes: {line!r}") from exc


def _to_int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ParseError(f"invalid {what}: {value!r}") from exc


def _to_float(value: str, what: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ParseError(f"invalid {what}: {value!r}") from exc


__all__ = ["InboundMessage", "parse_line"]
