"""Playwright-based Selenium-compatible adapters.

Provides ``PlaywrightElementAdapter``, ``PlaywrightDriverAdapter``,
``WebDriverWait``, and ``_ExpectedConditions`` (aliased as ``EC``) so that
existing code written against Selenium's API works transparently over
Playwright's CDP connection.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from playwright.sync_api import Page, Frame, ElementHandle  # type: ignore
except Exception:
    Page = Frame = ElementHandle = None  # type: ignore[assignment,misc]

from .exceptions import (
    By,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)


# ---------------------------------------------------------------------------
# Expected-condition helpers (mirrors ``selenium.webdriver.support.expected_conditions``)
# ---------------------------------------------------------------------------

class _ExpectedConditions:
    @staticmethod
    def presence_of_element_located(locator: Tuple[str, str]) -> Callable[["PlaywrightDriverAdapter"], "PlaywrightElementAdapter"]:
        def _predicate(driver: "PlaywrightDriverAdapter"):
            elements = driver.find_elements(*locator)
            if not elements:
                raise NoSuchElementException(f"Element not found for locator {locator}")
            return elements[0]
        return _predicate

    @staticmethod
    def element_to_be_clickable(locator: Tuple[str, str]) -> Callable[["PlaywrightDriverAdapter"], "PlaywrightElementAdapter"]:
        def _predicate(driver: "PlaywrightDriverAdapter"):
            element = _ExpectedConditions.presence_of_element_located(locator)(driver)
            if not element.is_displayed():
                raise TimeoutException("Element present but not visible")
            return element
        return _predicate


EC = _ExpectedConditions()


# ---------------------------------------------------------------------------
# WebDriverWait
# ---------------------------------------------------------------------------

class WebDriverWait:
    def __init__(self, driver: "PlaywrightDriverAdapter", timeout: float, poll_frequency: float = 0.2):
        self.driver = driver
        self.timeout = timeout
        self.poll_frequency = poll_frequency

    def until(self, condition: Callable[["PlaywrightDriverAdapter"], Any]):
        end_time = time.time() + self.timeout
        last_error: Optional[Exception] = None
        while True:
            try:
                value = condition(self.driver)
                if value:
                    return value
            except Exception as exc:
                last_error = exc
            if time.time() > end_time:
                raise TimeoutException(str(last_error) if last_error else "Timeout waiting for condition")
            time.sleep(self.poll_frequency)


# ---------------------------------------------------------------------------
# PlaywrightElementAdapter
# ---------------------------------------------------------------------------

class PlaywrightElementAdapter:
    def __init__(self, driver: "PlaywrightDriverAdapter", handle: "ElementHandle"):
        self._driver = driver
        self._handle = handle

    def get_attribute(self, name: str) -> Optional[str]:
        try:
            return self._handle.get_attribute(name)
        except Exception as exc:
            raise StaleElementReferenceException(str(exc))

    @property
    def text(self) -> str:
        try:
            return (self._handle.inner_text() or "").strip()
        except Exception:
            try:
                return (self._handle.text_content() or "").strip()
            except Exception as exc:
                raise StaleElementReferenceException(str(exc))

    def is_displayed(self) -> bool:
        try:
            return self._handle.is_visible()
        except Exception:
            return False

    def find_element(self, by: str, value: str) -> "PlaywrightElementAdapter":
        elements = self.find_elements(by, value)
        if not elements:
            raise NoSuchElementException(f"Element not found: {by}={value}")
        return elements[0]

    def find_elements(self, by: str, value: str) -> List["PlaywrightElementAdapter"]:
        selector = self._driver._translate_selector(by, value)
        try:
            handles = self._handle.query_selector_all(selector)
        except Exception as exc:
            raise WebDriverException(str(exc))
        return [PlaywrightElementAdapter(self._driver, h) for h in handles]

    def click(self) -> None:
        try:
            self._handle.click()
        except Exception as exc:
            raise WebDriverException(str(exc))

    def scroll_into_view(self) -> None:
        try:
            self._handle.scroll_into_view_if_needed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PlaywrightDriverAdapter
# ---------------------------------------------------------------------------

class PlaywrightDriverAdapter:
    def __init__(self, browser, page: "Page"):
        self._browser = browser
        self.page = page
        self._current_frame: "Frame" = page.main_frame
        self._frame_stack: List["Frame"] = []
        self._page_handle_map: Dict[int, str] = {}
        self._handle_page_map: Dict[str, "Page"] = {}
        self._cdp_sessions: Dict[str, Any] = {}
        self._refresh_window_handles()

    # --- Selenium-compatible properties ---
    @property
    def current_url(self) -> str:
        return self.page.url

    @property
    def window_handles(self) -> List[str]:
        self._refresh_window_handles()
        return list(self._handle_page_map.keys())

    # --- Frame/Window switching ---
    class _SwitchController:
        def __init__(self, driver: "PlaywrightDriverAdapter"):
            self._driver = driver

        def default_content(self) -> None:
            self._driver._frame_stack.clear()
            self._driver._current_frame = self._driver.page.main_frame

        def parent_frame(self) -> None:
            if self._driver._frame_stack:
                self._driver._current_frame = self._driver._frame_stack.pop()
            else:
                self.default_content()

        def frame(self, frame_reference: Any) -> None:
            handle = None
            if isinstance(frame_reference, PlaywrightElementAdapter):
                handle = frame_reference._handle
            elif ElementHandle is not None and isinstance(frame_reference, ElementHandle):
                handle = frame_reference
            if handle is None:
                raise WebDriverException("Unsupported frame reference")
            frame = handle.content_frame()
            if frame is None:
                raise NoSuchElementException("Frame has no content")
            self._driver._frame_stack.append(self._driver._current_frame)
            self._driver._current_frame = frame

        def window(self, handle: str) -> None:
            self._driver._set_active_window(handle)

    @property
    def switch_to(self) -> "PlaywrightDriverAdapter._SwitchController":
        return PlaywrightDriverAdapter._SwitchController(self)

    # --- Core element lookups ---
    def _translate_selector(self, by: str, value: str) -> str:
        if by == By.CSS_SELECTOR:
            return value
        if by == By.XPATH:
            return f"xpath={value}"
        if by == By.TAG_NAME:
            return value
        raise WebDriverException(f"Unsupported selector type: {by}")

    def find_element(self, by: str, value: str) -> PlaywrightElementAdapter:
        elements = self.find_elements(by, value)
        if not elements:
            raise NoSuchElementException(f"Element not found: {by}={value}")
        return elements[0]

    def find_elements(self, by: str, value: str) -> List[PlaywrightElementAdapter]:
        selector = self._translate_selector(by, value)
        try:
            handles = self._current_frame.query_selector_all(selector)
        except Exception as exc:
            raise WebDriverException(str(exc))
        return [PlaywrightElementAdapter(self, h) for h in handles]

    def execute_script(self, script: str, *args):
        arg_names = [f"arg{i}" for i in range(len(args))]
        converted_args = [self._extract_handle(arg) for arg in args]
        body = re.sub(r"arguments\[(\d+)\]", lambda m: f"arg{m.group(1)}", script)
        if not body.strip().startswith("return") and "return" not in body:
            body = body.rstrip("; ") + ";"
        expression = f"({', '.join(arg_names)}) => {{ {body} }}"
        try:
            return self._current_frame.evaluate(expression, *converted_args)
        except Exception as exc:
            raise WebDriverException(str(exc))

    def execute_cdp_cmd(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        session = self._get_cdp_session(self.page)
        return session.send(method, params or {})

    # --- Helpers ---
    def _extract_handle(self, arg: Any) -> Any:
        if isinstance(arg, PlaywrightElementAdapter):
            return arg._handle
        return arg

    def _refresh_window_handles(self) -> None:
        pages = []
        for ctx in self._browser.contexts:
            pages.extend(ctx.pages)
        for pg in pages:
            pid = id(pg)
            if pid not in self._page_handle_map:
                handle = f"page-{pid}"
                self._page_handle_map[pid] = handle
                self._handle_page_map[handle] = pg
            else:
                handle = self._page_handle_map[pid]
                self._handle_page_map[handle] = pg
        active_handles = {self._page_handle_map[id(pg)] for pg in pages if id(pg) in self._page_handle_map}
        for handle in list(self._handle_page_map.keys()):
            if handle not in active_handles:
                self._handle_page_map.pop(handle, None)

    def _set_active_window(self, handle: str) -> None:
        self._refresh_window_handles()
        page = self._handle_page_map.get(handle)
        if not page:
            raise NoSuchElementException(f"Unknown window handle {handle}")
        self.page = page
        try:
            page.bring_to_front()
        except Exception:
            pass
        self._current_frame = page.main_frame
        self._frame_stack.clear()

    def _get_cdp_session(self, page: "Page"):
        key = f"session-{id(page)}"
        session = self._cdp_sessions.get(key)
        if session is None:
            session = page.context.new_cdp_session(page)
            self._cdp_sessions[key] = session
        return session
