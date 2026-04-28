"""Pure text-parsing functions for EMR data extraction.

All functions in this module are framework-independent — they accept strings
and return primitive values or dicts.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .config import MEDICATION_CHOICES


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


HAIR_MEDICATION_KEYWORDS = (
    "finasteride",
    "minoxidil",
    "dutasteride",
    "spironolactone",
    "ketoconazole",
    "topical",
    "oral minoxidil",
    "solution",
    "spray",
    "foam",
    "compound",
    "cmpd",
)

HAIR_SYMPTOM_OPTIONS = (
    "Thinning at the hairline",
    "Thinning on the top of the head",
    "Bald patches, smooth and hairless not at the top of the head",
    "Redness and irritation found at sites of hair loss",
    "I'll take a photo of my head instead",
)

_HAIR_RESPONSE_PATTERNS = (
    r"good response|excellent response|positive response",
    r"no change|no improvement|same|stable",
    r"worse|getting worse|declining",
    r"improved|better|improvement|some improvement",
    r"minimal.*response|slight.*improvement",
    r"significant.*improvement|much better",
    r"side effects|stopped.*due",
    r"continued.*improvement|ongoing.*improvement",
)

_HAIR_HEADER_RE = re.compile(
    r"^(current dose|treatment plan|treatment|medication|current treatment|meds|dose|photos|notes|click an image|intake forms|patient selected|responses?|instructions|visit type|hair loss)\b",
    re.IGNORECASE,
)
_HAIR_TREATMENT_HEADER_RE = re.compile(r"^treatment\s*[:\-]*$", re.IGNORECASE)
_HAIR_TREATMENT_INLINE_RE = re.compile(r"^treatment\s*[:\-]\s*(.+)$", re.IGNORECASE)
_HAIR_FREQUENCY_RE = re.compile(
    r"\b(daily|weekly|monthly|every|q\d+h|nightly|bedtime|hs|qhs|qam|qpm|bid|tid|qid|once|twice|per\s+\w+|dose[s]?\s+per|each\s+\w+)\b",
    re.IGNORECASE,
)
_HAIR_TREATMENT_UI_RE = re.compile(
    r"^(edit|refer patient|continue rx|photos|click an image|diagnostic photo)\b",
    re.IGNORECASE,
)
_BP_DASH = r"[-\u2012\u2013\u2014\u2212]"
_BP_RANGE_RE = re.compile(
    rf"\b(\d{{2,3}})\s*{_BP_DASH}\s*(\d{{2,3}})\s*/\s*(\d{{2,3}})\s*{_BP_DASH}\s*(\d{{2,3}})\b",
    re.IGNORECASE,
)
_BP_SINGLE_RE = re.compile(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b(?!\s*/\s*\d{2,4})", re.IGNORECASE)
_MONTHLY_DOSE_COUNT_RE = re.compile(
    r"(?<!\d)(\d{1,3})\s*doses?\s*(?:(?:per|/|a)\s*month|monthly)\b",
    re.IGNORECASE,
)

_CADENCE_SUFFIX_GROUPS = {"sexual_health", "performance_anxiety"}


def infer_medication_frequency_suffix_from_text(
    *text_blocks: Optional[str],
    group: Optional[str] = None,
) -> str:
    """Return a normalized medication frequency suffix from grabbed text."""
    normalized_group = str(group or "").strip().lower()
    if normalized_group not in _CADENCE_SUFFIX_GROUPS:
        return ""
    for block in text_blocks:
        if not block:
            continue
        normalized = re.sub(r"\s+", " ", str(block).replace("\xa0", " "))
        for match in _MONTHLY_DOSE_COUNT_RE.finditer(normalized):
            if match.group(1) == "30":
                return ", daily"
    return ", as-needed"


def _is_hair_medication_candidate(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return False
    low = text.lower()
    if _HAIR_HEADER_RE.match(text):
        return False
    if low.endswith("?"):
        return False
    if re.match(r"^(photos|notes|click an image|intake forms|hair loss|visit type)\b", low, re.IGNORECASE):
        return False
    if any(keyword in low for keyword in HAIR_MEDICATION_KEYWORDS):
        return True
    if "%" in text:
        return True
    if re.search(r"\b(topical|spray|solution|foam|compound|cmpd)\b", low):
        return True
    if re.search(r"\b\d+(?:\.\d+)?\s*mg\b", low):
        return True
    return False


def extract_hair_medication_from_text(text: str) -> str:
    """Extract a plausible hair-loss medication line from copied EMR text."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""

    treatment_line = ""
    treatment_frequency_hint = ""
    saw_treatment_section = False

    def find_next_treatment_content(start_idx: int, lookahead: int = 8) -> str:
        for idx in range(start_idx, min(start_idx + lookahead, len(lines))):
            candidate = lines[idx].strip()
            if not candidate:
                continue
            if _HAIR_TREATMENT_UI_RE.match(candidate):
                continue
            if _HAIR_HEADER_RE.match(candidate):
                continue
            return candidate
        return ""

    def find_next_med_like(start_idx: int, lookahead: int = 8) -> str:
        for idx in range(start_idx, min(start_idx + lookahead, len(lines))):
            candidate = lines[idx].strip()
            if _HAIR_TREATMENT_UI_RE.match(candidate):
                continue
            if _is_hair_medication_candidate(candidate):
                return candidate
        return ""

    for idx, line in enumerate(lines):
        inline_match = _HAIR_TREATMENT_INLINE_RE.match(line)
        if inline_match:
            saw_treatment_section = True
            candidate = inline_match.group(1).strip()
            if _is_hair_medication_candidate(candidate):
                treatment_line = candidate
            break
        if _HAIR_TREATMENT_HEADER_RE.match(line):
            saw_treatment_section = True
            candidate = find_next_treatment_content(idx + 1, 8)
            if _is_hair_medication_candidate(candidate):
                treatment_line = candidate
            if treatment_line:
                for probe in range(idx + 1, min(idx + 6, len(lines))):
                    freq_candidate = lines[probe].strip()
                    if _HAIR_TREATMENT_UI_RE.match(freq_candidate):
                        continue
                    if _HAIR_FREQUENCY_RE.search(freq_candidate):
                        treatment_frequency_hint = freq_candidate
                        break
            break

    med = treatment_line
    if not med and not saw_treatment_section:
        for idx, line in enumerate(lines):
            if re.search(r"^(treatment|medication|current treatment|meds)\b", line, re.IGNORECASE):
                med = find_next_med_like(idx + 1, 6)
                if med:
                    break

    if not med and not saw_treatment_section:
        for line in lines:
            if _is_hair_medication_candidate(line):
                med = line
                break

    if not med:
        return ""

    med = med.strip().strip("*").strip()
    if _HAIR_HEADER_RE.match(med):
        idx = lines.index(med)
        med = find_next_med_like(idx + 1, 6) or med

    if med and not _HAIR_FREQUENCY_RE.search(med) and treatment_frequency_hint and _HAIR_FREQUENCY_RE.search(treatment_frequency_hint):
        return med.rstrip(" .") + " daily"
    return med


