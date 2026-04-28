"""
Grab Points Module
Provides selector information for EMR elements across different visit types.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

# Placeholder for grab points configuration
# This can be expanded with actual selectors as needed
GRAB_POINTS = {
    "sexual_health": {
        "medication": {
            "selenium": {
                "css": '[data-testid="medication-title"]',
            },
            "playwright": {
                "css": '[data-testid="medication-title"]',
            },
        },
        "medication_detail": {
            "selenium": {
                "css": '[data-testid="medication-text"]',
            },
            "playwright": {
                "css": '[data-testid="medication-text"]',
            },
        },
        "treatment_plan": {
            "selenium": {
                "css": '[data-testid="proposedTreatmentPlan"]',
            },
            "playwright": {
                "css": '[data-testid="proposedTreatmentPlan"]',
            },
        },
        "current_dose": {
            "selenium": {
                "css": '[data-testid="treatmentPlan"]',
            },
            "playwright": {
                "css": '[data-testid="treatmentPlan"]',
            },
        },
    },
    "birth_control": {
        "medication": {
            "selenium": {
                "css": '[data-testid="medication-title"]',
                "css_list": [
                    '[data-testid="medication-title"]',
                    '[data-testid="treatment-plan"]',
                    '[class*="medication"]',
                ],
            },
            "playwright": {
                "css": '[data-testid="medication-title"]',
            },
        },
        "blood_pressure": {
            "selenium": {
                "css_list": [
                    '[data-testid="blood-pressure"]',
                    '[data-testid="bloodPressure"]',
                    '[data-testid="bp"]',
                    '[data-testid="blood-pressure-reading"]',
                ],
            },
            "playwright": {
                "css_list": [
                    '[data-testid="blood-pressure"]',
                    '[data-testid="bloodPressure"]',
                    '[data-testid="bp"]',
                    '[data-testid="blood-pressure-reading"]',
                ],
            },
        },
        "lmp": {
            "selenium": {
                "css_list": [
                    '[data-testid="lmp"]',
                    '[data-testid="last-menstrual-period"]',
                ],
            },
        },
        "systolic_bp": {
            "selenium": {
                "css_list": [
                    '[data-testid="systolic-bp"]',
                    '[data-testid="systolic"]',
                ],
            },
            "playwright": {
                "css_list": [
                    '[data-testid="systolic-bp"]',
                    '[data-testid="systolic"]',
                ],
            },
        },
        "diastolic_bp": {
            "selenium": {
                "css_list": [
                    '[data-testid="diastolic-bp"]',
                    '[data-testid="diastolic"]',
                ],
            },
            "playwright": {
                "css_list": [
                    '[data-testid="diastolic-bp"]',
                    '[data-testid="diastolic"]',
                ],
            },
        },
    },
    "emr": {
        "get_next_task": {
            "selenium": {
                "css": '[data-testid="getNextTaskButton"]',
                "xpath": '//*[@data-testid="getNextTaskButton"]',
            },
            "playwright": {
                "css": '[data-testid="getNextTaskButton"]',
                "xpath": '//*[@data-testid="getNextTaskButton"]',
            },
        },
    },
}

SELECTOR_OVERRIDES_FILENAME = "selector_overrides.json"
SELECTOR_WORKBENCH_FILENAME = "selector_workbench.json"
AD_HOC_SELECTOR_LOG_FILENAME = "ad_hoc_selector_log.jsonl"

_AD_HOC_SELECTOR_CACHE_MTIME: float | None = None
_AD_HOC_SELECTOR_CACHE: Dict[tuple[str, str], List[str]] = {}

DEFAULT_SELECTOR_WORKBENCH_WINDOW_STATE: Dict[str, Any] = {
    "left": 8,
    "top": 40,
    "width": 760,
    "height": 560,
    "minimized": False,
}

VARIABLE_SELECTOR_CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "Hair Loss": [
        {
            "var_id": "hair_medication",
            "label": "Medication",
            "group": "hair_loss",
            "key": "medication",
            "keywords": [
                "treatment",
                "medication",
                "current dose",
                "finasteride",
                "minoxidil",
                "dutasteride",
            ],
            "anchors": ["Treatment", "Current Dose"],
        },
        {
            "var_id": "hair_response",
            "label": "Response",
            "group": "hair_loss",
            "key": "response",
            "keywords": ["response", "improvement", "worse", "stable", "hair loss"],
            "anchors": ["How has your treatment affected your hair loss"],
        },
        {
            "var_id": "hair_symptoms",
            "label": "Symptoms / Location",
            "group": "hair_loss",
            "key": "symptoms",
            "keywords": ["thinning", "hairline", "temples", "crown", "shedding"],
            "anchors": ["Select all that apply", "Hair loss"],
        },
    ],
    "Sexual Health": [
        {
            "var_id": "sexual_health_med",
            "label": "Medication",
            "group": "sexual_health",
            "key": "medication",
            "keywords": [
                "medication",
                "treatment",
                "current dose",
                "tadalafil",
                "sildenafil",
                "finasteride",
                "minoxidil",
            ],
            "anchors": ["Treatment", "Current Dose"],
        },
        {
            "var_id": "sexual_health_effectiveness",
            "label": "Effectiveness",
            "group": "sexual_health",
            "key": "effectiveness",
            "keywords": ["happy", "treatment working", "effectiveness", "yes", "no"],
            "anchors": ["Are you happy with the way your treatment is working"],
        },
        {
            "var_id": "sexual_health_bp",
            "label": "Blood Pressure",
            "group": "sexual_health",
            "key": "blood_pressure",
            "keywords": ["blood pressure", "bp"],
            "anchors": [
                "What was your last blood pressure reading?",
                "What is your current blood pressure?",
            ],
        },
        {
            "var_id": "hair_loss_location",
            "label": "Hair Loss Location",
            "group": "sexual_health",
            "key": "hair_loss_location",
            "keywords": ["hair loss", "thinning", "hairline", "temples", "crown"],
            "anchors": ["Where is your hair loss located?"],
        },
        {
            "var_id": "hair_loss_additional_sxx",
            "label": "Hair Loss Symptoms",
            "group": "sexual_health",
            "key": "hair_loss_additional_sxx",
            "keywords": ["burning", "pain", "scaly", "scarring", "crusting"],
            "anchors": ["Do you have any of these symptoms with your hair loss?"],
        },
    ],
    "Performance Anxiety": [
        {
            "var_id": "pa_medication",
            "label": "Medication",
            "group": "performance_anxiety",
            "key": "medication",
            "keywords": [
                "medication",
                "treatment",
                "tadalafil",
                "sildenafil",
                "propranolol",
            ],
            "anchors": ["Treatment", "Current Dose"],
        },
        {
            "var_id": "pa_bp",
            "label": "Blood Pressure",
            "group": "performance_anxiety",
            "key": "blood_pressure",
            "keywords": ["blood pressure", "bp"],
            "anchors": [
                "What was your last blood pressure reading?",
                "What is your current blood pressure?",
            ],
        },
        {
            "var_id": "pa_pulse",
            "label": "Pulse",
            "group": "performance_anxiety",
            "key": "pulse",
            "keywords": ["pulse", "heart rate", "bpm"],
            "anchors": ["What is your pulse?", "What is your heart rate?"],
        },
        {
            "var_id": "pa_situations",
            "label": "Situations",
            "group": "performance_anxiety",
            "key": "situations",
            "keywords": ["situational fears", "nervous", "anxious", "performance"],
            "anchors": ["What situational fears make you nervous or anxious?"],
        },
        {
            "var_id": "pa_symptoms",
            "label": "Symptoms",
            "group": "performance_anxiety",
            "key": "symptoms",
            "keywords": ["symptoms", "palpitations", "sweating", "shaking"],
            "anchors": ["What symptoms do you experience?"],
        },
    ],
    "Photoaging": [
        {
            "var_id": "photoaging_medication",
            "label": "Medication",
            "group": "photoaging",
            "key": "medication",
            "keywords": [
                "tretinoin",
                "retinoid",
                "niacinamide",
                "azelaic",
                "treatment",
            ],
            "anchors": ["Treatment", "Current Dose"],
        },
        {
            "var_id": "photoaging_goals",
            "label": "Skin Goals",
            "group": "photoaging",
            "key": "skin_goals",
            "keywords": ["skin goals", "concerns", "photoaging", "wrinkles", "spots"],
            "anchors": [
                "What are your skin goals?",
                "What concerns are you trying to address?",
            ],
        },
        {
            "var_id": "photoaging_retinoid",
            "label": "Retinoid History",
            "group": "photoaging",
            "key": "retinoid_history",
            "keywords": ["retinoid", "retin-a", "tretinoin"],
            "anchors": ["Have you used a retinoid before?"],
        },
    ],
    "Birth Control": [
        {
            "var_id": "bc_medication",
            "label": "Medication",
            "group": "birth_control",
            "key": "medication",
            "keywords": [
                "birth control",
                "medication",
                "treatment",
                "pill",
                "patch",
                "ring",
            ],
            "anchors": ["Treatment", "Current Dose"],
        },
        {
            "var_id": "bc_bp",
            "label": "Blood Pressure",
            "group": "birth_control",
            "key": "blood_pressure",
            "keywords": ["blood pressure", "bp", "systolic", "diastolic"],
            "anchors": [
                "What was your last blood pressure reading?",
                "What is your current blood pressure?",
                "Systolic",
                "Diastolic",
            ],
            "parser_rules": [
                {"strategy": "combined_selector"},
                {"strategy": "split_selector"},
                {"strategy": "generic_extractor"},
            ],
        },
        {
            "var_id": "bc_lmp",
            "label": "LMP",
            "group": "birth_control",
            "key": "lmp",
            "keywords": ["lmp", "last menstrual period", "period"],
            "anchors": ["Last menstrual period", "LMP"],
        },
        {
            "var_id": "bc_systolic_bp",
            "label": "Systolic BP",
            "group": "birth_control",
            "key": "systolic_bp",
            "keywords": ["systolic", "blood pressure"],
            "anchors": ["Systolic"],
        },
        {
            "var_id": "bc_diastolic_bp",
            "label": "Diastolic BP",
            "group": "birth_control",
            "key": "diastolic_bp",
            "keywords": ["diastolic", "blood pressure"],
            "anchors": ["Diastolic"],
        },
        {
            "var_id": "bc_side_effects",
            "label": "Side Effects",
            "group": "birth_control",
            "key": "side_effects",
            "keywords": ["side effects", "adverse effects"],
            "anchors": ["Have you had side effects?"],
        },
        {
            "var_id": "bc_med_history_changes",
            "label": "Medication History Changes",
            "group": "birth_control",
            "key": "med_history_changes",
            "keywords": ["medication history", "history changes"],
            "anchors": ["Any medication history changes?"],
        },
    ],
}


def _get_overrides_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), SELECTOR_OVERRIDES_FILENAME
    )


def _get_workbench_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), SELECTOR_WORKBENCH_FILENAME
    )


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return cleaned.strip("_")


def _visit_type_key(visit_type: str) -> str:
    return _slugify(visit_type or "unknown") or "unknown"


def _custom_group_name(visit_type: str) -> str:
    return f"custom_{_visit_type_key(visit_type)}"


def _load_selector_overrides() -> Dict[str, Any]:
    path = _get_overrides_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_selector_overrides(data: Dict[str, Any]) -> str:
    path = _get_overrides_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def _get_ad_hoc_selector_log_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), AD_HOC_SELECTOR_LOG_FILENAME
    )


def _load_logged_playwright_selectors() -> Dict[tuple[str, str], List[str]]:
    global _AD_HOC_SELECTOR_CACHE_MTIME, _AD_HOC_SELECTOR_CACHE

    path = _get_ad_hoc_selector_log_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _AD_HOC_SELECTOR_CACHE_MTIME = None
        _AD_HOC_SELECTOR_CACHE = {}
        return {}

    if _AD_HOC_SELECTOR_CACHE_MTIME == mtime:
        return dict(_AD_HOC_SELECTOR_CACHE)

    grouped: Dict[tuple[str, str], List[str]] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue

                group = str(entry.get("group") or "").strip()
                key = str(entry.get("key") or "").strip()
                selector = str(entry.get("selector") or "").strip()
                if not group or not key or not selector:
                    continue

                if "persist_selector" in entry and not bool(entry.get("persist_selector")):
                    continue

                bucket = grouped.setdefault((group, key), [])
                if selector not in bucket:
                    bucket.append(selector)
    except Exception:
        grouped = {}

    _AD_HOC_SELECTOR_CACHE_MTIME = mtime
    _AD_HOC_SELECTOR_CACHE = grouped
    return dict(grouped)


def _get_logged_playwright_selectors(group: str, key: str) -> List[str]:
    return list(_load_logged_playwright_selectors().get((group, key), []))


def _load_selector_workbench() -> Dict[str, Any]:
    path = _get_workbench_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_selector_workbench(data: Dict[str, Any]) -> str:
    path = _get_workbench_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def _get_visit_workbench_entry(
    data: Dict[str, Any], visit_type: str, create: bool = False
) -> Dict[str, Any]:
    visit_types = (
        data.setdefault("visit_types", {}) if create else data.get("visit_types", {})
    )
    if not isinstance(visit_types, dict):
        if not create:
            return {}
        visit_types = {}
        data["visit_types"] = visit_types
    visit_key = _visit_type_key(visit_type)
    if create:
        entry = visit_types.setdefault(visit_key, {})
    else:
        entry = visit_types.get(visit_key, {})
    return entry if isinstance(entry, dict) else {}


def _get_builtin_variable_overrides(
    visit_type: str, create: bool = False
) -> Dict[str, Dict[str, Any]]:
    data = _load_selector_workbench()
    entry = _get_visit_workbench_entry(data, visit_type, create=create)
    overrides = (
        entry.setdefault("builtin_variables", {})
        if create
        else entry.get("builtin_variables", {})
    )
    if not isinstance(overrides, dict):
        if not create:
            return {}
        overrides = {}
        entry["builtin_variables"] = overrides
    return overrides


def _normalize_string_list(values: Any) -> List[str]:
    if isinstance(values, str):
        raw_items = re.split(r"[\n,]", values)
    elif isinstance(values, list):
        raw_items = values
    else:
        raw_items = []

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_parser_rules(rules: Any) -> List[Dict[str, Any]]:
    raw_rules = rules
    if isinstance(rules, str):
        stripped = rules.strip()
        if not stripped:
            return []
        try:
            raw_rules = json.loads(stripped)
        except Exception as exc:
            raise ValueError(f"invalid parser_rules JSON: {exc}") from exc

    if not isinstance(raw_rules, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        rule: Dict[str, Any] = {}
        for key, value in item.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                rule[str(key)] = value
            elif isinstance(value, list):
                rule[str(key)] = [
                    entry
                    for entry in value
                    if isinstance(entry, (str, int, float, bool)) or entry is None
                ]
            elif isinstance(value, dict):
                rule[str(key)] = {
                    str(child_key): child_value
                    for child_key, child_value in value.items()
                    if isinstance(child_value, (str, int, float, bool))
                    or child_value is None
                }
        if rule:
            normalized.append(rule)
    return normalized


def _built_in_var_ids_for_visit(visit_type: str) -> List[str]:
    return [
        str(item.get("var_id") or "").strip()
        for item in VARIABLE_SELECTOR_CATALOG.get(visit_type, [])
        if str(item.get("var_id") or "").strip()
    ]


def _built_in_variable_record_for_visit(visit_type: str, var_id: str) -> Dict[str, Any]:
    target_var_id = str(var_id or "").strip()
    if not target_var_id:
        return {}
    for item in VARIABLE_SELECTOR_CATALOG.get(visit_type, []):
        if str(item.get("var_id") or "").strip() == target_var_id:
            return dict(item)
    return {}


def _normalize_custom_variable_record(
    visit_type: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    label = str(payload.get("label") or "").strip()
    raw_var_id = str(payload.get("var_id") or payload.get("key") or label).strip()
    var_id = _slugify(raw_var_id)
    if not var_id:
        raise ValueError("custom variable requires a label or var_id")
    if var_id in _built_in_var_ids_for_visit(visit_type):
        raise ValueError(f"'{var_id}' conflicts with a built-in variable")

    return {
        "var_id": var_id,
        "label": label or var_id.replace("_", " ").title(),
        "group": str(payload.get("group") or _custom_group_name(visit_type)).strip(),
        "key": str(payload.get("key") or var_id).strip(),
        "keywords": _normalize_string_list(payload.get("keywords")),
        "anchors": _normalize_string_list(payload.get("anchors")),
        "parser_rules": _normalize_parser_rules(payload.get("parser_rules")),
        "description": str(payload.get("description") or "").strip(),
        "enabled": bool(payload.get("enabled", True)),
        "is_custom": True,
        "source": "custom",
    }


def _get_custom_variable_records(visit_type: str) -> List[Dict[str, Any]]:
    data = _load_selector_workbench()
    entry = _get_visit_workbench_entry(data, visit_type)
    variables = entry.get("custom_variables", {}) if isinstance(entry, dict) else {}
    if not isinstance(variables, dict):
        return []
    records = [dict(item) for item in variables.values() if isinstance(item, dict)]
    records.sort(
        key=lambda item: (
            str(item.get("label") or "").lower(),
            str(item.get("var_id") or "").lower(),
        )
    )
    return records


def get_selector_workbench_state(visit_type: str) -> Dict[str, Any]:
    data = _load_selector_workbench()
    entry = _get_visit_workbench_entry(data, visit_type)
    window_state = dict(DEFAULT_SELECTOR_WORKBENCH_WINDOW_STATE)
    if isinstance(entry, dict):
        raw_window_state = entry.get("window_state", {})
        if isinstance(raw_window_state, dict):
            window_state.update(raw_window_state)
    return {
        "visit_type": visit_type,
        "visit_key": _visit_type_key(visit_type),
        "window_state": window_state,
        "custom_variables": _get_custom_variable_records(visit_type),
    }


def save_selector_workbench_window_state(visit_type: str, state: Dict[str, Any]) -> str:
    data = _load_selector_workbench()
    entry = _get_visit_workbench_entry(data, visit_type, create=True)
    raw_state = state if isinstance(state, dict) else {}
    window_state = dict(DEFAULT_SELECTOR_WORKBENCH_WINDOW_STATE)

    for key in ("left", "top", "width", "height"):
        try:
            window_state[key] = int(raw_state.get(key, window_state[key]))
        except Exception:
            pass

    for key in ("restore_left", "restore_top", "restore_width", "restore_height"):
        raw_value = raw_state.get(key)
        if raw_value is None:
            continue
        try:
            window_state[key] = int(raw_value)
        except Exception:
            pass

    window_state["minimized"] = bool(raw_state.get("minimized", False))
    entry["window_state"] = window_state
    return _write_selector_workbench(data)


def save_custom_variable(visit_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("custom variable payload must be a dict")

    record = _normalize_custom_variable_record(visit_type, payload)
    data = _load_selector_workbench()
    entry = _get_visit_workbench_entry(data, visit_type, create=True)
    variables = entry.setdefault("custom_variables", {})
    if not isinstance(variables, dict):
        variables = {}
        entry["custom_variables"] = variables
    variables[record["var_id"]] = record
    _write_selector_workbench(data)
    return record


def save_builtin_variable_settings(
    visit_type: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("built-in variable payload must be a dict")

    var_id = str(payload.get("var_id") or "").strip()
    base_record = _built_in_variable_record_for_visit(visit_type, var_id)
    if not base_record:
        raise ValueError(f"unknown built-in variable: {var_id}")

    record = {
        "var_id": var_id,
        "label": str(
            payload.get("label") or base_record.get("label") or var_id
        ).strip(),
        "keywords": _normalize_string_list(
            payload.get("keywords", base_record.get("keywords"))
        ),
        "anchors": _normalize_string_list(
            payload.get("anchors", base_record.get("anchors"))
        ),
        "parser_rules": _normalize_parser_rules(
            payload.get("parser_rules", base_record.get("parser_rules"))
        ),
    }

    data = _load_selector_workbench()
    entry = _get_visit_workbench_entry(data, visit_type, create=True)
    overrides = entry.setdefault("builtin_variables", {})
    if not isinstance(overrides, dict):
        overrides = {}
        entry["builtin_variables"] = overrides
    overrides[var_id] = record
    _write_selector_workbench(data)
    return record


def delete_custom_variable(visit_type: str, var_id: str) -> str:
    target_var_id = _slugify(var_id)
    if not target_var_id:
        raise ValueError("var_id is required")

    data = _load_selector_workbench()
    entry = _get_visit_workbench_entry(data, visit_type, create=True)
    variables = entry.setdefault("custom_variables", {})
    if not isinstance(variables, dict):
        variables = {}
        entry["custom_variables"] = variables
    variables.pop(target_var_id, None)
    _write_selector_workbench(data)

    override_group = _custom_group_name(visit_type)
    overrides = _load_selector_overrides()
    group_map = overrides.get(override_group, {})
    if isinstance(group_map, dict) and target_var_id in group_map:
        group_map.pop(target_var_id, None)
        if group_map:
            overrides[override_group] = group_map
        else:
            overrides.pop(override_group, None)
        _write_selector_overrides(overrides)

    return target_var_id


def _normalize_selector_values(values: Any) -> List[str]:
    normalized: List[str] = []
    raw_items = values if isinstance(values, list) else [values]
    for item in raw_items:
        selector = ""
        if isinstance(item, str):
            selector = item.strip()
        elif isinstance(item, dict):
            item_type = str(item.get("type") or "css").strip().lower()
            if item_type == "css":
                selector = str(item.get("selector") or "").strip()
        if selector and selector not in normalized:
            normalized.append(selector)
    return normalized


def _get_ranked_playwright_selectors_from_config(
    engine_config: Dict[str, Any],
) -> List[str]:
    if not isinstance(engine_config, dict):
        return []
    if "ranked_css_list" in engine_config:
        return _normalize_selector_values(engine_config.get("ranked_css_list"))
    ranked = _normalize_selector_values(engine_config.get("css_list"))
    if ranked:
        return ranked
    return _normalize_selector_values(engine_config.get("css"))


def _persist_ranked_playwright_selectors(
    data: Dict[str, Any], group: str, key: str, selectors: List[str]
) -> str:
    cleaned = _normalize_selector_values(selectors)
    group_map = data.setdefault(group, {})
    key_map = group_map.setdefault(key, {})
    engine_map = key_map.setdefault("playwright", {})
    engine_map["ranked_css_list"] = cleaned
    engine_map["css_list"] = list(cleaned)
    engine_map["css"] = cleaned[0] if cleaned else ""
    return _write_selector_overrides(data)


def _merge_selector_lists(*selector_groups: Any) -> List[str]:
    merged: List[str] = []
    for group in selector_groups:
        for selector in _normalize_selector_values(group):
            if selector not in merged:
                merged.append(selector)
    return merged


def save_selector_override(group: str, key: str, selector: str) -> str:
    """Persist a chosen CSS selector for both selenium and playwright lookups."""
    selector = (selector or "").strip()
    if not group or not key or not selector:
        raise ValueError("group, key, and selector are required")

    data = _load_selector_overrides()
    group_map = data.setdefault(group, {})
    key_map = group_map.setdefault(key, {})
    for engine in ("selenium", "playwright"):
        engine_map = key_map.setdefault(engine, {})
        engine_map["css"] = selector
        engine_map["css_list"] = [selector]
        if engine == "playwright":
            engine_map["ranked_css_list"] = [selector]
    return _write_selector_overrides(data)


def get_ranked_playwright_selectors(group: str, key: str) -> List[str]:
    override_engine_config = _get_override_engine_config(group, key, "playwright")
    element_config = GRAB_POINTS.get(group, {}).get(key, {})
    engine_config = element_config.get("playwright", {})
    override_ranked = _get_ranked_playwright_selectors_from_config(override_engine_config)
    logged_ranked = _get_logged_playwright_selectors(group, key)
    default_ranked = _get_ranked_playwright_selectors_from_config(engine_config)
    return _merge_selector_lists(override_ranked, logged_ranked, default_ranked)


def add_ranked_playwright_selector(
    group: str, key: str, selector: str, promote_to_top: bool = False
) -> str:
    selector = (selector or "").strip()
    if not group or not key or not selector:
        raise ValueError("group, key, and selector are required")
    selectors = get_ranked_playwright_selectors(group, key)
    selectors = [item for item in selectors if item != selector]
    if promote_to_top:
        selectors.insert(0, selector)
    else:
        selectors.append(selector)
    data = _load_selector_overrides()
    return _persist_ranked_playwright_selectors(data, group, key, selectors)


def move_ranked_playwright_selector(
    group: str, key: str, selector: str, direction: str
) -> str:
    selector = (selector or "").strip()
    if not group or not key or not selector:
        raise ValueError("group, key, and selector are required")
    selectors = list(get_ranked_playwright_selectors(group, key))
    if selector not in selectors:
        raise ValueError("selector not found in ranked list")
    idx = selectors.index(selector)
    direction_norm = (direction or "").strip().lower()
    if direction_norm == "up" and idx > 0:
        selectors[idx - 1], selectors[idx] = selectors[idx], selectors[idx - 1]
    elif direction_norm == "down" and idx < len(selectors) - 1:
        selectors[idx + 1], selectors[idx] = selectors[idx], selectors[idx + 1]
    data = _load_selector_overrides()
    return _persist_ranked_playwright_selectors(data, group, key, selectors)


def remove_ranked_playwright_selector(group: str, key: str, selector: str) -> str:
    selector = (selector or "").strip()
    if not group or not key or not selector:
        raise ValueError("group, key, and selector are required")
    selectors = [
        item for item in get_ranked_playwright_selectors(group, key) if item != selector
    ]
    data = _load_selector_overrides()
    return _persist_ranked_playwright_selectors(data, group, key, selectors)


def _get_override_engine_config(group: str, key: str, engine: str) -> Dict[str, Any]:
    overrides = _load_selector_overrides()
    return overrides.get(group, {}).get(key, {}).get(engine, {})


def _decorate_selector_spec(raw_item: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(raw_item)
    try:
        item["playwright_ranked_selectors"] = get_ranked_playwright_selectors(
            item["group"], item["key"]
        )
    except Exception:
        item["playwright_ranked_selectors"] = []

    configured: List[str] = []
    for engine in ("playwright", "selenium"):
        try:
            primary = get_selector(
                item["group"], item["key"], engine=engine, selector_type="css"
            )
            if primary and primary not in configured:
                configured.append(primary)
        except Exception:
            pass
        try:
            for sel in get_selector_list(
                item["group"], item["key"], engine=engine, selector_type="css_list"
            ):
                if sel and sel not in configured:
                    configured.append(sel)
        except Exception:
            pass
    item["configured_selectors"] = configured
    item["is_custom"] = bool(item.get("is_custom", False))
    item["source"] = str(
        item.get("source") or ("custom" if item["is_custom"] else "builtin")
    )
    item["parser_rules"] = _normalize_parser_rules(item.get("parser_rules"))
    item["keywords"] = _normalize_string_list(item.get("keywords"))
    item["anchors"] = _normalize_string_list(item.get("anchors"))
    return item


def get_variable_selector_spec(
    visit_type: str,
    *,
    var_id: str = "",
    group: str = "",
    key: str = "",
) -> Dict[str, Any]:
    target_var_id = str(var_id or "").strip()
    target_group = str(group or "").strip()
    target_key = str(key or "").strip()
    for spec in get_variable_selector_specs(visit_type):
        if target_var_id and str(spec.get("var_id") or "").strip() == target_var_id:
            return dict(spec)
        if (
            target_group
            and target_key
            and str(spec.get("group") or "").strip() == target_group
            and str(spec.get("key") or "").strip() == target_key
        ):
            return dict(spec)
    return {}


def get_variable_selector_specs(visit_type: str) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    builtin_overrides = _get_builtin_variable_overrides(visit_type)
    for raw in VARIABLE_SELECTOR_CATALOG.get(visit_type, []):
        item = dict(raw)
        item["is_custom"] = False
        item["source"] = "builtin"
        item.setdefault("parser_rules", [])
        override = builtin_overrides.get(str(item.get("var_id") or "").strip(), {})
        if isinstance(override, dict):
            for field in ("label", "keywords", "anchors", "parser_rules"):
                if field in override:
                    item[field] = override[field]
        specs.append(_decorate_selector_spec(item))

    for raw in _get_custom_variable_records(visit_type):
        specs.append(_decorate_selector_spec(raw))
    return specs


def get_preferred_method(group, key):
    """
    Get the preferred method (selenium or playwright) for a given element.
    
    Args:
        group: The visit type group (e.g., 'sexual_health', 'birth_control')
        key: The element key (e.g., 'medication', 'lmp')
    
    Returns:
        str: The preferred method ('selenium' or 'playwright'), defaults to 'selenium'
    """
    return 'selenium'


def get_selector(group, key, engine='selenium', selector_type='css'):
    """
    Get a selector for a given element.
    
    Args:
        group: The visit type group (e.g., 'sexual_health', 'birth_control')
        key: The element key (e.g., 'medication', 'lmp')
        engine: The engine to use ('selenium' or 'playwright')
        selector_type: The type of selector ('css', 'xpath', etc.)
    
    Returns:
        str or None: The selector string, or None if not found
    """
    try:
        override_engine_config = _get_override_engine_config(group, key, engine)
        if engine == "playwright" and selector_type == "css":
            ranked = get_ranked_playwright_selectors(group, key)
            if ranked:
                return ranked[0] if ranked else None
        override_selector = override_engine_config.get(selector_type)
        if override_selector:
            return override_selector
        element_config = GRAB_POINTS.get(group, {}).get(key, {})
        engine_config = element_config.get(engine, {})
        if engine == "playwright" and selector_type == "css":
            ranked = _get_ranked_playwright_selectors_from_config(engine_config)
            if ranked:
                return ranked[0]
        return engine_config.get(selector_type)
    except (KeyError, AttributeError):
        return None


def get_selector_list(group, key, engine='selenium', selector_type='css_list'):
    """
    Get a list of selectors for a given element.
    
    Args:
        group: The visit type group (e.g., 'sexual_health', 'birth_control')
        key: The element key (e.g., 'medication', 'lmp')
        engine: The engine to use ('selenium' or 'playwright')
        selector_type: The type of selector list ('css_list', 'xpath_list', etc.)
    
    Returns:
        list: A list of selector strings, or empty list if not found
    """
    try:
        override_engine_config = _get_override_engine_config(group, key, engine)
        if engine == "playwright" and selector_type == "css_list":
            ranked = get_ranked_playwright_selectors(group, key)
            if ranked:
                return ranked
        override_selector_list = override_engine_config.get(selector_type, [])
        if selector_type == "css_list" and not override_selector_list:
            override_css = override_engine_config.get("css")
            if override_css:
                return [override_css]
        if override_selector_list:
            return (
                override_selector_list
                if isinstance(override_selector_list, list)
                else []
            )

        element_config = GRAB_POINTS.get(group, {}).get(key, {})
        engine_config = element_config.get(engine, {})
        if engine == "playwright" and selector_type == "css_list":
            ranked = _get_ranked_playwright_selectors_from_config(engine_config)
            if ranked:
                return ranked
        selector_list = engine_config.get(selector_type, [])

        if not selector_list and selector_type == 'css_list':
            css_selector = engine_config.get('css')
            if css_selector:
                return [css_selector]

        return selector_list if isinstance(selector_list, list) else []
    except (KeyError, AttributeError):
        return []
