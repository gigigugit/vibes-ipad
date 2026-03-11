"""UI loader utilities for loading Qt Designer .ui files.

Provides helpers to load .ui files and map promoted custom widgets to
the actual Python widget classes used in the EMR Assist dashboard.

Usage
-----
    from emr_assist.ui.ui_loader import load_ui

    widget = load_ui("auto_clicker_panel.ui")
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtWidgets import QWidget
from PyQt6 import uic

from .designer import DESIGNER_DIR


def load_ui(
    ui_filename: str,
    base_instance: Optional[QWidget] = None,
) -> QWidget:
    """Load a .ui file from the designer directory.

    Parameters
    ----------
    ui_filename : str
        Name of the .ui file (e.g. ``"dashboard_main.ui"``).
    base_instance : QWidget | None
        If provided, the UI is loaded onto this widget instance
        (the widget's class must match the root widget in the .ui file).
        Otherwise a new widget is created and returned.

    Returns
    -------
    QWidget
        The loaded widget tree.
    """
    ui_path = os.path.join(DESIGNER_DIR, ui_filename)
    if not os.path.exists(ui_path):
        raise FileNotFoundError(f"UI file not found: {ui_path}")

    if base_instance is not None:
        uic.loadUi(ui_path, base_instance)
        return base_instance
    else:
        return uic.loadUi(ui_path)


def get_ui_path(ui_filename: str) -> str:
    """Return the absolute path to a .ui file in the designer directory.

    Parameters
    ----------
    ui_filename : str
        Name of the .ui file.

    Returns
    -------
    str
        Absolute file path.
    """
    return os.path.join(DESIGNER_DIR, ui_filename)
