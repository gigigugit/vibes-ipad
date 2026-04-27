"""Voice Assist — PyQt6 window for TTS generation with pluggable backends.

Reuses UI patterns and Ollama connectivity from the AI Assist window.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import List, Optional

import numpy as np

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .ollama_client import OllamaClient
from .audio_player import AudioPlayerWidget
from .voice_backends import (
    GenerationResult,
    VoiceBackend,
    VoiceInfo,
    get_all_backends,
    get_available_backends,
)

# ---------------------------------------------------------------------------
# Theme colours (shared with AI Assist)
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
ACCENT_TEAL = "#26c6da"
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
QPushButton[accent="teal"] {{
    background-color: {ACCENT_TEAL}; color: {BG_DARKEST};
    border: none; font-weight: bold;
}}
QPushButton[accent="teal"]:hover {{ background-color: #00bcd4; }}
QPlainTextEdit, QTextEdit {{
    background-color: {BG_INPUT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;
    padding: 6px; font-size: 13px;
    selection-background-color: {ACCENT_BLUE};
}}
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT_BLUE}; }}
QScrollArea {{ background-color: transparent; border: none; }}
QFrame[frameShape="4"] {{ color: {BORDER_SUBTLE}; }}
QSlider::groove:horizontal {{
    background: {BG_INPUT}; height: 6px; border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT_BLUE}; width: 14px;
    margin: -4px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT_BLUE}; border-radius: 3px; }}
QListWidget {{
    background-color: {BG_INPUT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;
    padding: 4px; font-size: 12px;
}}
QListWidget::item {{ padding: 4px 6px; border-radius: 3px; }}
QListWidget::item:selected {{ background-color: {BG_HOVER}; }}
QListWidget::item:hover {{ background-color: {BG_CARD}; }}
"""


# ---------------------------------------------------------------------------
# Signals for thread-safe TTS generation updates
# ---------------------------------------------------------------------------

class _VoiceSignals(QObject):
    generation_started = pyqtSignal()
    generation_done = pyqtSignal(object)      # GenerationResult
    generation_error = pyqtSignal(str)
    status_update = pyqtSignal(str)           # progress text


# ---------------------------------------------------------------------------
# Signals for thread-safe Ollama streaming
# ---------------------------------------------------------------------------

class _OllamaSignals(QObject):
    token = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
