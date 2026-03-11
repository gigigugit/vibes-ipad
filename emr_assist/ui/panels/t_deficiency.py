"""T Deficiency / Labs panel for the EMR Assist Dashboard."""

from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...core.config import LABS_CONFIG, PMH_OPTIONS
from ..widgets import (
    ActionButton,
    ChipGroup,
    EditableInfoCard,
    InfoCard,
    LabValueRow,
    MultiLineInfoCard,
    ToggleChip,
)
from . import _scrollable, _section_label, _separator


# ═══════════════════════════════════════════════════════════════════════════
# Actions panel — grab button + template insert buttons
# ═══════════════════════════════════════════════════════════════════════════

class TDeficiencyActions(QWidget):
    """Action buttons for the T Deficiency / Labs workflow."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # ── Grab ──
        layout.addWidget(_section_label("Grab"))
        self.btn_grab = ActionButton("Grab All Labs  (F4)", accent="blue")
        layout.addWidget(self.btn_grab)

        layout.addWidget(_separator())

        # ── Template insert ──
        layout.addWidget(_section_label("Insert Templates"))

        self.btn_insert_labs = ActionButton("Insert Labs", accent="green")
        self.btn_rx_note = ActionButton("Rx Note", accent="green")
        self.btn_referral = ActionButton("Referral Note", accent="green")
        self.btn_lab_message = ActionButton("Lab Message", accent="green")
        self.btn_td_followup = ActionButton("TD Follow-up Note", accent="green")
        self.btn_td_followup_labs = ActionButton("T Follow-up Labs", accent="green")

        for btn in (
            self.btn_insert_labs,
            self.btn_rx_note,
            self.btn_referral,
            self.btn_lab_message,
            self.btn_td_followup,
            self.btn_td_followup_labs,
        ):
            layout.addWidget(btn)

        layout.addWidget(_separator())

        # ── Utilities ──
        layout.addWidget(_section_label("Utilities"))
        self.btn_clinical_matrix = ActionButton("Clinical Matrix")
        self.btn_clear = ActionButton("Clear All", accent="red")

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.btn_clinical_matrix)
        row.addWidget(self.btn_clear)
        layout.addLayout(row)

        layout.addStretch()


# ═══════════════════════════════════════════════════════════════════════════
# Info panel — grabbed data display
# ═══════════════════════════════════════════════════════════════════════════

class TDeficiencyInfo(QWidget):
    """Grabbed-data display for T Deficiency / Labs."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # ── Scores row ──
        layout.addWidget(_section_label("Scores"))
        scores_row = QHBoxLayout()
        scores_row.setSpacing(6)
        self.card_tdcs = EditableInfoCard("TDCS", "—")
        self.card_tdcs_c = EditableInfoCard("TDCS-C", "—")
        self.card_ed = EditableInfoCard("ED", "—")
        scores_row.addWidget(self.card_tdcs)
        scores_row.addWidget(self.card_tdcs_c)
        scores_row.addWidget(self.card_ed)
        layout.addLayout(scores_row)

        # ── Medication ──
        layout.addWidget(_section_label("Treatment"))
        self.card_medication = EditableInfoCard("Medication")
        self.card_response = EditableInfoCard("Response")
        self.card_side_effects = EditableInfoCard("Side Effects")
        layout.addWidget(self.card_medication)

        treat_row = QHBoxLayout()
        treat_row.setSpacing(6)
        treat_row.addWidget(self.card_response)
        treat_row.addWidget(self.card_side_effects)
        layout.addLayout(treat_row)

        layout.addWidget(_separator())

        # ── Diagnoses ──
        layout.addWidget(_section_label("Diagnoses"))
        dx_row = QHBoxLayout()
        dx_row.setSpacing(6)
        self.chip_dx_td = ToggleChip("Testosterone Deficiency")
        self.chip_dx_ed = ToggleChip("Erectile Dysfunction")
        dx_row.addWidget(self.chip_dx_td)
        dx_row.addWidget(self.chip_dx_ed)
        layout.addLayout(dx_row)

        # ── PMH ──
        layout.addWidget(_section_label("PMH"))
        self.pmh_chips = ChipGroup("", PMH_OPTIONS)
        layout.addWidget(self.pmh_chips)

        layout.addStretch()


# ═══════════════════════════════════════════════════════════════════════════
# Labs panel — individual lab value rows
# ═══════════════════════════════════════════════════════════════════════════

class TDeficiencyLabs(QWidget):
    """Lab value rows with status indicators."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addWidget(_section_label("Lab Values"))

        self.lab_rows: Dict[str, LabValueRow] = {}
        for lab_name, cfg in LABS_CONFIG.items():
            row = LabValueRow(
                lab_name=lab_name,
                unit=cfg["unit"],
                normal_range=cfg.get("normal_range", ""),
            )
            self.lab_rows[cfg["var"]] = row
            layout.addWidget(row)

        layout.addStretch()

    def set_lab_value(self, var_name: str, value: str) -> None:
        if var_name in self.lab_rows:
            self.lab_rows[var_name].set_value(value)

    def get_lab_value(self, var_name: str) -> str:
        if var_name in self.lab_rows:
            return self.lab_rows[var_name].get_value()
        return ""

    def set_lab_status(self, var_name: str, status: str) -> None:
        if var_name in self.lab_rows:
            self.lab_rows[var_name].set_status(status)

    def get_lab_status(self, var_name: str) -> str:
        if var_name in self.lab_rows:
            return self.lab_rows[var_name].get_status()
        return "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Factory functions
# ═══════════════════════════════════════════════════════════════════════════

def build_actions(parent: Optional[QWidget] = None) -> TDeficiencyActions:
    return TDeficiencyActions(parent)


def build_info(parent: Optional[QWidget] = None) -> TDeficiencyInfo:
    return TDeficiencyInfo(parent)


def build_labs(parent: Optional[QWidget] = None) -> TDeficiencyLabs:
    return TDeficiencyLabs(parent)
