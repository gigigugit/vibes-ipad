from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from emr_assist.browser.grabber import BrowserEMRGrabber
from emr_assist.core.parsers import extract_hair_medication_from_text, normalize_blood_pressure_value


TAB_ALIASES = {
    "hair loss": "Hair Loss",
    "sexual health": "Sexual Health",
    "performance anxiety": "Performance Anxiety",
    "photoaging": "Photoaging",
    "birth control": "Birth Control",
}

DETECTED_TO_CANONICAL = {
    "Hair Loss": "Hair Loss",
    "Sexual Health": "Sexual Health",
    "Performance Anxiety": "Performance Anxiety",
    "Photoaging": "Photoaging",
    "Birth Control": "Birth Control",
}

QUESTION_KEYWORDS = {
    "hair_pattern": ["pattern", "location of hair", "where is your hair loss", "thinning"],
    "hair_duration": ["how long", "duration", "when did your hair loss"],
    "hair_previous_tx": ["previous treatment", "treatments tried", "what have you tried"],
    "ed_onset": ["onset", "started", "gradually", "sudden"],
    "ed_severity": ["difficulty getting", "stay hard", "maintain erections", "how severe"],
    "ed_goals": ["treatment goals", "results you want", "make it easier"],
    "ejaculation": ["ejaculation", "premature ejaculation", "ejaculate"],
    "pa_frequency": ["how often", "frequency", "often do you"],
    "pa_impact": ["impact", "affect", "daily life", "interfere"],
    "pa_previous_tx": ["previous treatment", "tried before", "past treatment"],
    "pa_symptoms": ["symptoms", "when you are anxious", "palpitations", "sweating"],
    "pa_triggers": ["situational fears", "triggers", "what makes you nervous"],
    "retinoid_history": ["retinoid", "retin-a", "tretinoin", "previously used"],
    "skin_concerns": ["skin concerns", "goals", "photoaging", "aging concerns"],
    "skin_type": ["skin type", "sensitive skin", "dry skin", "oily skin"],
    "bc_preference": ["preferred birth control", "preference", "method do you prefer"],
    "bc_history": ["history", "previous birth control", "med history changes"],
    "menstrual": ["lmp", "last menstrual", "period", "cycle"],
    "smoking": ["smok", "cigarette", "vape", "tobacco"],
    "migraine": ["migraine", "aura", "headache"],
    "dvt_pe": ["dvt", "blood clot", "pulmonary embol", "pe history"],
    "side_effects": ["side effects", "experienced side effects", "adverse effect"],
    "pmh": ["past medical history", "pmh", "medical history"],
}


def normalize_tab_name(raw: str) -> Optional[str]:
    if not raw:
        return None
    low = raw.strip().lower()
    if low in TAB_ALIASES:
        return TAB_ALIASES[low]
    for canonical in TAB_ALIASES.values():
        if low == canonical.lower():
            return canonical
    return None


def load_variable_spec(csv_path: Path) -> Dict[str, List[str]]:
    spec: Dict[str, List[str]] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            tab = normalize_tab_name((row.get("Tab") or "").strip())
            variable = (row.get("Variable") or "").strip()
            if not tab or not variable:
                continue
            spec.setdefault(tab, [])
            if variable not in spec[tab]:
                spec[tab].append(variable)
    return spec


def to_text_lines(full_text: str) -> List[str]:
    return [line.strip() for line in (full_text or "").splitlines() if line.strip()]


def extract_next_answer(full_text: str, keywords: List[str], lookahead: int = 5) -> str:
    if not full_text or not keywords:
        return ""
    lines = to_text_lines(full_text)
    low_lines = [line.lower() for line in lines]
    for index, low_line in enumerate(low_lines):
        if any(keyword in low_line for keyword in keywords):
            for step in range(1, lookahead + 1):
                if index + step >= len(lines):
                    break
                candidate = lines[index + step].strip()
                if not candidate:
                    continue
                candidate_low = candidate.lower()
                if any(keyword in candidate_low for keyword in keywords):
                    continue
                if candidate_low in {"yes", "no"}:
                    return candidate
                if len(candidate) >= 3:
                    return candidate
    return ""


def extract_bmi(full_text: str) -> str:
    if not full_text:
        return ""
    match = re.search(r"\bbmi\b[^0-9]{0,8}(\d{1,2}(?:\.\d{1,2})?)", full_text, re.IGNORECASE)
    return match.group(1) if match else ""


def extract_dob(full_text: str) -> str:
    if not full_text:
        return ""
    patterns = [
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2},\s+\d{4}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def extract_name(full_text: str) -> Dict[str, str]:
    if not full_text:
        return {"first_name": "", "last_name": ""}
    lines = to_text_lines(full_text)
    for line in lines:
        low = line.lower()
        if "patient" in low and "name" in low:
            cleaned = re.sub(r"^.*name\s*[:\-]\s*", "", line, flags=re.IGNORECASE).strip()
            parts = [part for part in re.split(r"\s+", cleaned) if part and part[0].isalpha()]
            if len(parts) >= 2:
                return {"first_name": parts[0], "last_name": parts[-1]}
    return {"first_name": "", "last_name": ""}


