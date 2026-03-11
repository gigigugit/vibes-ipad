"""Hair Loss panel for the EMR Assist Dashboard."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ..theme import ACCENT_GREEN
from ..widgets import (
    ActionButton,
    ChipGroup,
    EditableInfoCard,
    ToggleChip,
)
from ..theme import ACCENT_ORANGE
from . import _section_label, _separator


HAIR_EXAM_OPTIONS = [
    "Front hairline",
    "Top/crown",
    "Widening of the part",
    "Diffuse thinning",
    "Confluent front to crown",
    "Near front sparing hairline",
]


class HairLossActions(QWidget):
    """Action buttons for the Hair Loss workflow."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(_section_label("Grab"))
        self.btn_grab = ActionButton("Grab Hair Data  (F4)", accent="blue")
        layout.addWidget(self.btn_grab)

        layout.addWidget(_separator())

        layout.addWidget(_section_label("Insert Templates"))
        self.btn_initial = ActionButton("Initial Note", accent="green")
        self.btn_followup = ActionButton("Follow-up Note", accent="green")
        self.btn_limited = ActionButton("Limited Check-in", accent="green")

        for btn in (self.btn_initial, self.btn_followup, self.btn_limited):
            layout.addWidget(btn)

        layout.addStretch()


class HairLossInfo(QWidget):
    """Grabbed-data display for Hair Loss."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(_section_label("Treatment"))
        self.card_medication = EditableInfoCard("Medication")
        self.card_response = EditableInfoCard("Response")
        self.card_symptoms = EditableInfoCard("Symptoms / Location")
        layout.addWidget(self.card_medication)
        layout.addWidget(self.card_response)
        layout.addWidget(self.card_symptoms)

        layout.addWidget(_separator())

        layout.addWidget(_section_label("Exam Findings"))
        self.exam_chips = ChipGroup("", HAIR_EXAM_OPTIONS, color_active=ACCENT_ORANGE)
        layout.addWidget(self.exam_chips)

        layout.addStretch()

    def get_objective_section(self) -> str:
        """Build objective section text from selected exam findings."""
        selected = self.exam_chips.get_selected()
        if not selected:
            return "Hair exam: not assessed"
        return "Hair exam shows: " + ", ".join(s.lower() for s in selected)


def build_actions(parent: Optional[QWidget] = None) -> HairLossActions:
    return HairLossActions(parent)


def build_info(parent: Optional[QWidget] = None) -> HairLossInfo:
    return HairLossInfo(parent)
