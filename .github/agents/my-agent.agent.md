# AGENTS

## Purpose

This repository is migrating its desktop UI from wxPython to PyQt6.

The scope for migration work is strictly presentation-layer migration. Do not change clinical logic, browser automation behavior, parsing rules, template content, hotkey semantics, or workflow behavior unless a change is required purely because of the Qt framework.

## Primary Goal

Move UI construction, widget wiring, dialogs, layouts, styling, and UI event plumbing from the legacy wxPython implementation into the existing PyQt6 structure while preserving behavior.

## Source And Target

Use these files as the main references:

- Legacy wxPython source of truth: `input_GUI_w_fixed_autoclick_closed_loop.pyw`
- Existing PyQt6 main window target: `emr_assist/ui/main_window.py`
- Existing PyQt6 widgets and styling: `emr_assist/ui/widgets.py`, `emr_assist/ui/theme.py`
- Existing signal bridge for thread-safe UI updates: `emr_assist/ui/signals.py`
- PyQt6 app entry point: `emr_assist/main.py`
- Optional alternate PyQt6 dashboard direction: `emr_assist/dashboard.py`
- Existing dialogs and panels to extend first: `emr_assist/ui/dialogs/`, `emr_assist/ui/panels/`, `emr_assist/ui/tabs/`

Prefer extending the current modular PyQt6 codebase instead of porting more code into one giant window class.

## Non-Negotiable Constraints

- Do not alter business logic unless needed for UI framework compatibility.
- Do not rename externally meaningful workflow concepts without a strong reason.
- Preserve the existing tab and workflow structure unless explicitly asked to redesign it.
- Preserve current button meanings, field meanings, and hotkey behaviors.
- Preserve background-thread behavior, but route UI updates onto the Qt main thread.
- Keep migration incremental. Avoid large rewrites when a tab-by-tab or dialog-by-dialog move is possible.
- Reuse existing parser, browser, config, state, and template modules rather than duplicating logic.
- Keep Windows compatibility first.

## Critical Codebase Rules

- The current legacy UI is wxPython, centered around `MyFrame(wx.Frame)` and auxiliary popup/dialog classes.
- The PyQt6 target already exists and should be treated as the migration destination, not a scratch rewrite.
- Threaded work must not update Qt widgets directly. Use `emr_assist/ui/signals.py` or an equivalent Qt-safe dispatch path.
- If Torch or any Torch-based audio dependency is involved in a PyQt6 entry point on Windows, import `torch` before PyQt6 imports to avoid DLL initialization failures.
- Keep UI state names aligned with existing shared state in `emr_assist/core/state.py` where practical.

## Migration Strategy

When asked to migrate a piece of UI, follow this order:

1. Identify the exact wxPython source region and list the widgets, events, state dependencies, and helper calls involved.
2. Find the best PyQt6 destination module.
3. Port only the UI layer first:
   - widget creation
   - layouts
   - labels and text inputs
   - button wiring
   - tab construction
   - dialog construction
   - status displays
4. Reconnect existing logic using the current helpers, browser modules, parsers, config values, and shared state.
5. Replace wx-specific patterns with Qt-native equivalents.
6. Verify that behavior remains equivalent from a user perspective.

## Required wxPython To PyQt6 Mappings

Use these translations by default:

- `wx.Frame` -> `QMainWindow` or `QDialog` depending on role
- `wx.Panel` -> `QWidget`
- `wx.Notebook` -> `QTabWidget`
- `wx.BoxSizer` -> `QVBoxLayout` or `QHBoxLayout`
- `wx.StaticBoxSizer` -> `QGroupBox` plus layout
- `wx.StaticText` -> `QLabel`
- `wx.TextCtrl` -> `QLineEdit` or `QPlainTextEdit`
- `wx.Button` -> `QPushButton`
- `wx.CheckBox` -> `QCheckBox`
- `wx.RadioButton` -> `QRadioButton`
- `wx.Choice` -> `QComboBox`
- `wx.MessageBox` -> `QMessageBox`
- `wx.CallAfter` -> queued signal or main-thread callback
- `wx.CallLater` -> `QTimer.singleShot`
- `Bind(...)` -> signal-slot connections
- `SetSizer(...)` and `Layout()` -> assign layout directly
- `SetFont`, `SetForegroundColour`, ad hoc style tweaks -> centralize in QSS or shared widget classes when possible

## UI-Only Guardrails

Do not do the following unless explicitly requested:

- redesign workflows
- merge tabs
- change template text generation
- change parsing heuristics
- refactor browser automation logic for style only
- rewrite background workers just because the UI moved
- introduce unrelated architecture cleanup

## File Placement Rules

Prefer this structure:

- Main window shell and high-level tab orchestration in `emr_assist/ui/main_window.py`
- Reusable controls in `emr_assist/ui/widgets.py`
- Shared styling in `emr_assist/ui/theme.py`
- Thread-safe UI dispatch in `emr_assist/ui/signals.py`
- Popups and modal windows in `emr_assist/ui/dialogs/`
- Tab-specific or domain-specific sections in `emr_assist/ui/panels/` or `emr_assist/ui/tabs/`

If the wxPython code for one tab is very large, split it into a focused panel or builder module instead of expanding one monolithic file.

## Required Working Style

For each migration task:

1. Start by naming the exact wxPython section being migrated.
2. State the target PyQt6 file or files.
3. Explain what is UI-only versus what logic is being reused untouched.
4. Implement the smallest complete migration slice.
5. Verify imports, startup safety, and signal wiring.
6. Note any remaining wx-only pieces still to migrate.

## Definition Of Done For Any Migrated Slice

A migration slice is only done when:

- the corresponding UI exists in PyQt6
- the visible controls and labels match the intended workflow
- existing logic is still invoked through preserved handlers or adapters
- background work updates the UI safely on the Qt thread
- the old behavior is preserved from the user perspective
- no new functional behavior has been introduced unintentionally

## First Priorities For This Repo

If asked where to start, prefer this order:

1. Shared dialogs and popups, because they are smaller and easier to validate.
2. One workflow tab at a time from `MyFrame` into modular PyQt6 panels.
3. Status bar, tab switching, and top-level shell consistency.
4. Hotkey registration and visibility/show-hide behavior.
5. Auto-clicker and URL monitor presentation layer.

## Success Criterion

The end state should be a PyQt6 UI that feels equivalent to the current wxPython application from the user perspective, while business logic, parsing, automation, and templates remain materially unchanged.