def extract_hair_response_from_text(text: Optional[str]) -> str:
    """Extract the treatment-response text for Hair Loss visits."""
    raw = (text or "").strip()
    if not raw:
        return ""

    inline_match = re.search(
        r"how has your treatment affected your hair loss\??\s*[:\-]?\s*(.+)",
        raw,
        re.IGNORECASE,
    )
    if inline_match:
        candidate = inline_match.group(1).strip()
        if candidate and not _HAIR_HEADER_RE.match(candidate):
            return candidate

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        if re.search(r"how has your treatment affected your hair loss", line, re.IGNORECASE):
            for probe in range(idx + 1, min(idx + 4, len(lines))):
                candidate = lines[probe].strip()
                if not candidate:
                    continue
                if _HAIR_HEADER_RE.match(candidate) or _HAIR_TREATMENT_UI_RE.match(candidate):
                    continue
                if len(candidate) > 3:
                    return candidate
            break

    for line in lines:
        if _HAIR_HEADER_RE.match(line) or _HAIR_TREATMENT_UI_RE.match(line):
            continue
        for pattern in _HAIR_RESPONSE_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                return line

    for line in lines:
        if _HAIR_HEADER_RE.match(line) or _HAIR_TREATMENT_UI_RE.match(line):
            continue
        if (
            re.search(r"hair.*(?:better|worse|same|improved|stable|thicker|thinner)", line, re.IGNORECASE)
            or re.search(r"(?:better|worse|same|improved|stable|thicker|thinner).*hair", line, re.IGNORECASE)
        ):
            return line

    return ""


