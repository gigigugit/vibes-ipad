"""Visit-type panel package for the EMR Assist Dashboard.

Each panel module exports two widget builders:
    - ``build_actions(parent)``  → QWidget with action buttons
    - ``build_info(parent)``     → QWidget with grabbed-data display cards

The dashboard wires these into QStackedWidget containers that swap on visit-type change.
"""

from __future__ import annotations

from typing import Dict, List, Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import BG_DARKEST, BG_PANEL, BORDER_SUBTLE, TEXT_SECONDARY
from ..widgets import ActionButton


def _scrollable(widget: QWidget) -> QScrollArea:
    """Wrap *widget* in a transparent QScrollArea."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(widget)
    scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
    return scroll


def _section_label(text: str) -> QLabel:
    """Small uppercase section heading."""
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {TEXT_SECONDARY}; font-size: 10px; font-weight: bold; "
        f"text-transform: uppercase; letter-spacing: 1px; padding: 4px 0 2px 0;"
    )
    return lbl


def _separator() -> QWidget:
    """Thin horizontal line."""
    line = QWidget()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {BORDER_SUBTLE};")
    return line
