"""Central configuration constants for EMR Assist.

All tuneable values, selector lists, visit-type keyword tables, lab definitions,
medication choices, and template button descriptors live here so that both the
browser-automation layer and the UI can import them without circular deps.
"""

from __future__ import annotations

import json
from typing import Dict, Any, List, Tuple

# ---------------------------------------------------------------------------
# Performance / debug controls
# ---------------------------------------------------------------------------
PERF_DEBUG = False
FAST_MODE_BP = True

# Global hotkey toggles
ENABLE_QUICK_NEXT_TASK_HOTKEY = False
QUICK_NEXT_TASK_HOTKEY = "ctrl+alt+enter"

# ---------------------------------------------------------------------------
# Effectiveness question aliases
# ---------------------------------------------------------------------------
EFFECTIVENESS_QUESTION_ALIASES: list[str] = [
    'are you happy with the way your treatment is working',
    'are you satisfied with the effectiveness of your tx',
    'are you satisfied with the effectiveness of your treatment',
    'are you satisfied with treatment effectiveness',
    'are you happy with the way your medication is working',
    'how is your sexual health treatment going so far',
    'how is your treatment working for you',
    'how is your treatment going so far',
    'are you getting the results you want',
    'is your treatment working',
    'are you happy with the effectiveness of your current treatment',
    'effectiveness of your current treatment',
]

# ---------------------------------------------------------------------------
# CDP / EMR URL configuration
# ---------------------------------------------------------------------------
CDP_DEBUG_PORT = 9222

CDP_EMR_PATIENT_URL_PREFIX = "https://emr.forhims.com/patients/"
EMR_DASHBOARD_BASE_URLS = {
    "https://emr.forhims.com",
    "http://emr.forhims.com",
}
EMR_DASHBOARD_WELCOME_SELECTORS = [
    "h1.mt-6.mb-4.self-center.text-center.text-2xl.font-medium",
    "div:nth-of-type(1) > div:nth-of-type(4) > div:nth-of-type(1) > h1",
]
CDP_DEBUG_VERBOSE = True
CDP_POLL_INTERVAL_SEC = 1.0
CDP_RETRY_DETECTION_SEC = 6.0
CDP_EMR_HOSTS = ("emr.forhims.com",)
CDP_REFRESH_NOURL_SEC = 10.0

CDP_HEADER_SELECTORS = [
    "div.css-1rynq56.r-cqee49.r-1kfrs79",
    "div[dir='auto'].css-1rynq56.r-cqee49.r-1kfrs79",
    "div.r-1d09ksm div.css-1rynq56.r-cqee49.r-1kfrs79",
    ".r-1ifxtd0 > .css-1rynq56",
    "div.r-1ifxtd0 > div.css-1rynq56",
]

CDP_TITLE_XPATHS = [
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' r-1d09ksm ')]//*[contains(concat(' ', normalize-space(@class), ' '), ' css-1rynq56 ') and contains(concat(' ', normalize-space(@class), ' '), ' r-cqee49 ') and contains(concat(' ', normalize-space(@class), ' '), ' r-1kfrs79 ')]",
    "//div[contains(@class,'css-1rynq56') and contains(@class,'r-cqee49') and contains(@class,'r-1kfrs79')]",
]

CDP_TITLE_SELECTORS = [
    "div.css-1rynq56.r-cqee49.r-1kfrs79",
    "div[dir='auto'].css-1rynq56.r-cqee49.r-1kfrs79",
    "div.r-1d09ksm div.css-1rynq56.r-cqee49.r-1kfrs79",
    ".r-1ifxtd0 > .css-1rynq56",
]

CDP_VISIT_TYPE_KEYWORDS = [
    ("performance anxiety", "Performance Anxiety"),
    ("sexual health", "Sexual Health"),
    ("premature ejaculation", "Sexual Health"),
    ("birth control", "Birth Control"),
    ("contraception", "Birth Control"),
    ("hair loss", "Hair Loss"),
    ("photoaging", "Photoaging"),
    ("acne", "Acne"),
    ("primary care", "Primary Care"),
    ("weight", "Weight"),
    ("sleep", "Sleep"),
    ("anxiety", "Anxiety"),
    ("depression", "Depression"),
]

