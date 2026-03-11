"""MainWindow — PyQt6 replacement for the monolithic wxPython MyFrame.

All seven tabs are constructed here.  Grab / insert / autoclick / hotkey
methods are defined as instance methods so they can access every widget
by *self.<name>*.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
import winsound
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import keyboard
import pyautogui
import pyperclip
import pygetwindow as gw
import requests

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..browser.exceptions import (
    TimeoutException,
    NoSuchElementException,
)
from ..browser.adapters import PlaywrightDriverAdapter
from ..browser.grabber import BrowserEMRGrabber
from ..core.config import (
    CDP_DEBUG_PORT,
    CDP_DEBUG_VERBOSE,
    CDP_EMR_PATIENT_URL_PREFIX,
    CDP_HEADER_EXPRESSION,
    CDP_POLL_INTERVAL_SEC,
    CDP_VISIT_TYPE_KEYWORDS,
    EMR_DASHBOARD_BASE_URLS,
    EMR_DASHBOARD_WELCOME_SELECTORS,
    ENABLE_QUICK_NEXT_TASK_HOTKEY,
    QUICK_NEXT_TASK_HOTKEY,
    GUI_HIDDEN_VISIBLE_WIDTH,
    DASHBOARD_TAB_INDEX,
    AUTO_GRAB_DELAY_MS,
    LABS_CONFIG,
    VISIT_TAB_INDICES,
    VISIT_TYPE_FALLBACK_KEYWORDS,
    TEMPLATE_BUTTONS,
    PMH_OPTIONS,
    BIRTH_CONTROL_PMH_OPTIONS,
    PA_SITUATION_OPTIONS,
    PA_SYMPTOM_OPTIONS,
    dprint,
)
from ..core.parsers import (
    extract_lab_value_simple,
    parse_tdcs_score,
    parse_tdcsc_score,
    parse_ed_status,
    parse_treatment_satisfaction,
    parse_side_effects_response,
    detect_td_diagnosis,
    detect_medication_from_text,
)
from ..core.templates import load_templates_from_file as load_templates
from ..core import state
from ..core.state import (
    grabbed_vars,
    tdcs_value,
    tdcs_c_value,
    ed_value,
    td_satisfaction_value,
    td_side_effects_value,
    diagnoses,
    medication_value,
    selected_template,
    pmh_selected,
    auto_clicker_enabled,
    auto_clicker_x,
    auto_clicker_y,
    auto_clicker_interval,
    auto_clicker_last_url,
    auto_clicker_thread,
    build_pmh_text,
    emit_emr_bridge,
)
from .signals import signals
from .helpers import type_template_text, clear_text_selection
from .dialogs import PMHDialog, NewTaskPopup


# ---------------------------------------------------------------------------
# Thread-safe main-thread dispatcher
# ---------------------------------------------------------------------------
def _on_main(fn):
    """Execute *fn* on the GUI/main thread.  Safe to call from ANY thread.

    Unlike QTimer.singleShot(0, fn) — which silently drops the callback
    when called from a threading.Thread that has no Qt event-loop —
    this routes through the SignalBridge.call_on_main signal, which is
    always delivered to the main thread via a queued connection.
    """
    signals.call_on_main.emit(fn)


class MainWindow(QMainWindow):
    """PyQt6 main window — replaces wxPython MyFrame."""

    # Inline CDP hotkeys config (same as original CUSTOM_CDP_HOTKEYS)
    CUSTOM_CDP_HOTKEYS: List[Dict[str, Any]] = [
        {
            "hotkey": "ctrl+alt+g",
            "css": [
                "button.btn:has-text('Dashboard')",
                "button.btn.border-none.bg-transparent",
            ],
            "description": "Click dashboard",
            "auto_hide": True,
        },
        {
            "hotkey": "ctrl+alt+d",
            "css": [
                "button.btn.border-none.bg-transparent.hover\\:shadow-none > div.css-1rynq56.r-cqee49",
                "button.btn.border-none.bg-transparent.hover\\:shadow-none",
            ],
            "description": "Click Dashboard button",
        },
    ]

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(state.panel_title)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        # Geometry: full screen height, narrow width at left edge
        screen = QApplication.primaryScreen().availableGeometry()
        self.setGeometry(0, 0, 450, screen.height())

        # --- Shared state --------------------------------------------------
        self.templates: Dict[str, str] = {}
        self.text_ctrls: Dict[str, QLineEdit] = {}
        self.check_ctrls: Dict[str, Tuple[QCheckBox, QCheckBox]] = {}

        # Browser / CDP caches
        self._browser_grabber_cache: Optional[BrowserEMRGrabber] = None
        self._cdp_driver: Optional[PlaywrightDriverAdapter] = None
        self._cdp_driver_thread_id: Optional[int] = None
        self._cdp_monitor_running: bool = True
        self._cdp_monitor_thread: Optional[threading.Thread] = None
        self._cdp_active_url: Optional[str] = None
        self._cdp_last_display: Optional[str] = None
        self._cdp_last_visit_key: Optional[Tuple] = None
        self._cdp_last_grab_key: Optional[Tuple] = None
        self._last_visit_tab: Optional[str] = None

        # Location
        self._location_worker: Optional[threading.Thread] = None
        self._last_location_summary: Optional[Dict] = None

        # Auto-clicker state
        self.invisit_running = False
        self.invisit_thread: Optional[threading.Thread] = None
        self.page_refresh_running = False
        self.page_refresh_thread: Optional[threading.Thread] = None
        self.auto_refresh_enabled = False
        self.url_monitoring_enabled = False
        self.url_monitor_thread: Optional[threading.Thread] = None
        self.current_url = ""
        self.notify_with_popup = False
        self.new_task_popup: Optional[NewTaskPopup] = None
        self.browser_click_count = 0
        self.cdp_click_count = 0
        self.xy_click_count = 0
        self.clicker_method_mode = "cdp"
        self.requests_session = requests.Session()

        # Hotkey state
        self._custom_cdp_hotkey_handles: list = []
        self._custom_cdp_hotkeys_active: list = []
        self._custom_cdp_hotkeys: list = []
        self._custom_cdp_hotkeys_registered = False
        self._tab4_hotkeys_registered = False
        self._tab4_hotkeys_method: Optional[str] = None
        self._tab4_keyboard_handles: list = []
        self._hotkeys_target: Optional[str] = None
        self._gui_hidden = False
        self._gui_visible_sliver = GUI_HIDDEN_VISIBLE_WIDTH
        self._is_closing = False
        self.global_insert_delay_ms = 200

        # Tab hotkey name lists (customisable)
        self._tab1_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n', 'ctrl+alt+c']
        self._tab2_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n', 'ctrl+alt+c']
        self._tab4_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n', 'ctrl+alt+c']
        self._tab7_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n']

        # Tab indices
        self._tab_index_t_def = 0
        self._tab_index_hair_loss = 1
        self._tab_index_sexual_health = 3
        self._tab_index_performance_anxiety = 5
        self._tab_index_birth_control = 6

        # Suppress event flag
        self._suppress_sexual_health_dx_event = False

        # --- Build UI ------------------------------------------------------
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(2)

        # Top info bar
        info_row = QHBoxLayout()
        self.visit_type_text = QLabel("Visit type: Unknown")
        self.patient_location_text = QLabel("Location: \u2014")
        detect_btn = QPushButton("Detect Visit")
        detect_btn.clicked.connect(self.detect_visit_type_and_switch_tab)
        info_row.addWidget(self.visit_type_text, 1)
        info_row.addWidget(self.patient_location_text, 1)
        info_row.addWidget(detect_btn)
        root_layout.addLayout(info_row)

        self.grab_status_text = QLabel("")
        root_layout.addWidget(self.grab_status_text)

        # Tab widget
        self.notebook = QTabWidget()
        self.notebook.currentChanged.connect(self._on_tab_changed)
        root_layout.addWidget(self.notebook, 1)

        # Build all tabs
        self._build_tab1_t_deficiency()
        self._build_tab2_hair_loss()
        self._build_tab3_photoaging()
        self._build_tab4_sexual_health()
        self._build_tab5_dashboard()
        self._build_tab6_performance_anxiety()
        self._build_tab7_birth_control()

        # Load templates
        try:
            script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self.templates = load_templates(script_dir)
        except Exception as e:
            print(f"Template loading error: {e}")

        # Wire global signal bridge connections
        self._connect_signals()

        # Deferred startup tasks
        QTimer.singleShot(200, self._deferred_startup)

    # ------------------------------------------------------------------
    # Deferred startup (hotkeys, CDP monitor, etc.)
    # ------------------------------------------------------------------
    def _deferred_startup(self):
        self.setup_global_hotkeys()
        self._register_custom_cdp_hotkeys()
        self._start_cdp_visit_monitor()

    # ------------------------------------------------------------------
    # Signal bridge connections
    # ------------------------------------------------------------------
    def _connect_signals(self):
        """Connect SignalBridge signals to local slots."""
        def _safe_call(fn):
            try:
                fn()
            except Exception as exc:
                print(f"[_on_main callback error] {exc}")
                import traceback; traceback.print_exc()
        signals.call_on_main.connect(_safe_call)
        signals.set_status.connect(self.grab_status_text.setText)
        signals.set_visit_type.connect(self.visit_type_text.setText)
        signals.set_location.connect(self.patient_location_text.setText)

    # ------------------------------------------------------------------
    # Tab builders
    # ------------------------------------------------------------------
    def _build_tab1_t_deficiency(self):
        """Tab 1: T Deficiency / ED Labs"""
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(4)

        # Grab row
        grab_row = QHBoxLayout()
        grab_btn = QPushButton("Grab All Labs (F4)")
        grab_btn.clicked.connect(lambda: self.grab_all_labs())
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self.clear_all)
        grab_row.addWidget(grab_btn)
        grab_row.addWidget(clear_btn)
        layout.addLayout(grab_row)

        # TDCS / TDCS-C / ED row
        scores_row = QHBoxLayout()
        scores_row.addWidget(QLabel("TDCS:"))
        self.tdcs_text = QLineEdit("\u2014")
        self.tdcs_text.setMaximumWidth(50)
        scores_row.addWidget(self.tdcs_text)
        scores_row.addWidget(QLabel("TDCS-C:"))
        self.tdcs_c_text = QLineEdit("\u2014")
        self.tdcs_c_text.setMaximumWidth(50)
        scores_row.addWidget(self.tdcs_c_text)
        scores_row.addWidget(QLabel("ED:"))
        self.ed_text = QLineEdit("\u2014")
        self.ed_text.setMaximumWidth(50)
        scores_row.addWidget(self.ed_text)
        scores_row.addStretch()
        layout.addLayout(scores_row)

        # Lab rows
        for lab_name, lab_config in LABS_CONFIG.items():
            var = lab_config["var"]
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{lab_name} ({lab_config['unit']}):"))
            txt = QLineEdit()
            txt.setMaximumWidth(100)
            row.addWidget(txt)
            high_cb = QCheckBox("H")
            low_cb = QCheckBox("L")
            row.addWidget(high_cb)
            row.addWidget(low_cb)
            row.addStretch()
            layout.addLayout(row)
            self.text_ctrls[var] = txt
            self.check_ctrls[var] = (high_cb, low_cb)

        # Medication text field
        med_row = QHBoxLayout()
        med_row.addWidget(QLabel("Medication:"))
        self.td_med_text = QLineEdit()
        med_row.addWidget(self.td_med_text, 1)
        layout.addLayout(med_row)

        # Treatment response field (visible & editable)
        response_row = QHBoxLayout()
        response_row.addWidget(QLabel("Response:"))
        self.td_response_text = QLineEdit()
        self.td_response_text.setPlaceholderText("e.g. feeling better, no improvement...")
        response_row.addWidget(self.td_response_text, 1)
        layout.addLayout(response_row)

        # Side effects field (visible & editable)
        se_row = QHBoxLayout()
        se_row.addWidget(QLabel("Side Effects:"))
        self.td_side_effects_text = QLineEdit("No side effects reported")
        se_row.addWidget(self.td_side_effects_text, 1)
        layout.addLayout(se_row)

        # Diagnosis checkboxes + isolated display
        dx_row = QHBoxLayout()
        dx_row.addWidget(QLabel("Dx:"))
        self.dx_td_cb = QCheckBox("Testosterone Deficiency")
        self.dx_ed_cb = QCheckBox("ED")
        self.dx_td_cb.stateChanged.connect(self.on_dx_checkbox)
        self.dx_ed_cb.stateChanged.connect(self.on_dx_checkbox)
        dx_row.addWidget(self.dx_td_cb)
        dx_row.addWidget(self.dx_ed_cb)
        dx_row.addStretch()
        layout.addLayout(dx_row)

        # PMH selector
        pmh_row = QHBoxLayout()
        pmh_btn = QPushButton("Select PMH...")
        pmh_btn.clicked.connect(self.open_pmh_dialog)
        self.pmh_summary = QLabel("Current: none (noncontributory)")
        pmh_row.addWidget(pmh_btn)
        pmh_row.addWidget(self.pmh_summary, 1)
        layout.addLayout(pmh_row)

        # Template buttons
        btn_row = QHBoxLayout()
        for btn_cfg in TEMPLATE_BUTTONS:
            b = QPushButton(btn_cfg["label"])
            if btn_cfg.get("clear_all"):
                b.clicked.connect(lambda checked: self.clear_all())
            elif btn_cfg.get("show_matrix"):
                b.clicked.connect(lambda checked: self.show_clinical_matrix())
            elif btn_cfg.get("template_name"):
                tname = btn_cfg["template_name"]
                if tname == "Testosterone Deficiency Follow-up":
                    b.clicked.connect(lambda checked: self.insert_td_followup_note())
                elif tname == "Testosterone Follow-up Labs":
                    b.clicked.connect(lambda checked: self.insert_td_followup_labs())
                else:
                    b.clicked.connect(lambda checked, t=tname: self.insert_specific_template(t))
            elif btn_cfg.get("dynamic_labs"):
                b.clicked.connect(lambda checked: self.insert_labs_note())
            elif btn_cfg.get("rx_note"):
                b.clicked.connect(lambda checked: self.insert_rx_note())
            elif btn_cfg.get("referral_note"):
                b.clicked.connect(lambda checked: self.insert_referral_note())
            elif btn_cfg.get("lab_message"):
                b.clicked.connect(lambda checked: self.insert_lab_message())
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        layout.addStretch()
        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll)
        self.notebook.addTab(tab, "T Deficiency")

    def _build_tab2_hair_loss(self):
        """Tab 2: Hair Loss"""
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(4)

        grab_btn = QPushButton("Grab Hair Data (F4)")
        grab_btn.clicked.connect(self.grab_hair)
        layout.addWidget(grab_btn)

        # Fields
        for label_text, attr_name in [("Medication:", "hair_med_text"), ("Response:", "hair_hvar_text"), ("Symptoms:", "hair_hsx_text")]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            txt = QLineEdit()
            setattr(self, attr_name, txt)
            row.addWidget(txt, 1)
            layout.addLayout(row)

        # Exam checkboxes
        exam_group = QGroupBox("Hair Exam Findings")
        exam_layout = QVBoxLayout(exam_group)
        exam_checks = [
            ("Front hairline", "hair_exam_front_hairline"),
            ("Top/crown", "hair_exam_top_crown"),
            ("Widening of the part", "hair_exam_widening_part"),
            ("Diffuse thinning", "hair_exam_diffuse_thinning"),
            ("Confluent front to crown", "hair_exam_confluent"),
            ("Near front, sparing hairline", "hair_exam_near_front"),
        ]
        r1 = QHBoxLayout()
        r2 = QHBoxLayout()
        for i, (text, attr) in enumerate(exam_checks):
            cb = QCheckBox(text)
            setattr(self, attr, cb)
            if i < 3:
                r1.addWidget(cb)
            else:
                r2.addWidget(cb)
        exam_layout.addLayout(r1)
        exam_layout.addLayout(r2)
        layout.addWidget(exam_group)

        # Template buttons
        btn_row = QHBoxLayout()
        b1 = QPushButton("Follow-up Note")
        b1.clicked.connect(lambda: self.insert_hair_note(followup=True))
        b2 = QPushButton("Initial Note")
        b2.clicked.connect(lambda: self.insert_hair_note(initial=True))
        b3 = QPushButton("Limited Check-in")
        b3.clicked.connect(self.insert_hair_limited_checkin_note)
        btn_row.addWidget(b1)
        btn_row.addWidget(b2)
        btn_row.addWidget(b3)
        layout.addLayout(btn_row)

        layout.addStretch()
        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll)
        self.notebook.addTab(tab, "Hair Loss")

    def _build_tab3_photoaging(self):
        """Tab 3: Photoaging"""
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(4)

        grab_btn = QPushButton("Grab Photoaging Data (F4)")
        grab_btn.clicked.connect(self.grab_photoaging)
        layout.addWidget(grab_btn)

        for label_text, attr_name in [("Medication:", "photoaging_med_text"), ("Skin Goals:", "photoaging_goals_text"), ("Retinoid History:", "photoaging_retinoid_text")]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            txt = QLineEdit()
            setattr(self, attr_name, txt)
            row.addWidget(txt, 1)
            layout.addLayout(row)

        # Exam checkboxes
        exam_group = QGroupBox("Photoaging Exam Findings")
        exam_layout = QVBoxLayout(exam_group)
        pa_checks = [
            ("Fine lines", "photoaging_exam_fine_lines"),
            ("Wrinkles", "photoaging_exam_wrinkles"),
            ("Crow's feet", "photoaging_exam_crows_feet"),
            ("Pigmentation", "photoaging_exam_pigmentation"),
            ("Age spots", "photoaging_exam_age_spots"),
            ("Melasma", "photoaging_exam_melasma"),
            ("Texture changes", "photoaging_exam_texture_changes"),
            ("Enlarged pores", "photoaging_exam_enlarged_pores"),
            ("Loss of elasticity", "photoaging_exam_loss_elasticity"),
            ("Inflammation", "photoaging_exam_inflammation"),
            ("Acne scarring", "photoaging_exam_scarring"),
            ("Normal", "photoaging_exam_normal"),
        ]
        rows = [QHBoxLayout() for _ in range(4)]
        for i, (text, attr) in enumerate(pa_checks):
            cb = QCheckBox(text)
            setattr(self, attr, cb)
            rows[i // 3].addWidget(cb)
        for r in rows:
            exam_layout.addLayout(r)
        layout.addWidget(exam_group)

        note_btn = QPushButton("Insert Note")
        note_btn.clicked.connect(self.insert_photoaging_note)
        layout.addWidget(note_btn)

        layout.addStretch()
        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll)
        self.notebook.addTab(tab, "Photoaging")

    def _build_tab4_sexual_health(self):
        """Tab 4: Sexual Health"""
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(4)

        # Grab row
        grab_row = QHBoxLayout()
        grab_btn = QPushButton("Grab Sexual Health (F4)")
        grab_btn.clicked.connect(self.grab_sexual_health)
        self.sexual_health_mode_choice = QComboBox()
        self.sexual_health_mode_choice.addItems(["Browser (CDP)", "Clipboard"])
        grab_row.addWidget(grab_btn)
        grab_row.addWidget(QLabel("Mode:"))
        grab_row.addWidget(self.sexual_health_mode_choice)
        layout.addLayout(grab_row)

        # Fields
        for label_text, attr_name in [
            ("Medication:", "sexual_health_med_text"),
            ("Effectiveness:", "sexual_health_effectiveness_text"),
            ("BP:", "sexual_health_bp_text"),
            ("Hair Location:", "sexual_health_hair_location_text"),
            ("Hair Sxx:", "sexual_health_hair_sxx_text"),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            txt = QLineEdit()
            setattr(self, attr_name, txt)
            row.addWidget(txt, 1)
            layout.addLayout(row)

        # Change focus / detail
        change_row = QHBoxLayout()
        change_row.addWidget(QLabel("Change:"))
        self.sexual_health_change_choice = QComboBox()
        self.sexual_health_change_choice.addItems(["Cadence", "Dose number", "Medication"])
        change_row.addWidget(self.sexual_health_change_choice)
        self.sexual_health_change_detail = QLineEdit()
        change_row.addWidget(self.sexual_health_change_detail, 1)
        layout.addLayout(change_row)

        plan_row = QHBoxLayout()
        plan_row.addWidget(QLabel("Plan:"))
        self.sexual_health_plan_choice = QComboBox()
        self.sexual_health_plan_choice.addItems(["Continue present treatment", "Change treatment to--"])
        plan_row.addWidget(self.sexual_health_plan_choice, 1)
        layout.addLayout(plan_row)

        # Dx checkboxes
        dx_row = QHBoxLayout()
        self.sexual_health_dx_ed = QCheckBox("ED")
        self.sexual_health_dx_pe = QCheckBox("PE")
        self.sexual_health_dx_pe_like = QCheckBox("PE-like")
        self.sexual_health_dx_hair_loss = QCheckBox("Hair Loss")
        for cb in [self.sexual_health_dx_ed, self.sexual_health_dx_pe, self.sexual_health_dx_pe_like, self.sexual_health_dx_hair_loss]:
            cb.stateChanged.connect(self.on_sexual_health_dx_checkbox)
            dx_row.addWidget(cb)
        layout.addLayout(dx_row)

        # Template buttons
        btn_row1 = QHBoxLayout()
        b_fu = QPushButton("Follow-up")
        b_fu.clicked.connect(self.insert_sexual_health_note)
        b_plan = QPushButton("Plan")
        b_plan.clicked.connect(self.insert_sexual_health_brief_template)
        b_change = QPushButton("Change")
        b_change.clicked.connect(self.insert_sexual_health_change_template)
        btn_row1.addWidget(b_fu)
        btn_row1.addWidget(b_plan)
        btn_row1.addWidget(b_change)
        layout.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        b_cad = QPushButton("Change Cadence")
        b_cad.clicked.connect(self.insert_sh_change_cadence)
        b_num = QPushButton("Change Number")
        b_num.clicked.connect(self.insert_sh_change_number)
        b_med = QPushButton("Change Med")
        b_med.clicked.connect(self.insert_sh_change_medication)
        b_hair = QPushButton("Hair Info")
        b_hair.clicked.connect(self.insert_hair_info)
        btn_row2.addWidget(b_cad)
        btn_row2.addWidget(b_num)
        btn_row2.addWidget(b_med)
        btn_row2.addWidget(b_hair)
        layout.addLayout(btn_row2)

        layout.addStretch()
        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll)
        self.notebook.addTab(tab, "Sexual Health")

    def _build_tab5_dashboard(self):
        """Tab 5: Dashboard"""
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(4)

        layout.addWidget(QLabel("Dashboard / In-Visit Automation"))

        # Control buttons
        self.clicker_start_btn = QPushButton("Start Autoclick: Dashboard")
        self.clicker_start_btn.clicked.connect(self.toggle_auto_clicker)
        layout.addWidget(self.clicker_start_btn)

        self.clicker_invisit_btn = QPushButton("Start Autoclick: In-Visit")
        self.clicker_invisit_btn.clicked.connect(self.toggle_invisit_clicker)
        layout.addWidget(self.clicker_invisit_btn)

        self.page_refresh_btn = QPushButton("Start Auto-Refresh Page")
        self.page_refresh_btn.clicked.connect(self.toggle_page_refresh)
        layout.addWidget(self.page_refresh_btn)

        quick_btn = QPushButton("Quick Next Task")
        quick_btn.clicked.connect(self.quick_next_task)
        layout.addWidget(quick_btn)

        test_btn = QPushButton("Test Chrome Connection")
        test_btn.clicked.connect(self.test_chrome_connection)
        layout.addWidget(test_btn)

        detect_btn = QPushButton("Detect Visit Type")
        detect_btn.clicked.connect(self.detect_visit_type_and_switch_tab)
        layout.addWidget(detect_btn)

        # Status labels
        self.clicker_status_text = QLabel("Status: STOPPED")
        self.clicker_status_text.setStyleSheet("color: red")
        layout.addWidget(self.clicker_status_text)

        self.clicker_method_text = QLabel("Click method: —")
        layout.addWidget(self.clicker_method_text)

        self.clicker_last_text = QLabel("Last click: —")
        layout.addWidget(self.clicker_last_text)

        # Popup toggle
        self.popup_toggle = QCheckBox("Show popup on URL change")
        self.popup_toggle.stateChanged.connect(self.on_popup_toggle)
        layout.addWidget(self.popup_toggle)

        # Location checking toggle (WI additional steps)
        self.location_check_toggle = QCheckBox("Additional Location Checking (WI)")
        self.location_check_toggle.setChecked(False)
        layout.addWidget(self.location_check_toggle)

        # Settings group
        settings = QGroupBox("Settings")
        s_layout = QVBoxLayout(settings)
        coord_row = QHBoxLayout()
        coord_row.addWidget(QLabel("X:"))
        self.clicker_x_text = QLineEdit("2600")
        self.clicker_x_text.setMaximumWidth(60)
        coord_row.addWidget(self.clicker_x_text)
        coord_row.addWidget(QLabel("Y:"))
        self.clicker_y_text = QLineEdit("400")
        self.clicker_y_text.setMaximumWidth(60)
        coord_row.addWidget(self.clicker_y_text)
        coord_row.addWidget(QLabel("Interval:"))
        self.clicker_interval_text = QLineEdit("3")
        self.clicker_interval_text.setMaximumWidth(50)
        coord_row.addWidget(self.clicker_interval_text)
        coord_row.addStretch()
        s_layout.addLayout(coord_row)

        # Click method radio buttons
        method_row = QHBoxLayout()
        self.method_cdp_rb = QRadioButton("CDP")
        self.method_cdp_rb.setChecked(True)
        self.method_browser_rb = QRadioButton("Browser")
        self.method_xy_rb = QRadioButton("X/Y Screen")
        for rb in [self.method_cdp_rb, self.method_browser_rb, self.method_xy_rb]:
            rb.toggled.connect(self.on_click_method_changed)
            method_row.addWidget(rb)
        s_layout.addLayout(method_row)
        layout.addWidget(settings)

        # URL monitor
        url_group = QGroupBox("URL Monitor")
        u_layout = QVBoxLayout(url_group)
        self.clicker_url_text = QLabel("Current URL: —")
        self.clicker_url_text.setWordWrap(True)
        u_layout.addWidget(self.clicker_url_text)
        layout.addWidget(url_group)

        layout.addStretch()
        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll)
        self.notebook.addTab(tab, "Dashboard")

    def _build_tab6_performance_anxiety(self):
        """Tab 6: Performance Anxiety"""
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(4)

        grab_btn = QPushButton("Grab PA Data (F4)")
        grab_btn.clicked.connect(self.grab_performance_anxiety)
        layout.addWidget(grab_btn)

        # Medication
        med_row = QHBoxLayout()
        med_row.addWidget(QLabel("Medication:"))
        self.pa_med_text = QLineEdit()
        med_row.addWidget(self.pa_med_text, 1)
        layout.addLayout(med_row)

        # Multi-line fields
        for label_text, attr_name in [("Situations:", "pa_situations_text"), ("Symptoms:", "pa_symptoms_text")]:
            layout.addWidget(QLabel(label_text))
            txt = QPlainTextEdit()
            txt.setMaximumHeight(60)
            setattr(self, attr_name, txt)
            layout.addWidget(txt)

        # BP / Pulse
        vitals_row = QHBoxLayout()
        vitals_row.addWidget(QLabel("BP:"))
        self.pa_bp_text = QLineEdit("nr")
        self.pa_bp_text.setMaximumWidth(80)
        vitals_row.addWidget(self.pa_bp_text)
        vitals_row.addWidget(QLabel("Pulse:"))
        self.pa_pulse_text = QLineEdit("nr")
        self.pa_pulse_text.setMaximumWidth(80)
        vitals_row.addWidget(self.pa_pulse_text)
        vitals_row.addStretch()
        layout.addLayout(vitals_row)

        # Response / Side-effects radios
        resp_row = QHBoxLayout()
        resp_row.addWidget(QLabel("Response:"))
        self.pa_resp_good_rb = QRadioButton("Good")
        self.pa_resp_good_rb.setChecked(True)
        self.pa_resp_bad_rb = QRadioButton("Bad")
        resp_row.addWidget(self.pa_resp_good_rb)
        resp_row.addWidget(self.pa_resp_bad_rb)
        resp_row.addStretch()
        layout.addLayout(resp_row)

        se_row = QHBoxLayout()
        se_row.addWidget(QLabel("Side Effects:"))
        self.pa_se_without_rb = QRadioButton("Without")
        self.pa_se_without_rb.setChecked(True)
        self.pa_se_with_rb = QRadioButton("With")
        se_row.addWidget(self.pa_se_without_rb)
        se_row.addWidget(self.pa_se_with_rb)
        se_row.addStretch()
        layout.addLayout(se_row)

        # Template buttons
        btn_row = QHBoxLayout()
        b_fu = QPushButton("Follow-up Note")
        b_fu.clicked.connect(lambda: self.insert_performance_anxiety_note(followup=True))
        b_init = QPushButton("Initial Note")
        b_init.clicked.connect(lambda: self.insert_performance_anxiety_note(initial=True))
        btn_row.addWidget(b_fu)
        btn_row.addWidget(b_init)
        layout.addLayout(btn_row)

        layout.addStretch()
        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll)
        self.notebook.addTab(tab, "Perf. Anxiety")

    def _build_tab7_birth_control(self):
        """Tab 7: Birth Control"""
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(4)

        grab_btn = QPushButton("Grab BC Data (F4)")
        grab_btn.clicked.connect(self.grab_birth_control)
        layout.addWidget(grab_btn)

        for label_text, attr_name in [
            ("Medication:", "bc_med_text"),
            ("LMP:", "bc_lmp_text"),
            ("BP:", "bc_bp_text"),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            txt = QLineEdit()
            setattr(self, attr_name, txt)
            row.addWidget(txt, 1)
            layout.addLayout(row)

        layout.addWidget(QLabel("Side Effects:"))
        self.bc_side_effects_text = QPlainTextEdit()
        self.bc_side_effects_text.setMaximumHeight(50)
        layout.addWidget(self.bc_side_effects_text)

        # Visit type radios
        vt_row = QHBoxLayout()
        self.bc_initial_rb = QRadioButton("Initial")
        self.bc_initial_rb.setChecked(True)
        self.bc_followup_rb = QRadioButton("Follow-up")
        vt_row.addWidget(self.bc_initial_rb)
        vt_row.addWidget(self.bc_followup_rb)
        vt_row.addStretch()
        layout.addLayout(vt_row)

        # History changes
        hc_row = QHBoxLayout()
        hc_row.addWidget(QLabel("History changes:"))
        self.bc_history_changes_text = QLineEdit()
        hc_row.addWidget(self.bc_history_changes_text, 1)
        layout.addLayout(hc_row)

        # PMH checkboxes
        pmh_group = QGroupBox("PMH")
        pmh_layout = QVBoxLayout(pmh_group)
        self.bc_pmh_checkboxes: Dict[str, QCheckBox] = {}
        pmh_row = QHBoxLayout()
        for i, (key, label) in enumerate(BIRTH_CONTROL_PMH_OPTIONS):
            cb = QCheckBox(label)
            self.bc_pmh_checkboxes[key] = cb
            pmh_row.addWidget(cb)
            if (i + 1) % 3 == 0:
                pmh_layout.addLayout(pmh_row)
                pmh_row = QHBoxLayout()
        if pmh_row.count() > 0:
            pmh_layout.addLayout(pmh_row)
        other_row = QHBoxLayout()
        other_row.addWidget(QLabel("Other:"))
        self.bc_pmh_other_text = QLineEdit()
        other_row.addWidget(self.bc_pmh_other_text, 1)
        pmh_layout.addLayout(other_row)
        layout.addWidget(pmh_group)

        # Template buttons
        btn_row = QHBoxLayout()
        b_fu = QPushButton("Follow-up Note")
        b_fu.clicked.connect(self.insert_birth_control_followup_note)
        b_init = QPushButton("Initial Note")
        b_init.clicked.connect(self.insert_birth_control_initial_note)
        btn_row.addWidget(b_fu)
        btn_row.addWidget(b_init)
        layout.addLayout(btn_row)

        layout.addStretch()
        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(scroll)
        self.notebook.addTab(tab, "Birth Control")
    # ==================================================================
    # Browser grabber helpers
    # ==================================================================
    def _ensure_browser_grabber(self) -> Tuple[Optional[BrowserEMRGrabber], bool]:
        created = False
        grabber = getattr(self, "_browser_grabber_cache", None)
        if grabber is None:
            grabber = BrowserEMRGrabber()
            self._browser_grabber_cache = grabber
            created = True
        return grabber, created

    def _canonicalize_visit_type(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        mapping = {
            "sexual health": "Sexual Health",
            "hair loss": "Hair Loss",
            "photoaging": "Photoaging",
            "performance anxiety": "Performance Anxiety",
            "birth control": "Birth Control",
            "testosterone": "T Deficiency",
            "td": "T Deficiency",
            "t deficiency": "T Deficiency",
            "ed": "T Deficiency",
            "emr dashboard": "EMR Dashboard",
        }
        low = raw.strip().lower()
        for key, canonical in mapping.items():
            if key in low:
                return canonical
        return None

    def _reset_emr_view_via_script(self, grabber=None) -> bool:
        if grabber is None:
            grabber = getattr(self, "_browser_grabber_cache", None)
        created = False
        try:
            if grabber is None:
                grabber = BrowserEMRGrabber()
                self._browser_grabber_cache = grabber
                created = True
            if not grabber.connect_to_chrome():
                if created:
                    self._browser_grabber_cache = None
                return False
            if not grabber._ensure_emr_tab():
                return False
            driver = getattr(grabber, "driver", None)
            if not driver:
                return False
            try:
                grabber._switch_to_default()
            except Exception:
                pass
            script = """
