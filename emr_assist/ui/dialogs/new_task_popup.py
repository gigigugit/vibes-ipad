"""New-task popup dialog (modeless, always-on-top)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
)


class NewTaskPopup(QDialog):
    """Small modeless popup shown when auto-clicker detects a new task."""

    # Signal callback set by MainWindow
    open_emr_callback = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New task detected")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setMinimumWidth(260)

        layout = QVBoxLayout(self)
        msg = QLabel("New task detected")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(msg)

        btn_row = QHBoxLayout()
        open_btn = QPushButton("Open EMR in Chrome")
        open_btn.clicked.connect(self._on_open)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(open_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _on_open(self):
        self.close()
        if self.open_emr_callback:
            self.open_emr_callback()
