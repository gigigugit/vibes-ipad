"""Visit-type prompt templates for Ollama clinical note generation.

Each function returns a list of chat messages (system + user) that instruct
the model to fill narrative gaps in an existing template skeleton, using
de-identified grabbed variables as context.

The module also exposes a *section-based* API via ``get_section_prompts()``
that returns three independent prompt sets (conversation, subjective, plan)
suitable for generating three separate outputs.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Shared system instruction (also the default editable system prompt)
# ---------------------------------------------------------------------------

_SYSTEM_BASE = (
    "You are a clinical note assistant for a telehealth physician. "
    "Your role is to help fill in narrative portions of clinical notes. "
    "Use concise, professional medical language. "
    "Do NOT invent clinical data — only use values explicitly provided. "
    "Where information is missing, leave a bracketed placeholder like [value]. "
    "Output plain text only (no markdown headers or bullet points). "
    "Keep the SOAP format (Subjective, Objective, Assessment, Plan) when applicable."
)

# Public alias so the window can show the default in the editable system prompt
DEFAULT_SYSTEM_PROMPT: str = _SYSTEM_BASE

# ---------------------------------------------------------------------------
# Section-specific instructions (appended to the user prompt per-section)
# ---------------------------------------------------------------------------

SECTION_INSTRUCTIONS: Dict[str, str] = {
    "conversation": (
        "Generate a reply as if you are the clinician responding to the "
        "patient in a telehealth chat. Conversational, warm, concise — like "
        "a real chat message, not a formal letter or clinical note. "
        "Cover: explanations of results, teaching points, answering likely "
        "patient questions. No SOAP format, no headers. Just a natural chat "
        "response. Keep it under 200 words."
    ),
    "subjective": (
        "Generate ONLY the Subjective (S:) section of a SOAP note. "
        "Include the patient's reported symptoms, response to treatment, "
        "side effects, and any structured scoring instruments. "
        'Match clinician chart-note style, e.g. '
        '"S: Patient reports he feels \\"[response]\\" with regard to the '
        'treatment, and reports side effects of: \\"[side_effects]\\"". '
        "Do NOT include O:, A:, or P: sections. Be concise — 2-4 sentences."
    ),
    "plan": (
        "Generate ONLY the Assessment and Plan (A: and P:) sections of a "
        "SOAP note. Include: diagnosis, clinical reasoning, treatment plan, "
        "medication decisions, referral reasons, and follow-up timing. "
        "Match clinician chart-note style, e.g.:\n"
        "A: [assessment / diagnosis]\n\n"
        "P: [plan — medication, follow-up, referrals]\n"
        "Prescription written\n\n"
        "Do NOT include S: or O: sections. Be concise."
    ),
}

# Names for the three output sections (used by the window UI)
SECTION_NAMES: List[str] = ["conversation", "subjective", "plan"]

# ---------------------------------------------------------------------------
# Lab interpretation helper
# ---------------------------------------------------------------------------

_LAB_RANGES = {
    "total_testosterone": ("ng/dL", 264, 916),
    "free_testosterone": ("ng/dL", 5.0, 21.0),
    "psa": ("ng/mL", 0.0, 4.0),
    "estradiol": ("pg/mL", 10, 40),
    "hematocrit": ("%", 38.3, 48.6),
    "fsh": ("mIU/mL", 1.5, 12.4),
    "lh": ("mIU/mL", 1.8, 8.6),
    "shbg": ("nmol/L", 10, 57),
    "albumin": ("g/dL", 3.5, 5.0),
}


def _format_lab_context(vars_dict: Dict[str, str]) -> str:
    """Build a human-readable lab summary with normal-range annotations."""
    lines: list[str] = []
    for key, (unit, lo, hi) in _LAB_RANGES.items():
        val = vars_dict.get(key, "").strip()
        if not val or val == "—":
            continue
        try:
            num = float(val)
            flag = ""
            if num < lo:
                flag = " (LOW)"
            elif num > hi:
                flag = " (HIGH)"
            else:
                flag = " (normal)"
            lines.append(f"  {key.replace('_', ' ').title()}: {val} {unit}{flag}")
        except ValueError:
            lines.append(f"  {key.replace('_', ' ').title()}: {val} {unit}")
    return "\n".join(lines) if lines else "  No lab values available."


def format_lab_block(vars_dict: Dict[str, str]) -> str:
    """Return a formatted O: section from grabbed lab data (for SOAP insertion).

    This is NOT AI-generated — it mirrors the templates/*.txt convention.
    """
    lines: list[str] = []
    for key, (unit, lo, hi) in _LAB_RANGES.items():
        val = vars_dict.get(key, "").strip()
        if not val or val == "—":
            continue
        lines.append(f"{key.replace('_', ' ').title()}: {val} {unit}")
    return "\n".join(lines) if lines else ""


def _format_vars_context(vars_dict: Dict[str, str], keys: list[str]) -> str:
    """Format selected variables into a readable context block."""
    lines: list[str] = []
    for k in keys:
        v = vars_dict.get(k, "").strip()
        if v and v != "—":
            label = k.replace("_", " ").title()
            lines.append(f"  {label}: {v}")
    return "\n".join(lines) if lines else "  (no data)"


def _all_vars_context(vars_dict: Dict[str, str]) -> str:
    """Summarise every non-empty var for the conversation section."""
    lines: list[str] = []
    for k, val in sorted(vars_dict.items()):
        if val and val.strip() and val.strip() != "—" and not k.startswith("_"):
            lines.append(f"  {k.replace('_', ' ').title()}: {val}")
    return "\n".join(lines) if lines else "  (no data)"


# ---------------------------------------------------------------------------
# Per-visit-type variable keys used by each section
# ---------------------------------------------------------------------------

_SECTION_KEYS: Dict[str, Dict[str, list]] = {
    "T Deficiency": {
        "subjective": [
            "tdcs", "tdcs_c", "ed_status", "response", "side_effects",
        ],
        "plan": [
            "medication", "diagnoses", "pmh",
        ],
    },
    "Hair Loss": {
        "subjective": [
            "hair_med", "hsx", "hvar", "hair_loss_location",
            "hair_loss_additional_sxx",
        ],
        "plan": ["medication", "diagnoses"],
    },
    "Sexual Health": {
        "subjective": [
            "sh_med", "sh_effectiveness", "bp", "diagnoses",
        ],
        "plan": ["medication", "diagnoses"],
    },
    "Performance Anxiety": {
        "subjective": [
            "pa_med", "pa_situations", "pa_symptoms", "bp", "pulse",
        ],
        "plan": ["medication", "diagnoses"],
    },
    "Birth Control": {
        "subjective": [
            "bc_med", "bc_lmp", "bp", "bc_side_effects",
        ],
        "plan": ["medication", "pmh", "diagnoses"],
    },
    "Photoaging": {
        "subjective": [
            "photoaging_med", "photoaging_goals", "photoaging_retinoid",
        ],
        "plan": ["medication", "diagnoses"],
    },
}


def _keys_for_section(visit_type: str, section: str) -> list:
    """Return variable keys appropriate for *section* given the visit type."""
    vt = visit_type.split(" - ")[0]  # strip " - Follow-up" etc.
    vt_map = _SECTION_KEYS.get(vt, {})
    return vt_map.get(section, [])


# ---------------------------------------------------------------------------
# Section-based prompt builder (NEW — three outputs)
# ---------------------------------------------------------------------------

def get_section_prompts(
    visit_type: str,
    vars_dict: Dict[str, str],
    system_override: Optional[str] = None,
    user_override: Optional[str] = None,
) -> Dict[str, List[dict]]:
    """Build prompt message lists for the three output sections.

    Returns ``{"conversation": [...], "subjective": [...], "plan": [...]}``.
    Each value is a standard ``[{role: system, ...}, {role: user, ...}]`` list.
    """
    system_msg = system_override or _SYSTEM_BASE
    labs = _format_lab_context(vars_dict)
    result: Dict[str, List[dict]] = {}

    for section in SECTION_NAMES:
        section_instr = SECTION_INSTRUCTIONS[section]

        if section == "conversation":
            ctx = _all_vars_context(vars_dict)
        else:
            keys = _keys_for_section(visit_type, section)
            ctx = _format_vars_context(vars_dict, keys) if keys else _all_vars_context(vars_dict)

        user_base = user_override or (
            f"Visit type: {visit_type}\n\n"
            f"Lab Results:\n{labs}\n\n"
            f"Clinical Context:\n{ctx}"
        )

        user_content = f"{user_base}\n\n---\n{section_instr}"
        result[section] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_content},
        ]

    return result


# ---------------------------------------------------------------------------
# Single-section rebuild (for individual Regenerate buttons)
# ---------------------------------------------------------------------------

def get_single_section_prompt(
    section: str,
    visit_type: str,
    vars_dict: Dict[str, str],
    system_override: Optional[str] = None,
    user_override: Optional[str] = None,
) -> List[dict]:
    """Build prompt for one section only."""
    full = get_section_prompts(visit_type, vars_dict, system_override, user_override)
    return full[section]


# ---------------------------------------------------------------------------
# Visit-type prompt builders (legacy single-output API — kept for compat)
# ---------------------------------------------------------------------------

def get_prompt(visit_type: str, vars_dict: Dict[str, str]) -> List[dict]:
    """Return chat messages for the given visit type and variables."""
    builder = _PROMPT_BUILDERS.get(visit_type, _prompt_generic)
    return builder(vars_dict)


def _prompt_t_deficiency_initial(v: Dict[str, str]) -> List[dict]:
    labs = _format_lab_context(v)
    ctx = _format_vars_context(v, ["tdcs", "ed_status", "medication", "diagnoses", "pmh"])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a patient-facing lab results message for a new testosterone deficiency patient. "
            "This is the initial visit — explain the lab results and whether they qualify for treatment. "
            "Use a warm but professional tone. Keep it under 150 words.\n\n"
            f"Lab Results:\n{labs}\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Structure: Greeting → lab summary → qualification statement → what to expect next."
        )},
    ]


def _prompt_t_deficiency_followup(v: Dict[str, str]) -> List[dict]:
    labs = _format_lab_context(v)
    ctx = _format_vars_context(v, [
        "tdcs_c", "response", "side_effects", "medication", "diagnoses",
    ])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a patient-facing follow-up message for a testosterone deficiency patient. "
            "Compare current labs to expected ranges, address their reported response and side effects, "
            "and provide a brief recommendation. Keep under 150 words.\n\n"
            f"Lab Results:\n{labs}\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Structure: Greeting → lab review → response acknowledgment → recommendation."
        )},
    ]


def _prompt_t_deficiency_soap(v: Dict[str, str]) -> List[dict]:
    labs = _format_lab_context(v)
    ctx = _format_vars_context(v, [
        "tdcs", "tdcs_c", "ed_status", "response", "side_effects",
        "medication", "diagnoses", "pmh",
    ])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a clinical SOAP note for a testosterone deficiency follow-up visit. "
            "Fill in narrative phrasing around the provided data points. "
            "Be concise — this is a chart note, not a patient message.\n\n"
            f"Lab Results:\n{labs}\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Format:\n"
            "S: [subjective — patient's reported response and side effects]\n"
            "O: [objective — lab values with interpretations]\n"
            "A: [assessment — diagnosis, clinical reasoning]\n"
            "P: [plan — medication continuation/change, follow-up timing]"
        )},
    ]


def _prompt_hair_loss(v: Dict[str, str]) -> List[dict]:
    ctx = _format_vars_context(v, [
        "hair_med", "hsx", "hvar", "hair_loss_location",
        "hair_loss_additional_sxx", "medication",
    ])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a brief clinical SOAP note for a hair loss (androgenic alopecia) visit. "
            "Use the patient's reported symptoms and treatment response to fill narrative gaps.\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Format: S/O/A/P. Keep each section to 1-2 sentences."
        )},
    ]


def _prompt_sexual_health(v: Dict[str, str]) -> List[dict]:
    ctx = _format_vars_context(v, [
        "sh_med", "sh_effectiveness", "bp", "diagnoses",
        "medication", "hair_loss_location", "hair_loss_additional_sxx",
    ])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a brief clinical SOAP note for a sexual health visit "
            "(erectile dysfunction / premature ejaculation). "
            "Address the patient's reported effectiveness and any diagnoses.\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Format: S/O/A/P. Keep each section to 1-2 sentences."
        )},
    ]


def _prompt_performance_anxiety(v: Dict[str, str]) -> List[dict]:
    ctx = _format_vars_context(v, [
        "pa_med", "pa_situations", "pa_symptoms", "bp", "pulse", "medication",
    ])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a brief clinical SOAP note for a performance anxiety visit.\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Format: S/O/A/P. Keep each section to 1-2 sentences."
        )},
    ]


def _prompt_birth_control(v: Dict[str, str]) -> List[dict]:
    ctx = _format_vars_context(v, [
        "bc_med", "bc_lmp", "bp", "bc_side_effects", "medication", "pmh",
    ])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a brief clinical SOAP note for a birth control / contraception visit.\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Format: S/O/A/P. Keep each section to 1-2 sentences."
        )},
    ]


def _prompt_photoaging(v: Dict[str, str]) -> List[dict]:
    ctx = _format_vars_context(v, [
        "photoaging_med", "photoaging_goals", "photoaging_retinoid", "medication",
    ])
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a brief clinical SOAP note for a photoaging / anti-aging skincare visit.\n\n"
            f"Clinical Context:\n{ctx}\n\n"
            "Format: S/O/A/P. Keep each section to 1-2 sentences."
        )},
    ]


def _prompt_generic(v: Dict[str, str]) -> List[dict]:
    # Build a summary of all non-empty vars
    lines = []
    for k, val in sorted(v.items()):
        if val and val.strip() and val.strip() != "—":
            lines.append(f"  {k}: {val}")
    context_block = "\n".join(lines) if lines else "  (no data available)"

    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": (
            "Write a brief clinical SOAP note based on the following data. "
            "Use only the information provided.\n\n"
            f"Available Data:\n{context_block}\n\n"
            "Format: S/O/A/P. Keep concise."
        )},
    ]


def _prompt_custom(v: Dict[str, str]) -> List[dict]:
    """Placeholder — the window supplies a fully custom user prompt."""
    return [
        {"role": "system", "content": _SYSTEM_BASE},
        {"role": "user", "content": "(custom prompt will replace this)"},
    ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PROMPT_BUILDERS = {
    "T Deficiency - Initial": _prompt_t_deficiency_initial,
    "T Deficiency - Follow-up": _prompt_t_deficiency_followup,
    "T Deficiency - SOAP": _prompt_t_deficiency_soap,
    "Hair Loss": _prompt_hair_loss,
    "Sexual Health": _prompt_sexual_health,
    "Performance Anxiety": _prompt_performance_anxiety,
    "Birth Control": _prompt_birth_control,
    "Photoaging": _prompt_photoaging,
    "Custom": _prompt_custom,
}

PROMPT_TYPES: List[str] = list(_PROMPT_BUILDERS.keys())