def _normalize_hair_option_line(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).strip(" .,:;-")


def _hair_selected_option_lines(text: Optional[str]) -> List[str]:
    lines: List[str] = []
    for raw_line in (text or "").splitlines():
        line = _normalize_hair_option_line(raw_line)
        if not line:
            continue
        if line.lower().startswith("show unselected answers"):
            break
        if line.lower().startswith("patient selected"):
            continue
        lines.append(line)
    return lines


def _extract_hair_option_matches(text: Optional[str], options: List[str] | tuple[str, ...]) -> List[str]:
    normalized_options = {
        _normalize_hair_option_line(option).lower(): option
        for option in options
    }
    matches: List[str] = []
    for line in _hair_selected_option_lines(text):
        option = normalized_options.get(line.lower())
        if option and option not in matches:
            matches.append(option)
    return matches


def extract_hair_symptoms_from_text(text: Optional[str]) -> str:
    """Extract selected Hair Loss response lines before hidden answers appear."""
    matches = _extract_hair_option_matches(text, HAIR_SYMPTOM_OPTIONS)
    return ", ".join(matches)


def normalize_blood_pressure_value(value: Optional[str]) -> str:
    """Return a clean BP range/single reading or a normalized fallback token."""
    text = (value or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low in {"nr", "n/r", "not required"}:
        return "not required"
    if "not required to report bp" in low:
        return "not required"

    match = _BP_RANGE_RE.search(text)
    if match:
        s1, s2, d1, d2 = map(int, match.groups())
        if 70 <= s1 <= 250 and 70 <= s2 <= 250 and 30 <= d1 <= 150 and 30 <= d2 <= 150 and s1 <= s2 and d1 <= d2:
            return f"{s1}-{s2}/{d1}-{d2}"

    match = _BP_SINGLE_RE.search(text)
    if match:
        systolic, diastolic = map(int, match.groups())
        if 70 <= systolic <= 250 and 30 <= diastolic <= 150 and systolic > diastolic:
            return f"{systolic}/{diastolic}"

    return text


def extract_hair_loss_location_from_text(text: Optional[str]) -> str:
    """Return normalized hair-loss location text from a narrow raw selector grab."""
    matches = _extract_hair_option_matches(text, HAIR_SYMPTOM_OPTIONS)
    return ", ".join(matches)


def extract_hair_loss_additional_sxx_from_text(text: Optional[str]) -> str:
    """Return normalized additional hair-loss symptom text from a narrow raw selector grab."""
    raw = (text or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    none_text = "No, none of these"
    symptom_options = [
        "Burning or pain",
        "Patches of rough, scaly skin or scarring",
        "Pustules or crusting",
    ]
    matches = [option for option in symptom_options if option.lower() in lowered]
    if none_text.lower() in lowered and not matches:
        return "none, denies burning, pain, patches of rough scaly skin, scarring, pustules, and crusting"
    return ", ".join(matches) if matches else ""


def medication_implies_hair_loss(text: Optional[str]) -> bool:
    """Return True when the medication text clearly indicates a hair-loss treatment."""
    low = (text or "").strip().lower()
    if not low:
        return False
    return ("finasteride" in low) or ("minoxidil" in low)


def allow_hair_loss_diagnosis(visit_type: Optional[str], medication_text: Optional[str]) -> bool:
    """Hair Loss diagnosis is only valid on Hair Loss visits or hair-loss medications."""
    visit = (visit_type or "").strip().lower()
    if visit == "hair loss":
        return True
    return medication_implies_hair_loss(medication_text)