(() => {
    try {
        const sel = window.getSelection && window.getSelection();
        if (sel && sel.removeAllRanges) { sel.removeAllRanges(); }
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') { active.blur(); }
        if (typeof window.scrollTo === 'function') { window.scrollTo({left:0,top:0,behavior:'instant'}); }
        return true;
    } catch (err) { return false; }
})();
"""
            result = driver.execute_script(script)
            return bool(result) if result is not None else True
        except Exception as exc:
            print(f"Reset EMR view error: {exc}")
            return False
        finally:
            if created and getattr(self, "_browser_grabber_cache", None) is not grabber:
                try:
                    grabber.disconnect()
                except Exception:
                    pass
                try:
                    grabber.shutdown_playwright()
                except Exception:
                    pass

    # ==================================================================
    # Location helpers
    # ==================================================================
    def _format_patient_location_label(self, summary: Optional[Dict[str, Any]]) -> str:
        base = "Location: \u2014"
        if not summary:
            return base
        state_display = (summary.get("state_display") or summary.get("state") or "").strip()
        city = (summary.get("city") or "").strip()
        state_code = (summary.get("state") or state_display or "").strip()
        distance = summary.get("distance_miles")
        if not state_display:
            return base
        if city and state_code.upper() == "WI":
            if distance is not None:
                return f"Location: {city}, WI - {distance:.1f} mi from Chippewa Falls, WI"
            return f"Location: {city}, WI"
        return f"Location: {state_display}"

    def _update_patient_location_label(self, summary: Optional[Dict[str, Any]]) -> None:
        label = self._format_patient_location_label(summary)
        self.patient_location_text.setText(label)
        self._last_location_summary = summary

    def refresh_patient_location_async(self) -> None:
        if self._location_worker and self._location_worker.is_alive():
            return
        # Read toggle state on main thread before spawning worker
        skip_wi = (
            not getattr(self, "location_check_toggle", None)
            or not self.location_check_toggle.isChecked()
        )
        def worker():
            try:
                grabber = BrowserEMRGrabber()
            except Exception as exc:
                print(f"Location refresh init error: {exc}")
                self._location_worker = None
                return
            try:
                if not grabber.connect_to_chrome():
                    print("Location refresh error: could not attach to Chrome")
                    return
                summary = grabber.fetch_patient_location_summary(
                    debug_print=True, skip_wi_detail=skip_wi
                )
                print(f"[PATIENT LOCATION REFRESH] {summary}")
                _on_main(lambda: self._update_patient_location_label(summary))
            except Exception as exc:
                print(f"Location refresh error: {exc}")
            finally:
                try:
                    grabber.disconnect()
                except Exception:
                    pass
                try:
                    grabber.shutdown_playwright()
                except Exception:
                    pass
                self._location_worker = None
        self._location_worker = threading.Thread(target=worker, daemon=True)
        self._location_worker.start()

    # ==================================================================
    # Tab switching / auto-grab
    # ==================================================================
    def _schedule_tab_switch(self, visit_label: Optional[str], status_msg: Optional[str] = None) -> bool:
        canonical = self._canonicalize_visit_type(visit_label)
        if not canonical:
            return False
        target_tab = VISIT_TAB_INDICES.get(canonical)
        if canonical == "EMR Dashboard":
            target_tab = DASHBOARD_TAB_INDEX
        if target_tab is None:
            return False
        def apply_switch():
            current = self.notebook.currentIndex()
            if current != target_tab:
                self.notebook.setCurrentIndex(target_tab)
            if status_msg:
                self.grab_status_text.setText(status_msg)
            self._last_visit_tab = canonical
        _on_main(apply_switch)
        return True

    def _queue_auto_grab(self, canonical: str, source_url: Optional[str]) -> None:
        if not canonical or canonical == "EMR Dashboard":
            return
        key = ((source_url or ""), canonical)
        if self._cdp_last_grab_key == key:
            return
        self._cdp_last_grab_key = key
        _on_main(lambda: self.grab_status_text.setText(f"Auto-grabbing {canonical} data..."))
        def dispatch():
            if self._is_closing:
                return
            try:
                if canonical == "Sexual Health":
                    self.grab_sexual_health()
                elif canonical == "Hair Loss":
                    self.grab_hair()
                elif canonical == "Photoaging":
                    self.grab_photoaging()
                elif canonical == "Performance Anxiety":
                    self.grab_performance_anxiety()
                elif canonical == "Birth Control":
                    self.grab_birth_control()
                else:
                    self.grab_all_labs()
            except Exception as exc:
                print(f"[CDP MONITOR] Auto grab error for {canonical}: {exc}")
        _on_main(lambda: QTimer.singleShot(AUTO_GRAB_DELAY_MS, dispatch))

    # ==================================================================
    # Grab methods
    # ==================================================================
    def grab_all_labs(self):
        """Grab all lab values from EMR page."""
        finished = [False]
        def do_grab():
            original = ""
            try:
                try:
                    original = pyperclip.paste()
                except Exception:
                    original = ""
                pyperclip.copy("CLEARED_BY_METHOD_2")
                time.sleep(0.3)
                sw, sh = pyautogui.size()
                cx, cy = sw // 2, sh // 2
                pyautogui.click(cx, cy)
                time.sleep(0.5)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.5)
                pyautogui.hotkey("ctrl", "c")
                time.sleep(0.8)
                new_content = pyperclip.paste()
                if not (new_content and new_content != "CLEARED_BY_METHOD_2" and new_content != original and len(new_content) > 50):
                    try:
                        g = BrowserEMRGrabber()
                        if g.connect_to_chrome():
                            fb = g._get_page_text() or ""
                            if len(fb) > 50:
                                new_content = fb
                    except Exception:
                        pass
                if new_content and new_content != "CLEARED_BY_METHOD_2" and new_content != original and len(new_content) > 50:
                    clear_text_selection(self)
                    for lab_name, lab_config in LABS_CONFIG.items():
                        result = extract_lab_value_simple(lab_config, new_content)
                        var = lab_config["var"]
                        grabbed_vars[var] = result["raw_value"] if result["found"] else ""
                    tdcs_value[0] = str(parse_tdcs_score(new_content))
                    tdcs_c = parse_tdcsc_score(new_content)
                    tdcs_c_value[0] = str(tdcs_c) if tdcs_c is not None else "\u2014"
                    ed_status = parse_ed_status(new_content)
                    ed_value[0] = "He reports symptoms consistent with ED." if ed_status == "Yes" else "He denies symptoms of ED." if ed_status == "No" else "\u2014"
                    td_satisfaction_value[0] = parse_treatment_satisfaction(new_content)
                    td_side_effects_value[0] = parse_side_effects_response(new_content)
                    try:
                        if detect_td_diagnosis(new_content):
                            diagnoses[0] = "Testosterone Deficiency"
                            _on_main(lambda: self.dx_td_cb.setChecked(True))
                    except Exception:
                        pass
                    detected_med = detect_medication_from_text(new_content)
                    if detected_med:
                        medication_value[0] = detected_med
                    _on_main(self._update_ui_after_labs_grab)
                    emit_emr_bridge({"context": "labs"})
                    try:
                        pyperclip.copy(original)
                    except Exception:
                        pass
                else:
                    try:
                        pyperclip.copy(original)
                    except Exception:
                        pass
                    _on_main(lambda: QMessageBox.warning(self, "Error", "Failed to grab text from EMR."))
                    _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
            except Exception as e:
                try:
                    pyperclip.copy(original)
                except Exception:
                    pass
                _on_main(lambda: QMessageBox.critical(self, "Error", f"Error during grab: {e}"))
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
            finally:
                finished[0] = True
        self.hide()
        time.sleep(0.1)
        def _watchdog():
            if not finished[0]:
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
                _on_main(lambda: QMessageBox.warning(self, "Timeout", "Grab took too long."))
        timer = threading.Timer(7.0, _watchdog)
        timer.daemon = True
        timer.start()
        threading.Thread(target=do_grab, daemon=True).start()

    def _update_ui_after_labs_grab(self):
        for lab_name, lab_config in LABS_CONFIG.items():
            var = lab_config["var"]
            if var in self.text_ctrls:
                self.text_ctrls[var].setText(grabbed_vars[var])
        self.tdcs_text.setText(str(tdcs_value[0]))
        self.tdcs_c_text.setText(str(tdcs_c_value[0]))
        self.ed_text.setText("Yes" if "symptoms consistent with ED" in ed_value[0] else "No" if "denies symptoms of ED" in ed_value[0] else "\u2014")
        self.td_med_text.setText(medication_value[0])
        self.td_response_text.setText(td_satisfaction_value[0] if td_satisfaction_value[0] != "\u2014" else "")
        self.td_side_effects_text.setText(td_side_effects_value[0])
        self.show()
        self.raise_()
        self.activateWindow()

    def grab_hair(self):
        """Grab hair loss data from EMR."""
        self.hide()
        time.sleep(0.1)
        def do_grab():
            try:
                original = ""
                try:
                    original = pyperclip.paste()
                except Exception:
                    pass
                sw, sh = pyautogui.size()
                cx, cy = sw // 2, sh // 2
                pyautogui.click(cx, cy)
                time.sleep(0.3)
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.2)
                pyautogui.hotkey('ctrl', 'c')
                time.sleep(0.5)
                new_content = pyperclip.paste()
                if not new_content or len(new_content) < 20:
                    _on_main(lambda: QMessageBox.warning(self, "Error", "Failed to grab text for hair-loss parsing."))
                    pyperclip.copy(original)
                    _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
                    return
                clear_text_selection(self)
                # Parse medication, hvar, hsx
                lines = [ln.strip() for ln in new_content.splitlines() if ln.strip()]
                med = ''
                hvar = ''
                hsx = ''
                header_pattern = re.compile(r'^(current dose|treatment plan|treatment|medication|current treatment|meds|dose|photos|notes|click an image|intake forms|patient selected|responses?|instructions)\b', re.IGNORECASE)
                treatment_header_re = re.compile(r'^treatment\s*[:\-]*$', re.IGNORECASE)
                treatment_inline_re = re.compile(r'^treatment\s*[:\-]\s*(.+)$', re.IGNORECASE)
                med_keywords_re = re.compile(r'finasteride|minoxidil|ketoconazole|biotin|spray|solution|cmpd|compound|%|topical', re.IGNORECASE)
                frequency_hint_re = re.compile(r'\b(daily|weekly|monthly|every|q\d+h|nightly|bedtime|hs|qhs|qam|qpm|bid|tid|qid|once|twice|per\s+\w+|dose[s]?\s+per|each\s+\w+)\b', re.IGNORECASE)
                treatment_line = None
                treatment_frequency_hint = None
                def find_next_non_header_line(start_idx):
                    idx = start_idx
                    while idx < len(lines):
                        candidate = lines[idx].strip()
                        if not candidate:
                            idx += 1; continue
                        if header_pattern.match(candidate):
                            idx += 1; continue
                        if re.match(r'^(photos|notes|click an image|intake forms)\b', candidate, re.IGNORECASE):
                            idx += 1; continue
                        return idx, candidate
                    return None, None
                for idx, ln in enumerate(lines):
                    inline_match = treatment_inline_re.match(ln)
                    if inline_match:
                        candidate = inline_match.group(1).strip()
                        if candidate:
                            treatment_line = candidate
                        break
                    if treatment_header_re.match(ln):
                        next_idx, candidate = find_next_non_header_line(idx + 1)
                        if candidate:
                            treatment_line = candidate
                            if next_idx is not None:
                                _, freq_candidate = find_next_non_header_line(next_idx + 1)
                                if freq_candidate and frequency_hint_re.search(freq_candidate):
                                    treatment_frequency_hint = freq_candidate
                        break
                # HSX symptoms
                hsx_symptoms = []
                specific_symptoms = [
                    r'general thinning or shedding', r'thinning at temples',
                    r'thinning at the hairline', r'thinning on the top of the head',
                    r'bald patches, smooth and hairless not at the top of the head',
                    r'redness and irritation found at sites of hair loss',
                    r"i'll take a photo of my head instead"
                ]
                for line in lines:
                    line_clean = line.strip()
                    if line_clean and not line_clean.startswith('Patient selected'):
                        for pattern in specific_symptoms:
                            m = re.search(pattern, line_clean, re.IGNORECASE)
                            if m and m.group(0) not in hsx_symptoms:
                                hsx_symptoms.append(m.group(0))
                hsx = ', '.join(hsx_symptoms)
                # Med extraction
                def find_next_med_like(start_idx, lookahead=8):
                    for k in range(start_idx, min(start_idx + lookahead, len(lines))):
                        candidate = lines[k].strip()
                        if not candidate: continue
                        if header_pattern.match(candidate): continue
                        if med_keywords_re.search(candidate) or '%' in candidate:
                            return candidate
                        if len(candidate.split()) >= 3 and re.search(r'[A-Za-z0-9]', candidate):
                            return candidate
                    return None
                for i, ln in enumerate(lines):
                    if treatment_header_re.search(ln):
                        candidate = find_next_med_like(i + 1, 8)
                        if candidate:
                            med = candidate.strip().strip('*').strip()
                        break
                if not med:
                    for i, ln in enumerate(lines):
                        if re.search(r'^(treatment|medication|current treatment|meds)[:\-\s]', ln, re.IGNORECASE):
                            candidate = find_next_med_like(i + 1, 6)
                            if candidate:
                                med = candidate.strip().strip('*').strip()
                            break
                if not med:
                    for ln in lines:
                        if re.search(r'finasteride|minoxidil|dutasteride|spironolactone|topical', ln, re.IGNORECASE):
                            med = ln; break
                if treatment_line:
                    med = treatment_line.strip().strip('*').strip()
                if med and header_pattern.search(med):
                    for k in range(lines.index(med) + 1, len(lines)):
                        if not header_pattern.search(lines[k]):
                            med = lines[k]; break
                append_daily = True
                if med and frequency_hint_re.search(med):
                    append_daily = False
                if append_daily and treatment_frequency_hint and frequency_hint_re.search(treatment_frequency_hint):
                    append_daily = False
                if append_daily and med:
                    med = med.rstrip(' .') + ' daily'
                # Response
                for i, ln in enumerate(lines):
                    if re.search(r'how has your treatment affected your hair loss', ln, re.IGNORECASE):
                        for j in range(i + 1, min(i + 4, len(lines))):
                            candidate = lines[j].strip()
                            if candidate and len(candidate) > 3:
                                hvar = candidate; break
                        break
                if not hvar:
                    response_patterns = [r'good response|excellent response', r'no change|no improvement|same|stable',
                                         r'worse|getting worse', r'improved|better|improvement',
                                         r'minimal.*response', r'significant.*improvement',
                                         r'side effects|stopped.*due', r'continued.*improvement']
                    for ln in lines:
                        for pat in response_patterns:
                            if re.search(pat, ln, re.IGNORECASE):
                                hvar = ln.strip(); break
                        if hvar: break
                if not hvar:
                    for ln in lines:
                        if re.search(r'hair.*(?:better|worse|same|improved|stable|thicker|thinner)', ln, re.IGNORECASE) or \
                           re.search(r'(?:better|worse|same|improved|stable|thicker|thinner).*hair', ln, re.IGNORECASE):
                            hvar = ln.strip(); break
                _on_main(lambda: self.hair_med_text.setText(med))
                _on_main(lambda: self.hair_hvar_text.setText(hvar))
                _on_main(lambda: self.hair_hsx_text.setText(hsx))
                emit_emr_bridge({"context": "hair", "hair_med": med, "hair_response": hvar, "hair_symptoms": hsx})
                pyperclip.copy(original)
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
            except Exception as e:
                pyperclip.copy(original)
                _on_main(lambda: QMessageBox.critical(self, "Error", f"Error during hair grab: {e}"))
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_photoaging(self):
        """Grab photoaging data from EMR."""
        def do_grab():
            try:
                original = ""
                try:
                    original = pyperclip.paste()
                except Exception:
                    pass
                new_content = pyperclip.paste()
                if not new_content or len(new_content) < 20:
                    _on_main(lambda: QMessageBox.warning(self, "Error", "Failed to grab photoaging text."))
                    pyperclip.copy(original)
                    _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
                    return
                lines = [ln.strip() for ln in new_content.splitlines() if ln.strip()]
                med = ''
                skin_goals = ''
                retinoid_history = ''
                med_kw = re.compile(r'tretinoin|niacinamide|azelaic.*acid|retinoid|aging.*rx|custom.*formula', re.IGNORECASE)
                for i, ln in enumerate(lines):
                    if re.search(r'^(treatment|aging rx|patient preference)\b', ln, re.IGNORECASE):
                        best = ''
                        for j in range(i + 1, min(i + 8, len(lines))):
                            c = lines[j].strip()
                            if len(c.split()) >= 3:
                                if re.search(r'\d+\.?\d*%.*tretinoin|tretinoin.*\d+\.?\d*%', c, re.IGNORECASE):
                                    med = c; break
                                elif re.search(r'%.*%', c) and med_kw.search(c):
                                    best = c
                                elif med_kw.search(c) and not best:
                                    best = c
                        if not med and best:
                            med = best
                        if med: break
                for i, ln in enumerate(lines):
                    if re.search(r'patient.*skin.*care.*goals?', ln, re.IGNORECASE):
                        for j in range(i + 1, min(i + 4, len(lines))):
                            c = lines[j].strip()
                            if c and not re.match(r'^(patient|treatment|aging)', c, re.IGNORECASE):
                                skin_goals = c; break
                        break
                for ln in lines:
                    if re.search(r'retin-a|tretinoin|retinoid', ln, re.IGNORECASE) and not re.search(r'^treatment|^aging', ln, re.IGNORECASE):
                        if re.search(r'used|previously|past|been', ln, re.IGNORECASE):
                            retinoid_history = "Previously used Retin-A"; break
                        elif re.search(r'prescription retinoid', ln, re.IGNORECASE):
                            retinoid_history = "Has used prescription retinoids"; break
                if not retinoid_history:
                    for ln in lines:
                        if re.search(r'over-the-counter.*product|men.*skin.*cream|skincare', ln, re.IGNORECASE):
                            retinoid_history = "Has used OTC skincare products"; break
                if med and not re.search(r'\b(daily|bi-monthly|monthly|weekly)\b', med, re.IGNORECASE):
                    med = med.rstrip(' .') + ' daily'
                _on_main(lambda: self.photoaging_med_text.setText(med))
                _on_main(lambda: self.photoaging_goals_text.setText(skin_goals))
                _on_main(lambda: self.photoaging_retinoid_text.setText(retinoid_history))
                emit_emr_bridge({"context": "photoaging", "photoaging_med": med, "photoaging_goals": skin_goals, "photoaging_retinoid_history": retinoid_history})
                pyperclip.copy(original)
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
            except Exception as e:
                pyperclip.copy(original)
                _on_main(lambda: QMessageBox.critical(self, "Error", f"Error during photoaging grab: {e}"))
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_performance_anxiety(self):
        """Grab Performance Anxiety fields via browser."""
        def do_grab():
            try:
                grabber = None
                try:
                    if self._browser_grabber_cache is None:
                        self._browser_grabber_cache = BrowserEMRGrabber()
                        self._browser_grabber_cache.connect_to_chrome()
                    grabber = self._browser_grabber_cache
                except Exception:
                    grabber = BrowserEMRGrabber()
                    grabber.connect_to_chrome()
                if not grabber or not grabber.driver:
                    _on_main(lambda: QMessageBox.warning(self, "Browser Error", f"Could not attach to browser. Ensure Chrome is running with --remote-debugging-port={CDP_DEBUG_PORT}."))
                    return
                data = grabber.grab_performance_anxiety_data() or {}
                med = data.get('medication', '')
                situations = data.get('situations_text', '')
                symptoms = data.get('symptoms_text', '')
                bp = data.get('blood_pressure', 'nr')
                pulse = data.get('pulse', 'nr')
                _on_main(lambda: self.pa_med_text.setText(med))
                _on_main(lambda: self.pa_situations_text.setPlainText(situations))
                _on_main(lambda: self.pa_symptoms_text.setPlainText(symptoms))
                _on_main(lambda: self.pa_bp_text.setText(bp))
                _on_main(lambda: self.pa_pulse_text.setText(pulse))
                emit_emr_bridge({"context": "performance_anxiety", "pa_med": med, "pa_situations": situations, "pa_symptoms": symptoms, "pa_bp": bp, "pa_pulse": pulse})
                self.refresh_patient_location_async()
            except Exception as e:
                _on_main(lambda: QMessageBox.critical(self, "Error", f"Error during PA grab: {e}"))
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_birth_control(self):
        """Grab Birth Control data via browser."""
        def do_grab():
            try:
                grabber = None
                try:
                    if self._browser_grabber_cache is None:
                        self._browser_grabber_cache = BrowserEMRGrabber()
                        self._browser_grabber_cache.connect_to_chrome()
                    grabber = self._browser_grabber_cache
                except Exception:
                    grabber = BrowserEMRGrabber()
                    grabber.connect_to_chrome()
                if not grabber or not grabber.driver:
                    _on_main(lambda: QMessageBox.warning(self, "Browser Error", f"Could not attach to browser."))
                    return
                data = grabber.grab_birth_control_data() or {}
                med = data.get('medication', '')
                lmp = data.get('lmp', '')
                bp = data.get('blood_pressure', 'nr')
                side_effects = data.get('side_effects_text', '')
                pmh = data.get('pmh_list', [])
                pmh_other = data.get('pmh_other', '')
                initial_visit = data.get('initial_visit', True)
                med_history_changes = data.get('med_history_changes', '')
                _on_main(lambda: self.bc_med_text.setText(med))
                _on_main(lambda: self.bc_lmp_text.setText(lmp))
                _on_main(lambda: self.bc_bp_text.setText(bp))
                _on_main(lambda: self.bc_side_effects_text.setPlainText(side_effects or 'none'))
                _on_main(lambda: self.bc_history_changes_text.setText(med_history_changes))
                _on_main(lambda: self.bc_initial_rb.setChecked(bool(initial_visit)))
                _on_main(lambda: self.bc_followup_rb.setChecked(not bool(initial_visit)))
                _on_main(lambda: self._apply_birth_control_pmh(pmh, pmh_other))
                emit_emr_bridge({"context": "birth_control", "bc_med": med, "bc_lmp": lmp, "bc_bp": bp, "bc_side_effects": side_effects, "bc_pmh_list": pmh, "bc_pmh_other": pmh_other, "bc_initial_visit": bool(initial_visit), "bc_med_history_changes": med_history_changes})
                _on_main(lambda: self.grab_status_text.setText("Updated: Birth Control"))
                self.refresh_patient_location_async()
            except Exception as exc:
                _on_main(lambda: QMessageBox.critical(self, "Error", f"Error during BC grab: {exc}"))
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_sexual_health(self):
        """Browser-based Sexual Health data extraction."""
        self.hide()
        time.sleep(0.1)
        def do_grab():
            try:
                original = ""
                try:
                    original = pyperclip.paste()
                except Exception:
                    pass
                mode_sel = None
                try:
                    mode_sel = self.sexual_health_mode_choice.currentText()
                except Exception:
                    pass
                if mode_sel and mode_sel.startswith("Clipboard"):
                    try:
                        sw, sh = pyautogui.size()
                        cx, cy = sw // 2, sh // 2
                        pyautogui.click(cx, cy)
                        time.sleep(0.25)
                        pyautogui.hotkey('ctrl', 'a'); time.sleep(0.15)
                        pyautogui.hotkey('ctrl', 'c'); time.sleep(0.35)
                        clip_text = pyperclip.paste() or ''
                        clear_text_selection(self)
                    finally:
                        pyperclip.copy(original)
                        _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
                    if not clip_text or len(clip_text) < 30:
                        _on_main(lambda: QMessageBox.warning(self, "Error", "Clipboard grab failed."))
                        return
                    grabber = BrowserEMRGrabber()
                    grabber._cache_body_text = clip_text
                    grabber._cache_all_text = clip_text
                    grabber._cache_latest_segment = clip_text
                    med = grabber._extract_medication_from_text(clip_text)
                    effectiveness = grabber._extract_effectiveness(full_text=clip_text) or ''
                    bp_var = self._normalize_sexual_health_bp(grabber._extract_blood_pressure())
                    detected_diagnoses = grabber._extract_diagnoses() or []
                    _on_main(lambda: self.sexual_health_med_text.setText(med))
                    _on_main(lambda: self.sexual_health_effectiveness_text.setText(effectiveness))
                    _on_main(lambda: self.sexual_health_bp_text.setText(bp_var))
                    self._apply_sexual_health_diagnoses(detected_diagnoses)
                    emit_emr_bridge({"context": "sexual_health", "sexual_health_med": med, "sexual_health_effectiveness": effectiveness, "sexual_health_bp": bp_var, "sexual_health_diagnoses": detected_diagnoses})
                    self.refresh_patient_location_async()
                    return
                grabber = None
                t0 = time.perf_counter()
                try:
                    if self._browser_grabber_cache is None:
                        self._browser_grabber_cache = BrowserEMRGrabber()
                        self._browser_grabber_cache.connect_to_chrome()
                    grabber = self._browser_grabber_cache
                except Exception:
                    grabber = BrowserEMRGrabber()
                    grabber.connect_to_chrome()
                data = grabber.grab_sexual_health_data()
                if data:
                    med = data.get('medication', '')
                    effectiveness = data.get('effectiveness', '')
                    bp_var = self._normalize_sexual_health_bp(data.get('blood_pressure'))
                    detected_diagnoses = data.get('diagnoses', [])
                    hair_loc = data.get('hair_loss_location', '')
                    hair_sxx = data.get('hair_loss_additional_sxx', '')
                    _on_main(lambda: self.sexual_health_med_text.setText(med))
                    _on_main(lambda: self.sexual_health_effectiveness_text.setText(effectiveness))
                    _on_main(lambda: self.sexual_health_bp_text.setText(bp_var))
                    _on_main(lambda: self.sexual_health_hair_location_text.setText(hair_loc))
                    _on_main(lambda: self.sexual_health_hair_sxx_text.setText(hair_sxx))
                    grabbed_vars['hair_loss_location'] = hair_loc
                    grabbed_vars['hair_loss_additional_sxx'] = hair_sxx
                    if med and ('finasteride' in med.lower() or 'minoxidil' in med.lower()):
                        if 'Hair Loss' not in detected_diagnoses:
                            detected_diagnoses.append('Hair Loss')
                    self._apply_sexual_health_diagnoses(detected_diagnoses)
                    emit_emr_bridge({"context": "sexual_health", "sexual_health_med": med, "sexual_health_effectiveness": effectiveness, "sexual_health_bp": bp_var, "sexual_health_diagnoses": detected_diagnoses, "hair_loss_location": hair_loc, "hair_loss_additional_sxx": hair_sxx})
                else:
                    _on_main(lambda: QMessageBox.warning(self, "Browser Error", f"Browser grab failed."))
                self.refresh_patient_location_async()
                t1 = time.perf_counter()
                dprint(f"Sexual Health grab total time: {(t1 - t0)*1000:.0f} ms")
                pyperclip.copy(original)
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
            except Exception as e:
                pyperclip.copy(original)
                _on_main(lambda: QMessageBox.critical(self, "Error", f"Error during sexual health grab: {e}"))
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
        threading.Thread(target=do_grab, daemon=True).start()

    def _normalize_sexual_health_bp(self, bp_value: Optional[str]) -> str:
        text = (bp_value or '').strip()
        if not text:
            return 'not required'
        if text.lower() in ('nr', 'n/r', 'not required'):
            return 'not required'
        return text

    def _apply_sexual_health_diagnoses(self, detected: Optional[List[str]]):
        canonical_order = ["ED", "PE", "PE-like ejaculatory dysfunction", "Hair Loss"]
        normalized: List[str] = []
        for item in detected or []:
            if not item: continue
            low = item.strip().lower()
            for c in canonical_order:
                if low == c.lower() and c not in normalized:
                    normalized.append(c); break
        def _apply():
            try:
                self._suppress_sexual_health_dx_event = True
                if self._should_autoselect_hair_loss_from_medication() and "Hair Loss" not in normalized:
                    normalized.append("Hair Loss")
                self.sexual_health_dx_ed.setChecked("ED" in normalized)
                self.sexual_health_dx_pe.setChecked("PE" in normalized)
                self.sexual_health_dx_pe_like.setChecked("PE-like ejaculatory dysfunction" in normalized)
                self.sexual_health_dx_hair_loss.setChecked("Hair Loss" in normalized)
            finally:
                self._suppress_sexual_health_dx_event = False
            diagnoses[0] = ", ".join(normalized)
        _on_main(_apply)

    def _should_autoselect_hair_loss_from_medication(self) -> bool:
        kw = ("finasteride", "minoxidil")
        candidates = []
        try:
            candidates.append(self.sexual_health_med_text.text())
        except Exception:
            pass
        candidates.append(medication_value[0])
        for text in candidates:
            if text and any(k in text.lower() for k in kw):
                return True
        return False

    # ==================================================================
    # Insert / template methods
    # ==================================================================
    def insert_template_at_cursor(self, template_text: str) -> None:
        type_template_text(template_text)

    def insert_template(self, template_name: str) -> None:
        templates = load_templates()
        tpl = templates.get(template_name)
        if not tpl:
            QMessageBox.warning(self, "Template", f"Template '{template_name}' not found.")
            return
        self.hide()
        time.sleep(0.15)
        type_template_text(tpl)
        QTimer.singleShot(500, self.show)

    def insert_specific_template(self, template_name: str) -> None:
        templates = load_templates()
        tpl = templates.get(template_name)
        if not tpl:
            QMessageBox.warning(self, "Template", f"Template '{template_name}' not found.")
            return
        self.hide()
        time.sleep(0.15)
        type_template_text(tpl)
        QTimer.singleShot(500, self.show)

    def insert_hair_note(self):
        med = self.hair_med_text.text().strip()
        hvar = self.hair_hvar_text.text().strip()
        hsx_lines = self.hair_hsx_text.text().strip()
        exam_parts = []
        for cb_name, label in [("hair_temporal_cb", "Temporal recession"), ("hair_midline_cb", "Midline thinning"),
                               ("hair_frontal_cb", "Frontal recession"), ("hair_diffuse_cb", "Diffuse thinning"),
                               ("hair_vertex_cb", "Vertex thinning"), ("hair_miniaturization_cb", "Miniaturization")]:
            cb = getattr(self, cb_name, None)
            if cb and cb.isChecked():
                exam_parts.append(label)
        exam = ", ".join(exam_parts) if exam_parts else "No significant findings"
        note = f"Medication: {med}\n"
        note += f"Response: {hvar}\n"
        note += f"Hair symptoms: {hsx_lines}\n" if hsx_lines else ""
        note += f"Exam: {exam}\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def insert_hair_limited_checkin_note(self):
        med = self.hair_med_text.text().strip()
        hvar = self.hair_hvar_text.text().strip()
        note = f"Medication: {med}\n"
        note += f"Response: {hvar}\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def insert_hair_info(self):
        templates = load_templates()
        tpl = templates.get("Hair Info")
        if tpl:
            self.hide()
            time.sleep(0.15)
            type_template_text(tpl)
            QTimer.singleShot(500, self.show)

    def insert_photoaging_note(self):
        med = self.photoaging_med_text.text().strip()
        goals = self.photoaging_goals_text.text().strip()
        retinoid = self.photoaging_retinoid_text.text().strip()
        exam_parts = []
        for cb_name, label in [
            ("pa_fine_lines_cb", "Fine lines"), ("pa_wrinkles_cb", "Wrinkles"),
            ("pa_rough_texture_cb", "Rough texture"), ("pa_hyperpigmentation_cb", "Hyperpigmentation"),
            ("pa_lentigines_cb", "Lentigines"), ("pa_sallow_cb", "Sallowness"),
            ("pa_dryness_cb", "Dryness"), ("pa_loss_elasticity_cb", "Loss of elasticity"),
            ("pa_telangiectasia_cb", "Telangiectasia"), ("pa_actinic_keratosis_cb", "Actinic keratosis"),
            ("pa_mottled_cb", "Mottled pigmentation"), ("pa_atrophy_cb", "Cutaneous atrophy")]:
            cb = getattr(self, cb_name, None)
            if cb and cb.isChecked():
                exam_parts.append(label)
        exam = ", ".join(exam_parts) if exam_parts else "No significant findings"
        note = f"Medication: {med}\n"
        note += f"Skin care goals: {goals}\n" if goals else ""
        note += f"Retinoid history: {retinoid}\n" if retinoid else ""
        note += f"Exam: {exam}\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def insert_performance_anxiety_note(self):
        med = self.pa_med_text.text().strip()
        situations = self.pa_situations_text.toPlainText().strip()
        symptoms = self.pa_symptoms_text.toPlainText().strip()
        bp_val = self.pa_bp_text.text().strip()
        pulse_val = self.pa_pulse_text.text().strip()
        response_text = ""
        for rb in [self.pa_response_effective_rb, self.pa_response_partial_rb, self.pa_response_ineffective_rb]:
            if rb.isChecked():
                response_text = rb.text(); break
        se_text = ""
        for rb in [self.pa_se_none_rb, self.pa_se_mild_rb, self.pa_se_moderate_rb]:
            if rb.isChecked():
                se_text = rb.text(); break
        note = f"Medication: {med}\n"
        vitals = []
        if bp_val and bp_val.lower() not in ('nr', 'not required'):
            vitals.append(f"BP {bp_val}")
        if pulse_val and pulse_val.lower() not in ('nr', 'not required'):
            vitals.append(f"Pulse {pulse_val}")
        if vitals:
            note += "Vitals: " + ", ".join(vitals) + "\n"
        if situations:
            note += f"Situations: {situations}\n"
        if symptoms:
            note += f"Symptoms: {symptoms}\n"
        if response_text:
            note += f"Response: {response_text}\n"
        if se_text:
            note += f"Side effects: {se_text}\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def _apply_birth_control_pmh(self, pmh_list: Optional[List[str]], pmh_other: str = ""):
        for opt_label, cb in self.bc_pmh_checkboxes.items():
            cb.setChecked(opt_label in (pmh_list or []))
        if pmh_other:
            self.bc_pmh_other_text.setText(pmh_other)

    def _collect_birth_control_pmh(self) -> List[str]:
        selected = []
        for opt_label, cb in self.bc_pmh_checkboxes.items():
            if cb.isChecked():
                selected.append(opt_label)
        other_text = self.bc_pmh_other_text.text().strip()
        if other_text:
            selected.append(f"Other: {other_text}")
        return selected

    def _format_birth_control_pmh(self, pmh_list: List[str]) -> str:
        if not pmh_list:
            return "None reported"
        return ", ".join(pmh_list)

    def _normalize_birth_control_side_effects(self, se_text: str) -> str:
        text = (se_text or '').strip().lower()
        if not text or text == 'none' or text == 'no':
            return 'none'
        return se_text.strip()

    def insert_birth_control_initial_note(self):
        med = self.bc_med_text.text().strip()
        lmp = self.bc_lmp_text.text().strip()
        bp_val = self.bc_bp_text.text().strip()
        se = self._normalize_birth_control_side_effects(self.bc_side_effects_text.toPlainText())
        pmh_list = self._collect_birth_control_pmh()
        pmh_text = self._format_birth_control_pmh(pmh_list)
        note = f"Medication: {med}\n"
        note += f"LMP: {lmp}\n" if lmp else ""
        note += f"BP: {bp_val}\n" if bp_val and bp_val.lower() not in ('nr',) else ""
        note += f"PMH: {pmh_text}\n"
        note += f"Side effects: {se}\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def insert_birth_control_followup_note(self):
        med = self.bc_med_text.text().strip()
        bp_val = self.bc_bp_text.text().strip()
        lmp = self.bc_lmp_text.text().strip()
        se = self._normalize_birth_control_side_effects(self.bc_side_effects_text.toPlainText())
        changes = self.bc_history_changes_text.text().strip()
        note = f"Medication: {med}\n"
        note += f"LMP: {lmp}\n" if lmp else ""
        note += f"BP: {bp_val}\n" if bp_val and bp_val.lower() not in ('nr',) else ""
        if changes:
            note += f"Changes since last visit: {changes}\n"
        note += f"Side effects: {se}\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def _get_sh_dx_text(self) -> str:
        parts = []
        if self.sexual_health_dx_ed.isChecked():
            parts.append("ED")
        if self.sexual_health_dx_pe.isChecked():
            parts.append("PE")
        if self.sexual_health_dx_pe_like.isChecked():
            parts.append("PE-like ejaculatory dysfunction")
        if self.sexual_health_dx_hair_loss.isChecked():
            parts.append("Hair Loss")
        return ", ".join(parts)

    def _strip_frequency_suffix(self, text: str) -> str:
        freq_re = re.compile(r'\s*(daily|twice daily|weekly|bi-weekly|monthly|nightly|qhs|hs|qam|qpm|bid|tid|qid|once a day|every day|each day|per day|as needed|prn)\s*$', re.IGNORECASE)
        return freq_re.sub('', text).strip()

    def _normalize_phrase(self, text: str) -> str:
        return re.sub(r'[\s,\.\-]+', ' ', text).strip().lower()

    def _parse_dose_and_cadence(self, med_text: str) -> Tuple[str, str]:
        dose_re = re.compile(r'(\d+(?:\.\d+)?)\s*(mg|ml|g|mcg|units?|iu|%)', re.IGNORECASE)
        cadence_re = re.compile(r'(daily|twice daily|nightly|weekly|bi-weekly|monthly|qhs|hs|qam|qpm|bid|tid|qid|once a day|every\s+\d+\s+\w+|per day|prn|as needed)', re.IGNORECASE)
        dose_match = dose_re.search(med_text)
        cadence_match = cadence_re.search(med_text)
        dose = f"{dose_match.group(1)} {dose_match.group(2)}" if dose_match else ""
        cadence = cadence_match.group(1) if cadence_match else ""
        return dose, cadence

    def _auto_populate_sexual_health_change_fields(self):
        current_med = self.sexual_health_med_text.text().strip()
        if not current_med:
            return
        dose, cadence = self._parse_dose_and_cadence(current_med)

    def _strings_differ(self, a: str, b: str) -> bool:
        return self._normalize_phrase(a) != self._normalize_phrase(b)

    def insert_sexual_health_note(self):
        med = self.sexual_health_med_text.text().strip()
        effectiveness = self.sexual_health_effectiveness_text.text().strip()
        bp_var = self.sexual_health_bp_text.text().strip()
        dx_text = self._get_sh_dx_text()
        plan_text = self.sexual_health_plan_choice.currentText()
        hair_loc = self.sexual_health_hair_location_text.text().strip()
        hair_sxx = self.sexual_health_hair_sxx_text.text().strip()
        note = f"Medication: {med}\n"
        note += f"Effectiveness: {effectiveness}\n" if effectiveness else ""
        if bp_var and bp_var.lower() not in ('nr', 'not required', 'n/r'):
            note += f"BP: {bp_var}\n"
        if dx_text:
            note += f"Dx: {dx_text}\n"
        if "Hair Loss" in dx_text and (hair_loc or hair_sxx):
            if hair_loc:
                note += f"Hair loss location: {hair_loc}\n"
            if hair_sxx:
                note += f"Hair loss symptoms: {hair_sxx}\n"
        if plan_text and plan_text != "No change":
            note += f"Plan: {plan_text}\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def insert_sexual_health_brief_template(self):
        med = self.sexual_health_med_text.text().strip()
        effectiveness = self.sexual_health_effectiveness_text.text().strip()
        note = f"Medication: {med}\n"
        note += f"Effectiveness: {effectiveness}\n" if effectiveness else ""
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def insert_sexual_health_change_template(self):
        med = self.sexual_health_med_text.text().strip()
        effectiveness = self.sexual_health_effectiveness_text.text().strip()
        bp_var = self.sexual_health_bp_text.text().strip()
        dx_text = self._get_sh_dx_text()
        change_type = self.sexual_health_change_combo.currentText() if hasattr(self, 'sexual_health_change_combo') else ""
        change_detail = self.sexual_health_change_detail_text.text().strip() if hasattr(self, 'sexual_health_change_detail_text') else ""
        note = f"Medication: {med}\n"
        note += f"Effectiveness: {effectiveness}\n" if effectiveness else ""
        if bp_var and bp_var.lower() not in ('nr', 'not required', 'n/r'):
            note += f"BP: {bp_var}\n"
        if dx_text:
            note += f"Dx: {dx_text}\n"
        if change_type:
            note += f"Change: {change_type}"
            if change_detail:
                note += f" - {change_detail}"
            note += "\n"
        self.hide()
        time.sleep(0.15)
        type_template_text(note)
        QTimer.singleShot(500, self.show)

    def insert_sh_change_cadence(self):
        templates = load_templates()
        tpl = templates.get("SH Change Cadence", "")
        if tpl:
            self.hide()
            time.sleep(0.15)
            type_template_text(tpl)
            QTimer.singleShot(500, self.show)

    def insert_sh_change_number(self):
        templates = load_templates()
        tpl = templates.get("SH Change Number", "")
        if tpl:
            self.hide()
            time.sleep(0.15)
            type_template_text(tpl)
            QTimer.singleShot(500, self.show)

    def insert_sh_change_medication(self):
        templates = load_templates()
        tpl = templates.get("SH Change Medication", "")
        if tpl:
            self.hide()
            time.sleep(0.15)
            type_template_text(tpl)
            QTimer.singleShot(500, self.show)

    def _get_td_diagnoses_text(self) -> str:
        """Build T Deficiency diagnosis string from T-tab checkboxes only."""
        parts = []
        if self.dx_td_cb.isChecked():
            parts.append("Testosterone Deficiency")
        if self.dx_ed_cb.isChecked():
            parts.append("ED")
        return ", ".join(parts)

    def _get_td_ed_status_sentence(self) -> str:
        """Convert the ED field display value to the full sentence for templates."""
        ed_val = self.ed_text.text().strip()
        if ed_val == "Yes":
            return "He reports symptoms consistent with ED."
        elif ed_val == "No":
            return "He denies symptoms of ED."
        return "\u2014"

    def _build_lab_values_formatted(self) -> str:
        """Build formatted lab values string from T-tab widgets."""
        lines = []
        for lab_name, lab_config in LABS_CONFIG.items():
            var_name = lab_config["var"]
            ctrl = self.text_ctrls.get(var_name)
            value = ctrl.text().strip() if ctrl else ""
            if value:
                suffix = ""
                high_cb = self.check_ctrls.get(var_name, (None, None))[0]
                low_cb = self.check_ctrls.get(var_name, (None, None))[1]
                if high_cb and high_cb.isChecked():
                    suffix = " (high)"
                elif low_cb and low_cb.isChecked():
                    suffix = " (low)"
                lines.append(f"{lab_name}: {value} {lab_config['unit']}{suffix}")
        return "\n".join(lines) if lines else "(none grabbed)"

    def insert_td_followup_note(self):
        """Insert Testosterone Deficiency Follow-up template with variable substitution."""
        templates = load_templates()
        tpl = templates.get("Testosterone Deficiency Follow-up")
        if tpl:
            try:
                text = tpl.format(
                    response=self.td_response_text.text().strip(),
                    side_effects=self.td_side_effects_text.text().strip(),
                    tdcs_c=self.tdcs_c_text.text().strip(),
                    total_testosterone=self.text_ctrls.get('total_testosterone', QLineEdit()).text().strip(),
                    psa=self.text_ctrls.get('psa', QLineEdit()).text().strip(),
                    estradiol=self.text_ctrls.get('estradiol', QLineEdit()).text().strip(),
                    hematocrit=self.text_ctrls.get('hematocrit', QLineEdit()).text().strip(),
                    diagnoses=self._get_td_diagnoses_text(),
                    medication=self.td_med_text.text().strip(),
                )
            except KeyError as e:
                QMessageBox.warning(self, "Template Error", f"Missing variable: {e}")
                return
        else:
            text = "Testosterone Deficiency Follow-up template missing"
        self.hide()
        time.sleep(0.15)
        type_template_text(text)
        QTimer.singleShot(500, self.show)

    def insert_td_followup_labs(self):
        """Insert Testosterone Follow-up Labs template with variable substitution."""
        templates = load_templates()
        tpl = templates.get("Testosterone Follow-up Labs")
        if tpl:
            try:
                text = tpl.format(
                    total_testosterone=self.text_ctrls.get('total_testosterone', QLineEdit()).text().strip(),
                    estradiol=self.text_ctrls.get('estradiol', QLineEdit()).text().strip(),
                    hematocrit=self.text_ctrls.get('hematocrit', QLineEdit()).text().strip(),
                    psa=self.text_ctrls.get('psa', QLineEdit()).text().strip(),
                    medication=self.td_med_text.text().strip(),
                )
            except KeyError as e:
                QMessageBox.warning(self, "Template Error", f"Missing variable: {e}")
                return
        else:
            text = "Testosterone Follow-up Labs template missing"
        self.hide()
        time.sleep(0.15)
        type_template_text(text)
        QTimer.singleShot(500, self.show)

    def insert_labs_note(self):
        """Insert formatted lab values."""
        templates = load_templates()
        tpl = templates.get("Insert Labs")
        if tpl:
            text = tpl.format(lab_values_formatted=self._build_lab_values_formatted())
        else:
            text = "Labs: (template missing)"
        self.hide()
        time.sleep(0.15)
        type_template_text(text)
        QTimer.singleShot(500, self.show)

    def insert_rx_note(self):
        """Insert Rx Note template with lab values and diagnosis info."""
        templates = load_templates()
        tpl = templates.get("Rx Note")
        if tpl:
            text = tpl.format(
                tdcs=self.tdcs_text.text().strip(),
                tdcs_c=self.tdcs_c_text.text().strip(),
                ed_status=self._get_td_ed_status_sentence(),
                pmh=build_pmh_text(),
                lab_values_formatted=self._build_lab_values_formatted(),
                diagnoses=self._get_td_diagnoses_text(),
                medication=self.td_med_text.text().strip(),
            )
        else:
            text = "Rx Note template missing"
        self.hide()
        time.sleep(0.15)
        type_template_text(text)
        QTimer.singleShot(500, self.show)

    def insert_referral_note(self):
        """Insert Referral Note template."""
        templates = load_templates()
        tpl = templates.get("Referral Note") or templates.get("Referral note")
        if tpl:
            try:
                text = tpl.format(
                    tdcs=self.tdcs_text.text().strip(),
                    ed_status=self._get_td_ed_status_sentence(),
                    pmh=build_pmh_text(),
                    lab_values_formatted=self._build_lab_values_formatted(),
                    diagnoses=self._get_td_diagnoses_text(),
                    medication=self.td_med_text.text().strip(),
                )
            except KeyError as e:
                QMessageBox.warning(self, "Template Error", f"Missing variable: {e}")
                return
        else:
            text = "Referral Note template missing"
        self.hide()
        time.sleep(0.15)
        type_template_text(text)
        QTimer.singleShot(500, self.show)

    def insert_lab_message(self):
        """Insert Lab Message template."""
        templates = load_templates()
        tpl = templates.get("Lab Message")
        if tpl:
            text = tpl.format(
                total_testosterone=self.text_ctrls.get('total_testosterone', QLineEdit()).text().strip(),
                free_testosterone=self.text_ctrls.get('free_testosterone', QLineEdit()).text().strip(),
                medication=self.td_med_text.text().strip(),
            )
        else:
            text = "Lab Message template missing"
        self.hide()
        time.sleep(0.15)
        type_template_text(text)
        QTimer.singleShot(500, self.show)

    # ==================================================================
    # UI event handlers for state management
    # ==================================================================
    def on_dx_checkbox(self, state):
        # T-tab diagnosis is now read directly from checkboxes via
        # _get_td_diagnoses_text() at insert time.  We still update
        # the shared diagnoses[0] for backwards compat with EMR bridge,
        # but template inserts no longer read it.
        parts = []
        if self.dx_td_cb.isChecked():
            parts.append("Testosterone Deficiency")
        if self.dx_ed_cb.isChecked():
            parts.append("ED")
        diagnoses[0] = ", ".join(parts)

    def on_sexual_health_dx_checkbox(self, state):
        if getattr(self, '_suppress_sexual_health_dx_event', False):
            return
        diagnoses[0] = self._get_sh_dx_text()

    def open_pmh_dialog(self):
        from .dialogs.pmh_dialog import PMHDialog
        dlg = PMHDialog(PMH_OPTIONS, pmh_selected, parent=self)
        if dlg.exec():
            pmh_selected.clear()
            pmh_selected.extend(dlg.get_selected())
            self.update_pmh_summary()

    def update_pmh_summary(self):
        if pmh_selected:
            self.pmh_summary.setText(", ".join(pmh_selected))
        else:
            self.pmh_summary.setText("None selected")

    def clear_all(self):
        """Clear all T Deficiency tab fields only."""
        for var_name, ctrl in self.text_ctrls.items():
            ctrl.setText("")
            grabbed_vars[var_name] = ""
        self.tdcs_text.setText("")
        self.tdcs_c_text.setText("")
        self.ed_text.setText("")
        tdcs_value[0] = ""
        tdcs_c_value[0] = ""
        ed_value[0] = ""
        td_satisfaction_value[0] = ""
        td_side_effects_value[0] = ""
        self.td_med_text.setText("")
        self.td_response_text.setText("")
        self.td_side_effects_text.setText("No side effects reported")
        medication_value[0] = ""
        self.dx_td_cb.setChecked(False)
        self.dx_ed_cb.setChecked(False)
        diagnoses[0] = ""
        pmh_selected.clear()
        self.update_pmh_summary()

    def show_clinical_matrix(self):
        templates = load_templates()
        tpl = templates.get("Clinical Matrix")
        if tpl:
            self.hide()
            time.sleep(0.15)
            type_template_text(tpl)
            QTimer.singleShot(500, self.show)

    def update_ui_after_grab(self, data: dict):
        """Update all UI fields from a data dictionary."""
        for key, value in data.items():
            if key in self.text_ctrls:
                ctrl = self.text_ctrls[key]
                if isinstance(ctrl, QPlainTextEdit):
                    ctrl.setPlainText(str(value))
                else:
                    ctrl.setText(str(value))
            elif hasattr(self, key):
                widget = getattr(self, key)
                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QComboBox):
                    widget.setCurrentText(str(value))
                elif isinstance(widget, QPlainTextEdit):
                    widget.setPlainText(str(value))
                elif isinstance(widget, (QLineEdit, QLabel)):
                    widget.setText(str(value))

    def update_sexual_health_fields(self, data: dict):
        """Update sexual health tab fields."""
        if 'medication' in data:
            self.sexual_health_med_text.setText(data['medication'])
        if 'effectiveness' in data:
            self.sexual_health_effectiveness_text.setText(data['effectiveness'])
        if 'blood_pressure' in data:
            self.sexual_health_bp_text.setText(self._normalize_sexual_health_bp(data['blood_pressure']))
        if 'diagnoses' in data:
            self._apply_sexual_health_diagnoses(data['diagnoses'])
        if 'hair_loss_location' in data:
            self.sexual_health_hair_location_text.setText(data['hair_loss_location'])
        if 'hair_loss_additional_sxx' in data:
            self.sexual_health_hair_sxx_text.setText(data['hair_loss_additional_sxx'])

    def detect_visit_type_and_switch_tab(self) -> None:
        """Use CDP or page analysis to detect the visit type, then switch to the correct tab."""
        def worker():
            try:
                if not hasattr(self, '_browser_grabber_cache') or self._browser_grabber_cache is None:
                    self._browser_grabber_cache = BrowserEMRGrabber()
                grabber = self._browser_grabber_cache
                if not grabber.connect_to_chrome():
                    return
                url = grabber._get_current_url() or ""
                text = grabber._get_page_text() or ""
                canonical = None
                # Check URL patterns
                if 'sexual' in url.lower() or 'ed' in url.lower():
                    canonical = "Sexual Health"
                elif 'hair' in url.lower():
                    canonical = "Hair Loss"
                elif 'photo' in url.lower() or 'aging' in url.lower():
                    canonical = "Photoaging"
                elif 'anxiety' in url.lower() or 'performance' in url.lower():
                    canonical = "Performance Anxiety"
                elif 'birth' in url.lower() or 'contracepti' in url.lower():
                    canonical = "Birth Control"
                elif 'testosterone' in url.lower() or 'trt' in url.lower():
                    canonical = "T Deficiency"
                if not canonical:
                    for label, idx in VISIT_TAB_INDICES.items():
                        if label.lower() in text.lower():
                            canonical = label; break
                if canonical:
                    self._schedule_tab_switch(canonical, f"Detected: {canonical}")
            except Exception as exc:
                print(f"detect_visit_type_and_switch_tab error: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def universal_grab(self):
        """Grab data appropriate for the current tab."""
        tab = self.notebook.currentIndex()
        if tab == 0:
            self.grab_all_labs()
        elif tab == 1:
            self.grab_hair()
        elif tab == 2:
            self.grab_photoaging()
        elif tab == 3:
            self.grab_sexual_health()
        elif tab == 5:
            self.grab_performance_anxiety()
        elif tab == 6:
            self.grab_birth_control()

    def background_emr_grab(self):
        """Grab EMR data in background and update UI."""
        def worker():
            try:
                if not hasattr(self, '_browser_grabber_cache') or self._browser_grabber_cache is None:
                    self._browser_grabber_cache = BrowserEMRGrabber()
                grabber = self._browser_grabber_cache
                if not grabber.connect_to_chrome():
                    return
                data = grabber.grab_sexual_health_data()
                if data:
                    _on_main(lambda: self.update_sexual_health_fields(data))
            except Exception as exc:
                print(f"background_emr_grab error: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def toggle_auto_refresh(self):
        self._auto_refresh_active = not self._auto_refresh_active
        if self._auto_refresh_active:
            self.auto_refresh_loop()

    def auto_refresh_loop(self):
        if not self._auto_refresh_active or self._is_closing:
            return
        self.background_emr_grab()
        QTimer.singleShot(10000, self.auto_refresh_loop)

    # ==================================================================
    # Auto-clicker methods
    # ==================================================================
    def toggle_auto_clicker(self):
        if self._auto_clicker_running:
            self._auto_clicker_running = False
            self.auto_clicker_start_btn.setText("Start")
            self.auto_clicker_status_label.setText("Stopped")
            self.auto_clicker_status_label.setStyleSheet("color: red")
        else:
            self._auto_clicker_running = True
            self.auto_clicker_start_btn.setText("Stop")
            self.auto_clicker_status_label.setText("Running")
            self.auto_clicker_status_label.setStyleSheet("color: green")
            threading.Thread(target=self.auto_clicker_loop, daemon=True).start()

    def get_active_page_url(self) -> str:
        try:
            grabber = getattr(self, "_browser_grabber_cache", None)
            if grabber is None:
                grabber = BrowserEMRGrabber()
                self._browser_grabber_cache = grabber
            if not grabber.connect_to_chrome():
                return ""
            url = grabber._get_current_url() or ""
            return url
        except Exception:
            return ""

    def show_chrome_error(self, context: str = ""):
        msg = f"Could not connect to Chrome on port {CDP_DEBUG_PORT}."
        if context:
            msg = f"{context}: {msg}"
        msg += f"\nEnsure Chrome is running with --remote-debugging-port={CDP_DEBUG_PORT}."
        _on_main(lambda: QMessageBox.warning(self, "Chrome Connection Error", msg))

    def beep_sound(self):
        try:
            winsound.Beep(800, 200)
        except Exception:
            pass

    def auto_clicker_loop(self):
        while self._auto_clicker_running and not self._is_closing:
            try:
                method = "xy"
                for rb_name, method_name in [("ac_method_xy_rb", "xy"), ("ac_method_browser_rb", "browser"), ("ac_method_cdp_rb", "cdp")]:
                    rb = getattr(self, rb_name, None)
                    if rb and rb.isChecked():
                        method = method_name; break
                _on_main(lambda m=method: self.auto_clicker_method_label.setText(f"Method: {m}"))
                if self._url_monitoring_active:
                    current_url = self.get_active_page_url()
                    if current_url:
                        _on_main(lambda u=current_url: self.url_display_label.setText(f"URL: {u[:60]}"))
                if method == "xy":
                    try:
                        x_text = self.ac_x_text.text().strip()
                        y_text = self.ac_y_text.text().strip()
                        if x_text and y_text:
                            x, y = int(x_text), int(y_text)
                            pyautogui.click(x, y)
                    except (ValueError, Exception) as e:
                        print(f"XY click error: {e}")
                elif method == "browser":
                    try:
                        grabber = getattr(self, "_browser_grabber_cache", None)
                        if grabber is None:
                            grabber = BrowserEMRGrabber()
                            self._browser_grabber_cache = grabber
                        if grabber.connect_to_chrome():
                            selector = self.ac_selector_text.text().strip() if hasattr(self, 'ac_selector_text') else ""
                            if selector:
                                try:
                                    grabber._click_element(selector)
                                except Exception:
                                    pass
                    except Exception as e:
                        print(f"Browser click error: {e}")
                elif method == "cdp":
                    try:
                        grabber = getattr(self, "_browser_grabber_cache", None)
                        if grabber is None:
                            grabber = BrowserEMRGrabber()
                            self._browser_grabber_cache = grabber
                        if grabber.connect_to_chrome():
                            driver = getattr(grabber, 'driver', None)
                            if driver:
                                selector = self.ac_selector_text.text().strip() if hasattr(self, 'ac_selector_text') else ""
                                if selector:
                                    script = f'document.querySelector("{selector}")?.click()'
                                    driver.execute_script(script)
                    except Exception as e:
                        print(f"CDP click error: {e}")
                now = time.strftime("%H:%M:%S")
                _on_main(lambda t=now: self.auto_clicker_last_click_label.setText(f"Last: {t}"))
                try:
                    interval = float(self.ac_interval_text.text().strip() or "5")
                except ValueError:
                    interval = 5.0
                time.sleep(interval)
            except Exception as e:
                print(f"Auto clicker loop error: {e}")
                time.sleep(5)
        _on_main(self.update_clicker_stopped)

    def update_clicker_stopped(self):
        self.auto_clicker_status_label.setText("Stopped")
        self.auto_clicker_status_label.setStyleSheet("color: red")
        self.auto_clicker_start_btn.setText("Start")

    def update_click_method(self, method: str):
        self.auto_clicker_method_label.setText(f"Method: {method}")

    def update_last_click_time(self, time_str: str):
        self.auto_clicker_last_click_label.setText(f"Last: {time_str}")

    def update_url_display(self, url: str):
        self.url_display_label.setText(f"URL: {url[:60]}")

    def on_click_method_changed(self):
        for rb_name, method_name in [("ac_method_xy_rb", "XY"), ("ac_method_browser_rb", "Browser"), ("ac_method_cdp_rb", "CDP")]:
            rb = getattr(self, rb_name, None)
            if rb and rb.isChecked():
                self.auto_clicker_method_label.setText(f"Method: {method_name}")
                break

    def on_popup_toggle(self, checked: bool):
        self._popup_enabled = checked

    def show_new_task_popup(self, message: str = "New task available"):
        if not self._popup_enabled:
            return
        from .dialogs.new_task_popup import NewTaskPopup
        popup = NewTaskPopup(message, parent=None)
        popup.open_emr_signal.connect(self._on_popup_open_emr)
        popup.show()

    def _on_popup_open_emr(self):
        self.open_emr_in_chrome()

    def open_emr_in_chrome(self):
        try:
            import subprocess
            subprocess.Popen(["cmd", "/c", "start", "chrome", f"--remote-debugging-port={CDP_DEBUG_PORT}", "about:blank"], shell=True)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not open Chrome: {e}")

    def test_chrome_connection(self):
        def worker():
            try:
                grabber = BrowserEMRGrabber()
                if grabber.connect_to_chrome():
                    _on_main(lambda: QMessageBox.information(self, "Success", "Chrome connection successful!"))
                    try:
                        grabber.disconnect()
                    except Exception:
                        pass
                    try:
                        grabber.shutdown_playwright()
                    except Exception:
                        pass
                else:
                    _on_main(lambda: self.show_chrome_error("Test connection"))
            except Exception as e:
                _on_main(lambda: QMessageBox.critical(self, "Error", f"Connection test failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    # ==================================================================
    # In-visit auto-clicker (Playwright simulated clicks)
    # ==================================================================
    def toggle_invisit_clicker(self):
        if self._invisit_clicker_running:
            self.stop_invisit_clicker()
        else:
            self.start_invisit_clicker()

    def start_invisit_clicker(self):
        self._invisit_clicker_running = True
        self.auto_clicker_status_label.setText("In-Visit Running")
        self.auto_clicker_status_label.setStyleSheet("color: blue")
        def loop():
            while self._invisit_clicker_running and not self._is_closing:
                try:
                    grabber = getattr(self, "_browser_grabber_cache", None)
                    if grabber is None:
                        grabber = BrowserEMRGrabber()
                        self._browser_grabber_cache = grabber
                    if not grabber.connect_to_chrome():
                        time.sleep(5); continue
                    page = getattr(grabber, '_page', None)
                    if not page:
                        time.sleep(5); continue
                    # Click floating action button using Playwright
                    fab_selector = '[data-testid="floatingActionButton"]'
                    for attempt in range(2):
                        try:
                            locator = page.locator(fab_selector)
                            locator.wait_for(state="visible", timeout=3000)
                            locator.click()
                            break
                        except Exception:
                            if attempt < 1:
                                time.sleep(0.12)
                    time.sleep(0.5)
                    # Click "Get Next Task" menu item
                    next_task_selector = '[data-testid="menu-item-Get Next Task"]'
                    for attempt in range(2):
                        try:
                            locator = page.locator(next_task_selector)
                            locator.wait_for(state="visible", timeout=3000)
                            locator.click()
                            break
                        except Exception:
                            if attempt < 1:
                                time.sleep(0.02)
                    now = time.strftime("%H:%M:%S")
                    _on_main(lambda t=now: self.auto_clicker_last_click_label.setText(f"Last: {t}"))
                    self.beep_sound()
                except Exception as exc:
                    print(f"In-visit clicker error: {exc}")
                try:
                    interval = float(self.ac_interval_text.text().strip() or "5")
                except ValueError:
                    interval = 5.0
                time.sleep(interval)
            _on_main(self.update_clicker_stopped)
        threading.Thread(target=loop, daemon=True).start()

    def stop_invisit_clicker(self):
        self._invisit_clicker_running = False
        self.auto_clicker_status_label.setText("Stopped")
        self.auto_clicker_status_label.setStyleSheet("color: red")

    # ==================================================================
    # Page refresh toggle
    # ==================================================================
    def toggle_page_refresh(self):
        if self._page_refresh_running:
            self.stop_page_refresh()
        else:
            self.start_page_refresh()

    def start_page_refresh(self):
        self._page_refresh_running = True
        def loop():
            while self._page_refresh_running and not self._is_closing:
                try:
                    grabber = getattr(self, "_browser_grabber_cache", None)
                    if grabber is None:
                        grabber = BrowserEMRGrabber()
                        self._browser_grabber_cache = grabber
                    if grabber.connect_to_chrome():
                        page = getattr(grabber, '_page', None)
                        if page:
                            page.reload()
                except Exception as e:
                    print(f"Page refresh error: {e}")
                try:
                    interval = float(self.ac_interval_text.text().strip() or "30")
                except ValueError:
                    interval = 30.0
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()

    def stop_page_refresh(self):
        self._page_refresh_running = False

    def quick_next_task(self):
        """Use CDP/Playwright to click Get Next Task."""
        def worker():
            try:
                grabber = getattr(self, "_browser_grabber_cache", None)
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                if not grabber.connect_to_chrome():
                    _on_main(lambda: self.show_chrome_error("Quick Next Task"))
                    return
                page = getattr(grabber, '_page', None)
                if not page:
                    driver = getattr(grabber, 'driver', None)
                    if driver:
                        script = 'document.querySelector(\'[data-testid="floatingActionButton"]\')?.click()'
                        driver.execute_script(script)
                        time.sleep(0.5)
                        script2 = 'document.querySelector(\'[data-testid="menu-item-Get Next Task"]\')?.click()'
                        driver.execute_script(script2)
                    return
                fab_selector = '[data-testid="floatingActionButton"]'
                try:
                    locator = page.locator(fab_selector)
                    locator.wait_for(state="visible", timeout=3000)
                    locator.click()
                except Exception:
                    pass
                time.sleep(0.5)
                next_task_selector = '[data-testid="menu-item-Get Next Task"]'
                try:
                    locator = page.locator(next_task_selector)
                    locator.wait_for(state="visible", timeout=3000)
                    locator.click()
                except Exception:
                    pass
            except Exception as exc:
                print(f"quick_next_task error: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    # ==================================================================
    # URL monitoring
    # ==================================================================
    def start_url_monitoring(self):
        if self._url_monitoring_active:
            return
        self._url_monitoring_active = True
        threading.Thread(target=self.url_monitor_loop, daemon=True).start()

    def url_monitor_loop(self):
        last_url = ""
        while self._url_monitoring_active and not self._is_closing:
            try:
                current_url = self.get_active_page_url()
                if current_url and current_url != last_url:
                    last_url = current_url
                    _on_main(lambda u=current_url: self.url_display_label.setText(f"URL: {u[:60]}"))
                    # Detect new task from URL change
                    if "task" in current_url.lower() or "visit" in current_url.lower():
                        self.detect_visit_type_and_switch_tab()
            except Exception as e:
                print(f"URL monitor error: {e}")
            time.sleep(3)

    # ==================================================================
    # CDP Visit Monitor
    # ==================================================================
    def _start_cdp_visit_monitor(self):
        if self._cdp_monitor_running:
            return
        self._cdp_monitor_running = True
        threading.Thread(target=self._cdp_visit_monitor_loop, daemon=True).start()

    def _cdp_visit_monitor_loop(self):
        last_detected = None
        while self._cdp_monitor_running and not self._is_closing:
            try:
                grabber = getattr(self, "_browser_grabber_cache", None)
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                if not grabber.connect_to_chrome():
                    time.sleep(5); continue
                visit_type = self._classify_visit_type(grabber)
                if visit_type and visit_type != last_detected:
                    last_detected = visit_type
                    switched = self._schedule_tab_switch(visit_type, f"CDP detected: {visit_type}")
                    if switched and self._auto_grab_enabled:
                        url = ""
                        try:
                            url = grabber._get_current_url() or ""
                        except Exception:
                            pass
                        self._queue_auto_grab(visit_type, url)
            except Exception as exc:
                print(f"CDP monitor error: {exc}")
            time.sleep(CDP_POLL_INTERVAL_SEC)

    def _classify_visit_type(self, grabber) -> Optional[str]:
        try:
            url = grabber._get_current_url() or ""
            text = ""
            try:
                text = grabber._get_page_text() or ""
            except Exception:
                pass
            if not url and not text:
                return None
            combined = (url + " " + text).lower()
            patterns = [
                ("sexual health", "Sexual Health"),
                ("hair loss", "Hair Loss"),
                ("photoaging", "Photoaging"),
                ("performance anxiety", "Performance Anxiety"),
                ("birth control", "Birth Control"),
                ("testosterone", "T Deficiency"),
                ("trt", "T Deficiency"),
                ("dashboard", "EMR Dashboard"),
            ]
            for pattern, canonical in patterns:
                if pattern in combined:
                    return canonical
            return None
        except Exception:
            return None

    # ==================================================================
    # Hotkey system (keyboard module only)
    # ==================================================================
    def setup_global_hotkeys(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            keyboard.add_hotkey('ctrl+shift+g', self.trigger_appropriate_grab)
            keyboard.add_hotkey('ctrl+shift+h', self.toggle_gui_visibility)
            keyboard.add_hotkey('ctrl+shift+n', self.quick_next_task)
            self._register_tab_specific_hotkeys()
        except Exception as e:
            print(f"Hotkey setup error: {e}")

    def trigger_appropriate_grab(self):
        _on_main(self._do_appropriate_grab)

    def _do_appropriate_grab(self):
        tab = self.notebook.currentIndex()
        if tab == 0:
            self.grab_all_labs()
        elif tab == 1:
            self.grab_hair()
        elif tab == 2:
            self.grab_photoaging()
        elif tab == 3:
            self.grab_sexual_health()
        elif tab == 5:
            self.grab_performance_anxiety()
        elif tab == 6:
            self.grab_birth_control()

    def _on_tab_changed(self, index: int):
        self._register_tab_specific_hotkeys()

    def _register_tab_specific_hotkeys(self):
        tab = self.notebook.currentIndex()
        self._clear_tab_hotkeys()
        if tab == 0:
            self._register_tab1_hotkeys()
        elif tab == 1:
            self._register_tab2_hotkeys()
        elif tab == 3:
            self._register_tab4_hotkeys()
        elif tab == 6:
            self._register_tab7_hotkeys()

    def _clear_tab_hotkeys(self):
        tab_hotkeys = ['ctrl+shift+1', 'ctrl+shift+2', 'ctrl+shift+3', 'ctrl+shift+4',
                       'ctrl+shift+5', 'ctrl+shift+6', 'ctrl+shift+7', 'ctrl+shift+8',
                       'ctrl+shift+9', 'ctrl+shift+0']
        for hk in tab_hotkeys:
            try:
                keyboard.remove_hotkey(hk)
            except (KeyError, ValueError):
                pass

    def _register_tab1_hotkeys(self):
        try:
            keyboard.add_hotkey('ctrl+shift+1', lambda: _on_main(self.insert_labs_note))
            keyboard.add_hotkey('ctrl+shift+2', lambda: _on_main(self.insert_rx_note))
            keyboard.add_hotkey('ctrl+shift+3', lambda: _on_main(self.insert_referral_note))
            keyboard.add_hotkey('ctrl+shift+4', lambda: _on_main(self.insert_lab_message))
            keyboard.add_hotkey('ctrl+shift+5', lambda: _on_main(self.insert_td_followup_labs))
            keyboard.add_hotkey('ctrl+shift+6', lambda: _on_main(self.insert_td_followup_note))
        except Exception as e:
            print(f"Tab1 hotkey error: {e}")

    def _register_tab2_hotkeys(self):
        try:
            keyboard.add_hotkey('ctrl+shift+1', lambda: _on_main(self.insert_hair_note))
            keyboard.add_hotkey('ctrl+shift+2', lambda: _on_main(self.insert_hair_limited_checkin_note))
            keyboard.add_hotkey('ctrl+shift+3', lambda: _on_main(self.insert_hair_info))
        except Exception as e:
            print(f"Tab2 hotkey error: {e}")

    def _register_tab4_hotkeys(self):
        try:
            keyboard.add_hotkey('ctrl+shift+1', lambda: _on_main(self.insert_sexual_health_note))
            keyboard.add_hotkey('ctrl+shift+2', lambda: _on_main(self.insert_sexual_health_brief_template))
            keyboard.add_hotkey('ctrl+shift+3', lambda: _on_main(self.insert_sexual_health_change_template))
            keyboard.add_hotkey('ctrl+shift+4', lambda: _on_main(self.insert_sh_change_cadence))
            keyboard.add_hotkey('ctrl+shift+5', lambda: _on_main(self.insert_sh_change_number))
            keyboard.add_hotkey('ctrl+shift+6', lambda: _on_main(self.insert_sh_change_medication))
        except Exception as e:
            print(f"Tab4 hotkey error: {e}")

    def _register_tab7_hotkeys(self):
        try:
            keyboard.add_hotkey('ctrl+shift+1', lambda: _on_main(self.insert_birth_control_initial_note))
            keyboard.add_hotkey('ctrl+shift+2', lambda: _on_main(self.insert_birth_control_followup_note))
        except Exception as e:
            print(f"Tab7 hotkey error: {e}")

    def _unregister_tab4_hotkeys(self):
        self._clear_tab_hotkeys()

    # Trigger helpers for hotkeys
    def _trigger_sh_grab(self):
        _on_main(self.grab_sexual_health)
    def _trigger_hair_grab(self):
        _on_main(self.grab_hair)
    def _trigger_bc_grab(self):
        _on_main(self.grab_birth_control)
    def _trigger_td_grab(self):
        _on_main(self.grab_all_labs)

    # ==================================================================
    # Custom CDP hotkeys
    # ==================================================================
    def _clear_custom_cdp_hotkeys(self):
        for hk in list(self._custom_cdp_hotkeys):
            try:
                keyboard.remove_hotkey(hk)
            except (KeyError, ValueError):
                pass
        self._custom_cdp_hotkeys.clear()

    def _register_custom_cdp_hotkeys(self, hotkey_map: Optional[Dict[str, str]] = None):
        self._clear_custom_cdp_hotkeys()
        if hotkey_map is None:
            # Use class-level CUSTOM_CDP_HOTKEYS config
            for entry in self.CUSTOM_CDP_HOTKEYS:
                hk = entry.get("hotkey")
                css_list = entry.get("css", [])
                if not hk or not css_list:
                    continue
                try:
                    keyboard.add_hotkey(hk, lambda selectors=css_list: self._execute_custom_cdp_click_chain(selectors))
                    self._custom_cdp_hotkeys.append(hk)
                except Exception as e:
                    print(f"Custom CDP hotkey '{hk}' error: {e}")
            return
        for hk, selector in hotkey_map.items():
            try:
                keyboard.add_hotkey(hk, lambda s=selector: self._execute_custom_cdp_click(s))
                self._custom_cdp_hotkeys.append(hk)
            except Exception as e:
                print(f"Custom CDP hotkey '{hk}' error: {e}")

    def _execute_custom_cdp_click_chain(self, selectors: List[str]):
        """Try multiple CSS selectors in order until one succeeds."""
        def worker():
            try:
                grabber = getattr(self, "_browser_grabber_cache", None)
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                if not grabber.connect_to_chrome():
                    return
                page = getattr(grabber, '_page', None)
                for selector in selectors:
                    try:
                        if page:
                            locator = page.locator(selector)
                            locator.wait_for(state="visible", timeout=2000)
                            locator.click()
                            return
                        else:
                            driver = getattr(grabber, 'driver', None)
                            if driver:
                                driver.execute_script(f'document.querySelector("{selector}")?.click()')
                                return
                    except Exception:
                        continue
                print(f"Custom CDP click: none of {selectors} found")
            except Exception as e:
                print(f"Custom CDP click chain error: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _execute_custom_cdp_click(self, selector: str):
        def worker():
            try:
                grabber = getattr(self, "_browser_grabber_cache", None)
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                if not grabber.connect_to_chrome():
                    return
                page = getattr(grabber, '_page', None)
                if page:
                    try:
                        locator = page.locator(selector)
                        locator.wait_for(state="visible", timeout=3000)
                        locator.click()
                    except Exception:
                        driver = getattr(grabber, 'driver', None)
                        if driver:
                            driver.execute_script(f'document.querySelector("{selector}")?.click()')
                else:
                    driver = getattr(grabber, 'driver', None)
                    if driver:
                        driver.execute_script(f'document.querySelector("{selector}")?.click()')
            except Exception as e:
                print(f"Custom CDP click error for '{selector}': {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _log_custom_cdp_hotkeys(self):
        print(f"Custom CDP hotkeys registered: {self._custom_cdp_hotkeys}")

    # ==================================================================
    # GUI visibility / hide / show
    # ==================================================================
    def toggle_gui_visibility(self):
        _on_main(self._toggle_gui_visibility_impl)

    def _toggle_gui_visibility_impl(self):
        if self.isVisible() and self._is_gui_foreground():
            self._hide_gui_window()
        else:
            self._show_gui_window()

    def _is_gui_foreground(self) -> bool:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            my_hwnd = int(self.winId())
            return hwnd == my_hwnd
        except Exception:
            return False

    def _bring_gui_to_foreground(self):
        self.show()
        self.raise_()
        self.activateWindow()
        try:
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _get_taskbar_top_y(self) -> int:
        try:
            import ctypes
            from ctypes import wintypes
            class APPBARDATA(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                            ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
                            ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]
            abd = APPBARDATA()
            abd.cbSize = ctypes.sizeof(APPBARDATA)
            ctypes.windll.shell32.SHAppBarMessage(5, ctypes.byref(abd))
            return abd.rc.top
        except Exception:
            return pyautogui.size()[1] - 40

    def _hide_gui_window(self):
        self.hide()

    def _show_gui_window(self):
        taskbar_y = self._get_taskbar_top_y()
        screen_w = pyautogui.size()[0]
        win_w = self.width()
        win_h = self.height()
        x = screen_w - win_w - 10
        y = taskbar_y - win_h - 10
        if y < 0:
            y = 0
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        try:
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    # ==================================================================
    # Close handler
    # ==================================================================
    def closeEvent(self, event):
        self._is_closing = True
        self._auto_clicker_running = False
        self._invisit_clicker_running = False
        self._page_refresh_running = False
        self._url_monitoring_active = False
        self._cdp_monitor_running = False
        self._auto_refresh_active = False
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            grabber = getattr(self, "_browser_grabber_cache", None)
            if grabber:
                try:
                    grabber.disconnect()
                except Exception:
                    pass
                try:
                    grabber.shutdown_playwright()
                except Exception:
                    pass
        except Exception:
            pass
        event.accept()
