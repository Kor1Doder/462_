"""Touch-friendly numeric entry: tap a numeric field, get an on-screen numpad.

The Raspberry Pi appliance has a touch screen and no keyboard, so numeric fields
can't be typed into. ``attach_numpad`` makes a ``QLineEdit`` (or a spin box, via
``attach_numpad_spin``) open a big-button :class:`NumpadDialog` when tapped, and
writes the entered value back. Single source of truth so both ``gui.py`` and
``workpiece_view.py`` get the same keypad.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class NumpadDialog(QDialog):
    """A large-button numeric keypad sized for finger input."""

    def __init__(
        self,
        initial: str = "",
        *,
        title: str = "Enter value",
        allow_negative: bool = True,
        allow_decimal: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._text = initial.strip()
        self._allow_decimal = allow_decimal
        self._allow_negative = allow_negative

        root = QVBoxLayout(self)
        self._display = QLineEdit(self._text)
        self._display.setReadOnly(True)
        self._display.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._display.setFont(QFont("Consolas", 26))
        self._display.setMinimumHeight(56)
        root.addWidget(self._display)

        grid = QGridLayout()
        grid.setSpacing(6)
        root.addLayout(grid)
        keys = (
            ("7", 0, 0), ("8", 0, 1), ("9", 0, 2),
            ("4", 1, 0), ("5", 1, 1), ("6", 1, 2),
            ("1", 2, 0), ("2", 2, 1), ("3", 2, 2),
            ("0", 3, 1),
        )
        for label, r, c in keys:
            grid.addWidget(self._key(label, lambda _=False, d=label: self._append(d)), r, c)
        dot = self._key(".", lambda: self._append("."))
        dot.setEnabled(allow_decimal)
        grid.addWidget(dot, 3, 2)
        sign = self._key("+/-", self._toggle_sign)
        sign.setEnabled(allow_negative)
        grid.addWidget(sign, 3, 0)

        # Right-hand column: backspace, clear, then OK / Cancel.
        grid.addWidget(self._key("⌫", self._backspace), 0, 3)   # erase to the left
        grid.addWidget(self._key("C", self._clear), 1, 3)
        ok = self._key("OK", self.accept, kind="start")
        grid.addWidget(ok, 2, 3)
        grid.addWidget(self._key("Cancel", self.reject, kind="danger"), 3, 3)

    def _key(self, text: str, slot: object, *, kind: str = "") -> QPushButton:
        btn = QPushButton(text)
        btn.setMinimumSize(72, 56)
        btn.setFont(QFont("Consolas", 18, QFont.Weight.Bold))
        if kind:
            btn.setObjectName(kind)
        btn.clicked.connect(slot)  # type: ignore[arg-type]
        return btn

    # -- editing -------------------------------------------------------------
    def _append(self, digit: str) -> None:
        if digit == "." and (not self._allow_decimal or "." in self._text):
            return
        if digit == "." and self._text in ("", "-"):
            self._text += "0"
        self._text += digit
        self._sync()

    def _toggle_sign(self) -> None:
        if not self._allow_negative:
            return
        self._text = self._text[1:] if self._text.startswith("-") else "-" + self._text
        self._sync()

    def _backspace(self) -> None:
        self._text = self._text[:-1]
        self._sync()

    def _clear(self) -> None:
        self._text = ""
        self._sync()

    def _sync(self) -> None:
        self._display.setText(self._text)

    def value(self) -> str:
        return self._text

    @staticmethod
    def get_value(
        parent: QWidget | None,
        initial: str = "",
        *,
        title: str = "Enter value",
        allow_negative: bool = True,
        allow_decimal: bool = True,
    ) -> str | None:
        """Show the pad; return the entered text, or ``None`` if cancelled."""
        dlg = NumpadDialog(
            initial,
            title=title,
            allow_negative=allow_negative,
            allow_decimal=allow_decimal,
            parent=parent,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.value()
        return None


class KeyboardDialog(QDialog):
    """A finger-sized on-screen QWERTY keyboard for text fields."""

    _ROWS = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm")
    _SYMBOLS = "/.:-_=+()#%* "

    def __init__(
        self, initial: str = "", *, title: str = "Enter text", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._text = initial
        self._shift = False
        self._letters: list[tuple[QPushButton, str]] = []

        root = QVBoxLayout(self)
        self._display = QLineEdit(self._text)
        self._display.setReadOnly(True)
        self._display.setFont(QFont("Consolas", 22))
        self._display.setMinimumHeight(48)
        root.addWidget(self._display)

        for row in self._ROWS:
            line = QHBoxLayout()
            line.setSpacing(4)
            root.addLayout(line)
            for ch in row:
                btn = self._key(ch, lambda _=False, c=ch: self._type(c))
                if ch.isalpha():
                    self._letters.append((btn, ch))
                line.addWidget(btn)

        syms = QHBoxLayout()
        syms.setSpacing(4)
        root.addLayout(syms)
        for ch in self._SYMBOLS:
            syms.addWidget(self._key("space" if ch == " " else ch, lambda _=False, c=ch: self._type(c)))

        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        root.addLayout(bottom)
        bottom.addWidget(self._key("Shift", self._toggle_shift))
        bottom.addWidget(self._key("⌫", self._backspace))
        bottom.addWidget(self._key("Clear", self._clear))
        bottom.addWidget(self._key("Cancel", self.reject, kind="danger"))
        bottom.addWidget(self._key("OK", self.accept, kind="start"))

    def _key(self, text: str, slot: object, *, kind: str = "") -> QPushButton:
        btn = QPushButton(text)
        btn.setMinimumSize(46, 46)
        btn.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        if kind:
            btn.setObjectName(kind)
        btn.clicked.connect(slot)  # type: ignore[arg-type]
        return btn

    def _type(self, ch: str) -> None:
        self._text += ch.upper() if (self._shift and ch.isalpha()) else ch
        self._display.setText(self._text)

    def _toggle_shift(self) -> None:
        self._shift = not self._shift
        for btn, ch in self._letters:
            btn.setText(ch.upper() if self._shift else ch)

    def _backspace(self) -> None:
        self._text = self._text[:-1]
        self._display.setText(self._text)

    def _clear(self) -> None:
        self._text = ""
        self._display.setText(self._text)

    def value(self) -> str:
        return self._text

    @staticmethod
    def get_value(parent: QWidget | None, initial: str = "", *, title: str = "Enter text") -> str | None:
        """Show the keyboard; return the entered text, or ``None`` if cancelled."""
        dlg = KeyboardDialog(initial, title=title, parent=parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.value()
        return None


class _LineEditNumpad(QObject):
    """Event filter: open the numpad when a numeric line edit is tapped."""

    def __init__(self, edit: QLineEdit, allow_negative: bool, allow_decimal: bool, title: str):
        super().__init__(edit)  # parented to the edit, so it lives as long as it
        self._edit = edit
        self._neg = allow_negative
        self._dec = allow_decimal
        self._title = title

    def eventFilter(self, _obj: QObject, ev: QEvent) -> bool:  # noqa: N802 (Qt override)
        if (
            ev.type() == QEvent.Type.MouseButtonRelease
            and self._edit.isEnabled()
            and not self._edit.isReadOnly()
        ):
            result = NumpadDialog.get_value(
                self._edit.window(),
                self._edit.text(),
                title=self._title,
                allow_negative=self._neg,
                allow_decimal=self._dec,
            )
            if result is not None:
                self._edit.setText(result)
                self._edit.editingFinished.emit()
            return True
        return False


class _SpinNumpad(QObject):
    """Event filter: open the numpad when a spin box's field is tapped."""

    def __init__(self, spin: QAbstractSpinBox, title: str):
        super().__init__(spin)
        self._spin = spin
        self._title = title

    def eventFilter(self, _obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        if ev.type() == QEvent.Type.MouseButtonRelease and self._spin.isEnabled():
            decimal = isinstance(self._spin, QDoubleSpinBox)
            negative = self._spin.minimum() < 0
            current = self._spin.text().strip()
            result = NumpadDialog.get_value(
                self._spin.window(),
                current,
                title=self._title,
                allow_negative=negative,
                allow_decimal=decimal,
            )
            if result:
                try:
                    self._spin.setValue(float(result))
                except ValueError:
                    pass
            return True
        return False


def attach_numpad(
    edit: QLineEdit,
    *,
    allow_negative: bool = False,
    allow_decimal: bool = True,
    title: str = "Enter value",
) -> None:
    """Make tapping ``edit`` open the on-screen numpad."""
    edit.installEventFilter(_LineEditNumpad(edit, allow_negative, allow_decimal, title))


def attach_numpad_spin(spin: QAbstractSpinBox, *, title: str = "Enter value") -> None:
    """Make tapping a spin box's text field open the on-screen numpad.

    The up/down arrows keep working — only the text area opens the pad.
    """
    line = spin.lineEdit()
    if line is not None:
        line.installEventFilter(_SpinNumpad(spin, title))


class _LineEditKeyboard(QObject):
    """Event filter: open the QWERTY keyboard when a text line edit is tapped."""

    def __init__(self, edit: QLineEdit, title: str):
        super().__init__(edit)
        self._edit = edit
        self._title = title

    def eventFilter(self, _obj: QObject, ev: QEvent) -> bool:  # noqa: N802 (Qt override)
        if (
            ev.type() == QEvent.Type.MouseButtonRelease
            and self._edit.isEnabled()
            and not self._edit.isReadOnly()
        ):
            result = KeyboardDialog.get_value(self._edit.window(), self._edit.text(), title=self._title)
            if result is not None:
                self._edit.setText(result)
                self._edit.editingFinished.emit()
            return True
        return False


def attach_keyboard(edit: QLineEdit, *, title: str = "Enter text") -> None:
    """Make tapping ``edit`` open the on-screen QWERTY keyboard."""
    edit.installEventFilter(_LineEditKeyboard(edit, title))


class TapOverlay(QWidget):
    """A click-through overlay that draws a brief ripple where a window is
    tapped — visual feedback for a touch panel whose cursor is hidden.

    One of these is parented to each top-level window that gets tapped (the main
    window or a numpad/keyboard dialog), so the ripple appears at the true tap
    location *on that window*, on top of its content.
    """

    _STEP = 0.06          # progress per ~16 ms tick (~0.45 s total)
    _MAX_GROWTH = 38      # extra radius (px) a ripple expands by

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._ripples: list[list[float]] = []  # [x, y, progress]
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._advance)

    def tap(self, point: QPoint) -> None:
        self._ripples.append([float(point.x()), float(point.y()), 0.0])
        self.raise_()
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def _advance(self) -> None:
        self._ripples = [[x, y, t + self._STEP] for x, y, t in self._ripples if t + self._STEP < 1.0]
        if not self._ripples:
            self._timer.stop()
        self.update()

    def paintEvent(self, _event: object) -> None:  # noqa: N802 (Qt override)
        if not self._ripples:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for x, y, t in self._ripples:
            radius = int(8 + t * self._MAX_GROWTH)
            alpha = int((1.0 - t) * 200)
            pen = QPen(QColor(96, 165, 250, alpha))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(QColor(96, 165, 250, int(alpha * 0.25)))
            painter.drawEllipse(QPoint(int(x), int(y)), radius, radius)


class TapFeedback(QObject):
    """App-wide tap ripples. Install once on the QApplication; on every press it
    draws a ripple on the top-level window that received it — main window OR a
    modal dialog — at the correct location in that window's coordinates.

    The overlay is found/created as a child of the tapped window via
    ``findChild``, so its lifetime follows the window (a dialog's overlay dies
    with the dialog) and we never map a dialog tap into the wrong window.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget):
            window = obj.window()
            if window is not None:
                # Direct children only, so a parent window can't grab a child
                # dialog's overlay (which would re-introduce the wrong-window bug).
                overlay = window.findChild(
                    TapOverlay, options=Qt.FindChildOption.FindDirectChildrenOnly
                )
                if overlay is None:
                    overlay = TapOverlay(window)
                    overlay.show()
                overlay.setGeometry(window.rect())
                global_point = event.globalPosition().toPoint()  # type: ignore[attr-defined]
                overlay.tap(window.mapFromGlobal(global_point))
        return False  # never consume — the tap still reaches the widget

    @staticmethod
    def install(app: QApplication) -> TapFeedback:
        """Create a TapFeedback owned by ``app`` and start filtering its events."""
        feedback = TapFeedback(app)
        app.installEventFilter(feedback)
        return feedback