VISIT_TYPE_FALLBACK_KEYWORDS = [
    ("performance anxiety", "Performance Anxiety"),
    ("sexual health", "Sexual Health"),
    ("premature ejaculation", "Sexual Health"),
    ("hair loss", "Hair Loss"),
    ("photoaging", "Photoaging"),
    ("birth control", "Birth Control"),
    ("contraception", "Birth Control"),
]

# Pre-serialised JSON used inside CDP evaluation scripts
CDP_TITLE_SELECTORS_JSON = json.dumps(CDP_TITLE_SELECTORS)
CDP_TITLE_XPATHS_JSON = json.dumps(CDP_TITLE_XPATHS)
CDP_VISIT_KEYWORDS_JSON = json.dumps([kw for kw, _ in VISIT_TYPE_FALLBACK_KEYWORDS])
CDP_HEADER_SELECTORS_JSON = json.dumps(CDP_HEADER_SELECTORS)
CDP_VISIT_FALLBACK_KEYWORDS_JSON = json.dumps([kw for kw, _ in VISIT_TYPE_FALLBACK_KEYWORDS])

_CDP_HEADER_EXPR_TEMPLATE = (
    """
(() => {
    const sels = __SELS__;
    const xpaths = __XPATHS__;
    const keywords = __KEYWORDS__;

    const normalize = (text) => (text || '').trim();
    const accept = (text) => {
        const raw = normalize(text);
        if (!raw) return null;
        const low = raw.toLowerCase();
        if (low.includes('navigation') || low.includes('menu')) return null;
        for (const kw of keywords) {
            if (low.includes(kw)) return raw;
        }
        return null;
    };

    const scrapeNodes = (doc, nodes) => {
        for (const node of nodes) {
            try {
                const txt = accept(node.innerText || node.textContent || '');
                if (txt) return txt;
            } catch (e) {}
        }
        return null;
    };

    const queryDocument = (doc) => {
        if (!doc) return null;
        for (const xp of xpaths) {
            try {
                const res = doc.evaluate(xp, doc, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                const nodes = [];
                for (let i = 0; i < res.snapshotLength; i++) {
                    nodes.push(res.snapshotItem(i));
                }
                const hit = scrapeNodes(doc, nodes);
                if (hit) return hit;
            } catch (e) {}
        }
        for (const sel of sels) {
            try {
                const matches = Array.from(doc.querySelectorAll(sel));
                const hit = scrapeNodes(doc, matches);
                if (hit) return hit;
            } catch (e) {}
        }
        return null;
    };

    const mainDoc = document;
    const direct = queryDocument(mainDoc);
    if (direct) return direct;

    const frames = Array.from(document.querySelectorAll('iframe'));
    for (const frame of frames) {
        try {
            const doc = frame.contentDocument;
            const hit = queryDocument(doc);
            if (hit) return hit;
        } catch (e) {}
    }

    try {
        const titleHit = accept(document.title || '');
        if (titleHit) return titleHit;
    } catch (e) {}

    return null;
})();
"""
)

CDP_HEADER_EXPRESSION = (
    _CDP_HEADER_EXPR_TEMPLATE
    .replace("__SELS__", CDP_TITLE_SELECTORS_JSON)
    .replace("__XPATHS__", CDP_TITLE_XPATHS_JSON)
    .replace("__KEYWORDS__", CDP_VISIT_KEYWORDS_JSON)
)

_CDP_VISIT_HEADER_SCRIPT = (
    _CDP_HEADER_EXPR_TEMPLATE
    .replace("__SELS__", CDP_TITLE_SELECTORS_JSON)
    .replace("__XPATHS__", CDP_TITLE_XPATHS_JSON)
    .replace("__KEYWORDS__", CDP_VISIT_FALLBACK_KEYWORDS_JSON)
)

# ---------------------------------------------------------------------------
# Tab ↔ visit type mapping
# ---------------------------------------------------------------------------
VISIT_TAB_INDICES: Dict[str, int] = {
    "Hair Loss": 0,
    "Photoaging": 1,
    "Sexual Health": 2,
    "Performance Anxiety": 4,
    "Birth Control": 5,
}

AUTO_CLICKER_TAB_INDEX = 3

