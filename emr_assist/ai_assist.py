"""AI Assist — standalone entry point.

Launch with::

    python -m emr_assist.ai_assist

Or directly::

    python emr_assist/ai_assist.py

The window reads grabbed data from the EMR bridge JSON (written by the main
EMR Assist app) and uses a local Ollama instance for AI generation.
"""

from __future__ import annotations

import sys
import os

# Ensure the project root is on sys.path so relative imports work
# when running this file directly (python emr_assist/ai_assist.py)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PyQt6.QtWidgets import QApplication

from emr_assist.ai.window import AIAssistWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("AI Assist")
    window = AIAssistWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
