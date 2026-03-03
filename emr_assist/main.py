"""EMR Assist — PyQt6 entry point.

Launch with:
    pythonw emr_assist/main.pyw
or:
    python  emr_assist/main.pyw
"""
from __future__ import annotations

import os
import sys
import threading

# Ensure the project root is on sys.path so ``from emr_assist.…`` works
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from emr_assist.ui.main_window import MainWindow
from emr_assist.ui.helpers import window_tracker


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("EMR Assist")

    # Optional: high-DPI scaling (already default in Qt6, but explicit)
    app.setStyle("Fusion")

    win = MainWindow()
    win.show()

    # Start the window-tracker thread (keeps pygetwindow refs alive)
    tracker_thread = threading.Thread(target=window_tracker, daemon=True)
    tracker_thread.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
