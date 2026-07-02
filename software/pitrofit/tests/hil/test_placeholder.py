"""Placeholder so the HIL test directory exists. Real tests land alongside M8+.

Per CLAUDE.md §6: HIL tests are gated by ``CNCCTL_HIL=1`` and the first
action of every real test must print a 5-second 'abort now' message before
touching the machine.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.hil


def test_hil_placeholder() -> None:
    if os.environ.get("CNCCTL_HIL") != "1":
        pytest.skip("HIL tests require CNCCTL_HIL=1 and real hardware connected.")
    pytest.skip("Real HIL tests land in M8 (Facade + config) and later.")
