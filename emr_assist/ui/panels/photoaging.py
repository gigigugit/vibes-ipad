"""Photoaging panel for the EMR Assist Dashboard."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ..widgets import ActionButton, ChipGroup, EditableInfoCard
from ..theme import ACCENT_PURPLE
from . import _section_label, _separator


PHOTOAGING_EXAM_OPTIONS = [
    "Fine lines",
    "Wrinkles",
    "Crow's feet",
    "Pigmentation",
    "Age spots",
    "Melasma",
    "Texture changes",
    "Enlarged pores",
    "Loss of elasticity",
    "Inflammation",
    "Acne scarring",
    "Normal",
]


class PhotoagingActions(QWidget):
    """Action buttons for the Photoaging workflow."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(_section_label("Grab"))
        self.btn_grab = ActionButton("Grab Photoaging Data  (F4)", accent="blue")
        layout.addWidget(self.btn_grab)

        layout.addWidget(_separator())

        layout.addWidget(_section_label("Insert Templates"))
        self.btn_note = ActionButton("Insert Note", accent="green")
        layout.addWidget(self.btn_note)

        layout.addStretch()


class PhotoagingInfo(QWidget):
    """Grabbed-data display for Photoaging."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(_section_label("Treatment"))
        self.card_medication = EditableInfoCard("Medication")
        self.card_goals = EditableInfoCard("Skin Goals")
        self.card_retinoid = EditableInfoCard("Retinoid History")
        layout.addWidget(self.card_medication)
        layout.addWidget(self.card_goals)
        layout.addWidget(self.card_retinoid)

        layout.addWidget(_separator())

        layout.addWidget(_section_label("Exam Findings"))
        self.exam_chips = ChipGroup("", PHOTOAGING_EXAM_OPTIONS, color_active=ACCENT_PURPLE)
        layout.addWidget(self.exam_chips)

        layout.addStretch()

    def get_exam_text(self) -> str:
        """Build exam text from selected findings."""
        selected = self.exam_chips.get_selected()
        if not selected:
            return "Skin exam: unremarkable"
        return "Skin exam shows: " + ", ".join(s.lower() for s in selected)


def build_actions(parent: Optional[QWidget] = None) -> PhotoagingActions:
    return PhotoagingActions(parent)


def build_info(parent: Optional[QWidget] = None) -> PhotoagingInfo:
    return PhotoagingInfo(parent)
