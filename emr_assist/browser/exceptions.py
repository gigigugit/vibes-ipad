"""Exception classes and selector helpers for browser automation.

These mirror Selenium-style exceptions so the rest of the codebase can use
familiar names without depending on Selenium itself.
"""

from __future__ import annotations


# Playwright support for EMR interaction via Chrome DevTools Protocol
try:
    from playwright.sync_api import (  # type: ignore
        TimeoutError as _PWTimeoutError,
    )
except Exception:
    _PWTimeoutError = None  # type: ignore[assignment,misc]


class PlaywrightTimeoutError(Exception):
    """Fallback TimeoutError when Playwright is unavailable."""
    pass


class TimeoutException(Exception):
    pass


class NoSuchElementException(Exception):
    pass


class WebDriverException(Exception):
    pass


class StaleElementReferenceException(Exception):
    pass


class By:
    CSS_SELECTOR = "css"
    XPATH = "xpath"
    TAG_NAME = "tag"
