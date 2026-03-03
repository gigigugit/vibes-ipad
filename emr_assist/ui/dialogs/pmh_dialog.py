"""PMH multi-select dialog (modal, always-on-top)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QDialogButtonBox,
)

from ...core.config import PMH_OPTIONS
from ...core.state import pmh_selected, build_pmh_text


class PMHDialog(QDialog):
    """Checkable list dialog for selecting PMH diagnoses."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select PMH Diagnoses")
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)

        info = QLabel("Select all that apply; leave all unchecked for noncontributory:")
        layout.addWidget(info)

        self.list_widget = QListWidget()
        for option in PMH_OPTIONS:
            item = QListWidgetItem(option)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if option in pmh_selected else Qt.CheckState.Unchecked
            )
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def selected_items(self) -> set[str]:
        """Return the set of checked PMH option strings."""
        result: set[str] = set()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item and item.checkState() == Qt.CheckState.Checked:
                result.add(item.text())
        return result
