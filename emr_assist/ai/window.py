"""AI Assist — general-purpose floating PyQt6 window for local Ollama generation.

Two independent output panels, each with its own editable sub-prompt.
A collapsible system prompt (blank by default) applies to both panels.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .ollama_client import OllamaClient

# ---------------------------------------------------------------------------
# Theme colours
# ---------------------------------------------------------------------------
BG_DARKEST = "#1a1a2e"
BG_DARK = "#16213e"
BG_PANEL = "#1e2a45"
BG_CARD = "#253553"
BG_INPUT = "#2a3a5c"
BG_HOVER = "#324a73"
TEXT_PRIMARY = "#e0e0e0"
TEXT_SECONDARY = "#8899aa"
TEXT_MUTED = "#5c6b7a"
ACCENT_BLUE = "#4fc3f7"
ACCENT_GREEN = "#66bb6a"
ACCENT_RED = "#ef5350"
ACCENT_ORANGE = "#ffa726"
ACCENT_PURPLE = "#ba68c8"
BORDER_SUBTLE = "#2e4066"

_QSS = f"""
QMainWindow, QWidget {{
    background-color: {BG_DARKEST};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}
QLabel {{
    background: transparent;
}}
QComboBox {{
    background-color: {BG_INPUT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;
    padding: 5px 10px; min-height: 26px;
}}
QComboBox:hover {{ border-color: {ACCENT_BLUE}; }}
QComboBox QAbstractItemView {{
    background-color: {BG_CARD}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    selection-background-color: {BG_HOVER};
}}
QPushButton {{
    background-color: {BG_CARD}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;
    padding: 7px 14px; font-size: 12px; font-weight: 500; min-height: 28px;
}}
QPushButton:hover {{ background-color: {BG_HOVER}; border-color: {ACCENT_BLUE}; }}
QPushButton:pressed {{ background-color: #3b5998; }}
QPushButton:disabled {{ background-color: {BG_DARK}; color: {TEXT_MUTED}; }}
QPushButton[accent="blue"] {{
    background-color: {ACCENT_BLUE}; color: {BG_DARKEST};
    border: none; font-weight: bold;
}}
QPushButton[accent="blue"]:hover {{ background-color: #29b6f6; }}
QPushButton[accent="green"] {{
    background-color: {ACCENT_GREEN}; color: {BG_DARKEST};
    border: none; font-weight: bold;
}}
QPushButton[accent="green"]:hover {{ background-color: #43a047; }}
QPushButton[accent="red"] {{
    background-color: {ACCENT_RED}; color: {BG_DARKEST};
    border: none; font-weight: bold;
}}
QPushButton[accent="purple"] {{
    background-color: {ACCENT_PURPLE}; color: {BG_DARKEST};
    border: none; font-weight: bold;
}}
QPushButton[accent="purple"]:hover {{ background-color: #ab47bc; }}
QPlainTextEdit, QTextEdit {{
    background-color: {BG_INPUT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;
    padding: 6px; font-size: 13px;
    selection-background-color: {ACCENT_BLUE};
}}
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT_BLUE}; }}
QScrollArea {{ background-color: transparent; border: none; }}
QFrame[frameShape="4"] {{ color: {BORDER_SUBTLE}; }}
"""

# Panel display metadata
_NUM_PANELS = 2
_PANEL_META = [
    {"title": "Output 1", "accent": ACCENT_BLUE},
    {"title": "Output 2", "accent": ACCENT_PURPLE},
]


# ---------------------------------------------------------------------------
# Signals for thread-safe streaming updates
# ---------------------------------------------------------------------------

class _StreamSignals(QObject):
    token = pyqtSignal(str, int)        # (token_text, panel_index)
    panel_done = pyqtSignal(int)        # panel_index
    all_finished = pyqtSignal()
    error = pyqtSignal(str, int)        # (error_msg, panel_index)


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
_SETTINGS_PATH = os.path.join(
    os.getenv("APPDATA") or os.path.expanduser("~"),
    "emr_assist",
    "ai_assist_settings.json",
)


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(settings: dict) -> None:
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AIAssistWindow(QMainWindow):
    """Floating AI-generation window with two independent output panels."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI Assist")
        self.setMinimumSize(540, 580)
        self.resize(560, 820)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet(_QSS)

        # --- Settings ---
        self._settings = _load_settings()

        # --- Backend ---
        self._client = OllamaClient()
        self._generating = False
        self._stop_requested = False

        # --- Stream signals ---
        self._sig = _StreamSignals()
        self._sig.token.connect(self._on_token)
        self._sig.panel_done.connect(self._on_panel_done)
        self._sig.all_finished.connect(self._on_all_done)
        self._sig.error.connect(self._on_panel_error)

        # --- Per-panel widgets (populated by _build_output_panel) ---
        self._sub_prompt_edits: List[QPlainTextEdit] = []
        self._output_edits: List[QTextEdit] = []
        self._panel_gen_btns: List[QPushButton] = []
        self._panel_status_labels: List[QLabel] = []
        self._panel_containers: List[QWidget] = []

        # Output 2 collapse state (persisted)
        self._panel2_visible: bool = self._settings.get("panel2_visible", True)

        self._build_ui()
        self._setup_shortcuts()

        QTimer.singleShot(100, self._initial_load)

    # ==================================================================
    # UI construction
    # ==================================================================

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        self._build_top_bar(root)
        self._build_server_bar(root)
        self._build_system_prompt(root)

        # ── Generate-all row ──
        gen_row = QHBoxLayout()
        self._generate_all_btn = QPushButton("⚡  Generate All")
        self._generate_all_btn.setProperty("accent", "blue")
        self._generate_all_btn.clicked.connect(self._on_generate_all)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setProperty("accent", "red")
        self._stop_btn.setFixedWidth(60)
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._on_stop)
        self._gen_status_label = QLabel("")
        self._gen_status_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        gen_row.addWidget(self._generate_all_btn, 1)
        gen_row.addWidget(self._gen_status_label)
        gen_row.addWidget(self._stop_btn)
        root.addLayout(gen_row)

        # ── Output panels inside scroll area ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_inner = QWidget()
        self._panels_layout = QVBoxLayout(scroll_inner)
        self._panels_layout.setContentsMargins(0, 0, 0, 0)
        self._panels_layout.setSpacing(8)

        for idx in range(_NUM_PANELS):
            self._build_output_panel(idx)

        # Output 2 collapse toggle (sits between the two panels)
        self._panel2_toggle = QPushButton(
            "▼  Output 2" if self._panel2_visible else "▶  Output 2"
        )
        self._panel2_toggle.setStyleSheet(
            f"text-align: left; padding-left: 8px; color: {ACCENT_PURPLE}; "
            f"font-size: 12px; font-weight: bold; border: none; background: transparent;"
        )
        self._panel2_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._panel2_toggle.clicked.connect(self._toggle_panel2)
        # Insert toggle before the Output 2 container
        self._panels_layout.insertWidget(1, self._panel2_toggle)
        # Apply initial visibility
        self._panel_containers[1].setVisible(self._panel2_visible)
        self._update_generate_all_label()

        scroll.setWidget(scroll_inner)
        root.addWidget(scroll, 1)

        # ── Bottom bar ──
        self._build_action_bar(root)

    # -- Sub-builders --

    def _build_top_bar(self, parent: QVBoxLayout) -> None:
        top = QHBoxLayout()
        top.setSpacing(8)

        lbl_model = QLabel("Model:")
        lbl_model.setFixedWidth(42)
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(180)
        self._model_combo.currentTextChanged.connect(self._on_model_changed)

        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(20)
        self._status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label = QLabel("Checking…")
        self._status_label.setStyleSheet(f"color: {TEXT_SECONDARY};")

        self._refresh_models_btn = QPushButton("↻")
        self._refresh_models_btn.setFixedSize(28, 28)
        self._refresh_models_btn.setToolTip("Refresh model list")
        self._refresh_models_btn.clicked.connect(self._refresh_models)

        top.addWidget(lbl_model)
        top.addWidget(self._model_combo, 1)
        top.addWidget(self._refresh_models_btn)
        top.addStretch()
        top.addWidget(self._status_dot)
        top.addWidget(self._status_label)
        parent.addLayout(top)

    def _build_server_bar(self, parent: QVBoxLayout) -> None:
        srv_bar = QHBoxLayout()
        srv_bar.setSpacing(8)

        self._server_btn = QPushButton("Start Ollama")
        self._server_btn.setFixedHeight(26)
        self._server_btn.clicked.connect(self._on_toggle_server)

        self._autostart_chk = QCheckBox("Auto-start on launch")
        self._autostart_chk.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        self._autostart_chk.setChecked(self._settings.get("auto_start_ollama", False))
        self._autostart_chk.toggled.connect(self._on_autostart_toggled)

        srv_bar.addWidget(self._server_btn)
        srv_bar.addWidget(self._autostart_chk)
        srv_bar.addStretch()
        parent.addLayout(srv_bar)

    def _build_system_prompt(self, parent: QVBoxLayout) -> None:
        self._sys_prompt_toggle = QPushButton("▶  System Prompt")
        self._sys_prompt_toggle.setStyleSheet(
            f"text-align: left; padding-left: 8px; color: {TEXT_SECONDARY}; "
            f"font-size: 12px; border: none; background: transparent;"
        )
        self._sys_prompt_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sys_prompt_toggle.clicked.connect(self._toggle_system_prompt)

        self._sys_prompt_edit = QPlainTextEdit()
        self._sys_prompt_edit.setPlaceholderText("System prompt (sent as the system message)…")
        self._sys_prompt_edit.setMaximumHeight(120)
        self._sys_prompt_edit.setVisible(False)
        saved_sys = self._settings.get("system_prompt", "")
        self._sys_prompt_edit.setPlainText(saved_sys)
        self._sys_prompt_edit.textChanged.connect(self._on_system_prompt_changed)

        parent.addWidget(self._sys_prompt_toggle)
        parent.addWidget(self._sys_prompt_edit)

    def _build_output_panel(self, idx: int) -> None:
        """Build one output panel: sub-prompt → generate → output → actions."""
        meta = _PANEL_META[idx]
        container = QWidget()
        container.setStyleSheet(
            f"background: {BG_PANEL}; border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # Title
        title = QLabel(meta["title"])
        title.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {meta['accent']}; border: none;")
        layout.addWidget(title)

        # Sub-prompt
        sub_prompt = QPlainTextEdit()
        sub_prompt.setPlaceholderText(f"Sub-prompt for {meta['title']}…")
        sub_prompt.setMaximumHeight(80)
        sub_prompt.setStyleSheet(f"border: 1px solid {BORDER_SUBTLE}; border-radius: 4px;")
        saved_sub = self._settings.get(f"sub_prompt_{idx}", "")
        sub_prompt.setPlainText(saved_sub)
        sub_prompt.textChanged.connect(lambda i=idx: self._on_sub_prompt_changed(i))
        layout.addWidget(sub_prompt)
        self._sub_prompt_edits.append(sub_prompt)

        # Panel generate button
        gen_btn = QPushButton(f"Generate {meta['title']}")
        gen_btn.setProperty("accent", "blue")
        gen_btn.setStyleSheet(gen_btn.styleSheet() + "font-size: 11px; padding: 4px 10px; min-height: 24px;")
        gen_btn.clicked.connect(lambda i=idx: self._on_generate_single(i))
        layout.addWidget(gen_btn)
        self._panel_gen_btns.append(gen_btn)

        # Output
        edit = QTextEdit()
        edit.setPlaceholderText(f"Output will appear here…")
        edit.setFont(QFont("Consolas", 11))
        edit.setMinimumHeight(100)
        edit.setStyleSheet(f"border: 1px solid {BORDER_SUBTLE}; border-radius: 4px;")
        layout.addWidget(edit, 1)
        self._output_edits.append(edit)

        # Status + actions
        status_lbl = QLabel("")
        status_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border: none;")
        self._panel_status_labels.append(status_lbl)

        actions = QHBoxLayout()
        actions.setSpacing(4)

        copy_btn = QPushButton("Copy")
        copy_btn.setStyleSheet("font-size: 11px; padding: 4px 8px; min-height: 22px;")
        copy_btn.clicked.connect(lambda i=idx: self._copy_panel(i))

        paste_btn = QPushButton("📋  Paste")
        paste_btn.setProperty("accent", "green")
        paste_btn.setStyleSheet(paste_btn.styleSheet() + "font-size: 11px; padding: 4px 8px; min-height: 22px;")
        paste_btn.clicked.connect(lambda i=idx: self._paste_panel(i))

        clear_btn = QPushButton("Clear")
        clear_btn.setStyleSheet("font-size: 11px; padding: 4px 8px; min-height: 22px;")
        clear_btn.clicked.connect(lambda i=idx: self._clear_panel(i))

        actions.addWidget(status_lbl)
        actions.addStretch()
        actions.addWidget(paste_btn)
        actions.addWidget(copy_btn)
        actions.addWidget(clear_btn)
        layout.addLayout(actions)

        self._panels_layout.addWidget(container)
        self._panel_containers.append(container)

    def _build_action_bar(self, parent: QVBoxLayout) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        parent.addWidget(sep)

        actions = QHBoxLayout()
        actions.setSpacing(6)

        self._copy_all_btn = QPushButton("Copy All")
        self._copy_all_btn.clicked.connect(self._on_copy_all)

        self._clear_all_btn = QPushButton("Clear All")
        self._clear_all_btn.clicked.connect(self._on_clear_all)

        actions.addStretch()
        actions.addWidget(self._copy_all_btn)
        actions.addWidget(self._clear_all_btn)
        parent.addLayout(actions)

    # ==================================================================
    # Keyboard shortcuts
    # ==================================================================

    def _setup_shortcuts(self) -> None:
        return

    # ==================================================================
    # Output 2 collapse
    # ==================================================================

    def _toggle_panel2(self) -> None:
        self._panel2_visible = not self._panel2_visible
        self._panel_containers[1].setVisible(self._panel2_visible)
        arrow = "▼" if self._panel2_visible else "▶"
        self._panel2_toggle.setText(f"{arrow}  Output 2")
        self._settings["panel2_visible"] = self._panel2_visible
        _save_settings(self._settings)
        self._update_generate_all_label()

    def _update_generate_all_label(self) -> None:
        if self._panel2_visible:
            self._generate_all_btn.setText("⚡  Generate All")
        else:
            self._generate_all_btn.setText("⚡  Generate")

    # ==================================================================
    # Initial load
    # ==================================================================

    def _initial_load(self) -> None:
        if self._settings.get("auto_start_ollama", False):
            self._start_ollama_async()
        else:
            self._refresh_models()

    def _refresh_models(self) -> None:
        models = self._client.list_models()
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        if models:
            self._model_combo.addItems(models)
            self._set_status(True)
            saved = self._settings.get("selected_model", "")
            restored = False
            if saved:
                for i, m in enumerate(models):
                    if m == saved:
                        self._model_combo.setCurrentIndex(i)
                        restored = True
                        break
            if not restored:
                for i, m in enumerate(models):
                    if "llama3.2" in m.lower():
                        self._model_combo.setCurrentIndex(i)
                        break
        else:
            self._model_combo.addItem("(no models found)")
            self._set_status(False)
        self._model_combo.blockSignals(False)
        self._on_model_changed(self._model_combo.currentText())

    def _set_status(self, connected: bool) -> None:
        if connected:
            self._status_dot.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 16px;")
            self._status_label.setText("Connected")
            self._status_label.setStyleSheet(f"color: {ACCENT_GREEN};")
            self._server_btn.setText("Stop Ollama")
        else:
            self._status_dot.setStyleSheet(f"color: {ACCENT_RED}; font-size: 16px;")
            self._status_label.setText("Disconnected")
            self._status_label.setStyleSheet(f"color: {ACCENT_RED};")
            self._server_btn.setText("Start Ollama")

    # ==================================================================
    # Ollama server control
    # ==================================================================

    def _on_toggle_server(self) -> None:
        if self._client.is_available():
            self._client.stop_server()
            self._set_status(False)
            self._model_combo.clear()
            self._model_combo.addItem("(no models found)")
            self._status_label.setText("Stopped")
        else:
            self._start_ollama_async()

    def _start_ollama_async(self) -> None:
        self._server_btn.setEnabled(False)
        self._status_label.setText("Starting…")
        self._status_label.setStyleSheet(f"color: {ACCENT_ORANGE};")
        threading.Thread(target=self._server_start_worker, daemon=True).start()

    def _server_start_worker(self) -> None:
        try:
            ok = self._client.start_server(wait=True, timeout_sec=25)
            QTimer.singleShot(0, lambda: self._on_server_started(ok))
        except FileNotFoundError as exc:
            QTimer.singleShot(0, lambda: self._on_server_start_error(str(exc)))
        except Exception as exc:
            QTimer.singleShot(0, lambda: self._on_server_start_error(str(exc)))

    def _on_server_started(self, ok: bool) -> None:
        self._server_btn.setEnabled(True)
        if ok:
            self._refresh_models()
        else:
            self._set_status(False)
            self._status_label.setText("Start timed out")
            self._status_label.setStyleSheet(f"color: {ACCENT_ORANGE};")

    def _on_server_start_error(self, err: str) -> None:
        self._server_btn.setEnabled(True)
        self._set_status(False)
        self._status_label.setText(err[:60])
        self._status_label.setStyleSheet(f"color: {ACCENT_RED};")

    def _on_autostart_toggled(self, checked: bool) -> None:
        self._settings["auto_start_ollama"] = checked
        _save_settings(self._settings)

    # ==================================================================
    # System prompt
    # ==================================================================

    def _toggle_system_prompt(self) -> None:
        visible = not self._sys_prompt_edit.isVisible()
        self._sys_prompt_edit.setVisible(visible)
        arrow = "▼" if visible else "▶"
        self._sys_prompt_toggle.setText(f"{arrow}  System Prompt")

    def _on_system_prompt_changed(self) -> None:
        self._settings["system_prompt"] = self._sys_prompt_edit.toPlainText()
        _save_settings(self._settings)

    # ==================================================================
    # Sub-prompt persistence
    # ==================================================================

    def _on_sub_prompt_changed(self, idx: int) -> None:
        self._settings[f"sub_prompt_{idx}"] = self._sub_prompt_edits[idx].toPlainText()
        _save_settings(self._settings)

    # ==================================================================
    # Model
    # ==================================================================

    def _on_model_changed(self, model_name: str) -> None:
        if model_name and model_name != "(no models found)":
            self._client.model = model_name
            self._settings["selected_model"] = model_name
            _save_settings(self._settings)

    # ==================================================================
    # Message builder
    # ==================================================================

    def _build_messages(self, idx: int) -> List[dict]:
        """Build [system, user] message list for the given panel."""
        msgs: List[dict] = []
        sys_text = self._sys_prompt_edit.toPlainText().strip()
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        user_text = self._sub_prompt_edits[idx].toPlainText().strip()
        if user_text:
            msgs.append({"role": "user", "content": user_text})
        return msgs

    # ==================================================================
    # Generation — all panels sequentially
    # ==================================================================

    def _active_panel_indices(self) -> List[int]:
        """Return indices of panels that should participate in generation."""
        indices = [0]
        if self._panel2_visible:
            indices.append(1)
        return indices

    def _on_generate_all(self) -> None:
        if self._generating:
            return
        active = self._active_panel_indices()
        # Need at least one non-empty sub-prompt among active panels
        if not any(self._sub_prompt_edits[i].toPlainText().strip() for i in active):
            return

        self._generating = True
        self._stop_requested = False
        self._gen_start = time.time()
        self._generate_all_btn.setEnabled(False)
        for btn in self._panel_gen_btns:
            btn.setEnabled(False)
        self._stop_btn.setVisible(True)
        self._gen_status_label.setText("generating…")

        for i in active:
            self._output_edits[i].clear()
            self._panel_status_labels[i].setText("")

        all_msgs = [(i, self._build_messages(i)) for i in active]

        threading.Thread(
            target=self._generate_all_worker,
            args=(all_msgs,),
            daemon=True,
        ).start()

    def _generate_all_worker(self, all_msgs: List[tuple]) -> None:
        try:
            for panel_idx, msgs in all_msgs:
                if self._stop_requested:
                    break
                if not msgs or not any(m["role"] == "user" for m in msgs):
                    self._sig.panel_done.emit(panel_idx)
                    continue
                try:
                    for token in self._client.generate_stream(msgs):
                        if self._stop_requested:
                            break
                        self._sig.token.emit(token, panel_idx)
                    self._sig.panel_done.emit(panel_idx)
                except Exception as exc:
                    self._sig.error.emit(str(exc), panel_idx)
                    break
            self._sig.all_finished.emit()
        except Exception as exc:
            self._sig.error.emit(str(exc), 0)
            self._sig.all_finished.emit()

    # ==================================================================
    # Generation — single panel
    # ==================================================================

    def _on_generate_single(self, idx: int) -> None:
        if self._generating:
            return
        # Don't generate collapsed panel
        if idx == 1 and not self._panel2_visible:
            return
        msgs = self._build_messages(idx)
        if not msgs or not any(m["role"] == "user" for m in msgs):
            return

        self._generating = True
        self._stop_requested = False
        self._gen_start = time.time()
        self._generate_all_btn.setEnabled(False)
        for btn in self._panel_gen_btns:
            btn.setEnabled(False)
        self._stop_btn.setVisible(True)
        self._output_edits[idx].clear()
        self._panel_status_labels[idx].setText("generating…")

        threading.Thread(
            target=self._generate_single_worker,
            args=(msgs, idx),
            daemon=True,
        ).start()

    def _generate_single_worker(self, msgs: List[dict], idx: int) -> None:
        try:
            for token in self._client.generate_stream(msgs):
                if self._stop_requested:
                    break
                self._sig.token.emit(token, idx)
            self._sig.panel_done.emit(idx)
        except Exception as exc:
            self._sig.error.emit(str(exc), idx)
        self._sig.all_finished.emit()

    # ==================================================================
    # Stream signal handlers
    # ==================================================================

    def _on_token(self, token: str, idx: int) -> None:
        edit = self._output_edits[idx]
        cursor = edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(token)
        edit.setTextCursor(cursor)
        edit.ensureCursorVisible()
        elapsed = time.time() - self._gen_start
        self._gen_status_label.setText(f"Output {idx + 1} · {elapsed:.0f}s")

    def _on_panel_done(self, idx: int) -> None:
        elapsed = time.time() - self._gen_start
        self._panel_status_labels[idx].setText(f"done · {elapsed:.0f}s")

    def _on_all_done(self) -> None:
        self._generating = False
        self._generate_all_btn.setEnabled(True)
        for btn in self._panel_gen_btns:
            btn.setEnabled(True)
        self._stop_btn.setVisible(False)
        elapsed = time.time() - self._gen_start
        self._gen_status_label.setText(f"done · {elapsed:.0f}s")

    def _on_panel_error(self, err: str, idx: int) -> None:
        self._panel_status_labels[idx].setText(f"Error: {err[:60]}")
        edit = self._output_edits[idx]
        if not edit.toPlainText():
            edit.setPlainText(f"[Error] {err}")

    def _on_stop(self) -> None:
        self._stop_requested = True

    # ==================================================================
    # Clipboard helpers
    # ==================================================================

    def _clipboard_paste(self, text: str) -> None:
        """Copy text to clipboard, briefly hide window, paste, restore."""
        import pyperclip
        import pyautogui

        cached = ""
        try:
            cached = pyperclip.paste()
        except Exception:
            pass

        pyperclip.copy(text)
        time.sleep(0.05)
        self.hide()
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.1)
        self.show()

        try:
            pyperclip.copy(cached)
        except Exception:
            pass

    # ==================================================================
    # Per-panel actions
    # ==================================================================

    def _copy_panel(self, idx: int) -> None:
        text = self._output_edits[idx].toPlainText().strip()
        if text:
            QApplication.clipboard().setText(text)
            self._panel_status_labels[idx].setText("Copied ✓")

    def _paste_panel(self, idx: int) -> None:
        text = self._output_edits[idx].toPlainText().strip()
        if not text:
            return
        self._clipboard_paste(text)
        self._panel_status_labels[idx].setText("Pasted ✓")

    def _clear_panel(self, idx: int) -> None:
        self._output_edits[idx].clear()
        self._panel_status_labels[idx].setText("")

    # ==================================================================
    # Global actions
    # ==================================================================

    def _on_copy_all(self) -> None:
        parts = []
        for i in range(_NUM_PANELS):
            text = self._output_edits[i].toPlainText().strip()
            if text:
                parts.append(f"=== Output {i + 1} ===\n{text}")
        if parts:
            QApplication.clipboard().setText("\n\n".join(parts))
            self._gen_status_label.setText("Copied all ✓")

    def _on_clear_all(self) -> None:
        for i in range(_NUM_PANELS):
            self._output_edits[i].clear()
            self._panel_status_labels[i].setText("")
        self._gen_status_label.setText("")

    # ==================================================================
    # Cleanup
    # ==================================================================

    def closeEvent(self, event) -> None:
        self._stop_requested = True
        super().closeEvent(event)
