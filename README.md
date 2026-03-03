# EMR Assist (12_12_25 version)

This repo includes a working Windows GUI helper for EMR workflows. The primary entry point is the script below and it expects a small set of files next to it.

## Primary Entry Point
- [12_12_25 version_input_GUI.pyw](12_12_25%20version_input_GUI.pyw)

## Required Files (must be in the same folder)
- [templates.txt](templates.txt) — note templates consumed by the GUI.
- [grab_points.py](grab_points.py) — CSS selector registry and helper utilities.
- [clincal_decision_matrix.png](clincal_decision_matrix.png) — image shown in the Clinical Matrix window.

## Optional / Runtime-Generated
- emr_assist_vars.json — written to your %TEMP% folder at runtime for interop.

## What It Does
- Always-on-top wxPython GUI with multiple workflow tabs.
- Pulls data from the EMR via clipboard grab and Playwright CDP.
- Inserts templated notes and structured snippets.
- Auto-clicker utilities and URL monitoring for “next task” workflows.

## Dependencies
Install these Python packages in the environment used to run the script:
- wxPython
- pyperclip
- pyautogui
- pygetwindow
- keyboard
- requests
- playwright (optional but recommended for CDP features)
- geopy (optional; used for location helpers)

## Browser Setup (CDP)
If using CDP features, start Chrome or Thorium with remote debugging:
- Chrome: --remote-debugging-port=9222
- Thorium: --remote-debugging-port=9222 --user-data-dir="C:\\chrome_debug"

## Notes
- This README reflects the current “best working” 12_12_25 version.
- Keep the required files together to avoid runtime errors.
