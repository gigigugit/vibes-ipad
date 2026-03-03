"""Shared UI helper functions — mostly text-insertion and window utilities.

These are the module-level utility functions that were originally outside
MyFrame in the monolithic .pyw file.  They have no wx dependency.
"""
from __future__ import annotations

import os
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import pyautogui
import pyperclip
import pygetwindow as gw

from ..core.config import LABS_CONFIG, dprint
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
from ..core import state


# ---------------------------------------------------------------------------
# Text insertion helpers
# ---------------------------------------------------------------------------

def type_template_text(text: str, prepend_enter: bool = True, interval: float = 0.005) -> None:
    """Insert template text quickly via clipboard paste, with typing fallback."""

    def _fallback_type() -> None:
        if prepend_enter:
            pyautogui.press("enter")
            time.sleep(0.02)
        pyautogui.typewrite(text, interval=interval)

    if not text:
        if prepend_enter:
            pyautogui.press("enter")
            time.sleep(0.02)
        return

    cached_clipboard = ""
    clipboard_cached = False
    try:
        cached_clipboard = pyperclip.paste()
        clipboard_cached = True
    except pyperclip.PyperclipException:
        pass

    try:
        pyperclip.copy(text)
        time.sleep(0.05)
        if prepend_enter:
            pyautogui.press("enter")
            time.sleep(0.02)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
    except pyperclip.PyperclipException:
        _fallback_type()
    except Exception:
        try:
            _fallback_type()
        except Exception:
            raise
    finally:
        if clipboard_cached:
            try:
                pyperclip.copy(cached_clipboard)
            except pyperclip.PyperclipException:
                pass


def clear_text_selection(main_window=None) -> None:
    """Collapse any active selection and return the EMR view to a clean state."""
    # Try CDP script reset first
    if main_window is not None:
        try:
            if main_window._reset_emr_view_via_script():
                print("Selection cleared via CDP script (scroll reset)")
                return
        except Exception as ce:
            print(f"Warning: script-based selection reset failed: {ce}")

    try:
        pyautogui.press('right')
        time.sleep(0.05)
        pyautogui.press('left')
        time.sleep(0.05)
        pyautogui.press('escape')
        time.sleep(0.05)
        pyautogui.hotkey('ctrl', 'end')
        time.sleep(0.08)
        pyautogui.hotkey('ctrl', 'home')
        time.sleep(0.08)
        print("Selection cleared and view reset")
    except Exception as ce:
        print(f"Warning: clear selection sequence failed: {ce}")


def window_tracker() -> None:
    """Background thread: track the last active non-EMR window."""
    while True:
        try:
            win = gw.getActiveWindow()
            if win and state.panel_title not in win.title:
                state.last_active_window = win
        except Exception:
            pass
        time.sleep(0.2)
