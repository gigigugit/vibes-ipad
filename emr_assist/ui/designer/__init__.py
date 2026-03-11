"""Qt Designer .ui files for the EMR Assist Dashboard.

This package contains .ui files that can be edited in Qt Designer or
Qt Design Studio.  Each file corresponds to a panel or window in the
dashboard UI.

Custom widgets used in .ui files (registered as promoted widgets):
    - ActionButton     (emr_assist.ui.widgets)
    - StatusBadge      (emr_assist.ui.widgets)
    - ToggleChip       (emr_assist.ui.widgets)
    - EditableInfoCard (emr_assist.ui.widgets)
    - InfoCard         (emr_assist.ui.widgets)
    - MultiLineInfoCard(emr_assist.ui.widgets)
    - ChipGroup        (emr_assist.ui.widgets)
    - LabValueRow      (emr_assist.ui.widgets)
    - CollapsibleSection (emr_assist.ui.widgets)

Files:
    dashboard_main.ui             Main window layout (status bar, dock areas)
    auto_clicker_panel.ui         Auto-clicker controls
    t_deficiency_actions.ui       T Deficiency action buttons
    t_deficiency_info.ui          T Deficiency info cards
    t_deficiency_labs.ui          T Deficiency lab values
    hair_loss_actions.ui          Hair Loss action buttons
    hair_loss_info.ui             Hair Loss info cards
    photoaging_actions.ui         Photoaging action buttons
    photoaging_info.ui            Photoaging info cards
    sexual_health_actions.ui      Sexual Health action buttons
    sexual_health_info.ui         Sexual Health info cards
    performance_anxiety_actions.ui Performance Anxiety action buttons
    performance_anxiety_info.ui   Performance Anxiety info cards
    birth_control_actions.ui      Birth Control action buttons
    birth_control_info.ui         Birth Control info cards
"""

from __future__ import annotations

import os

DESIGNER_DIR = os.path.dirname(os.path.abspath(__file__))
