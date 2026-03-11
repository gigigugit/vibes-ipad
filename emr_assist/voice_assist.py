"""Voice Assist — standalone entry point.

Launch with::

    python -m emr_assist.voice_assist

Or directly::

    python emr_assist/voice_assist.py

TTS generation window with pluggable voice backends (Kokoro TTS, etc.)
and optional Ollama text I/O.
"""

from __future__ import annotations

import sys
import os

# Ensure the project root is on sys.path so relative imports work
# when running this file directly (python emr_assist/voice_assist.py)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Pre-import torch BEFORE PyQt6 to avoid DLL conflict on Windows.
# PyQt6 loads DLLs that clash with torch's c10.dll if loaded first.
try:
    import torch  # noqa: F401
except Exception:
    pass

from PyQt6.QtWidgets import QApplication

from emr_assist.ai.voice_window import VoiceAssistWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Voice Assist")
    window = VoiceAssistWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
