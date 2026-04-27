# EMR Assist (JS Overlay Edition)

This repo includes a working Windows GUI helper for EMR workflows. It provides **two UI modes**: a traditional always-on-top wxPython desktop window, and a **JS overlay bar** injected directly into the EMR browser page via Chrome DevTools Protocol (CDP).

## Primary Entry Point
- [input_GUI_w_fixed_autoclick_closed_loop.pyw](input_GUI_w_fixed_autoclick_closed_loop.pyw)

## Required Files (must be in the same folder)
- [templates.txt](templates.txt) - note templates consumed by the GUI.
- [grab_points.py](grab_points.py) - CSS selector registry and helper utilities.

## Required Folders
- `template_config/` - per-visit-type template lists as JSON files (e.g. `sexual_health_templates.json`).
- `templates/` - per-visit-type template text files (e.g. `templates_sexual_health.txt`).

## Optional / Runtime-Generated
- `emr_assist_vars.json` - written to `%TEMP%` at runtime for AHK / external tool interop.

## What It Does
- **Python UI mode**: Always-on-top wxPython GUI with multiple workflow tabs (Sexual Health, Hair Loss, Photoaging, Performance Anxiety, Birth Control). Pulls data from the EMR, displays grabbed variables in text fields, inserts templated notes.
- **JS Overlay mode**: A slim toolbar injected at the top of the EMR browser page. Shows visit type, template dropdown, grab button, and a collapsible variables panel - all without leaving the browser. Activated via a button on the Auto Clicker tab.
- **Data grabbing**: Clipboard path (Hide window -> click center -> Ctrl+A -> Ctrl+C -> parse text). CDP question-seeking is attempted first but frequently finds nothing; clipboard is the reliable path.
- **Auto-clicker** utilities and **URL monitoring** for "next task" workflows.
- **CDP visit-type detection**: Monitors the EMR page header text to auto-detect visit type and switch tabs.

## UI Modes

### Python UI (default)
The wxPython window with tabs for each visit type. Each tab has text fields for grabbed variables, template insertion buttons, and workflow shortcuts.

### JS Overlay
Activated from the Auto Clicker tab. Injects an HTML/CSS/JS toolbar into the EMR page:
- **Visit label** with Detect button
- **Template dropdown** with Insert button
- **Grab button** (grab / F4)
- **Variables panel** (collapsible, shows all grabbed data)
- **Return to Python UI** button
- **Close** button

The overlay runs on a dedicated thread with a command queue to avoid Playwright/greenlet conflicts. The Python window is hidden (not destroyed) while the overlay is active.

## Architecture Notes
- **Single monolithic file** (~13,000 lines): `input_GUI_w_fixed_autoclick_closed_loop.pyw`
- **Overlay thread**: Owns its own Playwright instance. All `page.evaluate()` calls happen on this thread only. Other threads communicate via `queue.Queue`.
- **self.Show patch**: While overlay is active, `frame.Show()` is a no-op so clipboard grabs don't re-show the Python window.
- **Bridge hook**: `emit_emr_bridge()` passes the full payload to the overlay thread via `_emr_bridge_hook`, which queues a `push_vars` command with the data.
- **grabbed_vars** dict is minimal (2 keys). All real data flows through `emit_emr_bridge(extra={...})`.

## Dependencies
Install these Python packages in the environment used to run the script:
- wxPython
- pyperclip
- pyautogui
- pygetwindow
- keyboard
- requests
- mouse
- playwright (required for CDP features and JS overlay)
- geopy (optional; used for location helpers)

## Browser Setup (CDP)
Start Chrome with remote debugging enabled:
`chrome.exe --remote-debugging-port=9222`

The JS overlay and CDP visit-type detection both require this.

## Notes
- The `USE_CDP_FOR_GRAB` flag defaults to `False`. **Clipboard is the only reliable grab path.**
- CDP question-seeking runs but usually finds nothing for this EMR. The clipboard fallback does the actual data extraction.
- Keep all required files and folders together to avoid runtime errors.