# ---------------------------------------------------------------------------
# Intake form selectors
# ---------------------------------------------------------------------------
INTAKE_FORM_DATE_SELECTOR = (
    "div[data-testid='intake-form'] time, "
    "div[data-testid='IntakeForm'] time, "
    "time[datetime][dir]"
)

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
USE_PLAYWRIGHT_FOR_MED = True
USE_PLAYWRIGHT_FOR_SH = False
USE_CDP_FOR_TEXT = True
USE_CLIPBOARD_FOR_TEXT = True
AUTO_GRAB_DELAY_MS = 900
GUI_HIDDEN_VISIBLE_WIDTH = 65

AUTO_CLICKER_DEBUG = False


MEDICATION_CHOICES: List[str] = [
    "Enclomiphene 12.5 mg daily",
    "Enclomiphene/Tadalafil 12.5mg/8.5mg daily",
    "Enclomiphene 25 mg daily",
    "Enclomiphene 12.5 mg every other day",
    "Enclomiphene/tadalafil 12.5mg/8.5mg every other day",
]

# ---------------------------------------------------------------------------
# Birth Control PMH
# ---------------------------------------------------------------------------
BIRTH_CONTROL_PMH_OPTIONS: List[Tuple[str, str]] = [
    ("htn", "HTN (hypertension)"),
    ("hld", "HLD (hyperlipidemia)"),
    ("seizure", "Seizure disorder"),
    ("PCOS", "PCOS"),
    ("DM", "Diabetes (DM)"),
    ("endometriosis", "Endometriosis"),
]

BIRTH_CONTROL_PMH_KEYWORDS: Dict[str, List[str]] = {
    "htn": ["htn", "hypertension", "high blood pressure"],
    "hld": ["hld", "hyperlipidemia", "high cholesterol"],
    "seizure": ["seizure", "epilepsy", "seizure disorder"],
    "PCOS": ["pcos", "polycystic ovary", "polycystic ovarian"],
    "DM": ["dm", "diabetes", "type 1", "type 2", "diabetic"],
    "endometriosis": ["endometriosis"],
}

# ---------------------------------------------------------------------------
# Performance Anxiety options
# ---------------------------------------------------------------------------
PA_SITUATION_OPTIONS: List[str] = [
    "Public speaking", "Presentations", "Performances", "Auditions",
    "Job interviews", "Work meetings", "Exams", "Tests", "Class presentations",
    "Dating", "Networking", "Meeting new people", "Social gatherings",
    "Sexual performance", "Athletic competitions",
    "Driving", "Flying",
]

PA_SYMPTOM_OPTIONS: List[str] = [
    "Racing pulse", "Heart palpitations", "Fast breathing", "Shortness of breath",
    "Trembling", "Shaking",
    "Sweating", "Cold and clammy hands",
    "Stomach upset", "Nausea", "Butterflies", "Dry mouth",
    "Throat tightness", "Blurred vision", "Dizziness", "Lightheadedness",
    "Chest tightness", "Flushed", "Hot flashes", "Chills",
    "Racing pulse you can hear in my ears",
    "Hands, knees, lips, or my voice might tremble",
    "An uneasy feeling in the stomach",
]

PA_SYMPTOM_KEYWORDS: List[str] = [
    'breath', 'breathing', 'pulse', 'palpit', 'heart rate',
    'trembl', 'shake',
    'sweat', 'clammy', 'cold and clammy',
    'stomach', 'nausea', 'butterflies', 'dry mouth',
    'throat', 'vision', 'dizz', 'lighthead',
    'chest', 'flushed', 'hot', 'chills',
]


# ---------------------------------------------------------------------------
# PMH options (Rx note)
# ---------------------------------------------------------------------------
PMH_OPTIONS: List[str] = [
    "ED", "PE", "Thyroid disease", "BPH", "HTN", "HLD",
    "sleep apnea", "obesity", "overweight", "diabetes",
]

# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------
GEOCODER_USER_AGENT = "emr_assist_location_lookup"
CHIPPEWA_FALLS_QUERIES = ("Chippewa Falls, WI", "Chippewa Falls, Wisconsin")

# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------
def dprint(*args, **kwargs):
    """Print only when ``PERF_DEBUG`` is enabled."""
    if PERF_DEBUG:
        try:
            print(*args, **kwargs)
        except Exception:
            pass
