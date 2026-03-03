"""Pure text-parsing functions for EMR data extraction.

All functions in this module are framework-independent — they accept strings
and return primitive values or dicts.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .config import LABS_CONFIG, MEDICATION_CHOICES


# ---------------------------------------------------------------------------
# Lab value extraction
# ---------------------------------------------------------------------------

def extract_lab_value_simple(label_config: Dict[str, Any], text: str) -> Dict[str, str]:
    """Extract a lab value from EMR text using line-based pattern matching."""
    unit = label_config["unit"]
    patterns = label_config["patterns"]
    normal_range = label_config.get("normal_range", "")

    lines = [line.strip() for line in text.split('\n') if line.strip()]
    best_comparator_match: Optional[Dict[str, Any]] = None

    for pattern in patterns:
        for i, line in enumerate(lines):
            if pattern.lower() in line.lower():
                for j in range(i + 1, min(i + 5, len(lines))):
                    next_line = lines[j]
                    value_match = re.search(r'([<>≤≥]?\s*\d+\.?\d*)', next_line)
                    if value_match:
                        potential_value = value_match.group(1).strip()
                        unit_found = False
                        for k in range(j, min(j + 3, len(lines))):
                            if unit.lower() in lines[k].lower():
                                unit_found = True
                                break
                        if unit_found:
                            numeric_part = re.search(r'(\d+\.?\d*)', potential_value)
                            if numeric_part:
                                try:
                                    float_val = float(numeric_part.group(1))
                                    if 0.01 <= float_val <= 10000:
                                        result = {
                                            "value": f"{potential_value} {unit}",
                                            "raw_value": potential_value,
                                            "unit": unit,
                                            "normal_range": normal_range,
                                            "found": True,
                                            "matched_pattern": f"Line-based: {pattern}",
                                        }
                                        is_comparator = potential_value.strip().startswith(("<", ">", "≤", "≥"))
                                        if is_comparator and label_config.get("var") == "total_testosterone":
                                            if best_comparator_match is None:
                                                best_comparator_match = result
                                            continue
                                        return result
                                except ValueError:
                                    continue

    if best_comparator_match:
        return best_comparator_match

    return {
        "value": "",
        "raw_value": "",
        "unit": label_config["unit"],
        "normal_range": normal_range,
        "found": False,
        "matched_pattern": "Not found",
    }


# ---------------------------------------------------------------------------
# TDCS / TDCS-C scoring
# ---------------------------------------------------------------------------

def parse_tdcs_score(text: str) -> int:
    """Count ``Add +1`` and ``Add +2`` markers to compute a TDCS score."""
    plus_one = len(re.findall(r'Add \+1 to TDCS score', text, re.IGNORECASE))
    plus_two = len(re.findall(r'Add \+2 to TDCS score', text, re.IGNORECASE))
    score = plus_one + plus_two * 2
    print(f"TDCS parsing: Found {plus_one} '+1' and {plus_two} '+2' = Total score: {score}")
    return score


def parse_tdcsc_score(text: str) -> Optional[int]:
    """Parse TDCS-C (change) score from symptom-change questionnaire."""
    questions = [
        ("Since starting your treatment, how has your sex drive changed?", 2),
        ("Since starting your treatment, how has your ability to get or keep an erection changed?", 2),
        ("Since starting your treatment, how has your physical strength or endurance changed?", 1),
        ("Since starting your treatment, how has your need to nap to feel alert changed?", 1),
        ("Since starting your treatment, how have your energy levels changed?", 1),
        ("Since starting your treatment, how has your mental clarity or brain fog changed?", 1),
        ("Since starting your treatment, how has your motivation changed?", 1),
        ("Since starting your treatment, how has your mood changed?", 1),
    ]
    response_points = {
        "much better": 2,
        "a little better": 1,
        "no change": 0,
        "a little worse": -1,
        "much worse": -2,
    }
    total = 0
    matched = 0
    for question, weight in questions:
        resp = _extract_response_after_question(question, text)
        if not resp:
            continue
        normalized = resp.strip().lower()
        points = None
        for key, value in response_points.items():
            if key in normalized:
                points = value
                break
        if points is None:
            continue
        matched += 1
        total += points * weight
    if matched == 0:
        print("TDCS-C parsing: no responses found")
        return None
    print(f"TDCS-C parsing: Matched {matched} responses = Total score: {total}")
    return total


# ---------------------------------------------------------------------------
# ED status
# ---------------------------------------------------------------------------

def parse_ed_status(text: str) -> str:
    """Return ``'Yes'``, ``'No'``, or ``'—'`` from the erection-difficulty question."""
    pattern = r'Do you sometimes have difficulty getting or keeping a hard erection\?\s*([YN][eo][s]?)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        answer = match.group(1).lower()
        if answer.startswith('y'):
            result = "Yes"
        elif answer.startswith('n'):
            result = "No"
        else:
            result = "—"
        print(f"ED status parsed: {result}")
        return result
    print("ED status not found in text")
    return "—"


# ---------------------------------------------------------------------------
# Questionnaire helpers
# ---------------------------------------------------------------------------

def _extract_response_after_question(question: str, text: str) -> str:
    """Find the line immediately after *question* and return it."""
    try:
        pattern = re.escape(question) + r"\s*\n\s*(?:Response:\s*)?([^\n]+)"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    except Exception as e:
        print(f"Question parse error: {e}")
    return ""


def parse_treatment_satisfaction(text: str) -> str:
    resp = _extract_response_after_question("Q: Overall, how satisfied are you with your treatment?", text)
    if not resp:
        return "—"
    normalized = resp.lower()
    mapping = {
        "very satisfied": "Very satisfied",
        "satisfied": "Satisfied",
        "neutral": "Neutral",
        "dissatisfied": "Dissatisfied",
        "very dissatisfied": "Very dissatisfied",
    }
    for key, val in mapping.items():
        if key in normalized:
            return val
    return resp


def parse_side_effects_response(text: str) -> str:
    resp = _extract_response_after_question("Q: Have you experienced any side effects?", text)
    if not resp:
        return "No side effects reported"
    if resp.strip().lower().startswith("no"):
        return "No side effects reported"
    return resp


# ---------------------------------------------------------------------------
# Diagnosis detection
# ---------------------------------------------------------------------------

def detect_td_diagnosis(text: str) -> bool:
    """Return ``True`` if the text mentions testosterone deficiency / low-T."""
    if not text:
        return False
    low = text.lower()
    keywords = (
        "testosterone deficiency", "t deficiency", "low testosterone",
        "low t", "hypogonadism", "hypogonadal",
    )
    return any(k in low for k in keywords)


# ---------------------------------------------------------------------------
# Medication detection
# ---------------------------------------------------------------------------

def _normalize_medication_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


# Flexible matching patterns: map regex → canonical MEDICATION_CHOICES string
_FLEXIBLE_MED_PATTERNS: list = [
    # Enclomiphene + Tadalafil combos (check combos first)
    (re.compile(r'enclomiphene.*?12\.?5\s*mg.*?tadalafil.*?8\.?5\s*mg.*?every\s*other\s*day', re.IGNORECASE),
     "Enclomiphene/tadalafil 12.5mg/8.5mg every other day"),
    (re.compile(r'tadalafil.*?8\.?5\s*mg.*?enclomiphene.*?12\.?5\s*mg.*?every\s*other\s*day', re.IGNORECASE),
     "Enclomiphene/tadalafil 12.5mg/8.5mg every other day"),
    (re.compile(r'enclomiphene.*?tadalafil.*?12\.?5.*?8\.?5.*?every\s*other\s*day', re.IGNORECASE),
     "Enclomiphene/tadalafil 12.5mg/8.5mg every other day"),
    (re.compile(r'enclomiphene.*?12\.?5\s*mg.*?tadalafil.*?8\.?5\s*mg.*?(?:daily|qd)', re.IGNORECASE),
     "Enclomiphene/Tadalafil 12.5mg/8.5mg daily"),
    (re.compile(r'tadalafil.*?8\.?5\s*mg.*?enclomiphene.*?12\.?5\s*mg.*?(?:daily|qd)', re.IGNORECASE),
     "Enclomiphene/Tadalafil 12.5mg/8.5mg daily"),
    (re.compile(r'enclomiphene.*?tadalafil.*?12\.?5.*?8\.?5.*?(?:daily|qd)', re.IGNORECASE),
     "Enclomiphene/Tadalafil 12.5mg/8.5mg daily"),
    # Enclomiphene 25mg
    (re.compile(r'enclomiphene(?:\s+citrate)?\s*25\s*mg.*?(?:daily|qd)', re.IGNORECASE),
     "Enclomiphene 25 mg daily"),
    # Enclomiphene 12.5mg every other day
    (re.compile(r'enclomiphene(?:\s+citrate)?\s*12\.?5\s*mg.*?every\s*other\s*day', re.IGNORECASE),
     "Enclomiphene 12.5 mg every other day"),
    # Enclomiphene 12.5mg daily (broadest single-agent match last)
    (re.compile(r'enclomiphene(?:\s+citrate)?\s*12\.?5\s*mg.*?(?:daily|qd)', re.IGNORECASE),
     "Enclomiphene 12.5 mg daily"),
]


def detect_medication_from_text(text: str) -> Optional[str]:
    """Return the first matching ``MEDICATION_CHOICES`` entry found in *text*.

    Uses two strategies:
    1. Exact normalised substring match against MEDICATION_CHOICES.
    2. Flexible regex matching to handle EMR formatting variants
       (e.g. 'Enclomiphene Citrate', extra spaces, reversed order).
    """
    if not text:
        return None
    normalized_content = _normalize_medication_text(text)
    if not normalized_content:
        return None
    # Strategy 1: exact normalised match
    for option in MEDICATION_CHOICES:
        normalized_option = _normalize_medication_text(option)
        if normalized_option and normalized_option in normalized_content:
            return option
    # Strategy 2: flexible regex on original text
    for pattern, canonical in _FLEXIBLE_MED_PATTERNS:
        if pattern.search(text):
            return canonical
    return None
