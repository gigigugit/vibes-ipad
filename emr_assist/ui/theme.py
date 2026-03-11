"""Dark-mode QSS theme for the EMR Assist Dashboard.

All custom styling lives here so the dashboard never shows default Qt widgets.
Colours use a VSCode-dark-inspired palette.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
BG_DARKEST = "#1a1a2e"
BG_DARK = "#16213e"
BG_PANEL = "#1e2a45"
BG_CARD = "#253553"
BG_INPUT = "#2a3a5c"
BG_HOVER = "#324a73"
BG_PRESSED = "#3b5998"

TEXT_PRIMARY = "#e0e0e0"
TEXT_SECONDARY = "#8899aa"
TEXT_MUTED = "#5c6b7a"
TEXT_DARK = "#1a1a2e"

ACCENT_BLUE = "#4fc3f7"
ACCENT_GREEN = "#66bb6a"
ACCENT_RED = "#ef5350"
ACCENT_YELLOW = "#ffca28"
ACCENT_ORANGE = "#ffa726"
ACCENT_PURPLE = "#ab47bc"

BORDER_SUBTLE = "#2e4066"
BORDER_FOCUS = "#4fc3f7"

SCROLLBAR_BG = "#1a1a2e"
SCROLLBAR_HANDLE = "#3b5998"
SCROLLBAR_HOVER = "#4fc3f7"

# ---------------------------------------------------------------------------
# QSS stylesheet
# ---------------------------------------------------------------------------

DARK_THEME_QSS = f"""
/* ── Global ── */
QMainWindow, QWidget {{
    background-color: {BG_DARKEST};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

/* ── Dock widgets ── */
QDockWidget {{
    background-color: {BG_DARK};
    color: {TEXT_PRIMARY};
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}
QDockWidget::title {{
    background-color: {BG_PANEL};
    color: {TEXT_SECONDARY};
    padding: 6px 10px;
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}}
QDockWidget::close-button, QDockWidget::float-button {{
    border: none;
    background: transparent;
    padding: 2px;
}}

/* ── Scroll area ── */
QScrollArea {{
    background-color: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: {SCROLLBAR_BG};
    width: 8px;
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {SCROLLBAR_HANDLE};
    min-height: 30px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: {SCROLLBAR_HOVER};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {SCROLLBAR_BG};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {SCROLLBAR_HANDLE};
    min-width: 30px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {SCROLLBAR_HOVER};
}}

/* ── Labels ── */
QLabel {{
    color: {TEXT_PRIMARY};
    background: transparent;
    padding: 0;
}}
QLabel[role="heading"] {{
    font-size: 15px;
    font-weight: bold;
    color: {ACCENT_BLUE};
    padding: 4px 0;
}}
QLabel[role="secondary"] {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
}}
QLabel[role="value"] {{
    font-size: 14px;
    font-weight: bold;
    color: {TEXT_PRIMARY};
}}

/* ── Push buttons (default) ── */
QPushButton {{
    background-color: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    padding: 7px 14px;
    font-size: 12px;
    font-weight: 500;
    min-height: 28px;
}}
QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {ACCENT_BLUE};
}}
QPushButton:pressed {{
    background-color: {BG_PRESSED};
}}
QPushButton:disabled {{
    background-color: {BG_DARK};
    color: {TEXT_MUTED};
    border-color: {BG_DARK};
}}

/* ── Accent button variants ── */
QPushButton[accent="blue"] {{
    background-color: {ACCENT_BLUE};
    color: {TEXT_DARK};
    border: none;
    font-weight: bold;
}}
QPushButton[accent="blue"]:hover {{
    background-color: #29b6f6;
}}
QPushButton[accent="green"] {{
    background-color: {ACCENT_GREEN};
    color: {TEXT_DARK};
    border: none;
    font-weight: bold;
}}
QPushButton[accent="green"]:hover {{
    background-color: #43a047;
}}
QPushButton[accent="red"] {{
    background-color: {ACCENT_RED};
    color: {TEXT_DARK};
    border: none;
    font-weight: bold;
}}
QPushButton[accent="red"]:hover {{
    background-color: #e53935;
}}

/* ── Line edits ── */
QLineEdit {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
    selection-background-color: {ACCENT_BLUE};
    selection-color: {TEXT_DARK};
}}
QLineEdit:focus {{
    border-color: {ACCENT_BLUE};
}}
QLineEdit:read-only {{
    background-color: {BG_DARK};
    color: {TEXT_SECONDARY};
}}

/* ── Plain text edit ── */
QPlainTextEdit {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    padding: 6px;
    font-size: 13px;
    selection-background-color: {ACCENT_BLUE};
}}
QPlainTextEdit:focus {{
    border-color: {ACCENT_BLUE};
}}

/* ── Combo boxes ── */
QComboBox {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 26px;
}}
QComboBox:hover {{
    border-color: {ACCENT_BLUE};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    selection-background-color: {BG_HOVER};
    selection-color: {TEXT_PRIMARY};
}}

/* ── Group boxes ── */
QGroupBox {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 8px 8px 8px;
    font-weight: bold;
    color: {TEXT_SECONDARY};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 8px;
    color: {ACCENT_BLUE};
    font-size: 11px;
}}

/* ── Frames / separators ── */
QFrame[frameShape="4"] {{
    /* HLine */
    background-color: {BORDER_SUBTLE};
    max-height: 1px;
    border: none;
}}
QFrame[frameShape="5"] {{
    /* VLine */
    background-color: {BORDER_SUBTLE};
    max-width: 1px;
    border: none;
}}

/* ── Tool tips ── */
QToolTip {{
    background-color: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}}

/* ── Stacked widget ── */
QStackedWidget {{
    background-color: transparent;
}}
"""