def join_list(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def safe_call(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def build_common_fields(grabber: BrowserEMRGrabber, full_text: str, tab_name: str) -> Dict[str, str]:
    out: Dict[str, str] = {}

    summary = safe_call(lambda: grabber.fetch_patient_location_summary(debug_print=False), {}) or {}
    out["patient_state"] = str(summary.get("state_display") or summary.get("state") or "")

    bp_val = normalize_blood_pressure_value(safe_call(lambda: grabber._extract_blood_pressure(), "") or "")
    if not bp_val:
        bp_val = normalize_blood_pressure_value(full_text)
    out["bp"] = str(bp_val)

    med_val = safe_call(lambda: grabber._extract_medication({}, {}), "") or ""
    if not med_val:
        med_val = extract_hair_medication_from_text(full_text) if tab_name == "Hair Loss" else (
            safe_call(lambda: grabber._extract_medication_from_text(full_text), "") or ""
        )
    out["current_dose"] = str(med_val)

    diagnoses = safe_call(lambda: grabber._extract_diagnoses(), []) or []
    out["dx"] = join_list(diagnoses)

    out["pmh"] = extract_next_answer(full_text, QUESTION_KEYWORDS["pmh"])

    if tab_name in {"Photoaging", "Birth Control"}:
        name_bits = extract_name(full_text)
        out["first_name"] = name_bits["first_name"]
        out["last_name"] = name_bits["last_name"]
        out["dob"] = extract_dob(full_text)

    return out


def extract_for_tab(grabber: BrowserEMRGrabber, tab_name: str, variables: List[str]) -> Dict[str, str]:
    full_text = safe_call(lambda: grabber._get_all_text_across_frames(), "") or ""
    if not full_text:
        full_text = safe_call(lambda: grabber._get_page_text(), "") or ""

    result = build_common_fields(grabber, full_text, tab_name)

    if tab_name == "Hair Loss":
        result["hair_pattern"] = extract_next_answer(full_text, QUESTION_KEYWORDS["hair_pattern"])
        result["hair_duration"] = extract_next_answer(full_text, QUESTION_KEYWORDS["hair_duration"])
        result["hair_previous_tx"] = extract_next_answer(full_text, QUESTION_KEYWORDS["hair_previous_tx"])
        result["side_effects"] = extract_next_answer(full_text, QUESTION_KEYWORDS["side_effects"])

    elif tab_name == "Sexual Health":
        sh_data = safe_call(lambda: grabber.grab_sexual_health_data(), {}) or {}
        if sh_data.get("medication"):
            result["current_dose"] = str(sh_data.get("medication"))
        if sh_data.get("blood_pressure"):
            result["bp"] = normalize_blood_pressure_value(str(sh_data.get("blood_pressure")))
        if sh_data.get("diagnoses"):
            result["dx"] = join_list(sh_data.get("diagnoses"))
            low_dx = result["dx"].lower()
            result["sexual_health_dx_ed"] = "Yes" if "ed" in low_dx else "No"
            result["sexual_health_dx_pe"] = "Yes" if "pe" in low_dx else "No"
        else:
            result.setdefault("sexual_health_dx_ed", "No")
            result.setdefault("sexual_health_dx_pe", "No")
        result["ed_goals"] = extract_next_answer(full_text, QUESTION_KEYWORDS["ed_goals"])
        result["ed_onset"] = extract_next_answer(full_text, QUESTION_KEYWORDS["ed_onset"])
        result["ed_severity"] = extract_next_answer(full_text, QUESTION_KEYWORDS["ed_severity"])
        result["ejaculation"] = extract_next_answer(full_text, QUESTION_KEYWORDS["ejaculation"])
        result["side_effects"] = extract_next_answer(full_text, QUESTION_KEYWORDS["side_effects"])

    elif tab_name == "Performance Anxiety":
        pa_data = safe_call(lambda: grabber.grab_performance_anxiety_data(), {}) or {}
        if pa_data.get("medication"):
            result["current_dose"] = str(pa_data.get("medication"))
        if pa_data.get("blood_pressure"):
            result["bp"] = normalize_blood_pressure_value(str(pa_data.get("blood_pressure")))
        if pa_data.get("situations_text"):
            result["pa_triggers"] = str(pa_data.get("situations_text"))
        if pa_data.get("symptoms_text"):
            result["pa_symptoms"] = str(pa_data.get("symptoms_text"))
        result.setdefault("pa_frequency", extract_next_answer(full_text, QUESTION_KEYWORDS["pa_frequency"]))
        result.setdefault("pa_impact", extract_next_answer(full_text, QUESTION_KEYWORDS["pa_impact"]))
        result.setdefault("pa_previous_tx", extract_next_answer(full_text, QUESTION_KEYWORDS["pa_previous_tx"]))
        result["side_effects"] = extract_next_answer(full_text, QUESTION_KEYWORDS["side_effects"])

    elif tab_name == "Photoaging":
        result["retinoid_history"] = extract_next_answer(full_text, QUESTION_KEYWORDS["retinoid_history"])
        result["skin_concerns"] = extract_next_answer(full_text, QUESTION_KEYWORDS["skin_concerns"])
        result["skin_type"] = extract_next_answer(full_text, QUESTION_KEYWORDS["skin_type"])
        result["side_effects"] = extract_next_answer(full_text, QUESTION_KEYWORDS["side_effects"])

    elif tab_name == "Birth Control":
        bc_data = safe_call(lambda: grabber.grab_birth_control_data(), {}) or {}
        if bc_data.get("medication"):
            result["current_dose"] = str(bc_data.get("medication"))
        if bc_data.get("blood_pressure"):
            result["bp"] = normalize_blood_pressure_value(str(bc_data.get("blood_pressure")))
        if bc_data.get("lmp"):
            result["menstrual"] = str(bc_data.get("lmp"))
        if bc_data.get("med_history_changes"):
            result["bc_history"] = str(bc_data.get("med_history_changes"))
        if bc_data.get("side_effects_text"):
            result["side_effects"] = str(bc_data.get("side_effects_text"))
        result.setdefault("bc_preference", extract_next_answer(full_text, QUESTION_KEYWORDS["bc_preference"]))
        result.setdefault("dvt_pe", extract_next_answer(full_text, QUESTION_KEYWORDS["dvt_pe"]))
        result.setdefault("migraine", extract_next_answer(full_text, QUESTION_KEYWORDS["migraine"]))
        result.setdefault("smoking", extract_next_answer(full_text, QUESTION_KEYWORDS["smoking"]))

    filtered = {var: str(result.get(var, "") or "") for var in variables}
    return filtered


def write_json(payload: Dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def write_flat_csv(payload: Dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tab_name = payload.get("visit_type", "")
    values: Dict[str, str] = payload.get("values", {})
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Tab", "Variable", "Value"])
        for key in sorted(values.keys()):
            writer.writerow([tab_name, key, values[key]])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal EMR data grabber for selected CSV variables.")
    parser.add_argument("--visit-type", default="", help="Visit type (e.g., 'sexual health'). If omitted, auto-detects.")
    parser.add_argument(
        "--csv",
        default=str(PROJECT_ROOT / "grab_variables.csv"),
        help="Path to variable spec CSV (default: project-root/grab_variables.csv)",
    )
    parser.add_argument(
        "--out-json",
        default=str(PROJECT_ROOT / "minimal_emr_grabber" / "output" / "latest_grab.json"),
        help="Output JSON path",
    )
    parser.add_argument(
        "--out-csv",
        default=str(PROJECT_ROOT / "minimal_emr_grabber" / "output" / "latest_grab.csv"),
        help="Output flat CSV path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"❌ CSV not found: {csv_path}")
        return 1

    spec = load_variable_spec(csv_path)
    if not spec:
        print("❌ No variables loaded from CSV.")
        return 1

    grabber = BrowserEMRGrabber()
    if not grabber.connect_to_chrome():
        print("❌ Could not connect to Chrome CDP. Start Chrome with --remote-debugging-port=9222.")
        return 1

    requested_tab = normalize_tab_name(args.visit_type) if args.visit_type else None
    if not requested_tab:
        detected = safe_call(grabber.detect_visit_type, None)
        requested_tab = DETECTED_TO_CANONICAL.get(str(detected), None)

    if not requested_tab:
        print("❌ Could not determine visit type. Pass --visit-type explicitly.")
        return 1

    if requested_tab not in spec:
        print(f"❌ Visit type '{requested_tab}' not present in CSV variable spec.")
        return 1

    variables = spec[requested_tab]
    values = extract_for_tab(grabber, requested_tab, variables)

    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "visit_type": requested_tab,
        "variables_requested": variables,
        "values": values,
        "source_csv": str(csv_path),
    }

    out_json = Path(args.out_json)
    out_csv = Path(args.out_csv)
    write_json(payload, out_json)
    write_flat_csv(payload, out_csv)

    print(f"✅ Grab complete for: {requested_tab}")
    print(f"✅ JSON: {out_json}")
    print(f"✅ CSV : {out_csv}")

    missing = [key for key, value in values.items() if not value]
    if missing:
        print("⚠ Missing values:")
        for key in missing:
            print(f"   - {key}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
