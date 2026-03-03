"""
Grab Points Module
Provides selector information for EMR elements across different visit types.
"""

# Placeholder for grab points configuration
# This can be expanded with actual selectors as needed
GRAB_POINTS = {
    'sexual_health': {
        'medication': {
            'selenium': {
                'css': '[data-testid="medication-title"]',
            },
            'playwright': {
                'css': '[data-testid="medication-title"]',
            },
        },
        'medication_detail': {
            'selenium': {
                'css': '[data-testid="medication-text"]',
            },
            'playwright': {
                'css': '[data-testid="medication-text"]',
            },
        },
        'treatment_plan': {
            'selenium': {
                'css': '[data-testid="proposedTreatmentPlan"]',
            },
            'playwright': {
                'css': '[data-testid="proposedTreatmentPlan"]',
            },
        },
        'current_dose': {
            'selenium': {
                'css': '[data-testid="treatmentPlan"]',
            },
            'playwright': {
                'css': '[data-testid="treatmentPlan"]',
            },
        },
    },
    'birth_control': {
        'medication': {
            'selenium': {
                'css': '[data-testid="medication-title"]',
                'css_list': [
                    '[data-testid="medication-title"]',
                    '[data-testid="treatment-plan"]',
                    '[class*="medication"]',
                ],
            },
            'playwright': {
                'css': '[data-testid="medication-title"]',
            },
        },
        'lmp': {
            'selenium': {
                'css_list': [
                    '[data-testid="lmp"]',
                    '[data-testid="last-menstrual-period"]',
                ],
            },
        },
        'systolic_bp': {
            'selenium': {
                'css_list': [
                    '[data-testid="systolic-bp"]',
                    '[data-testid="systolic"]',
                ],
            },
        },
        'diastolic_bp': {
            'selenium': {
                'css_list': [
                    '[data-testid="diastolic-bp"]',
                    '[data-testid="diastolic"]',
                ],
            },
        },
    },
    'emr': {
        'get_next_task': {
            'selenium': {
                'css': '[data-testid="getNextTaskButton"]',
                'xpath': '//*[@data-testid="getNextTaskButton"]',
            },
            'playwright': {
                'css': '[data-testid="getNextTaskButton"]',
                'xpath': '//*[@data-testid="getNextTaskButton"]',
            },
        },
    },
}


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
        element_config = GRAB_POINTS.get(group, {}).get(key, {})
        engine_config = element_config.get(engine, {})
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
        element_config = GRAB_POINTS.get(group, {}).get(key, {})
        engine_config = element_config.get(engine, {})
        selector_list = engine_config.get(selector_type, [])
        
        # If selector_type is 'css_list' but not found, try getting a single 'css' selector
        if not selector_list and selector_type == 'css_list':
            css_selector = engine_config.get('css')
            if css_selector:
                return [css_selector]
        
        return selector_list if isinstance(selector_list, list) else []
    except (KeyError, AttributeError):
        return []
