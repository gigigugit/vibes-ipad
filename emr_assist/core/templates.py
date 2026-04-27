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
    return {}
