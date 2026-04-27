"""Thread-safe signal bridge replacing wx.CallAfter / wx.CallLater.

Every background thread emits signals here; the MainWindow connects slots
to update the GUI on the main thread.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal


class SignalBridge(QObject):
    """Singleton-like QObject that carries typed signals for thread→UI updates."""

    # --- Generic helpers ---
    call_on_main = pyqtSignal(object)           # callable (no args)
    call_on_main_args = pyqtSignal(object, object)  # callable, args-tuple

    # --- Status updates ---
    set_status = pyqtSignal(str)                # grab_status_text
    set_visit_type = pyqtSignal(str)            # visit_type_text
    set_location = pyqtSignal(str)              # patient_location_text
    set_clicker_status = pyqtSignal(str, str)   # text, color hex
    set_clicker_method = pyqtSignal(str, str)   # text, color hex
    set_clicker_last = pyqtSignal(str)          # last click time text
    set_url_display = pyqtSignal(str)           # current URL

    # --- Tab 1: Hair Loss ---
    set_hair_med = pyqtSignal(str)
    set_hair_hsx = pyqtSignal(str)
    set_hair_hvar = pyqtSignal(str)

    # --- Tab 3: Photoaging ---
    set_photoaging_med = pyqtSignal(str)
    set_photoaging_goals = pyqtSignal(str)
    set_photoaging_retinoid = pyqtSignal(str)

    # --- Tab 4: Sexual Health ---
    set_sh_med = pyqtSignal(str)
    set_sh_effectiveness = pyqtSignal(str)
    set_sh_bp = pyqtSignal(str)
    set_sh_hair_location = pyqtSignal(str)
    set_sh_hair_sxx = pyqtSignal(str)
    set_sh_dx_ed = pyqtSignal(bool)
    set_sh_dx_pe = pyqtSignal(bool)
    set_sh_dx_pe_like = pyqtSignal(bool)
    set_sh_dx_hair_loss = pyqtSignal(bool)

    # --- Tab 5: Auto Clicker ---
    set_clicker_btn_text = pyqtSignal(str)
    set_invisit_btn_text = pyqtSignal(str)
    set_refresh_btn_text = pyqtSignal(str)

    # --- Tab 6: Performance Anxiety ---
    set_pa_med = pyqtSignal(str)
    set_pa_situations = pyqtSignal(str)
    set_pa_symptoms = pyqtSignal(str)
    set_pa_bp = pyqtSignal(str)
    set_pa_pulse = pyqtSignal(str)

    # --- Tab 7: Birth Control ---
    set_bc_med = pyqtSignal(str)
    set_bc_lmp = pyqtSignal(str)
    set_bc_bp = pyqtSignal(str)
    set_bc_side_effects = pyqtSignal(str)
    set_bc_initial_visit = pyqtSignal(bool)
    set_bc_history_changes = pyqtSignal(str)
    set_bc_pmh = pyqtSignal(list, str)  # selected keys list, other text

    # --- Popup / notification ---
    show_new_task_popup = pyqtSignal()
    beep = pyqtSignal()

    # --- Tab switching ---
    switch_tab = pyqtSignal(int)                # tab index
    switch_tab_by_name = pyqtSignal(str)        # canonical visit type name


# Module-level singleton (created once, imported everywhere)
signals = SignalBridge()
