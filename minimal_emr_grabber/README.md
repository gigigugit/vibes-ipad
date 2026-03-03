# Minimal EMR Grabber

Standalone scripts for rapid data grabbing and data-entry export, without modifying anything under `emr_assist/`.

## Files

- `emr_data_grabber.py` — grabs only variables listed in `../grab_variables.csv` for one visit type.
- `emr_data_entry.py` — logs grabbed values to CSV and can paste a formatted summary into the active window.

## Prerequisites

- Chrome launched with remote debugging:

```powershell
chrome.exe --remote-debugging-port=9222
```

- Python packages used by your existing project (`playwright`, `pyautogui`, `pyperclip`, etc.).

## Usage

### 1) Grab values for a specific visit type

```powershell
python minimal_emr_grabber/emr_data_grabber.py --visit-type "sexual health"
```

Or auto-detect the current visit type:

```powershell
python minimal_emr_grabber/emr_data_grabber.py
```

Outputs:

- `minimal_emr_grabber/output/latest_grab.json`
- `minimal_emr_grabber/output/latest_grab.csv`

### 2) Log/paste for data entry

Append last grab to an entry log:

```powershell
python minimal_emr_grabber/emr_data_entry.py
```

Append + paste summary into active window after countdown:

```powershell
python minimal_emr_grabber/emr_data_entry.py --paste-summary --countdown 3
```

## Notes

- The variable source-of-truth is `grab_variables.csv` at repo root.
- If a value is missing, it will be left blank and reported after grab.
- No files under `emr_assist/` are changed.
