"""Load and validate ``config/machine.toml``.

The file holds the machine's calibration and limits. This module decodes it
into typed structs, derives the grbl settings map to push at boot, and checks
the config is actually *commissioned* (no placeholder zeros) before any of it
reaches the machine.
"""

from __future__ import annotations

from pathlib import Path

import msgspec

from cncctl.controller.errors import ConfigError

# grbl setting numbers, per axis (X, Y, Z) and the scalar junction deviation.
_STEPS_PER_MM = (100, 101, 102)
_MAX_RATE = (110, 111, 112)
_ACCELERATION = (120, 121, 122)
_SOFT_LIMIT = (130, 131, 132)
_JUNCTION_DEVIATION = 11


class AxisConfig(msgspec.Struct, frozen=True):
    """Per-axis calibration. ``microsteps``/``lead_screw_mm`` are recorded for
    auditability; ``steps_per_mm`` is the derived value pushed as ``$100`` etc."""

    microsteps: int
    lead_screw_mm: float
    steps_per_mm: float
    max_rate_mm_min: float
    acceleration_mm_s2: float
    soft_limit_mm: float


class AxesConfig(msgspec.Struct, frozen=True):
    x: AxisConfig
    y: AxisConfig
    z: AxisConfig


class TransportConfig(msgspec.Struct, frozen=True):
    # USB-CDC device the grblHAL Pico enumerates as on the Raspberry Pi.
    default_port: str = "/dev/ttyACM0"
    baudrate: int = 115200
    rx_buffer_bytes: int = 128


class MotionConfig(msgspec.Struct, frozen=True):
    junction_deviation_mm: float


class MachineConfig(msgspec.Struct, frozen=True):
    name: str


class HomingConfig(msgspec.Struct, frozen=True):
    enabled: bool = False


class Config(msgspec.Struct, frozen=True):
    """The whole ``machine.toml`` as typed data."""

    machine: MachineConfig
    transport: TransportConfig
    axes: AxesConfig
    motion: MotionConfig
    homing: HomingConfig = msgspec.field(default_factory=HomingConfig)


def load_config(path: Path) -> Config:
    """Load and structurally validate the machine config.

    Raises:
        ConfigError: the file is missing, malformed TOML, or fails validation.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        return msgspec.toml.decode(raw, type=Config)
    except (msgspec.ValidationError, msgspec.DecodeError) as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc


def save_config(config: Config, path: Path) -> None:
    """Write ``config`` back to ``path`` as ``machine.toml``.

    Hand-rolled (no extra dependency) for our small, fixed schema; the output
    round-trips through :func:`load_config`. Comments in an existing file are not
    preserved.
    """
    axes = config.axes
    lines = [
        "# Machine calibration and limits. Written by cncctl. See the design",
        "",
        "[machine]",
        f"name = {_toml_str(config.machine.name)}",
        "",
        "[transport]",
        f"default_port = {_toml_str(config.transport.default_port)}",
        f"baudrate = {config.transport.baudrate}",
        f"rx_buffer_bytes = {config.transport.rx_buffer_bytes}",
        "",
    ]
    for name, axis in (("x", axes.x), ("y", axes.y), ("z", axes.z)):
        lines += [
            f"[axes.{name}]",
            f"microsteps = {axis.microsteps}",
            f"lead_screw_mm = {_toml_num(axis.lead_screw_mm)}",
            f"steps_per_mm = {_toml_num(axis.steps_per_mm)}",
            f"max_rate_mm_min = {_toml_num(axis.max_rate_mm_min)}",
            f"acceleration_mm_s2 = {_toml_num(axis.acceleration_mm_s2)}",
            f"soft_limit_mm = {_toml_num(axis.soft_limit_mm)}",
            "",
        ]
    lines += [
        "[motion]",
        f"junction_deviation_mm = {_toml_num(config.motion.junction_deviation_mm)}",
        "",
        "[homing]",
        f"enabled = {str(config.homing.enabled).lower()}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_num(value: float) -> str:
    # Keep a decimal point so the value decodes back as a float (the schema type).
    return repr(float(value))


def default_port(config: Config) -> str:
    """Return the configured default serial port.

    The Raspberry Pi enumerates the grblHAL Pico as ``/dev/ttyACM0`` by default;
    override per machine in ``config/machine.toml`` or at the call site.
    """
    return config.transport.default_port


def settings_from_config(config: Config) -> dict[int, str]:
    """Derive the grbl ``$N=value`` map to push at boot."""
    axes = config.axes
    kx, ky, kz = _STEPS_PER_MM
    rx, ry, rz = _MAX_RATE
    ax, ay, az = _ACCELERATION
    sx, sy, sz = _SOFT_LIMIT
    return {
        kx: _fmt(axes.x.steps_per_mm),
        ky: _fmt(axes.y.steps_per_mm),
        kz: _fmt(axes.z.steps_per_mm),
        rx: _fmt(axes.x.max_rate_mm_min),
        ry: _fmt(axes.y.max_rate_mm_min),
        rz: _fmt(axes.z.max_rate_mm_min),
        ax: _fmt(axes.x.acceleration_mm_s2),
        ay: _fmt(axes.y.acceleration_mm_s2),
        az: _fmt(axes.z.acceleration_mm_s2),
        sx: _fmt(axes.x.soft_limit_mm),
        sy: _fmt(axes.y.soft_limit_mm),
        sz: _fmt(axes.z.soft_limit_mm),
        _JUNCTION_DEVIATION: _fmt(config.motion.junction_deviation_mm),
    }


def require_commissioned(config: Config) -> None:
    """Reject an uncommissioned config before any value reaches the machine.

    Placeholder zeros (the committed ``machine.toml`` ships all-zero,) are
    physically meaningless and dangerous to push, so the bootstrap refuses them.

    Raises:
        ConfigError: any axis has a non-positive steps/mm, max rate,
            acceleration, or soft-limit travel.
    """
    problems: list[str] = []
    for name, axis in (("x", config.axes.x), ("y", config.axes.y), ("z", config.axes.z)):
        for field_name, value in (
            ("steps_per_mm", axis.steps_per_mm),
            ("max_rate_mm_min", axis.max_rate_mm_min),
            ("acceleration_mm_s2", axis.acceleration_mm_s2),
            ("soft_limit_mm", axis.soft_limit_mm),
        ):
            if value <= 0:
                problems.append(f"axis {name} {field_name}={value}")
    if problems:
        raise ConfigError(
            "config is not commissioned (non-positive values): " + ", ".join(problems)
        )


def _fmt(value: float) -> str:
    """Format a value as a grbl setting string (3 decimals, as grbl reports)."""
    return f"{value:.3f}"


__all__ = [
    "AxesConfig",
    "AxisConfig",
    "Config",
    "HomingConfig",
    "MachineConfig",
    "MotionConfig",
    "TransportConfig",
    "default_port",
    "load_config",
    "require_commissioned",
    "save_config",
    "settings_from_config",
]
