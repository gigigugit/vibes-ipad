"""Birth Control panel for the EMR Assist Dashboard."""

from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ...core.config import BIRTH_CONTROL_PMH_OPTIONS
from ..widgets import (
    ActionButton,
    ChipGroup,
    EditableInfoCard,
    MultiLineInfoCard,
    ToggleChip,
)
from ..theme import ACCENT_BLUE, ACCENT_GREEN, ACCENT_ORANGE, ACCENT_PURPLE
from . import _section_label, _separator


class BirthControlActions(QWidget):
    """Action buttons for the Birth Control workflow."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(_section_label("Grab"))
        self.btn_grab = ActionButton("Grab BC Data  (F4)", accent="blue")
        layout.addWidget(self.btn_grab)

        layout.addWidget(_separator())

        layout.addWidget(_section_label("Insert Templates"))
        self.btn_initial = ActionButton("Initial Note", accent="green")
        self.btn_followup = ActionButton("Follow-up Note", accent="green")
        layout.addWidget(self.btn_initial)
        layout.addWidget(self.btn_followup)

        layout.addStretch()


class BirthControlInfo(QWidget):
    """Grabbed-data display for Birth Control."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # ── Visit type toggle ──
        layout.addWidget(_section_label("Visit Type"))
        vt_row = QHBoxLayout()
        vt_row.setSpacing(6)
        self.chip_initial = ToggleChip("Initial", ACCENT_BLUE)
        self.chip_followup = ToggleChip("Follow-up", ACCENT_BLUE)
        self.chip_initial.active = True  # default
        vt_row.addWidget(self.chip_initial)
        vt_row.addWidget(self.chip_followup)
        layout.addLayout(vt_row)
        # Make exclusive
        self.chip_initial.toggled_state.connect(
            lambda on: setattr(self.chip_followup, "active", False) if on else None
        )
        self.chip_followup.toggled_state.connect(
            lambda on: setattr(self.chip_initial, "active", False) if on else None
        )

        layout.addWidget(_separator())

        # ── Treatment ──
        layout.addWidget(_section_label("Treatment"))
        self.card_medication = EditableInfoCard("Medication")
        self.card_lmp = EditableInfoCard("LMP")
        layout.addWidget(self.card_medication)
        layout.addWidget(self.card_lmp)

        # ── Vitals ──
        layout.addWidget(_section_label("Vitals"))
        self.card_bp = EditableInfoCard("BP")
        layout.addWidget(self.card_bp)

        layout.addWidget(_separator())

        # ── Side Effects ──
        layout.addWidget(_section_label("Side Effects"))
        self.card_side_effects = MultiLineInfoCard("Side Effects", "", max_height=60)
        layout.addWidget(self.card_side_effects)

        # ── History Changes ──
        layout.addWidget(_section_label("History"))
        self.card_history_changes = EditableInfoCard("History Changes")
        layout.addWidget(self.card_history_changes)

        layout.addWidget(_separator())

        # ── PMH Chips ──
        layout.addWidget(_section_label("Past Medical History"))
        _pmh_labels = [label for _key, label in BIRTH_CONTROL_PMH_OPTIONS]
        self.pmh_chips = ChipGroup("PMH", _pmh_labels, color_active=ACCENT_PURPLE)
        layout.addWidget(self.pmh_chips)

        self.card_pmh_other = EditableInfoCard("PMH Other")
        layout.addWidget(self.card_pmh_other)

        layout.addStretch()

    @property
    def is_initial(self) -> bool:
        return self.chip_initial.active

    def get_pmh_keys(self) -> List[str]:
        """Return list of selected PMH keys."""
        selected: List[str] = []
        sel_labels = self.pmh_chips.get_selected()
        for key, label in BIRTH_CONTROL_PMH_OPTIONS:
            if label in sel_labels:
                selected.append(key)
        return selected


def build_actions(parent: Optional[QWidget] = None) -> BirthControlActions:
    return BirthControlActions(parent)


def build_info(parent: Optional[QWidget] = None) -> BirthControlInfo:
    return BirthControlInfo(parent)
