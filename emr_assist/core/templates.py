"""Template loading and formatting utilities."""

from __future__ import annotations

import os
from typing import Dict


def load_templates_from_file(base_dir: str | None = None) -> Dict[str, str]:
    """Load templates from ``templates.txt`` next to *base_dir* (or project root)."""
    if base_dir is None:
        # Go up two levels: core/ -> emr_assist/ -> project root
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    templates_path = os.path.join(base_dir, 'templates.txt')

    templates: Dict[str, str] = {}
    try:
        with open(templates_path, 'r', encoding='utf-8') as f:
            content = f.read()

        current_template = None
        current_content: list[str] = []

        for line in content.split('\n'):
            stripped_line = line.strip()
            if stripped_line.startswith('[') and stripped_line.endswith(']'):
                if current_template and current_content:
                    template_text = '\n'.join(current_content).rstrip()
                    if template_text:
                        templates[current_template] = template_text
                current_template = stripped_line[1:-1]
                current_content = []
            elif current_template is not None:
                current_content.append(line.rstrip())

        if current_template and current_content:
            template_text = '\n'.join(current_content).rstrip()
            if template_text:
                templates[current_template] = template_text

    except FileNotFoundError:
        print(f"Templates file not found: {templates_path}")
        templates = _fallback_templates()
    except Exception as e:
        print(f"Error loading templates: {e}")
        templates = {"Error": "Could not load templates"}

    return templates


def _fallback_templates() -> Dict[str, str]:
    return {
        "Insert Labs": "Labs:\n{lab_values_formatted}",
        "Lab Message": (
            "I have reviewed your intake and the labs which are now complete. "
            "There is total testosterone of {total_testosterone} ng/dL and "
            "free testosterone of {free_testosterone} ng/dL. The other labs done "
            "for safety, to rule out possible concerning causes of deficiency, "
            "are unremarkable"
        ),
        "Testosterone Follow-up Labs": (
            "Hi, my name is Matthew Tomcik, MD, a board-certified family physician "
            "licensed in your state.\n\n"
            "I have reviewed your responses here, and the labs which are now complete.\n\n"
            "The total testosterone is now in the normal range at {total_testosterone} ng/dL "
            "([up/down] from [last value total testosterone] in [Month, year]), "
            "while the estradiol, hematocrit, and PSA are [normal/elevated] at "
            "{estradiol} pg/mL, {hematocrit} %, and {psa} ng/mL. "
            "Please list each as \"lab name is [normal/abnormal] at [value]\" for "
            "any additional tests reviewed.\n\n"
            "I see you've reported [improvement/no change/worsening] in symptoms and "
            "[blank/no] side effects [including side effects from the questionnaire], "
            "we'd recommend that you [continue the treatment as-is/increase the dose "
            "to ***/decrease to ***] at this time."
        ),
        "Testosterone Deficiency Follow-up": (
            "S: Reports he is {response} treatment with {side_effects}\n"
            "O:\n"
            "Total testosterone: {total_testosterone} ng/dL\n"
            "PSA: {psa} ng/mL\n"
            "estradiol: {estradiol} pg/mL\n"
            "Hematocrit: {hematocrit} %\n"
            "A: {diagnoses}\n"
            "P: Proceed with {medication}\n"
            "Prescription written, follow-up per routine."
        ),
    }
