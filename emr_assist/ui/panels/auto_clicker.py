"""Auto-Clicker / In-Visit Automation panel for the EMR Assist Dashboard.

This panel is designed to be compact so it can float as a detached QDockWidget.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..widgets import ActionButton, CollapsibleSection, StatusBadge, ToggleChip
from ..theme import ACCENT_BLUE, ACCENT_GREEN, ACCENT_ORANGE, ACCENT_RED
from . import _section_label, _separator


class AutoClickerPanel(QWidget):
    """Compact auto-clicker controls – designed to float."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # ── Main toggle buttons ──
        self.btn_dashboard_clicker = ActionButton(
            "Autoclick: Dashboard", accent="green"
        )
        self.btn_invisit_clicker = ActionButton(
            "Autoclick: In-Visit", accent="green"
        )
        self.btn_page_refresh = ActionButton(
            "Auto-Refresh Page", accent="orange"
        )

        layout.addWidget(self.btn_dashboard_clicker)
        layout.addWidget(self.btn_invisit_clicker)
        layout.addWidget(self.btn_page_refresh)

        layout.addWidget(_separator())

        # ── Quick actions ──
        quick_row = QHBoxLayout()
        quick_row.setSpacing(4)
        self.btn_quick_next = ActionButton("Quick Next Task", accent="blue")
        self.btn_test_chrome = ActionButton("Test Chrome", accent="blue")
        self.btn_detect_visit = ActionButton("Detect Visit", accent="blue")
        quick_row.addWidget(self.btn_quick_next)
        quick_row.addWidget(self.btn_test_chrome)
        layout.addLayout(quick_row)
        layout.addWidget(self.btn_detect_visit)

        layout.addWidget(_separator())

        # ── Status area ──
        self.status_badge = StatusBadge("STOPPED", "red")
        self.lbl_method = QLabel("Click method: —")
        self.lbl_method.setStyleSheet("color: #a0b0c0; font-size: 11px;")
        self.lbl_last_click = QLabel("Last click: —")
        self.lbl_last_click.setStyleSheet("color: #a0b0c0; font-size: 11px;")
        layout.addWidget(self.status_badge)
        layout.addWidget(self.lbl_method)
        layout.addWidget(self.lbl_last_click)

        # ── Popup toggle ──
        self.chip_popup = ToggleChip("Show popup on URL change", ACCENT_ORANGE)
        layout.addWidget(self.chip_popup)

        # ── Location checking toggle (WI additional steps) ──
        self.chip_location_check = ToggleChip("Additional Location Checking (WI)", ACCENT_ORANGE)
        layout.addWidget(self.chip_location_check)

        layout.addWidget(_separator())

        # ── Collapsible Settings ──
        settings_widget = QWidget()
        s_layout = QVBoxLayout(settings_widget)
        s_layout.setContentsMargins(0, 0, 0, 0)
        s_layout.setSpacing(4)

        coord_row = QHBoxLayout()
        coord_row.setSpacing(4)
        coord_row.addWidget(QLabel("X:"))
        self.txt_x = QLineEdit("2600")
        self.txt_x.setMaximumWidth(60)
        coord_row.addWidget(self.txt_x)
        coord_row.addWidget(QLabel("Y:"))
        self.txt_y = QLineEdit("400")
        self.txt_y.setMaximumWidth(60)
        coord_row.addWidget(self.txt_y)
        coord_row.addWidget(QLabel("Interval:"))
        self.txt_interval = QLineEdit("3")
        self.txt_interval.setMaximumWidth(50)
        coord_row.addWidget(self.txt_interval)
        coord_row.addStretch()
        s_layout.addLayout(coord_row)

        # Click method chips (exclusive)
        method_row = QHBoxLayout()
        method_row.setSpacing(4)
        self.chip_cdp = ToggleChip("CDP", ACCENT_BLUE)
        self.chip_browser = ToggleChip("Browser", ACCENT_BLUE)
        self.chip_xy = ToggleChip("X/Y Screen", ACCENT_BLUE)
        self.chip_cdp.active = True  # default
        for chip in (self.chip_cdp, self.chip_browser, self.chip_xy):
            method_row.addWidget(chip)
        s_layout.addLayout(method_row)

        # Exclusive click-method selection
        self.chip_cdp.toggled_state.connect(lambda on: self._exclusive_method("cdp") if on else None)
        self.chip_browser.toggled_state.connect(lambda on: self._exclusive_method("browser") if on else None)
        self.chip_xy.toggled_state.connect(lambda on: self._exclusive_method("xy") if on else None)

        self.settings_section = CollapsibleSection("Settings", expanded=True)
        self.settings_section.add_widget(settings_widget)
        layout.addWidget(self.settings_section)

        # ── URL Monitor (collapsed by default) ──
        url_widget = QWidget()
        u_layout = QVBoxLayout(url_widget)
        u_layout.setContentsMargins(0, 0, 0, 0)
        self.lbl_url = QLabel("Current URL: —")
        self.lbl_url.setWordWrap(True)
        self.lbl_url.setStyleSheet("color: #8090a0; font-size: 10px;")
        u_layout.addWidget(self.lbl_url)
        self.url_section = CollapsibleSection("URL Monitor", expanded=False)
        self.url_section.add_widget(url_widget)
        layout.addWidget(self.url_section)

        layout.addStretch()

    # ── Helpers ──
    def _exclusive_method(self, selected: str) -> None:
        """Keep only one click-method chip active."""
        self.chip_cdp.active = selected == "cdp"
        self.chip_browser.active = selected == "browser"
        self.chip_xy.active = selected == "xy"

    @property
    def click_method(self) -> str:
        if self.chip_browser.active:
            return "browser"
        if self.chip_xy.active:
            return "xy"
        return "cdp"

    @property
    def interval(self) -> float:
        try:
            return float(self.txt_interval.text())
        except ValueError:
            return 3.0

    @property
    def click_x(self) -> int:
        try:
            return int(self.txt_x.text())
        except ValueError:
            return 2600

    @property
    def click_y(self) -> int:
        try:
            return int(self.txt_y.text())
        except ValueError:
            return 400

    def set_status(self, text: str, color: str = "red") -> None:
        self.status_badge.set_status(f"  {text}", color)
