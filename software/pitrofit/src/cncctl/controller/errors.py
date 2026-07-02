"""Typed exception hierarchy for the controller layer.

Per CLAUDE.md §3.5, machine-state surprises are never swallowed: lost
connections, parse errors, alarm transitions, and unexpected responses are
typed exceptions surfaced immediately. Every layer boundary re-raises lower
errors as one of these types (CLAUDE.md §9: "no bare except").

The full tree is defined here even though some leaves are first *raised* in
later milestones — having the hierarchy stable early lets every layer catch
at the right granularity. The milestone that first raises each leaf is noted
in its docstring.
"""

from __future__ import annotations


class CncError(Exception):
    """Base class for every error raised by cncctl.

    Catching ``CncError`` catches anything this library raises on purpose.
    Anything that is *not* a ``CncError`` escaping our code is a bug.
    """


# --- transport / connection -------------------------------------------------
class TransportError(CncError):
    """A failure in the byte transport (serial/USB-CDC). First raised in M2."""


class NotConnectedError(TransportError):
    """An operation requiring an open transport was attempted while closed."""


class ConnectionLostError(TransportError):
    """The transport dropped unexpectedly.

    Per CLAUDE.md §8.6, a mid-program disconnect must not auto-resume; the
    operator reconnects and re-acknowledges state. First raised in M2/M5.
    """


# --- protocol / parsing -----------------------------------------------------
class ProtocolError(CncError):
    """The device spoke something we could not reconcile with the grbl protocol."""


class ParseError(ProtocolError):
    """An inbound line did not match any known shape (CLAUDE.md §5.3). First raised in M3."""


class UnexpectedResponseError(ProtocolError):
    """A well-formed line arrived that does not fit the current exchange.

    Status reports are asynchronous w.r.t. acks (CLAUDE.md §5.3), so this is
    reserved for genuinely contradictory responses, not mere reordering.
    """


# --- machine state ----------------------------------------------------------
class MachineStateError(CncError):
    """Base for errors about the machine's state machine (CLAUDE.md §5, §8)."""


class IllegalTransitionError(MachineStateError):
    """An attempt was made to move the state model along an illegal edge.

    The legal transition graph lives in ``state.py``. Raised when an observed
    or requested transition is not in that graph — surfaced, never assumed
    away (CLAUDE.md §5: "Never assume the machine is Idle").
    """

    def __init__(self, frm: object, to: object) -> None:
        self.frm = frm
        self.to = to
        super().__init__(f"illegal state transition: {frm!r} -> {to!r}")


class MachineNotReadyError(MachineStateError):
    """A motion command was issued while the machine could not safely move.

    SAFETY INVARIANT (CLAUDE.md §8.1): no motion command is sent in ``Alarm``
    or ``Door`` state. Rejected here before it can reach the streamer.
    """


class AlarmError(MachineStateError):
    """The machine is in (or entered) an alarm condition (CLAUDE.md §5.5).

    Alarm is sticky: motion stays locked out until ``$X`` (unlock) or ``$H``
    (home). Carries the grbl alarm code when known.
    """

    def __init__(self, code: int | None = None, message: str | None = None) -> None:
        self.code = code
        detail = message or (f"alarm {code}" if code is not None else "alarm")
        super().__init__(detail)


# --- settings ---------------------------------------------------------------
class SettingsError(CncError):
    """Base for settings read/write problems (CLAUDE.md §5.6)."""


class SettingsMismatchError(SettingsError):
    """A written setting did not read back identically.

    SAFETY INVARIANT (CLAUDE.md §8.7): calibration writes are verified by
    re-reading ``$$``; a mismatch is an error, not a warning. First raised in
    M8/M11.
    """


# --- streaming --------------------------------------------------------------
class StreamingError(CncError):
    """Base for errors during character-counted program streaming (M4)."""


class SoftLimitError(StreamingError):
    """A program would move outside the machine's soft limits (§8.2).

    Raised by the file sender's host-side pre-flight (``viz.analyze``) before
    any line is sent. First raised in M9.
    """


class BufferOverflowError(StreamingError):
    """The streamer was about to exceed the device RX buffer.

    SAFETY INVARIANT (CLAUDE.md §8.4): the streamer never sends a line that
    would exceed the known RX buffer size — no "probably fine" margins. This
    should be impossible by construction; if raised, the streamer has a bug.
    First raised in M4.
    """


# --- commands ---------------------------------------------------------------
class CommandRejectedError(CncError):
    """The device answered a command line with ``error:N`` (§5.3).

    Carries the grbl error ``code``. First raised in M5 when an individual
    command (home, jog, settings write, ...) is rejected.
    """

    def __init__(self, code: int, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or f"command rejected with error:{code}")


# --- g-code / simulation ----------------------------------------------------
class UnsupportedGcodeError(CncError):
    """The toolpath simulator met a construct it does not model (M10).

    Raised rather than silently producing a wrong bounding box, which would
    defeat the soft-limit pre-flight (§8.2). E.g. arcs in the G18/G19 planes or
    the R radius form, which are not yet supported.
    """


# --- configuration ----------------------------------------------------------
class ConfigError(CncError):
    """The machine config (``config/machine.toml``) is malformed, invalid, or
    not commissioned (e.g. placeholder zero steps/mm). First raised in M8."""


class CalibrationError(CncError):
    """A calibration input or measurement was invalid (M11)."""


# --- timing -----------------------------------------------------------------
class CommandTimeoutError(CncError):
    """A command did not receive its expected response within the deadline."""


__all__ = [
    "AlarmError",
    "BufferOverflowError",
    "CalibrationError",
    "CncError",
    "CommandRejectedError",
    "CommandTimeoutError",
    "ConfigError",
    "ConnectionLostError",
    "IllegalTransitionError",
    "MachineNotReadyError",
    "MachineStateError",
    "NotConnectedError",
    "ParseError",
    "ProtocolError",
    "SettingsError",
    "SettingsMismatchError",
    "SoftLimitError",
    "StreamingError",
    "TransportError",
    "UnexpectedResponseError",
    "UnsupportedGcodeError",
]
