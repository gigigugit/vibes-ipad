"""Mutable application state shared across modules.

All values that were previously module-level globals in the monolithic script
now live here so that any module (UI, browser, parsers) can read/write them
without circular imports.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional, Set


# ---------------------------------------------------------------------------
# Grabbed clinical values
# ---------------------------------------------------------------------------
grabbed_vars: Dict[str, str] = {}
grabbed_vars['hair_loss_additional_sxx'] = ""
grabbed_vars['hair_loss_location'] = ""

diagnoses = [""]
medication_value = ["Enclomiphene 12.5 mg daily"]
selected_template = [""]

# PMH selections
pmh_selected: Set[str] = set()

# Auto-clicker state
auto_clicker_x = [2600]
auto_clicker_y = [400]
auto_clicker_interval = [3]
auto_clicker_enabled = [False]
auto_clicker_last_url = [""]
auto_clicker_thread = [None]

# Active-window tracker
last_active_window = None
panel_title = "EMR Assist"

# ---------------------------------------------------------------------------
# EMR bridge (JSON file for AHK interop)
# ---------------------------------------------------------------------------
EMR_BRIDGE_FILENAME = "emr_assist_vars.json"
EMR_BRIDGE_PATH = os.path.join(os.getenv("TEMP") or os.getcwd(), EMR_BRIDGE_FILENAME)


def build_pmh_text() -> str:
    """Return PMH phrase for Rx note based on user selection."""
    try:
        if not pmh_selected:
            return "is noncontributory"
        return "significant for " + ", ".join(sorted(pmh_selected))
    except Exception:
        return "is noncontributory"


def _build_emr_bridge_payload(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "grabbed_vars": dict(grabbed_vars),
        "diagnoses": diagnoses[0],
        "medication": medication_value[0],
        "pmh": build_pmh_text(),
    }
    payload.update(dict(grabbed_vars))
    if extra:
        payload.update(extra)
    return payload


def _write_emr_bridge(payload: Dict[str, Any]) -> None:
    temp_dir = os.path.dirname(EMR_BRIDGE_PATH) or "."
    os.makedirs(temp_dir, exist_ok=True)
    tmp_path = EMR_BRIDGE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp_path, EMR_BRIDGE_PATH)


def emit_emr_bridge(extra: Optional[Dict[str, Any]] = None) -> None:
    try:
        payload = _build_emr_bridge_payload(extra)
        _write_emr_bridge(payload)
    except Exception as exc:
        print(f"EMR bridge write failed: {exc}")
