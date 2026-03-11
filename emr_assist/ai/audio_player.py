"""Audio playback widget using sounddevice.

Provides play / pause / stop controls, a progress slider, volume,
and save-to-file via soundfile.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

import numpy as np

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Lazy imports for optional audio dependencies
# ---------------------------------------------------------------------------

def _import_sd():
    import sounddevice as sd
    return sd


def _import_sf():
    import soundfile as sf
    return sf


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class AudioPlayerWidget(QWidget):
    """Compact audio player for numpy waveforms."""

    playback_finished = pyqtSignal()
    position_changed = pyqtSignal(float)  # 0.0 – 1.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._audio: Optional[np.ndarray] = None
        self._sr: int = 24000
        self._duration: float = 0.0

        # Playback state
        self._playing = False
        self._paused = False
        self._stream = None
        self._play_pos: int = 0       # sample index
        self._lock = threading.Lock()

        # Periodic UI update
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

        self._build_ui()
        self._set_enabled(False)

    # ==================================================================
    # UI
    # ==================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        # -- Transport row --
        transport = QHBoxLayout()
        transport.setSpacing(6)

        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedSize(32, 28)
        self._play_btn.setToolTip("Play / Pause")
        self._play_btn.clicked.connect(self._on_play_pause)

        self._stop_btn = QPushButton("■")
        self._stop_btn.setFixedSize(32, 28)
        self._stop_btn.setToolTip("Stop")
        self._stop_btn.clicked.connect(self.stop)

        self._time_lbl = QLabel("0:00 / 0:00")
        self._time_lbl.setFixedWidth(90)
        self._time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._time_lbl.setStyleSheet("font-size: 11px;")

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.setValue(0)
        self._slider.sliderPressed.connect(self._on_seek_start)
        self._slider.sliderReleased.connect(self._on_seek_end)

        self._vol_lbl = QLabel("🔊")
        self._vol_lbl.setFixedWidth(18)
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        self._vol_slider.setFixedWidth(70)
        self._vol_slider.setToolTip("Volume")

        transport.addWidget(self._play_btn)
        transport.addWidget(self._stop_btn)
        transport.addWidget(self._slider, 1)
        transport.addWidget(self._time_lbl)
        transport.addWidget(self._vol_lbl)
        transport.addWidget(self._vol_slider)

        root.addLayout(transport)

    # ==================================================================
    # Public API
    # ==================================================================

    def load_audio(self, audio: np.ndarray, sample_rate: int) -> None:
        """Load a waveform for playback."""
        self.stop()
        self._audio = audio.astype(np.float32)
        self._sr = sample_rate
        self._duration = audio.size / sample_rate if audio.size else 0
        self._play_pos = 0
        self._slider.setValue(0)
        self._time_lbl.setText(f"0:00 / {_fmt_time(self._duration)}")
        self._set_enabled(True)

    def play(self) -> None:
        if self._audio is None or self._audio.size == 0:
            return
        if self._paused:
            self._paused = False
            self._play_btn.setText("⏸")
            return
        self.stop()
        self._playing = True
        self._paused = False
        self._play_pos = 0
        self._play_btn.setText("⏸")
        self._timer.start()
        threading.Thread(target=self._playback_worker, daemon=True).start()

    def pause(self) -> None:
        if self._playing:
            self._paused = True
            self._play_btn.setText("▶")

    def stop(self) -> None:
        self._playing = False
        self._paused = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._timer.stop()
        self._play_btn.setText("▶")
        if self._audio is not None:
            self._play_pos = 0
            self._slider.setValue(0)
            self._time_lbl.setText(f"0:00 / {_fmt_time(self._duration)}")

    def save_audio(self, path: str) -> None:
        """Write current audio to *path* (format inferred from extension)."""
        if self._audio is None:
            return
        sf = _import_sf()
        sf.write(path, self._audio, self._sr)

    def save_dialog(self) -> Optional[str]:
        """Open a file dialog and save; returns path or None."""
        if self._audio is None:
            return None
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Audio", "", "WAV (*.wav);;FLAC (*.flac);;OGG (*.ogg)"
        )
        if path:
            self.save_audio(path)
        return path

    def has_audio(self) -> bool:
        return self._audio is not None and self._audio.size > 0

    # ==================================================================
    # Internal playback
    # ==================================================================

    def _playback_worker(self) -> None:
        sd = _import_sd()
        audio = self._audio
        if audio is None or audio.size == 0:
            return
        volume = self._vol_slider.value() / 100.0
        block_size = 1024
        try:
            self._stream = sd.OutputStream(
                samplerate=self._sr,
                channels=1,
                dtype="float32",
                blocksize=block_size,
            )
            self._stream.start()

            while self._playing and self._play_pos < audio.size:
                if self._paused:
                    time.sleep(0.05)
                    continue
                volume = self._vol_slider.value() / 100.0
                end = min(self._play_pos + block_size, audio.size)
                chunk = audio[self._play_pos:end] * volume
                self._stream.write(chunk.reshape(-1, 1))
                with self._lock:
                    self._play_pos = end

            self._stream.stop()
            self._stream.close()
        except Exception as exc:
            pass  # silently handle audio device issues
        finally:
            self._stream = None
            self._playing = False
            self._paused = False
            QTimer.singleShot(0, self._on_playback_done)

    def _on_playback_done(self) -> None:
        self._timer.stop()
        self._play_btn.setText("▶")
        self._slider.setValue(1000)
        self._time_lbl.setText(f"{_fmt_time(self._duration)} / {_fmt_time(self._duration)}")
        self.playback_finished.emit()

    # ==================================================================
    # Seek
    # ==================================================================

    _seeking = False

    def _on_seek_start(self) -> None:
        self._seeking = True

    def _on_seek_end(self) -> None:
        self._seeking = False
        if self._audio is None:
            return
        frac = self._slider.value() / 1000.0
        with self._lock:
            self._play_pos = int(frac * self._audio.size)

    # ==================================================================
    # Periodic tick
    # ==================================================================

    def _tick(self) -> None:
        if self._audio is None or self._audio.size == 0:
            return
        with self._lock:
            pos = self._play_pos
        frac = pos / self._audio.size
        if not self._seeking:
            self._slider.setValue(int(frac * 1000))
        elapsed = pos / self._sr
        self._time_lbl.setText(f"{_fmt_time(elapsed)} / {_fmt_time(self._duration)}")
        self.position_changed.emit(frac)

    # ==================================================================
    # Play / Pause toggle
    # ==================================================================

    def _on_play_pause(self) -> None:
        if not self._playing:
            self.play()
        elif self._paused:
            self._paused = False
            self._play_btn.setText("⏸")
        else:
            self.pause()

    # ==================================================================
    # Enable / disable
    # ==================================================================

    def _set_enabled(self, enabled: bool) -> None:
        self._play_btn.setEnabled(enabled)
        self._stop_btn.setEnabled(enabled)
        self._slider.setEnabled(enabled)
