"""Custom styled widgets for the EMR Assist Dashboard.

Replaces every default Qt widget appearance with polished, dark-mode-native
alternatives: toggle chips, status badges, action buttons, info cards, and
collapsible sections.
"""

from __future__ import annotations

from typing import Optional, List

from PyQt6.QtCore import Qt, pyqtSignal, QSize, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor, QPainter, QPen, QFont, QIcon
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QGraphicsOpacityEffect,
    QLineEdit,
    QPlainTextEdit,
)

from .theme import (
    ACCENT_BLUE,
    ACCENT_GREEN,
    ACCENT_RED,
    ACCENT_YELLOW,
    ACCENT_ORANGE,
    ACCENT_PURPLE,
    BG_CARD,
    BG_DARK,
    BG_HOVER,
    BG_INPUT,
    BG_PANEL,
    BORDER_SUBTLE,
    TEXT_DARK,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


# ═══════════════════════════════════════════════════════════════════════════
# ToggleChip — replaces checkboxes with pill-shaped toggles
# ═══════════════════════════════════════════════════════════════════════════

class ToggleChip(QPushButton):
    """Compact pill-shaped toggle replacing ugly default checkboxes.

    Parameters
    ----------
    label : str
        Display text.
    color_active : str
        Hex colour when active (default: accent blue).
    parent : QWidget | None
        Parent widget.
    """

    toggled_state = pyqtSignal(bool)

    def __init__(
        self,
        label: str = "",
        color_active: str = ACCENT_BLUE,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(label, parent)
        self._active = False
        self._color_active = color_active
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setCheckable(True)
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._apply_style()
        self.clicked.connect(self._on_click)

    @property
    def active(self) -> bool:
        return self._active

    @active.setter
    def active(self, value: bool) -> None:
        self._active = value
        self.setChecked(value)
        self._apply_style()

    def _on_click(self) -> None:
        self._active = self.isChecked()
        self._apply_style()
        self.toggled_state.emit(self._active)

    def _apply_style(self) -> None:
        if self._active:
            self.setStyleSheet(f"""
                QPushButton {{
                    background-color: {self._color_active};
                    color: {TEXT_DARK};
                    border: none;
                    border-radius: 14px;
                    padding: 4px 14px;
                    font-size: 12px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: {self._color_active};
                    opacity: 0.85;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QPushButton {{
                    background-color: {BG_INPUT};
                    color: {TEXT_SECONDARY};
                    border: 1px solid {BORDER_SUBTLE};
                    border-radius: 14px;
                    padding: 4px 14px;
                    font-size: 12px;
                    font-weight: normal;
                }}
                QPushButton:hover {{
                    background-color: {BG_HOVER};
                    color: {TEXT_PRIMARY};
                    border-color: {self._color_active};
                }}
            """)


# ═══════════════════════════════════════════════════════════════════════════
# StatusBadge — coloured indicator dot + label
# ═══════════════════════════════════════════════════════════════════════════

class StatusBadge(QWidget):
    """Small coloured dot + text label for status indicators.

    Parameters
    ----------
    text : str
        Label text.
    color : str
        Dot colour hex.
    parent : QWidget | None
        Parent widget.
    """

    def __init__(
        self,
        text: str = "",
        color: str = TEXT_MUTED,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._label = QLabel(text)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout.addWidget(self._dot)
        layout.addWidget(self._label)

        self.set_color(color)

    def set_color(self, color: str) -> None:
        self._dot.setStyleSheet(f"color: {color}; font-size: 10px; background: transparent;")

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def set_status(self, text: str, color: str) -> None:
        self.set_text(text)
        self.set_color(color)


# ═══════════════════════════════════════════════════════════════════════════
# ActionButton — styled pill button with accent colour variants
# ═══════════════════════════════════════════════════════════════════════════

class ActionButton(QPushButton):
    """Styled action button with accent colour and optional icon.

    Parameters
    ----------
    label : str
        Button text.
    accent : str
        One of 'blue', 'green', 'red', 'default'.
    parent : QWidget | None
        Parent widget.
    """

    _ACCENT_MAP = {
        "blue": (ACCENT_BLUE, "#29b6f6"),
        "green": (ACCENT_GREEN, "#43a047"),
        "red": (ACCENT_RED, "#e53935"),
        "orange": (ACCENT_ORANGE, "#fb8c00"),
        "purple": (ACCENT_PURPLE, "#9c27b0"),
        "yellow": (ACCENT_YELLOW, "#fdd835"),
    }

    def __init__(
        self,
        label: str = "",
        accent: str = "default",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(label, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(32)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._set_accent(accent)

    def _set_accent(self, accent: str) -> None:
        if accent in self._ACCENT_MAP:
            bg, hover = self._ACCENT_MAP[accent]
            self.setStyleSheet(f"""
                QPushButton {{
                    background-color: {bg};
                    color: {TEXT_DARK};
                    border: none;
                    border-radius: 6px;
                    padding: 7px 16px;
                    font-size: 12px;
                    font-weight: bold;
                    min-height: 28px;
                }}
                QPushButton:hover {{
                    background-color: {hover};
                }}
                QPushButton:pressed {{
                    background-color: {bg};
                    padding-top: 8px;
                }}
                QPushButton:disabled {{
                    background-color: {BG_DARK};
                    color: {TEXT_MUTED};
                }}
            """)
        # else: uses global QPushButton style from theme.py


# ═══════════════════════════════════════════════════════════════════════════
# InfoCard — read-only key/value display card
# ═══════════════════════════════════════════════════════════════════════════

class InfoCard(QFrame):
    """Compact read-only display for a key/value pair.

    Parameters
    ----------
    key : str
        Field label (e.g. "Medication").
    value : str
        Display value (e.g. "tadalafil 10 mg").
    parent : QWidget | None
        Parent widget.
    """

    def __init__(
        self,
        key: str = "",
        value: str = "—",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {BG_CARD};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 8px;
                padding: 0;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        self._key_label = QLabel(key)
        self._key_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 10px; font-weight: bold; "
            f"text-transform: uppercase; letter-spacing: 1px; background: transparent; border: none;"
        )

        self._value_label = QLabel(value)
        self._value_label.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: bold; background: transparent; border: none;"
        )
        self._value_label.setWordWrap(True)

        layout.addWidget(self._key_label)
        layout.addWidget(self._value_label)

    def set_value(self, value: str) -> None:
        self._value_label.setText(value or "—")

    def value(self) -> str:
        return self._value_label.text()


# ═══════════════════════════════════════════════════════════════════════════
# EditableInfoCard — key/value card with inline editing
# ═══════════════════════════════════════════════════════════════════════════

class EditableInfoCard(QFrame):
    """Key/value card with an editable text field.

    Parameters
    ----------
    key : str
        Field label.
    value : str
        Initial value.
    parent : QWidget | None
        Parent.
    """

    value_changed = pyqtSignal(str)

    def __init__(
        self,
        key: str = "",
        value: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {BG_CARD};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 8px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        self._key_label = QLabel(key)
        self._key_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 10px; font-weight: bold; "
            f"text-transform: uppercase; letter-spacing: 1px; background: transparent; border: none;"
        )

        self._edit = QLineEdit(value)
        self._edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {BG_INPUT};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border-color: {ACCENT_BLUE};
            }}
        """)
        self._edit.textChanged.connect(self.value_changed.emit)

        layout.addWidget(self._key_label)
        layout.addWidget(self._edit)

    def set_value(self, value: str) -> None:
        self._edit.setText(value or "")

    def value(self) -> str:
        return self._edit.text()


# ═══════════════════════════════════════════════════════════════════════════
# MultiLineInfoCard — key + multi-line text area
# ═══════════════════════════════════════════════════════════════════════════

class MultiLineInfoCard(QFrame):
    """Key/value card with a multi-line text area.

    Parameters
    ----------
    key : str
        Field label.
    value : str
        Initial value.
    max_height : int
        Maximum pixel height for the text area.
    read_only : bool
        If True, text area is not editable.
    parent : QWidget | None
        Parent.
    """

    value_changed = pyqtSignal(str)

    def __init__(
        self,
        key: str = "",
        value: str = "",
        max_height: int = 120,
        read_only: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {BG_CARD};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 8px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        self._key_label = QLabel(key)
        self._key_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 10px; font-weight: bold; "
            f"text-transform: uppercase; letter-spacing: 1px; background: transparent; border: none;"
        )

        self._text = QPlainTextEdit(value)
        self._text.setReadOnly(read_only)
        self._text.setMaximumHeight(max_height)
        self._text.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {BG_INPUT};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 4px;
                padding: 4px 6px;
                font-size: 12px;
            }}
            QPlainTextEdit:focus {{
                border-color: {ACCENT_BLUE};
            }}
        """)
        self._text.textChanged.connect(lambda: self.value_changed.emit(self._text.toPlainText()))

        layout.addWidget(self._key_label)
        layout.addWidget(self._text)

    def set_value(self, value: str) -> None:
        self._text.setPlainText(value or "")

    def value(self) -> str:
        return self._text.toPlainText()


# ═══════════════════════════════════════════════════════════════════════════
# CollapsibleSection — expandable header with show/hide content
# ═══════════════════════════════════════════════════════════════════════════

class CollapsibleSection(QWidget):
    """Collapsible section with animated expand/collapse.

    Parameters
    ----------
    title : str
        Section header text.
    expanded : bool
        Whether initially expanded.
    parent : QWidget | None
        Parent widget.
    """

    def __init__(
        self,
        title: str = "",
        expanded: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._expanded = expanded

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        # Header button
        self._header = QPushButton(f"{'▼' if expanded else '▶'}  {title}")
        self._header.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG_PANEL};
                color: {TEXT_SECONDARY};
                border: none;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 12px;
                font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {BG_HOVER};
                color: {TEXT_PRIMARY};
            }}
        """)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.clicked.connect(self.toggle)
        self._title = title

        # Content container
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(4)
        self._content.setVisible(expanded)

        self._main_layout.addWidget(self._header)
        self._main_layout.addWidget(self._content)

    def content_layout(self) -> QVBoxLayout:
        """Return the layout to add child widgets to."""
        return self._content_layout

    def add_widget(self, widget: QWidget) -> None:
        self._content_layout.addWidget(widget)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f"{arrow}  {self._title}")

    def set_expanded(self, expanded: bool) -> None:
        if expanded != self._expanded:
            self.toggle()


# ═══════════════════════════════════════════════════════════════════════════
# LabValueRow — specialised row for a single lab with status indicator
# ═══════════════════════════════════════════════════════════════════════════

class LabValueRow(QFrame):
    """Displays one lab value with a colour-coded status indicator.

    Parameters
    ----------
    lab_name : str
        Display name (e.g. "Total Testosterone").
    unit : str
        Unit string (e.g. "ng/dL").
    normal_range : str
        Normal range text (e.g. "264-916").
    parent : QWidget | None
        Parent widget.
    """

    status_changed = pyqtSignal(str, str)  # lab_name, "normal"/"low"/"high"

    def __init__(
        self,
        lab_name: str = "",
        unit: str = "",
        normal_range: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._lab_name = lab_name
        self._unit = unit
        self._normal_range = normal_range
        self._status = "unknown"  # "normal", "low", "high", "unknown"

        self.setStyleSheet(f"""
            QFrame {{
                background-color: {BG_CARD};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 6px;
                padding: 0;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)

        # Status dot
        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dot.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent; border: none;")

        # Lab name
        self._name_label = QLabel(lab_name)
        self._name_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 11px; background: transparent; border: none;"
        )
        self._name_label.setFixedWidth(110)

        # Value display
        self._value_label = QLabel("—")
        self._value_label.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: bold; background: transparent; border: none;"
        )
        self._value_label.setMinimumWidth(60)

        # Unit + range
        range_text = f"{unit}" + (f"  ({normal_range})" if normal_range else "")
        self._range_label = QLabel(range_text)
        self._range_label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 10px; background: transparent; border: none;"
        )

        # Status toggle chips
        self._status_chips = QWidget()
        self._status_chips.setStyleSheet("background: transparent; border: none;")
        chips_layout = QHBoxLayout(self._status_chips)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(2)

        self._chip_normal = self._make_chip("N", ACCENT_GREEN)
        self._chip_low = self._make_chip("L", ACCENT_YELLOW)
        self._chip_high = self._make_chip("H", ACCENT_RED)

        chips_layout.addWidget(self._chip_normal)
        chips_layout.addWidget(self._chip_low)
        chips_layout.addWidget(self._chip_high)

        layout.addWidget(self._dot)
        layout.addWidget(self._name_label)
        layout.addWidget(self._value_label, 1)
        layout.addWidget(self._range_label)
        layout.addWidget(self._status_chips)

    def _make_chip(self, text: str, color: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedSize(24, 22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setCheckable(True)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG_INPUT};
                color: {TEXT_MUTED};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 4px;
                font-size: 10px;
                font-weight: bold;
                padding: 0;
            }}
            QPushButton:hover {{
                border-color: {color};
                color: {color};
            }}
            QPushButton:checked {{
                background-color: {color};
                color: {TEXT_DARK};
                border-color: {color};
            }}
        """)
        btn.clicked.connect(lambda: self._on_chip_click(text, color, btn))
        return btn

    def _on_chip_click(self, text: str, color: str, btn: QPushButton) -> None:
        # Exclusive toggle
        for chip in (self._chip_normal, self._chip_low, self._chip_high):
            if chip is not btn:
                chip.setChecked(False)

        if btn.isChecked():
            status_map = {"N": "normal", "L": "low", "H": "high"}
            self._status = status_map.get(text, "unknown")
            self._dot.setStyleSheet(f"color: {color}; font-size: 10px; background: transparent; border: none;")
        else:
            self._status = "unknown"
            self._dot.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent; border: none;")

        self.status_changed.emit(self._lab_name, self._status)

    def set_value(self, value: str) -> None:
        self._value_label.setText(value or "—")

    def get_value(self) -> str:
        return self._value_label.text()

    def get_status(self) -> str:
        return self._status

    def set_status(self, status: str) -> None:
        """Programmatically set status: 'normal', 'low', 'high', or 'unknown'."""
        chips = {"normal": self._chip_normal, "low": self._chip_low, "high": self._chip_high}
        colors = {"normal": ACCENT_GREEN, "low": ACCENT_YELLOW, "high": ACCENT_RED}
        for chip in chips.values():
            chip.setChecked(False)
        if status in chips:
            chips[status].setChecked(True)
            self._dot.setStyleSheet(
                f"color: {colors[status]}; font-size: 10px; background: transparent; border: none;"
            )
        else:
            self._dot.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 10px; background: transparent; border: none;"
            )
        self._status = status


# ═══════════════════════════════════════════════════════════════════════════
# ChipGroup — horizontal wrap of ToggleChips for multi-select
# ═══════════════════════════════════════════════════════════════════════════

class ChipGroup(QFrame):
    """A flow-layout-like group of ToggleChip widgets.

    Parameters
    ----------
    label : str
        Group label.
    items : list[str]
        Chip labels.
    color_active : str
        Active colour for all chips.
    parent : QWidget | None
        Parent widget.
    """

    selection_changed = pyqtSignal(list)  # list of selected labels

    def __init__(
        self,
        label: str = "",
        items: Optional[List[str]] = None,
        color_active: str = ACCENT_BLUE,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        if items is None:
            items = []
        self.setStyleSheet(f"background: transparent; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        if label:
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {TEXT_SECONDARY}; font-size: 10px; font-weight: bold; "
                f"text-transform: uppercase; letter-spacing: 1px;"
            )
            layout.addWidget(lbl)

        from PyQt6.QtWidgets import QGridLayout
        self._grid = QGridLayout()
        self._grid.setSpacing(4)
        layout.addLayout(self._grid)

        self._chips: List[ToggleChip] = []
        cols = 3
        for i, item in enumerate(items):
            chip = ToggleChip(item, color_active)
            chip.toggled_state.connect(lambda _: self._emit_selection())
            self._chips.append(chip)
            self._grid.addWidget(chip, i // cols, i % cols)

    def _emit_selection(self) -> None:
        selected = [c.text() for c in self._chips if c.active]
        self.selection_changed.emit(selected)

    def get_selected(self) -> List[str]:
        return [c.text() for c in self._chips if c.active]

    def set_selected(self, labels: List[str]) -> None:
        for chip in self._chips:
            chip.active = chip.text() in labels

    def clear(self) -> None:
        for chip in self._chips:
            chip.active = False