_SETTINGS_PATH = os.path.join(
    os.getenv("APPDATA") or os.path.expanduser("~"),
    "emr_assist",
    "voice_assist_settings.json",
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
# History item
# ---------------------------------------------------------------------------

class _HistoryEntry:
    __slots__ = ("timestamp", "text_snippet", "voice", "backend",
                 "duration", "audio", "sample_rate")

    def __init__(self, text: str, voice: str, backend: str,
                 result: GenerationResult) -> None:
        self.timestamp = datetime.now().strftime("%H:%M:%S")
        self.text_snippet = (text[:60] + "…") if len(text) > 60 else text
        self.voice = voice
        self.backend = backend
        self.duration = result.duration
        self.audio = result.audio
        self.sample_rate = result.sample_rate


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class VoiceAssistWindow(QMainWindow):
    """Floating TTS generation window with pluggable voice backends."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Voice Assist")
        self.setMinimumSize(580, 650)
        self.resize(620, 860)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet(_QSS)

        # --- Settings ---
        self._settings = _load_settings()

        # --- Backends ---
        self._backends: List[VoiceBackend] = []
        self._current_backend: Optional[VoiceBackend] = None
        self._voices: List[VoiceInfo] = []

        # --- Ollama ---
        self._ollama = OllamaClient()

        # --- Generation state ---
        self._generating = False
        self._stop_requested = False

        # --- Signals ---
        self._vsig = _VoiceSignals()
        self._vsig.generation_started.connect(self._on_gen_started)
        self._vsig.generation_done.connect(self._on_gen_done)
        self._vsig.generation_error.connect(self._on_gen_error)
        self._gen_start: float = 0.0
        self._ollama_gen_start: float = 0.0
        self._vsig.status_update.connect(self._on_status_update)

        self._osig = _OllamaSignals()
        self._osig.token.connect(self._on_ollama_token)
        self._osig.finished.connect(self._on_ollama_done)
        self._osig.error.connect(self._on_ollama_error)

        # --- History ---
        self._history: List[_HistoryEntry] = []

        self._build_ui()
        self._setup_shortcuts()
        self._load_backends()

        # Restore window geometry
        geo = self._settings.get("window_geometry")
        if geo:
            try:
                self.restoreGeometry(bytes.fromhex(geo))
            except Exception:
                pass

    # ==================================================================
    # UI construction
    # ==================================================================

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        self._build_backend_bar(root)
        self._build_voice_bar(root)
        self._build_text_input(root)
        self._build_generate_bar(root)
        self._build_audio_section(root)
        self._build_history_section(root)
        self._build_ollama_section(root)

    # -- Backend selector --

    def _build_backend_bar(self, parent: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)

        lbl = QLabel("Backend:")
        lbl.setFixedWidth(60)
        self._backend_combo = QComboBox()
        self._backend_combo.setMinimumWidth(160)
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)

        self._backend_status = QLabel("●")
        self._backend_status.setFixedWidth(20)
        self._backend_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._backend_status_label = QLabel("Loading…")
        self._backend_status_label.setStyleSheet(f"color: {TEXT_SECONDARY};")

        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedSize(28, 28)
        self._refresh_btn.setToolTip("Refresh backends")
        self._refresh_btn.clicked.connect(self._load_backends)

        row.addWidget(lbl)
        row.addWidget(self._backend_combo, 1)
        row.addWidget(self._refresh_btn)
        row.addStretch()
        row.addWidget(self._backend_status)
        row.addWidget(self._backend_status_label)
        parent.addLayout(row)

    # -- Voice selector + speed --

    def _build_voice_bar(self, parent: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)

        lbl = QLabel("Voice:")
        lbl.setFixedWidth(60)
        self._voice_combo = QComboBox()
        self._voice_combo.setMinimumWidth(200)
        self._voice_combo.currentIndexChanged.connect(self._on_voice_changed)

        speed_lbl = QLabel("Speed:")
        speed_lbl.setFixedWidth(42)
        self._speed_slider = QSlider(Qt.Orientation.Horizontal)
        self._speed_slider.setRange(50, 200)
        self._speed_slider.setValue(self._settings.get("speed", 100))
        self._speed_slider.setFixedWidth(100)
        self._speed_slider.setToolTip("Playback speed")
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        self._speed_label = QLabel(f"{self._speed_slider.value() / 100:.1f}x")
        self._speed_label.setFixedWidth(32)
        self._speed_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")

        row.addWidget(lbl)
        row.addWidget(self._voice_combo, 1)
        row.addWidget(speed_lbl)
        row.addWidget(self._speed_slider)
        row.addWidget(self._speed_label)
        parent.addLayout(row)

    # -- Text input --

    def _build_text_input(self, parent: QVBoxLayout) -> None:
        self._text_input = QPlainTextEdit()
        self._text_input.setPlaceholderText("Enter text to convert to speech…")
        self._text_input.setMinimumHeight(100)
        self._text_input.setMaximumHeight(200)
        saved = self._settings.get("input_text", "")
        if saved:
            self._text_input.setPlainText(saved)
        self._text_input.textChanged.connect(self._on_input_changed)
        parent.addWidget(self._text_input)

    # -- Generate / stop bar --

    def _build_generate_bar(self, parent: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)

        self._gen_btn = QPushButton("🔊  Generate Audio")
        self._gen_btn.setProperty("accent", "teal")
        self._gen_btn.clicked.connect(self._on_generate)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setProperty("accent", "red")
        self._stop_btn.setFixedWidth(60)
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._on_stop)

        self._gen_status = QLabel("")
        self._gen_status.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")

        row.addWidget(self._gen_btn, 1)
        row.addWidget(self._gen_status)
        row.addWidget(self._stop_btn)
        parent.addLayout(row)

    # -- Audio output --

    def _build_audio_section(self, parent: QVBoxLayout) -> None:
        panel = QWidget()
        panel.setStyleSheet(
            f"background: {BG_PANEL}; border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        title = QLabel("Audio Output")
        title.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {ACCENT_TEAL}; border: none;"
        )
        layout.addWidget(title)

        self._player = AudioPlayerWidget()
        layout.addWidget(self._player)

        # Save + auto-play row
        save_row = QHBoxLayout()
        save_row.setSpacing(6)

        self._save_btn = QPushButton("💾  Save As…")
        self._save_btn.clicked.connect(self._on_save)
        self._save_btn.setEnabled(False)

        self._autoplay_chk = QCheckBox("Auto-play")
        self._autoplay_chk.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        self._autoplay_chk.setChecked(self._settings.get("auto_play", True))
        self._autoplay_chk.toggled.connect(self._on_autoplay_toggled)

        self._audio_info = QLabel("")
        self._audio_info.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border: none;")

        save_row.addWidget(self._save_btn)
        save_row.addWidget(self._autoplay_chk)
        save_row.addStretch()
        save_row.addWidget(self._audio_info)
        layout.addLayout(save_row)

        parent.addWidget(panel)

    # -- History --

    def _build_history_section(self, parent: QVBoxLayout) -> None:
        self._history_toggle = QPushButton("▶  History")
        self._history_toggle.setStyleSheet(
            f"text-align: left; padding-left: 8px; color: {TEXT_SECONDARY}; "
            f"font-size: 12px; border: none; background: transparent;"
        )
        self._history_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._history_toggle.clicked.connect(self._toggle_history)

        self._history_widget = QWidget()
        self._history_widget.setVisible(False)
        h_layout = QVBoxLayout(self._history_widget)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(4)

        self._history_list = QListWidget()
        self._history_list.setMaximumHeight(150)
        self._history_list.itemDoubleClicked.connect(self._on_history_play)
        h_layout.addWidget(self._history_list)

        h_actions = QHBoxLayout()
        h_actions.setSpacing(4)
        self._history_play_btn = QPushButton("▶  Play")
        self._history_play_btn.setStyleSheet("font-size: 11px; padding: 4px 8px; min-height: 22px;")
        self._history_play_btn.clicked.connect(self._on_history_play_selected)
        self._history_save_btn = QPushButton("💾  Save")
        self._history_save_btn.setStyleSheet("font-size: 11px; padding: 4px 8px; min-height: 22px;")
        self._history_save_btn.clicked.connect(self._on_history_save)
        self._history_clear_btn = QPushButton("Clear")
        self._history_clear_btn.setStyleSheet("font-size: 11px; padding: 4px 8px; min-height: 22px;")
        self._history_clear_btn.clicked.connect(self._on_history_clear)
        h_actions.addWidget(self._history_play_btn)
        h_actions.addWidget(self._history_save_btn)
        h_actions.addStretch()
        h_actions.addWidget(self._history_clear_btn)
        h_layout.addLayout(h_actions)

        parent.addWidget(self._history_toggle)
        parent.addWidget(self._history_widget)

    # -- Ollama collapsible panel --

    def _build_ollama_section(self, parent: QVBoxLayout) -> None:
        self._ollama_toggle = QPushButton("▶  Ollama Text I/O")
        self._ollama_toggle.setStyleSheet(
            f"text-align: left; padding-left: 8px; color: {ACCENT_BLUE}; "
            f"font-size: 12px; font-weight: bold; border: none; background: transparent;"
        )
        self._ollama_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ollama_toggle.clicked.connect(self._toggle_ollama)

        self._ollama_panel = QWidget()
        self._ollama_panel.setVisible(False)
        self._ollama_panel.setStyleSheet(
            f"background: {BG_PANEL}; border: 1px solid {BORDER_SUBTLE}; border-radius: 6px;"
        )
        o_layout = QVBoxLayout(self._ollama_panel)
        o_layout.setContentsMargins(8, 6, 8, 6)
        o_layout.setSpacing(4)

        # Model + server row
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        lbl_model = QLabel("Model:")
        lbl_model.setFixedWidth(42)
        lbl_model.setStyleSheet("border: none;")
        self._ollama_model_combo = QComboBox()
        self._ollama_model_combo.setMinimumWidth(160)
        self._ollama_model_combo.currentTextChanged.connect(self._on_ollama_model_changed)
        self._ollama_refresh_btn = QPushButton("↻")
        self._ollama_refresh_btn.setFixedSize(28, 28)
        self._ollama_refresh_btn.clicked.connect(self._refresh_ollama_models)
        self._ollama_server_btn = QPushButton("Start Ollama")
        self._ollama_server_btn.setFixedHeight(26)
        self._ollama_server_btn.clicked.connect(self._on_toggle_ollama_server)
        self._ollama_status = QLabel("●")
        self._ollama_status.setFixedWidth(16)
        self._ollama_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ollama_status.setStyleSheet(f"color: {ACCENT_RED}; font-size: 14px; border: none;")

        model_row.addWidget(lbl_model)
        model_row.addWidget(self._ollama_model_combo, 1)
        model_row.addWidget(self._ollama_refresh_btn)
        model_row.addWidget(self._ollama_server_btn)
        model_row.addWidget(self._ollama_status)
        o_layout.addLayout(model_row)

        # System prompt
        sys_lbl = QLabel("System:")
        sys_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border: none;")
        o_layout.addWidget(sys_lbl)
        self._ollama_sys_edit = QPlainTextEdit()
        self._ollama_sys_edit.setPlaceholderText("System prompt (optional)…")
        self._ollama_sys_edit.setMaximumHeight(50)
        saved_sys = self._settings.get("ollama_system_prompt", "")
        if saved_sys:
            self._ollama_sys_edit.setPlainText(saved_sys)
        self._ollama_sys_edit.textChanged.connect(self._on_ollama_sys_changed)
        o_layout.addWidget(self._ollama_sys_edit)

        # Prompt input
        prompt_lbl = QLabel("Prompt:")
        prompt_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border: none;")
        o_layout.addWidget(prompt_lbl)
        self._ollama_prompt = QPlainTextEdit()
        self._ollama_prompt.setPlaceholderText("Enter prompt for Ollama…")
        self._ollama_prompt.setMaximumHeight(60)
        o_layout.addWidget(self._ollama_prompt)

        # Generate + actions
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._ollama_gen_btn = QPushButton("⚡  Generate Text")
        self._ollama_gen_btn.setProperty("accent", "blue")
        self._ollama_gen_btn.clicked.connect(self._on_ollama_generate)
        self._ollama_stop_btn = QPushButton("Stop")
        self._ollama_stop_btn.setProperty("accent", "red")
        self._ollama_stop_btn.setFixedWidth(50)
        self._ollama_stop_btn.setVisible(False)
        self._ollama_stop_btn.clicked.connect(self._on_ollama_stop)

        self._ollama_to_tts_btn = QPushButton("↑  Send to TTS")
        self._ollama_to_tts_btn.setProperty("accent", "green")
        self._ollama_to_tts_btn.setToolTip("Copy Ollama output into TTS text input")
        self._ollama_to_tts_btn.clicked.connect(self._send_ollama_to_tts)

        btn_row.addWidget(self._ollama_gen_btn, 1)
        btn_row.addWidget(self._ollama_stop_btn)
        btn_row.addWidget(self._ollama_to_tts_btn)
        o_layout.addLayout(btn_row)

        # Output
        self._ollama_output = QTextEdit()
        self._ollama_output.setPlaceholderText("Ollama output…")
        self._ollama_output.setFont(QFont("Consolas", 11))
        self._ollama_output.setMinimumHeight(60)
        self._ollama_output.setMaximumHeight(150)
        self._ollama_output.setReadOnly(True)
        o_layout.addWidget(self._ollama_output)

        self._ollama_info = QLabel("")
        self._ollama_info.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border: none;")
        o_layout.addWidget(self._ollama_info)

        parent.addWidget(self._ollama_toggle)
        parent.addWidget(self._ollama_panel)

    # ==================================================================
    # Keyboard shortcuts
    # ==================================================================

    def _setup_shortcuts(self) -> None:
        return

    # ==================================================================
    # Backend management
    # ==================================================================

    def _load_backends(self) -> None:
        self._backends = get_all_backends()
        self._backend_combo.blockSignals(True)
        self._backend_combo.clear()

        if not self._backends:
            self._backend_combo.addItem("(no backends found)")
            self._set_backend_status(False, "No TTS backends installed")
            self._backend_combo.blockSignals(False)
            return

        for b in self._backends:
            try:
                avail = b.is_available()
            except Exception:
                avail = False
            suffix = "" if avail else " (not installed)"
            self._backend_combo.addItem(f"{b.name}{suffix}")

        # Restore saved selection
        saved = self._settings.get("selected_backend", "")
        restored = False
        if saved:
            for i, b in enumerate(self._backends):
                if b.name == saved:
                    self._backend_combo.setCurrentIndex(i)
                    restored = True
                    break
        if not restored:
            # Select first available
            for i, b in enumerate(self._backends):
                if b.is_available():
                    self._backend_combo.setCurrentIndex(i)
                    break

        self._backend_combo.blockSignals(False)
        self._on_backend_changed(self._backend_combo.currentIndex())

    def _on_backend_changed(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._backends):
            self._current_backend = None
            self._set_backend_status(False, "No backend selected")
            return

        backend = self._backends[idx]
        self._current_backend = backend
        avail = backend.is_available()
        self._set_backend_status(avail, backend.name if avail else f"{backend.name} — not installed")
        self._settings["selected_backend"] = backend.name
        _save_settings(self._settings)

        # Populate voices
        self._voice_combo.blockSignals(True)
        self._voice_combo.clear()
        if avail:
            self._voices = backend.list_voices()
            for v in self._voices:
                self._voice_combo.addItem(f"{v.name}", v.id)
            # Restore saved voice
            saved_voice = self._settings.get("selected_voice", "")
            if saved_voice:
                for i, v in enumerate(self._voices):
                    if v.id == saved_voice:
                        self._voice_combo.setCurrentIndex(i)
                        break
        else:
            self._voices = []
            self._voice_combo.addItem("(backend not available)")
        self._voice_combo.blockSignals(False)

    def _on_voice_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._voices):
            self._settings["selected_voice"] = self._voices[idx].id
            _save_settings(self._settings)

    def _set_backend_status(self, ok: bool, text: str) -> None:
        if ok:
            self._backend_status.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 16px;")
            self._backend_status_label.setText(text)
            self._backend_status_label.setStyleSheet(f"color: {ACCENT_GREEN};")
        else:
            self._backend_status.setStyleSheet(f"color: {ACCENT_RED}; font-size: 16px;")
            self._backend_status_label.setText(text)
            self._backend_status_label.setStyleSheet(f"color: {ACCENT_RED};")

    # ==================================================================
    # Speed control
    # ==================================================================

    def _on_speed_changed(self, val: int) -> None:
        self._speed_label.setText(f"{val / 100:.1f}x")
        self._settings["speed"] = val
        _save_settings(self._settings)

    # ==================================================================
    # Text input persistence
    # ==================================================================

    def _on_input_changed(self) -> None:
        self._settings["input_text"] = self._text_input.toPlainText()
        _save_settings(self._settings)

    # ==================================================================
    # TTS generation
    # ==================================================================

    def _on_generate(self) -> None:
        if self._generating:
            return
        text = self._text_input.toPlainText().strip()
        if not text:
            return
        if self._current_backend is None or not self._current_backend.is_available():
            self._gen_status.setText("No TTS backend available")
            return

        voice_idx = self._voice_combo.currentIndex()
        if voice_idx < 0 or voice_idx >= len(self._voices):
            return
        voice_id = self._voices[voice_idx].id
        speed = self._speed_slider.value() / 100.0

        self._generating = True
        self._stop_requested = False
        self._gen_start = time.time()
        self._gen_btn.setEnabled(False)
        self._stop_btn.setVisible(True)
        self._gen_status.setText("Generating…")
        self._save_btn.setEnabled(False)

        threading.Thread(
            target=self._gen_worker,
            args=(text, voice_id, speed),
            daemon=True,
        ).start()

    def _gen_worker(self, text: str, voice: str, speed: float) -> None:
        self._vsig.generation_started.emit()
        try:
            backend = self._current_backend
            if backend is None:
                return
            result = backend.generate(text, voice, speed=speed)
            if self._stop_requested:
                return
            self._vsig.generation_done.emit(result)
        except Exception as exc:
            self._vsig.generation_error.emit(str(exc))

    def _on_gen_started(self) -> None:
        self._gen_status.setText("Generating…")

    def _on_gen_done(self, result: GenerationResult) -> None:
        elapsed = time.time() - self._gen_start
        self._generating = False
        self._gen_btn.setEnabled(True)
        self._stop_btn.setVisible(False)
        self._save_btn.setEnabled(True)
        self._gen_status.setText(f"Done · {elapsed:.1f}s")
        self._audio_info.setText(
            f"{result.duration:.1f}s · {result.sample_rate} Hz · "
            f"{result.audio.size:,} samples"
        )

        # Load into player
        self._player.load_audio(result.audio, result.sample_rate)
        if self._autoplay_chk.isChecked():
            self._player.play()

        # Add to history
        text = self._text_input.toPlainText().strip()
        voice_name = self._voice_combo.currentText()
        backend_name = self._current_backend.name if self._current_backend else "?"
        entry = _HistoryEntry(text, voice_name, backend_name, result)
        self._history.append(entry)
        item_text = f"[{entry.timestamp}] {entry.voice} · {entry.duration:.1f}s — {entry.text_snippet}"
        self._history_list.addItem(item_text)
        self._history_list.scrollToBottom()

    def _on_gen_error(self, err: str) -> None:
        self._generating = False
        self._gen_btn.setEnabled(True)
        self._stop_btn.setVisible(False)
        self._gen_status.setText(f"Error: {err[:80]}")

    def _on_status_update(self, text: str) -> None:
        self._gen_status.setText(text)

    def _on_stop(self) -> None:
        self._stop_requested = True
        self._generating = False
        self._gen_btn.setEnabled(True)
        self._stop_btn.setVisible(False)
        self._gen_status.setText("Stopped")
        # Also stop Ollama if running
        self._ollama_stop_requested = True

    # ==================================================================
    # Save
    # ==================================================================

    def _on_save(self) -> None:
        path = self._player.save_dialog()
        if path:
            self._gen_status.setText(f"Saved ✓ {os.path.basename(path)}")

    def _on_autoplay_toggled(self, checked: bool) -> None:
        self._settings["auto_play"] = checked
        _save_settings(self._settings)

    # ==================================================================
    # History
    # ==================================================================

    def _toggle_history(self) -> None:
        vis = not self._history_widget.isVisible()
        self._history_widget.setVisible(vis)
        arrow = "▼" if vis else "▶"
        self._history_toggle.setText(f"{arrow}  History")

    def _on_history_play(self, item: QListWidgetItem) -> None:
        idx = self._history_list.row(item)
        self._play_history_entry(idx)

    def _on_history_play_selected(self) -> None:
        idx = self._history_list.currentRow()
        if idx >= 0:
            self._play_history_entry(idx)

    def _play_history_entry(self, idx: int) -> None:
        if 0 <= idx < len(self._history):
            entry = self._history[idx]
            self._player.load_audio(entry.audio, entry.sample_rate)
            self._player.play()
            self._audio_info.setText(
                f"{entry.duration:.1f}s · {entry.sample_rate} Hz · Replay"
            )

    def _on_history_save(self) -> None:
        idx = self._history_list.currentRow()
        if idx < 0 or idx >= len(self._history):
            return
        entry = self._history[idx]
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Audio", "", "WAV (*.wav);;FLAC (*.flac);;OGG (*.ogg)"
        )
        if path:
            import soundfile as sf
            sf.write(path, entry.audio, entry.sample_rate)
            self._gen_status.setText(f"Saved ✓ {os.path.basename(path)}")

    def _on_history_clear(self) -> None:
        self._history.clear()
        self._history_list.clear()

    # ==================================================================
    # Ollama section
    # ==================================================================

    _ollama_generating = False
    _ollama_stop_requested = False

    def _toggle_ollama(self) -> None:
        vis = not self._ollama_panel.isVisible()
        self._ollama_panel.setVisible(vis)
        arrow = "▼" if vis else "▶"
        self._ollama_toggle.setText(f"{arrow}  Ollama Text I/O")
        if vis and self._ollama_model_combo.count() == 0:
            self._refresh_ollama_models()

    def _refresh_ollama_models(self) -> None:
        models = self._ollama.list_models()
        self._ollama_model_combo.blockSignals(True)
        self._ollama_model_combo.clear()
        if models:
            self._ollama_model_combo.addItems(models)
            self._ollama_status.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 14px; border: none;")
            self._ollama_server_btn.setText("Stop Ollama")
            saved = self._settings.get("ollama_model", "")
            if saved:
                for i, m in enumerate(models):
                    if m == saved:
                        self._ollama_model_combo.setCurrentIndex(i)
                        break
        else:
            self._ollama_model_combo.addItem("(no models)")
            self._ollama_status.setStyleSheet(f"color: {ACCENT_RED}; font-size: 14px; border: none;")
            self._ollama_server_btn.setText("Start Ollama")
        self._ollama_model_combo.blockSignals(False)

    def _on_ollama_model_changed(self, name: str) -> None:
        if name and name != "(no models)":
            self._ollama.model = name
            self._settings["ollama_model"] = name
            _save_settings(self._settings)

    def _on_ollama_sys_changed(self) -> None:
        self._settings["ollama_system_prompt"] = self._ollama_sys_edit.toPlainText()
        _save_settings(self._settings)

    def _on_toggle_ollama_server(self) -> None:
        if self._ollama.is_available():
            self._ollama.stop_server()
            self._ollama_status.setStyleSheet(f"color: {ACCENT_RED}; font-size: 14px; border: none;")
            self._ollama_server_btn.setText("Start Ollama")
            self._ollama_model_combo.clear()
            self._ollama_model_combo.addItem("(stopped)")
        else:
            self._ollama_server_btn.setEnabled(False)
            self._ollama_info.setText("Starting…")
            threading.Thread(target=self._ollama_server_worker, daemon=True).start()

    def _ollama_server_worker(self) -> None:
        try:
            ok = self._ollama.start_server(wait=True, timeout_sec=25)
            QTimer.singleShot(0, lambda: self._on_ollama_server_result(ok))
        except Exception as exc:
            QTimer.singleShot(0, lambda: self._on_ollama_server_result(False, str(exc)))

    def _on_ollama_server_result(self, ok: bool, err: str = "") -> None:
        self._ollama_server_btn.setEnabled(True)
        if ok:
            self._refresh_ollama_models()
            self._ollama_info.setText("Connected")
        else:
            self._ollama_status.setStyleSheet(f"color: {ACCENT_RED}; font-size: 14px; border: none;")
            self._ollama_info.setText(err[:60] if err else "Failed to start")

    def _on_ollama_generate(self) -> None:
        if self._ollama_generating:
            return
        prompt = self._ollama_prompt.toPlainText().strip()
        if not prompt:
            return

        msgs: list[dict] = []
        sys_text = self._ollama_sys_edit.toPlainText().strip()
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.append({"role": "user", "content": prompt})

        self._ollama_generating = True
        self._ollama_stop_requested = False
        self._ollama_gen_start = time.time()
        self._ollama_gen_btn.setEnabled(False)
        self._ollama_stop_btn.setVisible(True)
        self._ollama_output.clear()
        self._ollama_info.setText("generating…")

        threading.Thread(
            target=self._ollama_gen_worker,
            args=(msgs,),
            daemon=True,
        ).start()

    def _ollama_gen_worker(self, msgs: list[dict]) -> None:
        try:
            for token in self._ollama.generate_stream(msgs):
                if self._ollama_stop_requested:
                    break
                self._osig.token.emit(token)
            self._osig.finished.emit()
        except Exception as exc:
            self._osig.error.emit(str(exc))

    def _on_ollama_token(self, token: str) -> None:
        cursor = self._ollama_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(token)
        self._ollama_output.setTextCursor(cursor)
        self._ollama_output.ensureCursorVisible()
        elapsed = time.time() - self._ollama_gen_start
        self._ollama_info.setText(f"generating… {elapsed:.0f}s")

    def _on_ollama_done(self) -> None:
        self._ollama_generating = False
        self._ollama_gen_btn.setEnabled(True)
        self._ollama_stop_btn.setVisible(False)
        elapsed = time.time() - self._ollama_gen_start
        self._ollama_info.setText(f"done · {elapsed:.1f}s")

    def _on_ollama_error(self, err: str) -> None:
        self._ollama_generating = False
        self._ollama_gen_btn.setEnabled(True)
        self._ollama_stop_btn.setVisible(False)
        self._ollama_info.setText(f"Error: {err[:60]}")

    def _on_ollama_stop(self) -> None:
        self._ollama_stop_requested = True

    def _send_ollama_to_tts(self) -> None:
        """Copy Ollama output text into TTS text input."""
        text = self._ollama_output.toPlainText().strip()
        if text:
            self._text_input.setPlainText(text)

    # ==================================================================
    # Cleanup
    # ==================================================================

    def closeEvent(self, a0) -> None:  # type: ignore[override]
        self._stop_requested = True
        self._ollama_stop_requested = True
        self._player.stop()
        # Save geometry
        self._settings["window_geometry"] = self.saveGeometry().toHex().data().decode()
        _save_settings(self._settings)
        super().closeEvent(a0)
