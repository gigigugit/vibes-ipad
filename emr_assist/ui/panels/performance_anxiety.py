"""Performance Anxiety panel for the EMR Assist Dashboard."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ...core.config import PA_SITUATION_OPTIONS, PA_SYMPTOM_OPTIONS
from ..widgets import (
    ActionButton,
    ChipGroup,
    EditableInfoCard,
    MultiLineInfoCard,
    ToggleChip,
)
from ..theme import ACCENT_BLUE, ACCENT_ORANGE, ACCENT_GREEN, ACCENT_RED
from . import _section_label, _separator


class PerformanceAnxietyActions(QWidget):
    """Action buttons for the Performance Anxiety workflow."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(_section_label("Grab"))
        self.btn_grab = ActionButton("Grab PA Data  (F4)", accent="blue")
        layout.addWidget(self.btn_grab)

        layout.addWidget(_separator())

        layout.addWidget(_section_label("Insert Templates"))
        self.btn_initial = ActionButton("Initial Note", accent="green")
        self.btn_followup = ActionButton("Follow-up Note", accent="green")
        layout.addWidget(self.btn_initial)
        layout.addWidget(self.btn_followup)

        layout.addStretch()


class PerformanceAnxietyInfo(QWidget):
    """Grabbed-data display for Performance Anxiety."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # ── Treatment ──
        layout.addWidget(_section_label("Treatment"))
        self.card_medication = EditableInfoCard("Medication")
        layout.addWidget(self.card_medication)

        # ── Vitals ──
        layout.addWidget(_section_label("Vitals"))
        vitals_row = QHBoxLayout()
        vitals_row.setSpacing(6)
        self.card_bp = EditableInfoCard("BP")
        self.card_pulse = EditableInfoCard("Pulse")
        vitals_row.addWidget(self.card_bp)
        vitals_row.addWidget(self.card_pulse)
        layout.addLayout(vitals_row)

        layout.addWidget(_separator())

        # ── Response / Side Effects chips ──
        layout.addWidget(_section_label("Response"))
        resp_row = QHBoxLayout()
        resp_row.setSpacing(6)
        self.chip_good_response = ToggleChip("Good Response", ACCENT_GREEN)
        self.chip_poor_response = ToggleChip("Poor Response", ACCENT_RED)
        self.chip_no_se = ToggleChip("No Side Effects", ACCENT_GREEN)
        self.chip_has_se = ToggleChip("Has Side Effects", ACCENT_RED)
        resp_row.addWidget(self.chip_good_response)
        resp_row.addWidget(self.chip_poor_response)
        layout.addLayout(resp_row)

        se_row = QHBoxLayout()
        se_row.setSpacing(6)
        se_row.addWidget(self.chip_no_se)
        se_row.addWidget(self.chip_has_se)
        layout.addLayout(se_row)

        # Make response exclusive
        self.chip_good_response.toggled_state.connect(
            lambda on: self.chip_poor_response.__setattr__("active", False) if on else None
        )
        self.chip_poor_response.toggled_state.connect(
            lambda on: self.chip_good_response.__setattr__("active", False) if on else None
        )
        self.chip_no_se.toggled_state.connect(
            lambda on: self.chip_has_se.__setattr__("active", False) if on else None
        )
        self.chip_has_se.toggled_state.connect(
            lambda on: self.chip_no_se.__setattr__("active", False) if on else None
        )

        layout.addWidget(_separator())

        # ── Situations ──
        layout.addWidget(_section_label("Situations"))
        self.card_situations = MultiLineInfoCard("Situations", "", max_height=80)
        layout.addWidget(self.card_situations)

        # ── Symptoms ──
        layout.addWidget(_section_label("Symptoms"))
        self.card_symptoms = MultiLineInfoCard("Symptoms", "", max_height=80)
        layout.addWidget(self.card_symptoms)

        layout.addStretch()


def build_actions(parent: Optional[QWidget] = None) -> PerformanceAnxietyActions:
    return PerformanceAnxietyActions(parent)


def build_info(parent: Optional[QWidget] = None) -> PerformanceAnxietyInfo:
    return PerformanceAnxietyInfo(parent)
