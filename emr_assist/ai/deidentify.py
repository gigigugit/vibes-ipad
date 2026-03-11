"""De-identification utilities for EMR bridge data.

Strips PHI (patient names, date of birth) from the grabbed variables dict
before the data is sent to the local Ollama model.  A companion
``reidentify_text`` function can restore placeholders afterward if needed.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Dict, List, Tuple

# Keys that contain direct PHI and should be blanked entirely
_PHI_KEYS = {"first_name", "last_name", "dob", "date_of_birth", "patient_name", "name"}

# Placeholder tokens inserted in place of PHI
PLACEHOLDER_NAME = "[PATIENT]"
PLACEHOLDER_DOB = "[DOB]"


def deidentify_vars(vars_dict: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (safe_vars, phi_map) with PHI replaced by placeholders.

    *phi_map* records  ``{placeholder: original}`` so callers can
    optionally call :func:`reidentify_text` later.
    """
    safe = deepcopy(vars_dict)
    phi_map: Dict[str, str] = {}

    # Collect name parts for scanning
    name_parts: List[str] = []
    for key in ("first_name", "last_name", "patient_name", "name"):
        val = vars_dict.get(key, "").strip()
        if val:
            name_parts.extend(val.split())

    dob_val = ""
    for key in ("dob", "date_of_birth"):
        val = vars_dict.get(key, "").strip()
        if val:
            dob_val = val
            break

    # 1. Blank explicit PHI keys
    for key in list(safe.keys()):
        if key in _PHI_KEYS:
            original = safe[key]
            if not original:
                continue
            if key in ("dob", "date_of_birth"):
                placeholder = PLACEHOLDER_DOB
            else:
                placeholder = PLACEHOLDER_NAME
            phi_map[placeholder] = original
            safe[key] = placeholder

    # 2. Scrub name substrings from all other string values
    if name_parts:
        # Build pattern matching any name part (case-insensitive, word boundary)
        escaped = [re.escape(p) for p in name_parts if len(p) > 1]
        if escaped:
            pattern = re.compile(
                r"\b(" + "|".join(escaped) + r")\b",
                re.IGNORECASE,
            )
            for key, val in safe.items():
                if key in _PHI_KEYS or not isinstance(val, str):
                    continue
                safe[key] = pattern.sub(PLACEHOLDER_NAME, val)

    # 3. Scrub DOB occurrences from values
    if dob_val and len(dob_val) >= 6:
        for key, val in safe.items():
            if key in _PHI_KEYS or not isinstance(val, str):
                continue
            if dob_val in val:
                safe[key] = val.replace(dob_val, PLACEHOLDER_DOB)

    return safe, phi_map


def reidentify_text(text: str, phi_map: Dict[str, str]) -> str:
    """Replace placeholders in *text* with original PHI values from *phi_map*."""
    result = text
    for placeholder, original in phi_map.items():
        result = result.replace(placeholder, original)
    return result
