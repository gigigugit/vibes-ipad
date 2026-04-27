"""EMR Assist Dashboard — New QDockWidget-based UI.

Launch with:
    python  emr_assist/dashboard.py
    pythonw emr_assist/dashboard.py
"""
from __future__ import annotations

import os
import sys
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Ensure the project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import keyboard
import pyautogui
import pyperclip
import pygetwindow as gw
import requests

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# Core imports
from emr_assist.core.config import (
    CDP_DEBUG_PORT,
    CDP_POLL_INTERVAL_SEC,
    AUTO_GRAB_DELAY_MS,
    PMH_OPTIONS,
    dprint,
)
from emr_assist.core.templates import load_templates_from_file as load_templates
from emr_assist.core import state
from emr_assist.core.state import (
    grabbed_vars,
    diagnoses,
    medication_value,
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
from emr_assist.core.parsers import (
    allow_hair_loss_diagnosis,
    detect_medication_from_text,
    extract_hair_medication_from_text,
    normalize_blood_pressure_value,
)
from emr_assist.browser.grabber import BrowserEMRGrabber

# UI imports
from emr_assist.ui.signals import signals
from emr_assist.ui.helpers import type_template_text, clear_text_selection, window_tracker
from emr_assist.ui.theme import DARK_THEME_QSS, ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED, BG_PANEL
from emr_assist.ui.widgets import ActionButton, StatusBadge, InfoCard

# Panel imports
from emr_assist.ui.panels.hair_loss import HairLossActions, HairLossInfo
from emr_assist.ui.panels.photoaging import PhotoagingActions, PhotoagingInfo
from emr_assist.ui.panels.sexual_health import SexualHealthActions, SexualHealthInfo
from emr_assist.ui.panels.performance_anxiety import PerformanceAnxietyActions, PerformanceAnxietyInfo
from emr_assist.ui.panels.birth_control import BirthControlActions, BirthControlInfo
from emr_assist.ui.panels.auto_clicker import AutoClickerPanel
from emr_assist.ui.dialogs import NewTaskPopup


# ---------------------------------------------------------------------------
# Thread-safe main-thread dispatcher
# ---------------------------------------------------------------------------
def _on_main(fn):
    """Execute *fn* on the GUI/main thread."""
    signals.call_on_main.emit(fn)


# ---------------------------------------------------------------------------
# Visit type constants
# ---------------------------------------------------------------------------
VISIT_TYPES = [
    "Hair Loss",
    "Photoaging",
    "Sexual Health",
    "Performance Anxiety",
    "Birth Control",
]
_VISIT_INDEX = {name: i for i, name in enumerate(VISIT_TYPES)}


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard Main Window
# ═══════════════════════════════════════════════════════════════════════════
class Dashboard(QMainWindow):
    """Dockable-panel EMR dashboard replacing the old tab-based UI."""

    def __init__(self) -> None:
        super().__init__()
        state.panel_title = "EMR Dashboard"
        self.setWindowTitle(state.panel_title)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        _screen = QApplication.primaryScreen()
        if _screen:
            screen = _screen.availableGeometry()
            self.setGeometry(0, 0, 460, screen.height())
        else:
            self.setGeometry(0, 0, 460, 900)

        # ── Shared state ──────────────────────────────────────────────
        self.templates: Dict[str, str] = {}
        self._browser_grabber_cache: Optional[BrowserEMRGrabber] = None
        self._cdp_monitor_running = True
        self._auto_grab_enabled = True
        self._is_closing = False
        self._current_visit: str = VISIT_TYPES[0]

        # Auto-clicker
        self.invisit_running = False
        self.invisit_thread: Optional[threading.Thread] = None
        self.page_refresh_running = False
        self.clicker_method_mode = "cdp"
        self.requests_session = requests.Session()
        self.notify_with_popup = False
        self.new_task_popup: Optional[NewTaskPopup] = None

        # ── Build UI ──────────────────────────────────────────────────
        self._build_status_bar()
        self._build_action_dock()
        self._build_info_dock()
        self._build_clicker_dock()

        # ── Load templates ────────────────────────────────────────────
        try:
            script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self.templates = load_templates(script_dir)
        except Exception as e:
            print(f"Template loading error: {e}")

        # ── Wire signals + buttons ────────────────────────────────────
        self._connect_signals()
        self._connect_buttons()

        # ── Deferred startup ──────────────────────────────────────────
        QTimer.singleShot(200, self._deferred_startup)

    # ==================================================================
    # UI Construction
    # ==================================================================

    def _build_status_bar(self) -> None:
        """Fixed status bar at top of the central widget."""
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Row 1: visit type + connection
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.badge_visit = StatusBadge("Hair Loss", ACCENT_BLUE)
        self.badge_connection = StatusBadge("Chrome: ?", "#888")
        row1.addWidget(self.badge_visit)
        row1.addStretch()
        row1.addWidget(self.badge_connection)
        layout.addLayout(row1)

        # Row 2: medication display + status
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.lbl_medication = QLabel("Medication: —")
        self.lbl_medication.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 12px; font-weight: bold;")
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #a0b0c0; font-size: 11px;")
        row2.addWidget(self.lbl_medication, 1)
        row2.addWidget(self.lbl_status, 1)
        layout.addLayout(row2)

        # Row 3: visit type quick-switch chips
        row3 = QHBoxLayout()
        row3.setSpacing(4)
        self._visit_btns: List[ActionButton] = []
        short_labels = ["Hair", "Photo", "SH", "PA", "BC"]
        for i, (full, short) in enumerate(zip(VISIT_TYPES, short_labels)):
            btn = ActionButton(short, accent="blue")
            btn.setMaximumHeight(24)
            btn.setMinimumWidth(36)
            btn.setProperty("visit_index", i)
            btn.clicked.connect(lambda checked, idx=i: self._switch_visit(idx))
            row3.addWidget(btn)
            self._visit_btns.append(btn)
        layout.addLayout(row3)

        # Spacer — docks go around the central widget
        layout.addStretch()

    def _build_action_dock(self) -> None:
        """Left dock: stacked action panels per visit type."""
        self.action_stack = QStackedWidget()

        self.hair_actions = HairLossActions()
        self.photo_actions = PhotoagingActions()
        self.sh_actions = SexualHealthActions()
        self.pa_actions = PerformanceAnxietyActions()
        self.bc_actions = BirthControlActions()

        for widget in [
            self.hair_actions, self.photo_actions,
            self.sh_actions, self.pa_actions, self.bc_actions,
        ]:
            self.action_stack.addWidget(widget)

        dock = QDockWidget("Actions", self)
        dock.setWidget(self.action_stack)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.action_dock = dock

    def _build_info_dock(self) -> None:
        """Center dock: stacked info panels per visit type."""
        self.info_stack = QStackedWidget()

        self.hair_info = HairLossInfo()
        self.photo_info = PhotoagingInfo()
        self.sh_info = SexualHealthInfo()
        self.pa_info = PerformanceAnxietyInfo()
        self.bc_info = BirthControlInfo()

        for widget in [
            self.hair_info, self.photo_info,
            self.sh_info, self.pa_info, self.bc_info,
        ]:
            self.info_stack.addWidget(widget)

        dock = QDockWidget("Patient Info", self)
        dock.setWidget(self.info_stack)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.info_dock = dock

    def _build_clicker_dock(self) -> None:
        """Bottom dock: auto-clicker panel (detachable)."""
        self.clicker_panel = AutoClickerPanel()
        dock = QDockWidget("Auto Clicker", self)
        dock.setWidget(self.clicker_panel)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.clicker_dock = dock

    # ==================================================================
    # Signal & Button Wiring
    # ==================================================================

    def _connect_signals(self) -> None:
        """Wire the SignalBridge signals to dashboard widget updates."""
        def _safe_call(fn):
            try:
                fn()
            except Exception as exc:
                print(f"[_on_main callback error] {exc}")
        signals.call_on_main.connect(_safe_call)
        signals.set_status.connect(self.lbl_status.setText)
        signals.set_visit_type.connect(lambda t: self.badge_visit.set_text(f"  {t}"))

        # Hair Loss signals
        signals.set_hair_med.connect(lambda v: self.hair_info.card_medication.set_value(v))
        signals.set_hair_hsx.connect(lambda v: self.hair_info.card_response.set_value(v))
        signals.set_hair_hvar.connect(lambda v: self.hair_info.card_symptoms.set_value(v))

        # Photoaging signals
        signals.set_photoaging_med.connect(lambda v: self.photo_info.card_medication.set_value(v))
        signals.set_photoaging_goals.connect(lambda v: self.photo_info.card_goals.set_value(v))
        signals.set_photoaging_retinoid.connect(lambda v: self.photo_info.card_retinoid.set_value(v))

        # Sexual Health signals
        signals.set_sh_med.connect(lambda v: self.sh_info.card_medication.set_value(v))
        signals.set_sh_effectiveness.connect(lambda v: self.sh_info.card_effectiveness.set_value(v))
        signals.set_sh_bp.connect(lambda v: self.sh_info.card_bp.set_value(v))
        signals.set_sh_hair_location.connect(lambda v: self.sh_info.card_hair_location.set_value(v))
        signals.set_sh_hair_sxx.connect(lambda v: self.sh_info.card_hair_sxx.set_value(v))

        # Performance Anxiety signals
        signals.set_pa_med.connect(lambda v: self.pa_info.card_medication.set_value(v))
        signals.set_pa_bp.connect(lambda v: self.pa_info.card_bp.set_value(v))
        signals.set_pa_pulse.connect(lambda v: self.pa_info.card_pulse.set_value(v))
        signals.set_pa_situations.connect(lambda v: self.pa_info.card_situations.set_value(v))
        signals.set_pa_symptoms.connect(lambda v: self.pa_info.card_symptoms.set_value(v))

        # Birth Control signals
        signals.set_bc_med.connect(lambda v: self.bc_info.card_medication.set_value(v))
        signals.set_bc_lmp.connect(lambda v: self.bc_info.card_lmp.set_value(v))
        signals.set_bc_bp.connect(lambda v: self.bc_info.card_bp.set_value(v))
        signals.set_bc_side_effects.connect(lambda v: self.bc_info.card_side_effects.set_value(v))
        signals.set_bc_history_changes.connect(lambda v: self.bc_info.card_history_changes.set_value(v))
        signals.set_bc_initial_visit.connect(self._on_bc_initial_visit)
        signals.set_bc_pmh.connect(self._on_bc_pmh)

        # Auto-Clicker signals
        signals.set_clicker_status.connect(
            lambda t, c: self.clicker_panel.set_status(t, c)
        )
        signals.set_clicker_method.connect(
            lambda t, c: self.clicker_panel.lbl_method.setText(t)
        )
        signals.set_clicker_last.connect(
            lambda t: self.clicker_panel.lbl_last_click.setText(t)
        )
        signals.set_url_display.connect(
            lambda u: self.clicker_panel.lbl_url.setText(f"URL: {u[:80]}")
        )

        # Tab switching (from CDP monitor)
        signals.switch_tab_by_name.connect(self._switch_visit_by_name)

    def _connect_buttons(self) -> None:
        """Wire action-panel buttons to grab/insert methods."""
        # Hair Loss
        self.hair_actions.btn_grab.clicked.connect(self.grab_hair)
        self.hair_actions.btn_initial.clicked.connect(
            lambda: self._insert_template("Hair Loss Initial Note")
        )
        self.hair_actions.btn_followup.clicked.connect(
            lambda: self._insert_template("Hair Loss Follow-up Note")
        )
        self.hair_actions.btn_limited.clicked.connect(
            lambda: self._insert_template("Hair Loss Limited Check-in")
        )

        # Photoaging
        self.photo_actions.btn_grab.clicked.connect(self.grab_photoaging)
        self.photo_actions.btn_note.clicked.connect(
            lambda: self._insert_template("Photoaging Note")
        )

        # Sexual Health
        self.sh_actions.btn_grab.clicked.connect(self.grab_sexual_health)
        self.sh_actions.btn_followup.clicked.connect(
            lambda: self._insert_template("Sexual Health Follow-up Note")
        )
        self.sh_actions.btn_plan.clicked.connect(
            lambda: self._insert_template("Sexual Health Plan")
        )
        self.sh_actions.btn_change.clicked.connect(
            lambda: self._insert_template("Sexual Health Change Template")
        )
        self.sh_actions.btn_change_cadence.clicked.connect(
            lambda: self._insert_template("Sexual Health Change Cadence")
        )
        self.sh_actions.btn_change_number.clicked.connect(
            lambda: self._insert_template("Sexual Health Change Number")
        )
        self.sh_actions.btn_change_med.clicked.connect(
            lambda: self._insert_template("Sexual Health Change Medication")
        )

        # Performance Anxiety
        self.pa_actions.btn_grab.clicked.connect(self.grab_performance_anxiety)
        self.pa_actions.btn_initial.clicked.connect(
            lambda: self._insert_template("Performance Anxiety Initial Note")
        )
        self.pa_actions.btn_followup.clicked.connect(
            lambda: self._insert_template("Performance Anxiety Follow-up Note")
        )

        # Birth Control
        self.bc_actions.btn_grab.clicked.connect(self.grab_birth_control)
        self.bc_actions.btn_initial.clicked.connect(
            lambda: self._insert_template("Birth Control Initial Note")
        )
        self.bc_actions.btn_followup.clicked.connect(
            lambda: self._insert_template("Birth Control Follow-up Note")
        )

        # Auto-Clicker
        self.clicker_panel.btn_dashboard_clicker.clicked.connect(self.toggle_auto_clicker)
        self.clicker_panel.btn_invisit_clicker.clicked.connect(self.toggle_invisit_clicker)
        self.clicker_panel.btn_page_refresh.clicked.connect(self.toggle_page_refresh)
        self.clicker_panel.btn_quick_next.clicked.connect(self.quick_next_task)
        self.clicker_panel.btn_test_chrome.clicked.connect(self.test_chrome_connection)
        self.clicker_panel.btn_detect_visit.clicked.connect(
            self.detect_visit_type_and_switch
        )

    # ==================================================================
    # Visit-Type Switching
    # ==================================================================

    def _switch_visit(self, index: int) -> None:
        """Switch all stacked panels to the given visit type index."""
        if 0 <= index < len(VISIT_TYPES):
            self._current_visit = VISIT_TYPES[index]
            self.action_stack.setCurrentIndex(index)
            self.info_stack.setCurrentIndex(index)
            self.badge_visit.set_text(f"  {self._current_visit}")
            # Highlight active visit button
            for i, btn in enumerate(self._visit_btns):
                btn.setProperty("accent", "green" if i == index else "blue")
                btn.setStyleSheet(btn.styleSheet())  # force QSS refresh

    def _switch_visit_by_name(self, name: str) -> None:
        idx = _VISIT_INDEX.get(name)
        if idx is not None:
            self._switch_visit(idx)

    # ==================================================================
    # Deferred Startup
    # ==================================================================

    def _deferred_startup(self) -> None:
        self._setup_global_hotkeys()
        self._start_cdp_visit_monitor()
        # Default to Hair Loss
        self._switch_visit(0)

    def _setup_global_hotkeys(self) -> None:
        """Alternate dashboard hotkeys are intentionally disabled."""
        print("Alternate dashboard hotkeys disabled")

    def _f4_grab_dispatch(self) -> None:
        """F4 dispatches grab based on current visit type."""
        _on_main(self._do_grab_for_current_visit)

    def _do_grab_for_current_visit(self) -> None:
        vt = self._current_visit
        if vt == "Hair Loss":
            self.grab_hair()
        elif vt == "Photoaging":
            self.grab_photoaging()
        elif vt == "Sexual Health":
            self.grab_sexual_health()
        elif vt == "Performance Anxiety":
            self.grab_performance_anxiety()
        elif vt == "Birth Control":
            self.grab_birth_control()

    # ==================================================================
    # Browser Grabber Helper
    # ==================================================================

    def _ensure_grabber(self) -> Optional[BrowserEMRGrabber]:
        grabber = self._browser_grabber_cache
        if grabber is None:
            grabber = BrowserEMRGrabber()
            self._browser_grabber_cache = grabber
        return grabber

    # ==================================================================
    # Grab Methods
    # ==================================================================

    def grab_hair(self) -> None:
        """Grab hair loss data from browser via copy-paste."""
        def do_grab():
            original = ""
            try:
                original = pyperclip.paste()
            except Exception:
                pass
            signals.set_status.emit("Grabbing hair loss data…")
            try:
                sw, sh = pyautogui.size()
                pyautogui.click(sw // 2, sh // 2)
                time.sleep(0.3)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "c")
                time.sleep(0.5)
                new_content = pyperclip.paste()
                if not new_content or len(new_content) < 20:
                    signals.set_status.emit("Failed to grab hair loss text")
                    return
                clear_text_selection(self)
                med = extract_hair_medication_from_text(new_content)
                if med:
                    signals.set_hair_med.emit(med)
                emit_emr_bridge()
                signals.set_status.emit("Hair Loss data grabbed ✓")
            except Exception as exc:
                signals.set_status.emit(f"Hair grab error: {exc}")
            finally:
                try:
                    pyperclip.copy(original)
                except Exception:
                    pass
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
        self.hide()
        time.sleep(0.1)
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_photoaging(self) -> None:
        """Grab photoaging data from browser via copy-paste."""
        def do_grab():
            original = ""
            try:
                original = pyperclip.paste()
            except Exception:
                pass
            signals.set_status.emit("Grabbing photoaging data…")
            try:
                sw, sh = pyautogui.size()
                pyautogui.click(sw // 2, sh // 2)
                time.sleep(0.3)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "c")
                time.sleep(0.5)
                new_content = pyperclip.paste()
                if not new_content or len(new_content) < 20:
                    signals.set_status.emit("Failed to grab photoaging text")
                    return
                clear_text_selection(self)
                import re as _re
                lines = [ln.strip() for ln in new_content.splitlines() if ln.strip()]
                med = ""
                treatment_re = _re.compile(r"^treatment\s*[:\-]\s*(.+)$", _re.IGNORECASE)
                treatment_header_re = _re.compile(r"^treatment\s*[:\-]*$", _re.IGNORECASE)
                for idx, ln in enumerate(lines):
                    m = treatment_re.match(ln)
                    if m:
                        med = m.group(1).strip()
                        break
                    if treatment_header_re.match(ln):
                        for j in range(idx + 1, min(len(lines), idx + 4)):
                            candidate = lines[j].strip()
                            if candidate and not _re.match(r"^(photos|notes|click|intake)", candidate, _re.IGNORECASE):
                                med = candidate
                                break
                        break
                if med:
                    signals.set_photoaging_med.emit(med)
                emit_emr_bridge()
                signals.set_status.emit("Photoaging data grabbed ✓")
            except Exception as exc:
                signals.set_status.emit(f"Photoaging grab error: {exc}")
            finally:
                try:
                    pyperclip.copy(original)
                except Exception:
                    pass
                _on_main(lambda: (self.show(), self.raise_(), self.activateWindow()))
        self.hide()
        time.sleep(0.1)
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_sexual_health(self) -> None:
        """Grab sexual health data from browser."""
        def do_grab():
            signals.set_status.emit("Grabbing sexual health data…")
            grabber = self._ensure_grabber()
            if grabber is None:
                signals.set_status.emit("No browser connection")
                return
            try:
                if not grabber.connect_to_chrome():
                    signals.set_status.emit("Chrome connection failed")
                    return
                raw = grabber.grab_sexual_health_data()
                if raw:
                    if "medication" in raw:
                        signals.set_sh_med.emit(str(raw["medication"]))
                    if "effectiveness" in raw:
                        signals.set_sh_effectiveness.emit(str(raw["effectiveness"]))
                    bp = normalize_blood_pressure_value(raw.get("blood_pressure", ""))
                    if bp:
                        signals.set_sh_bp.emit(bp)
                    if "hair_loss_location" in raw:
                        signals.set_sh_hair_location.emit(str(raw["hair_loss_location"]))
                    if "hair_loss_additional_sxx" in raw:
                        signals.set_sh_hair_sxx.emit(str(raw["hair_loss_additional_sxx"]))
                    emit_emr_bridge()
                    signals.set_status.emit("Sexual Health data grabbed ✓")
                else:
                    signals.set_status.emit("No SH data found")
            except Exception as exc:
                signals.set_status.emit(f"SH grab error: {exc}")
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_performance_anxiety(self) -> None:
        """Grab performance anxiety data from browser."""
        def do_grab():
            signals.set_status.emit("Grabbing PA data…")
            grabber = self._ensure_grabber()
            if grabber is None:
                signals.set_status.emit("No browser connection")
                return
            try:
                if not grabber.connect_to_chrome():
                    signals.set_status.emit("Chrome connection failed")
                    return
                raw = grabber.grab_performance_anxiety_data()
                if raw:
                    if "medication" in raw:
                        signals.set_pa_med.emit(str(raw["medication"]))
                    bp = raw.get("blood_pressure", "nr")
                    signals.set_pa_bp.emit(str(bp))
                    pulse = raw.get("pulse", "nr")
                    signals.set_pa_pulse.emit(str(pulse))
                    if "situations_text" in raw:
                        signals.set_pa_situations.emit(str(raw["situations_text"]))
                    if "symptoms_text" in raw:
                        signals.set_pa_symptoms.emit(str(raw["symptoms_text"]))
                    emit_emr_bridge()
                    signals.set_status.emit("PA data grabbed ✓")
                else:
                    signals.set_status.emit("No PA data found")
            except Exception as exc:
                signals.set_status.emit(f"PA grab error: {exc}")
        threading.Thread(target=do_grab, daemon=True).start()

    def grab_birth_control(self) -> None:
        """Grab birth control data from browser."""
        def do_grab():
            signals.set_status.emit("Grabbing BC data…")
            grabber = self._ensure_grabber()
            if grabber is None:
                signals.set_status.emit("No browser connection")
                return
            try:
                if not grabber.connect_to_chrome():
                    signals.set_status.emit("Chrome connection failed")
                    return
                raw = grabber.grab_birth_control_data()
                if raw:
                    if "medication" in raw:
                        signals.set_bc_med.emit(str(raw["medication"]))
                    if "lmp" in raw:
                        signals.set_bc_lmp.emit(str(raw["lmp"]))
                    bp = raw.get("blood_pressure", "nr")
                    signals.set_bc_bp.emit(str(bp))
                    if "side_effects_text" in raw:
                        signals.set_bc_side_effects.emit(str(raw["side_effects_text"]))
                    if "med_history_changes" in raw:
                        signals.set_bc_history_changes.emit(str(raw["med_history_changes"]))
                    # Visit type toggle
                    initial = raw.get("initial_visit", True)
                    signals.set_bc_initial_visit.emit(bool(initial))
                    # PMH
                    pmh_list = raw.get("pmh_list", [])
                    pmh_other = raw.get("pmh_other", "")
                    signals.set_bc_pmh.emit(pmh_list, pmh_other)
                    emit_emr_bridge()
                    signals.set_status.emit("BC data grabbed ✓")
                else:
                    signals.set_status.emit("No BC data found")
            except Exception as exc:
                signals.set_status.emit(f"BC grab error: {exc}")
        threading.Thread(target=do_grab, daemon=True).start()

    # ==================================================================
    # Template Insertion
    # ==================================================================

    def _insert_template(self, template_name: str) -> None:
        """Generic template insertion — looks up template, formats, inserts."""
        tpl = self.templates.get(template_name)
        if not tpl:
            self.lbl_status.setText(f"Template '{template_name}' not found")
            return
        try:
            text = tpl.format(**self._build_template_vars())
        except KeyError as e:
            self.lbl_status.setText(f"Missing variable: {e}")
            return
        self._do_insert(text)

    def _build_template_vars(self) -> Dict[str, str]:
        """Collect all current widget values into a template variable dict."""
        v: Dict[str, str] = {}

        v["diagnoses"] = diagnoses[0]
        v["medication"] = medication_value[0]
        v["pmh"] = build_pmh_text()

        # Hair loss
        v["hair_medication"] = self.hair_info.card_medication.value()
        v["hair_response"] = self.hair_info.card_response.value()
        v["hair_symptoms"] = self.hair_info.card_symptoms.value()
        v["objective_exam"] = self.hair_info.get_objective_section()

        # Photoaging
        v["photoaging_medication"] = self.photo_info.card_medication.value()
        v["photoaging_goals"] = self.photo_info.card_goals.value()
        v["photoaging_retinoid"] = self.photo_info.card_retinoid.value()
        v["exam_text"] = self.photo_info.get_exam_text()

        # Sexual Health
        v["sh_medication"] = self.sh_info.card_medication.value()
        v["sh_effectiveness"] = self.sh_info.card_effectiveness.value()
        v["sh_bp"] = self.sh_info.card_bp.value()
        v["sh_diagnoses"] = self.sh_info.get_diagnoses_text(v["sh_medication"])
        v["hair_location"] = self.sh_info.card_hair_location.value()
        v["hair_sxx"] = self.sh_info.card_hair_sxx.value()

        # Performance Anxiety
        v["pa_medication"] = self.pa_info.card_medication.value()
        v["pa_bp"] = self.pa_info.card_bp.value()
        v["pa_pulse"] = self.pa_info.card_pulse.value()
        v["pa_situations"] = self.pa_info.card_situations.value()
        v["pa_symptoms"] = self.pa_info.card_symptoms.value()

        # Birth Control
        v["bc_medication"] = self.bc_info.card_medication.value()
        v["bc_lmp"] = self.bc_info.card_lmp.value()
        v["bc_bp"] = self.bc_info.card_bp.value()
        v["bc_side_effects"] = self.bc_info.card_side_effects.value()
        v["bc_history_changes"] = self.bc_info.card_history_changes.value()

        return v

    def _do_insert(self, text: str) -> None:
        """Hide window, paste text, show window."""
        self.hide()
        time.sleep(0.15)
        type_template_text(text)
        QTimer.singleShot(500, self.show)

    def _on_bc_initial_visit(self, is_initial: bool) -> None:
        """Toggle Birth Control initial/follow-up chips."""
        self.bc_info.chip_initial.active = is_initial
        self.bc_info.chip_followup.active = not is_initial

    def _on_bc_pmh(self, pmh_keys: list, pmh_other: str) -> None:
        """Apply PMH selection from grabbed data."""
        from emr_assist.core.config import BIRTH_CONTROL_PMH_OPTIONS
        labels = []
        for key, label in BIRTH_CONTROL_PMH_OPTIONS:
            if key in pmh_keys:
                labels.append(label)
        self.bc_info.pmh_chips.set_selected(labels)
        self.bc_info.card_pmh_other.set_value(pmh_other)

    # ==================================================================
    # Auto-Clicker
    # ==================================================================

    def toggle_auto_clicker(self) -> None:
        if auto_clicker_enabled[0]:
            auto_clicker_enabled[0] = False
            signals.set_clicker_status.emit("STOPPED", "red")
            self.clicker_panel.btn_dashboard_clicker.setText("Autoclick: Dashboard")
        else:
            auto_clicker_enabled[0] = True
            auto_clicker_x[0] = self.clicker_panel.click_x
            auto_clicker_y[0] = self.clicker_panel.click_y
            auto_clicker_interval[0] = int(self.clicker_panel.interval)
            signals.set_clicker_status.emit("RUNNING", "green")
            self.clicker_panel.btn_dashboard_clicker.setText("STOP Dashboard Clicker")
            threading.Thread(target=self._auto_clicker_loop, daemon=True).start()

    def _auto_clicker_loop(self) -> None:
        while auto_clicker_enabled[0] and not self._is_closing:
            try:
                method = self.clicker_panel.click_method
                if method == "cdp":
                    grabber = self._ensure_grabber()
                    if grabber and grabber.connect_to_chrome():
                        try:
                            grabber.click_get_next_task_cdp()
                            now = datetime.now().strftime("%H:%M:%S")
                            signals.set_clicker_last.emit(f"Last click: {now} (CDP)")
                        except Exception as e:
                            signals.set_clicker_last.emit(f"CDP click error: {e}")
                elif method == "xy":
                    x, y = self.clicker_panel.click_x, self.clicker_panel.click_y
                    pyautogui.click(x, y)
                    now = datetime.now().strftime("%H:%M:%S")
                    signals.set_clicker_last.emit(f"Last click: {now} (X/Y {x},{y})")
                else:
                    grabber = self._ensure_grabber()
                    if grabber and grabber.connect_to_chrome():
                        try:
                            grabber.click_get_next_task()
                            now = datetime.now().strftime("%H:%M:%S")
                            signals.set_clicker_last.emit(f"Last click: {now} (Browser)")
                        except Exception:
                            pass
            except Exception as exc:
                print(f"Auto-clicker error: {exc}")
            time.sleep(auto_clicker_interval[0])

    def toggle_invisit_clicker(self) -> None:
        if self.invisit_running:
            self.invisit_running = False
            self.clicker_panel.btn_invisit_clicker.setText("Autoclick: In-Visit")
            signals.set_clicker_status.emit("STOPPED", "red")
        else:
            self.invisit_running = True
            self.clicker_panel.btn_invisit_clicker.setText("STOP In-Visit Clicker")
            signals.set_clicker_status.emit("RUNNING (In-Visit)", "green")
            self.invisit_thread = threading.Thread(
                target=self._invisit_clicker_loop, daemon=True
            )
            self.invisit_thread.start()

    def _invisit_clicker_loop(self) -> None:
        while self.invisit_running and not self._is_closing:
            try:
                grabber = self._ensure_grabber()
                if grabber and grabber.connect_to_chrome():
                    page = getattr(grabber, '_page', None)
                    if page:
                        # Click floating action button
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
                        now = datetime.now().strftime("%H:%M:%S")
                        signals.set_clicker_last.emit(f"Last click: {now} (In-Visit)")
            except Exception as exc:
                print(f"In-visit clicker error: {exc}")
            time.sleep(auto_clicker_interval[0])

    def toggle_page_refresh(self) -> None:
        if self.page_refresh_running:
            self.page_refresh_running = False
            self.clicker_panel.btn_page_refresh.setText("Auto-Refresh Page")
        else:
            self.page_refresh_running = True
            self.clicker_panel.btn_page_refresh.setText("STOP Auto-Refresh")
            threading.Thread(target=self._page_refresh_loop, daemon=True).start()

    def _page_refresh_loop(self) -> None:
        while self.page_refresh_running and not self._is_closing:
            try:
                grabber = self._ensure_grabber()
                if grabber and grabber.connect_to_chrome():
                    page = getattr(grabber, '_page', None)
                    if page:
                        page.reload()
            except Exception as exc:
                print(f"Page refresh error: {exc}")
            time.sleep(max(auto_clicker_interval[0], 5))

    def quick_next_task(self) -> None:
        def do_quick():
            grabber = self._ensure_grabber()
            if grabber and grabber.connect_to_chrome():
                try:
                    grabber.click_get_next_task_cdp()
                    signals.set_status.emit("Quick next task ✓")
                except Exception as exc:
                    signals.set_status.emit(f"Quick next task error: {exc}")
        threading.Thread(target=do_quick, daemon=True).start()

    def test_chrome_connection(self) -> None:
        def do_test():
            grabber = self._ensure_grabber()
            if grabber and grabber.connect_to_chrome():
                signals.set_status.emit("Chrome connected ✓")
                _on_main(lambda: self.badge_connection.set_status("  Chrome: OK", ACCENT_GREEN))
            else:
                signals.set_status.emit("Chrome connection failed ✗")
                _on_main(lambda: self.badge_connection.set_status("  Chrome: FAIL", ACCENT_RED))
        threading.Thread(target=do_test, daemon=True).start()

    def detect_visit_type_and_switch(self) -> None:
        def do_detect():
            grabber = self._ensure_grabber()
            if grabber and grabber.connect_to_chrome():
                vt = self._classify_visit_type(grabber)
                if vt:
                    _on_main(lambda: self._switch_visit_by_name(vt))
                    signals.set_status.emit(f"Detected: {vt}")
                else:
                    signals.set_status.emit("Visit type not detected")
        threading.Thread(target=do_detect, daemon=True).start()

    # ==================================================================
    # CDP Visit Monitor
    # ==================================================================

    def _start_cdp_visit_monitor(self) -> None:
        threading.Thread(target=self._cdp_visit_monitor_loop, daemon=True).start()

    def _cdp_visit_monitor_loop(self) -> None:
        last_detected = None
        while self._cdp_monitor_running and not self._is_closing:
            try:
                grabber = self._ensure_grabber()
                if grabber is None:
                    time.sleep(5)
                    continue
                if not grabber.connect_to_chrome():
                    time.sleep(5)
                    continue
                visit_type = self._classify_visit_type(grabber)
                if visit_type and visit_type != last_detected:
                    last_detected = visit_type
                    _on_main(lambda vt=visit_type: self._switch_visit_by_name(vt))
                    if self._auto_grab_enabled:
                        _on_main(
                            lambda vt=visit_type: QTimer.singleShot(
                                AUTO_GRAB_DELAY_MS,
                                self._do_grab_for_current_visit,
                            )
                        )
            except Exception as exc:
                print(f"CDP monitor error: {exc}")
            time.sleep(CDP_POLL_INTERVAL_SEC)

    def _classify_visit_type(self, grabber) -> Optional[str]:
        try:
            url = ""
            try:
                if grabber.driver:
                    url = grabber.driver.current_url or ""
            except Exception:
                pass
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
            ]
            for pattern, canonical in patterns:
                if pattern in combined:
                    return canonical
            return None
        except Exception:
            return None

    # ==================================================================
    # Cleanup
    # ==================================================================

    def closeEvent(self, a0) -> None:  # noqa: N803
        self._is_closing = True
        self._cdp_monitor_running = False
        auto_clicker_enabled[0] = False
        self.invisit_running = False
        self.page_refresh_running = False
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        super().closeEvent(a0)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════
def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("EMR Dashboard")
    app.setStyleSheet(DARK_THEME_QSS)

    win = Dashboard()
    win.show()

    # Window-tracker thread
    threading.Thread(target=window_tracker, daemon=True).start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
