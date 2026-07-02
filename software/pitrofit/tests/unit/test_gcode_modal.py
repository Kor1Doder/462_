"""Modal-state tests (M6)."""

from __future__ import annotations

from cncctl.gcode.modal import DistanceMode, ModalState, MotionMode, Plane, Units


def test_defaults_match_grbl_power_up() -> None:
    snap = ModalState().snapshot()
    assert snap.motion is MotionMode.RAPID
    assert snap.units is Units.MM
    assert snap.distance is DistanceMode.ABSOLUTE
    assert snap.plane is Plane.XY
    assert snap.feed is None
    assert snap.spindle is None
    assert snap.wcs == "G54"


def test_motion_modes() -> None:
    modal = ModalState()
    for code, mode in [
        (0, MotionMode.RAPID),
        (1, MotionMode.LINEAR),
        (2, MotionMode.ARC_CW),
        (3, MotionMode.ARC_CCW),
    ]:
        modal.apply("G", code)
        assert modal.motion is mode


def test_units_distance_and_plane() -> None:
    modal = ModalState()
    modal.apply("G", 20)
    assert modal.units is Units.INCH
    modal.apply("G", 21)
    assert modal.units is Units.MM
    modal.apply("G", 91)
    assert modal.distance is DistanceMode.INCREMENTAL
    modal.apply("G", 18)
    assert modal.plane is Plane.ZX


def test_feed_spindle_and_wcs() -> None:
    modal = ModalState()
    modal.apply("F", 300.0)
    modal.apply("S", 1000.0)
    modal.apply("G", 55)
    snap = modal.snapshot()
    assert snap.feed == 300.0
    assert snap.spindle == 1000.0
    assert snap.wcs == "G55"


def test_non_modal_words_are_ignored() -> None:
    modal = ModalState()
    before = modal.snapshot()
    modal.apply("X", 10.0)  # axis word
    modal.apply("M", 3.0)  # M-code (handled by the parser, not modal here)
    modal.apply("T", 2.0)  # tool select
    assert modal.snapshot() == before


def test_snapshot_is_an_immutable_copy() -> None:
    modal = ModalState()
    snap = modal.snapshot()
    modal.apply("G", 1)
    assert snap.motion is MotionMode.RAPID  # earlier snapshot unaffected
    assert modal.snapshot().motion is MotionMode.LINEAR
