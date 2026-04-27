"""Read the EMR bridge JSON written by the main EMR Assist app.

The main application writes ``emr_assist_vars.json`` into the system TEMP
directory every time data is grabbed or a template is inserted.  This module
reads that file and optionally watches it for changes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Dict, Optional

_DEFAULT_PATH = os.path.join(
    os.getenv("TEMP") or os.getcwd(), "emr_assist_vars.json"
)


class BridgeReader:
    """Read and watch the EMR bridge JSON file."""

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self.path = path
        self._last_mtime: float = 0.0
        self._last_data: Dict[str, str] = {}
        self._watcher_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_latest(self) -> Dict[str, str]:
        """Parse the bridge JSON and return a flat dict of all variables.

        Returns an empty dict if the file doesn't exist or can't be parsed.
        """
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

        # Flatten: the bridge has a nested "grabbed_vars" key plus top-level extras
        flat: Dict[str, str] = {}
        grabbed = raw.get("grabbed_vars", {})
        if isinstance(grabbed, dict):
            flat.update({k: str(v) for k, v in grabbed.items()})

        # Merge top-level keys (tdcs, medication, etc.)
        for key, val in raw.items():
            if key in ("grabbed_vars", "timestamp"):
                continue
            flat[key] = str(val) if val is not None else ""

        # Keep timestamp separately
        flat["_timestamp"] = raw.get("timestamp", "")

        self._last_data = flat
        return flat

    def has_changed(self) -> bool:
        """Return True if the bridge file has been modified since last read."""
        if not os.path.isfile(self.path):
            return False
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime > self._last_mtime:
            self._last_mtime = mtime
            return True
        return False

    # ------------------------------------------------------------------
    # File watching
    # ------------------------------------------------------------------

    def start_watching(self, callback: Callable[[Dict[str, str]], None], interval: float = 1.5) -> None:
        """Start a background thread that calls *callback(vars_dict)* on changes."""
        if self._watcher_thread is not None:
            return
        self._stop_event.clear()
        self._watcher_thread = threading.Thread(
            target=self._watch_loop,
            args=(callback, interval),
            daemon=True,
        )
        self._watcher_thread.start()

    def stop_watching(self) -> None:
        """Stop the file-watching thread."""
        self._stop_event.set()
        self._watcher_thread = None

    def _watch_loop(self, callback: Callable[[Dict[str, str]], None], interval: float) -> None:
        while not self._stop_event.is_set():
            if self.has_changed():
                data = self.read_latest()
                if data:
                    try:
                        callback(data)
                    except Exception as exc:
                        print(f"Bridge watcher callback error: {exc}")
            time.sleep(interval)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def detect_visit_type(self, vars_dict: Optional[Dict[str, str]] = None) -> str:
        """Guess the visit type from bridge data.

        Checks for keys set by specific visit-type grabs.
        """
        d = vars_dict or self._last_data
        if not d:
            return "Unknown"

        # Check for visit-type-specific markers

        if d.get("bc_med") or d.get("bc_lmp"):
            return "Birth Control"
        if d.get("pa_situations") or d.get("pa_symptoms"):
            return "Performance Anxiety"
        if d.get("sh_med") or d.get("sh_effectiveness"):
            return "Sexual Health"
        if d.get("hsx") or d.get("hvar") or d.get("hair_med"):
            return "Hair Loss"
        if d.get("photoaging_med") or d.get("photoaging_retinoid"):
            return "Photoaging"


        return "Unknown"
