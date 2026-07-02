"""M0 smoke test: the package imports and pytest can collect tests on both OSes."""

from __future__ import annotations

import cncctl


def test_package_imports() -> None:
    assert cncctl.__version__ == "0.0.0"
