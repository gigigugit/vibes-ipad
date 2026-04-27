"""Sexual Health panel for the EMR Assist Dashboard."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QVBoxLayout, QWidget

from ...core.parsers import allow_hair_loss_diagnosis
from ..widgets import ActionButton, EditableInfoCard, ToggleChip
from ..theme import ACCENT_BLUE, ACCENT_ORANGE, BG_INPUT, BORDER_SUBTLE, TEXT_PRIMARY
from . import _section_label, _separator


class SexualHealthActions(QWidget):
    """Action buttons for the Sexual Health workflow."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(_section_label("Grab"))
        self.btn_grab = ActionButton("Grab Sexual Health  (F4)", accent="blue")
        layout.addWidget(self.btn_grab)

        layout.addWidget(_separator())

        layout.addWidget(_section_label("Insert Templates"))
        self.btn_followup = ActionButton("Follow-up Note", accent="green")
        self.btn_plan = ActionButton("Plan", accent="green")
        self.btn_change = ActionButton("Change Template", accent="green")
        self.btn_change_cadence = ActionButton("Change Cadence", accent="green")
        self.btn_change_number = ActionButton("Change Number", accent="green")
        self.btn_change_med = ActionButton("Change Medication", accent="green")

        for btn in (
            self.btn_followup,
            self.btn_plan,
            self.btn_change,
            self.btn_change_cadence,
            self.btn_change_number,
            self.btn_change_med,
        ):
            layout.addWidget(btn)

        layout.addStretch()


class SexualHealthInfo(QWidget):
    """Grabbed-data display for Sexual Health."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # ── Treatment ──
        layout.addWidget(_section_label("Treatment"))
        self.card_medication = EditableInfoCard("Medication")
        self.card_effectiveness = EditableInfoCard("Effectiveness")
        self.card_bp = EditableInfoCard("Blood Pressure")
        layout.addWidget(self.card_medication)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        row1.addWidget(self.card_effectiveness)
        row1.addWidget(self.card_bp)
        layout.addLayout(row1)

        layout.addWidget(_separator())

        # ── Change plan ──
        layout.addWidget(_section_label("Change / Plan"))
        self.combo_plan = QComboBox()
        self.combo_plan.addItems([
            "Continue present treatment",
            "Change treatment to—",
        ])
        self.combo_plan.setStyleSheet(f"""
            QComboBox {{
                background-color: {BG_INPUT};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_SUBTLE};
                border-radius: 6px;
                padding: 5px 10px;
                min-height: 26px;
            }}
        """)
        layout.addWidget(self.combo_plan)

        self.combo_change = QComboBox()
        self.combo_change.addItems(["Cadence", "Dose number", "Medication"])
        self.combo_change.setStyleSheet(self.combo_plan.styleSheet())
        self.card_change_detail = EditableInfoCard("Change Detail")
        layout.addWidget(self.combo_change)
        layout.addWidget(self.card_change_detail)

        layout.addWidget(_separator())

        # ── Diagnoses ──
        layout.addWidget(_section_label("Diagnoses"))
        dx_row = QHBoxLayout()
        dx_row.setSpacing(4)
        self.chip_dx_ed = ToggleChip("ED", ACCENT_BLUE)
        self.chip_dx_pe = ToggleChip("PE", ACCENT_ORANGE)
        self.chip_dx_pe_like = ToggleChip("PE-like", ACCENT_ORANGE)
        self.chip_dx_hair = ToggleChip("Hair Loss", ACCENT_ORANGE)
        dx_row.addWidget(self.chip_dx_ed)
        dx_row.addWidget(self.chip_dx_pe)
        dx_row.addWidget(self.chip_dx_pe_like)
        dx_row.addWidget(self.chip_dx_hair)
        layout.addLayout(dx_row)

        layout.addWidget(_separator())

        # ── Hair info (SH-specific) ──
        layout.addWidget(_section_label("Hair Loss Info"))
        self.card_hair_location = EditableInfoCard("Hair Location")
        self.card_hair_sxx = EditableInfoCard("Hair Symptoms")
        layout.addWidget(self.card_hair_location)
        layout.addWidget(self.card_hair_sxx)

        layout.addStretch()

    def get_diagnoses_text(self, medication_text: str = "") -> str:
        parts = []
        if self.chip_dx_ed.active:
            parts.append("Erectile Dysfunction")
        if self.chip_dx_pe.active:
            parts.append("Premature Ejaculation")
        if self.chip_dx_pe_like.active:
            parts.append("PE-like symptoms")
        if self.chip_dx_hair.active and allow_hair_loss_diagnosis("Sexual Health", medication_text):
            parts.append("Hair Loss")
        return ", ".join(parts) if parts else ""


def build_actions(parent: Optional[QWidget] = None) -> SexualHealthActions:
    return SexualHealthActions(parent)


def build_info(parent: Optional[QWidget] = None) -> SexualHealthInfo:
    return SexualHealthInfo(parent)
