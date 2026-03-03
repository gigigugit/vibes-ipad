from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

try:
    import pyautogui
    import pyperclip
except Exception:
    pyautogui = None
    pyperclip = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_payload(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_entry_csv(payload: Dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": payload.get("timestamp", ""),
        "visit_type": payload.get("visit_type", ""),
        "variable": "",
        "value": "",
    }
    is_new = not out_path.exists()
    with out_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if is_new:
            writer.writeheader()
        for variable, value in (payload.get("values") or {}).items():
            row["variable"] = variable
            row["value"] = value
            writer.writerow(row)


def build_summary_text(payload: Dict[str, Any]) -> str:
    lines = [
        f"Visit Type: {payload.get('visit_type', '')}",
        f"Timestamp: {payload.get('timestamp', '')}",
        "",
    ]
    values = payload.get("values") or {}
    for key in sorted(values.keys()):
        lines.append(f"{key}: {values[key]}")
    return "\n".join(lines)


def paste_to_active_window(text: str, countdown: int, interval: float) -> None:
    if pyautogui is None or pyperclip is None:
        raise RuntimeError("pyautogui/pyperclip not installed. Install requirements first.")

    print(f"Focus target field/window now. Pasting in {countdown} second(s)...")
    for remaining in range(countdown, 0, -1):
        print(f"  {remaining}...")
        time.sleep(1)

    original = ""
    try:
        original = pyperclip.paste()
    except Exception:
        original = ""

    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(max(interval, 0.01))

    try:
        pyperclip.copy(original)
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal EMR data-entry/export helper.")
    parser.add_argument(
        "--input",
        default=str(PROJECT_ROOT / "minimal_emr_grabber" / "output" / "latest_grab.json"),
        help="Path to grab JSON payload",
    )
    parser.add_argument(
        "--entry-log",
        default=str(PROJECT_ROOT / "minimal_emr_grabber" / "output" / "entry_log.csv"),
        help="CSV log path for exported entries",
    )
    parser.add_argument("--paste-summary", action="store_true", help="Paste formatted summary to active window")
    parser.add_argument("--countdown", type=int, default=3, help="Countdown seconds before paste")
    parser.add_argument("--interval", type=float, default=0.02, help="Post-paste delay")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"❌ Input JSON not found: {input_path}")
        return 1

    payload = load_payload(input_path)
    append_entry_csv(payload, Path(args.entry_log))
    print(f"✅ Appended entry log: {args.entry_log}")

    if args.paste_summary:
        summary = build_summary_text(payload)
        try:
            paste_to_active_window(summary, countdown=max(args.countdown, 0), interval=max(args.interval, 0.0))
            print("✅ Summary pasted to active window.")
        except Exception as exc:
            print(f"❌ Paste failed: {exc}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
