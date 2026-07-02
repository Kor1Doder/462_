"""config_io tests (M8): loading, validation, settings mapping, commissioning."""

from __future__ import annotations

from pathlib import Path

import pytest

from cncctl.config_io import (
    default_port,
    load_config,
    require_commissioned,
    save_config,
    settings_from_config,
)
from cncctl.controller.errors import ConfigError

_COMMITTED_CONFIG = Path("config/machine.toml")

_COMMISSIONED = """
[machine]
name = "test mill"
[transport]
default_port_windows = "COM7"
default_port_linux = "/dev/ttyACM0"
rx_buffer_bytes = 128
[axes.x]
microsteps = 8
lead_screw_mm = 4.0
steps_per_mm = 320.0
max_rate_mm_min = 3000.0
acceleration_mm_s2 = 100.0
soft_limit_mm = 200.0
[axes.y]
microsteps = 8
lead_screw_mm = 4.0
steps_per_mm = 320.0
max_rate_mm_min = 3000.0
acceleration_mm_s2 = 100.0
soft_limit_mm = 200.0
[axes.z]
microsteps = 8
lead_screw_mm = 2.0
steps_per_mm = 640.0
max_rate_mm_min = 1500.0
acceleration_mm_s2 = 80.0
soft_limit_mm = 100.0
[motion]
junction_deviation_mm = 0.01
[homing]
enabled = true
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "machine.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_committed_placeholder_config() -> None:
    config = load_config(_COMMITTED_CONFIG)
    assert config.machine.name
    assert config.transport.rx_buffer_bytes == 128


def test_loads_commissioned_config(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _COMMISSIONED))
    assert config.axes.x.steps_per_mm == 320.0
    assert config.homing.enabled is True


def test_homing_defaults_when_section_omitted(tmp_path: Path) -> None:
    text = _COMMISSIONED.replace("[homing]\nenabled = true\n", "")
    assert load_config(_write(tmp_path, text)).homing.enabled is False


def test_malformed_toml_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "not = = valid ["))


def test_missing_required_section_raises(tmp_path: Path) -> None:
    text = _COMMISSIONED.replace("[motion]\njunction_deviation_mm = 0.01\n", "")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.toml")


def test_settings_mapping(tmp_path: Path) -> None:
    settings = settings_from_config(load_config(_write(tmp_path, _COMMISSIONED)))
    assert settings[100] == "320.000"  # X steps/mm
    assert settings[102] == "640.000"  # Z steps/mm
    assert settings[110] == "3000.000"  # X max rate
    assert settings[130] == "200.000"  # X soft limit
    assert settings[11] == "0.010"  # junction deviation
    assert len(settings) == 13


def test_require_commissioned_accepts_real_values(tmp_path: Path) -> None:
    require_commissioned(load_config(_write(tmp_path, _COMMISSIONED)))  # no raise


def test_require_commissioned_rejects_placeholder_zeros() -> None:
    with pytest.raises(ConfigError, match="commissioned"):
        require_commissioned(load_config(_COMMITTED_CONFIG))


def test_default_port_is_one_of_the_configured(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _COMMISSIONED))
    assert default_port(config) in {
        config.transport.default_port_windows,
        config.transport.default_port_linux,
    }


def test_save_config_round_trips(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _COMMISSIONED))
    out = tmp_path / "saved.toml"
    save_config(config, out)
    assert load_config(out) == config
