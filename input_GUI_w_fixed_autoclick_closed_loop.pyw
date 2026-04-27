# ================================================================
# EMR Assist (wxPython + Playwright)
# ------------------------------------------------
# Browser Configuration:
# - Works with Chrome or Thorium in debug mode
# - For Chrome: Start with --remote-debugging-port=9222
# - For Thorium: Start with --remote-debugging-port=9222 --user-data-dir="C:\chrome_debug"
# - Set CDP_DEBUG_PORT in configuration section to match your port (default: 9222)
# ------------------------------------------------
# Purpose
# - A Windows desktop helper to speed up Hims/Hers EMR workflows.
# - Provides a small always-on-top GUI with tabs for:
#   - Labs/TDCS/ED tools and template insertion
#   - Auto-clicker for "Get next task" with robust browser-based clicking,
#     URL change detection, and popup/beep notifications
#   - Performance Anxiety (PA) workflow: Playwright-powered data grab
#     (medication, blood pressure, pulse, situational fears, somatic symptoms)
#     and one-click insertion of initial/follow-up notes.

from __future__ import annotations

import wx
import wx.lib.scrolledpanel as scrolled
import pyperclip
import pyautogui
import pygetwindow as gw
import time
import threading
import queue
from functools import partial
import re
import os
from typing import Dict, Any, Optional, List, Tuple, Callable
import json
import requests
import winsound
from urllib.parse import urlsplit, urlunsplit
import keyboard
try:
    import mouse as mouse_lib
    print("[IMPORT] mouse library imported successfully")
except ImportError as e:
    print(f"[IMPORT] Failed to import mouse library: {e}")
    mouse_lib = None
import ctypes
from datetime import datetime
import importlib
from grab_points import (
    GRAB_POINTS,
    get_preferred_method,
    get_selector,
    get_selector_list,
)

# Playwright support for EMR interaction via Chrome DevTools Protocol
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Frame, ElementHandle  # type: ignore
except Exception:
    sync_playwright = None


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
            except Exception as exc:  # noqa: PERF203 - intentional broad catch to mirror Selenium behaviour
                last_error = exc
            if time.time() > end_time:
                raise TimeoutException(str(last_error) if last_error else "Timeout waiting for condition")
            time.sleep(self.poll_frequency)


class PlaywrightElementAdapter:
    def __init__(self, driver: "PlaywrightDriverAdapter", handle: ElementHandle):
        self._driver = driver
        self._handle = handle

    def get_attribute(self, name: str) -> Optional[str]:
        try:
            return self._handle.get_attribute(name)
        except Exception as exc:  # noqa: PERF203
            raise StaleElementReferenceException(str(exc))

    @property
    def text(self) -> str:
        try:
            return (self._handle.inner_text() or "").strip()
        except Exception:
            try:
                return (self._handle.text_content() or "").strip()
            except Exception as exc:  # noqa: PERF203
                raise StaleElementReferenceException(str(exc))

    def is_displayed(self) -> bool:
        try:
            return self._handle.is_visible()
        except Exception:  # noqa: PERF203
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
        except Exception as exc:  # noqa: PERF203
            raise WebDriverException(str(exc))
        return [PlaywrightElementAdapter(self._driver, h) for h in handles]

    def click(self) -> None:
        try:
            self._handle.click()
        except Exception as exc:  # noqa: PERF203
            raise WebDriverException(str(exc))

    def scroll_into_view(self) -> None:
        try:
            self._handle.scroll_into_view_if_needed()
        except Exception:  # noqa: PERF203
            pass


class PlaywrightDriverAdapter:
    def __init__(self, browser, page: Page):
        self._browser = browser
        self.page = page
        self._current_frame: Frame = page.main_frame
        self._frame_stack: List[Frame] = []
        self._page_handle_map: Dict[int, str] = {}
        self._handle_page_map: Dict[str, Page] = {}
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
            elif isinstance(frame_reference, ElementHandle):
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
    def switch_to(self) -> "PlaywrightDriverAdapter._SwitchController":  # type: ignore[override]
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
        except Exception as exc:  # noqa: PERF203
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
        except Exception as exc:  # noqa: PERF203
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
        # Remove closed pages
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

    def _get_cdp_session(self, page: Page):
        key = f"session-{id(page)}"
        session = self._cdp_sessions.get(key)
        if session is None:
            session = page.context.new_cdp_session(page)
            self._cdp_sessions[key] = session
        return session

# --- Performance/debug controls ---
PERF_DEBUG = False
FAST_MODE_BP = True
# Global hotkey toggles
ENABLE_QUICK_NEXT_TASK_HOTKEY = False
QUICK_NEXT_TASK_HOTKEY = "ctrl+alt+enter"

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


def dprint(*args, **kwargs):
    if PERF_DEBUG:
        try:
            print(*args, **kwargs)
        except Exception:
            pass

try:
    geopy_geocoders = importlib.import_module("geopy.geocoders")
    geopy_distance = importlib.import_module("geopy.distance")
    geopy_exc = importlib.import_module("geopy.exc")

    Nominatim = geopy_geocoders.Nominatim
    geodesic = geopy_distance.geodesic
    GeocoderTimedOut = getattr(geopy_exc, "GeocoderTimedOut", Exception)
    GeocoderServiceError = getattr(geopy_exc, "GeocoderServiceError", Exception)
    GEOCODER_ERRORS = (GeocoderTimedOut, GeocoderServiceError)
except ImportError:
    Nominatim = None
    geodesic = None
    GEOCODER_ERRORS = (Exception,)

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
    ("testosterone", "Testosterone"),
    ("t deficiency", "T Deficiency"),
    ("testosterone deficiency", "T Deficiency"),
    ("low t", "Testosterone"),
    ("hypogonadism", "Testosterone"),
    ("androgen deficiency", "T Deficiency"),
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
    ("testosterone deficiency", "T Deficiency"),
    ("t deficiency", "T Deficiency"),
    ("td/ed", "T Deficiency"),
    ("td/ed labs", "T Deficiency"),
    ("td labs", "T Deficiency"),
    ("low t", "T Deficiency"),
    ("testosterone", "T Deficiency"),
    ("hair loss", "Hair Loss"),
    ("photoaging", "Photoaging"),
    ("birth control", "Birth Control"),
    ("contraception", "Birth Control"),
]

CDP_TITLE_SELECTORS_JSON = json.dumps(CDP_TITLE_SELECTORS)
CDP_TITLE_XPATHS_JSON = json.dumps(CDP_TITLE_XPATHS)
CDP_VISIT_KEYWORDS_JSON = json.dumps([kw for kw, _ in VISIT_TYPE_FALLBACK_KEYWORDS])
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
CDP_HEADER_EXPRESSION = _CDP_HEADER_EXPR_TEMPLATE.replace("__SELS__", CDP_TITLE_SELECTORS_JSON).replace(
    "__XPATHS__", CDP_TITLE_XPATHS_JSON
).replace("__KEYWORDS__", CDP_VISIT_KEYWORDS_JSON)

VISIT_TAB_INDICES = {
    "T Deficiency": 0,
    "Hair Loss": 1,
    "Photoaging": 2,
    "Sexual Health": 3,
    "Performance Anxiety": 5,
    "Birth Control": 6,
}

AUTO_CLICKER_TAB_INDEX = 4

BIRTH_CONTROL_PMH_OPTIONS = [
    ("htn", "HTN (hypertension)"),
    ("hld", "HLD (hyperlipidemia)"),
    ("seizure", "Seizure disorder"),
    ("PCOS", "PCOS"),
    ("DM", "Diabetes (DM)"),
    ("endometriosis", "Endometriosis"),
]

BIRTH_CONTROL_PMH_KEYWORDS = {
    "htn": ["htn", "hypertension", "high blood pressure"],
    "hld": ["hld", "hyperlipidemia", "high cholesterol"],
    "seizure": ["seizure", "epilepsy", "seizure disorder"],
    "PCOS": ["pcos", "polycystic ovary", "polycystic ovarian"],
    "DM": ["dm", "diabetes", "type 1", "type 2", "diabetic"],
    "endometriosis": ["endometriosis"],
}

AUTO_CLICKER_DEBUG = False

CDP_HEADER_SELECTORS_JSON = json.dumps(CDP_HEADER_SELECTORS)
CDP_TITLE_XPATHS_JSON = json.dumps(CDP_TITLE_XPATHS)
CDP_TITLE_SELECTORS_JSON = json.dumps(CDP_TITLE_SELECTORS)
CDP_VISIT_FALLBACK_KEYWORDS_JSON = json.dumps([kw for kw, _ in VISIT_TYPE_FALLBACK_KEYWORDS])

_CDP_VISIT_HEADER_SCRIPT = (
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
    .replace("__SELS__", CDP_TITLE_SELECTORS_JSON)
    .replace("__XPATHS__", CDP_TITLE_XPATHS_JSON)
    .replace("__KEYWORDS__", CDP_VISIT_FALLBACK_KEYWORDS_JSON)
)

INTAKE_FORM_DATE_SELECTOR = "div[data-testid='intake-form'] time, div[data-testid='IntakeForm'] time, time[datetime][dir]"

GEOCODER_USER_AGENT = "emr_assist_location_lookup"
CHIPPEWA_FALLS_QUERIES = ("Chippewa Falls, WI", "Chippewa Falls, Wisconsin")
_CHIPPEWA_FALLS_CACHE: Optional[Tuple[float, float]] = None


def geocode_address(geolocator, address: Optional[str], retries: int = 2, delay: float = 0.5) -> Optional[Tuple[float, float]]:
    if not geolocator or not address:
        return None

    cleaned = " ".join(address.split())
    for attempt in range(retries):
        try:
            location = geolocator.geocode(cleaned)
            if location:
                return (location.latitude, location.longitude)
        except GEOCODER_ERRORS:
            pass
        except Exception:
            return None

        if attempt < retries - 1:
            time.sleep(delay)

    return None


def get_chippewa_falls_coords(geolocator) -> Optional[Tuple[float, float]]:
    global _CHIPPEWA_FALLS_CACHE
    if _CHIPPEWA_FALLS_CACHE:
        return _CHIPPEWA_FALLS_CACHE
    if not geolocator:
        return None
    for query in CHIPPEWA_FALLS_QUERIES:
        coords = geocode_address(geolocator, query)
        if coords:
            _CHIPPEWA_FALLS_CACHE = coords
            return coords
    return None

# Optimize pyautogui for faster but reliable typing
pyautogui.PAUSE = 0.01  # Very small pause between pyautogui calls for reliability
pyautogui.FAILSAFE = True  # Keep failsafe enabled

# --- CONFIGURATION SECTION ---

# Browser debugging configuration
# Set to 9222 for Chrome or Thorium depending on which browser you're using
CDP_DEBUG_PORT = 9222  # Change to match your --remote-debugging-port value

# Prefer Playwright for grabbing medication in Sexual Health tab
USE_PLAYWRIGHT_FOR_MED = True
# Prefer Playwright for Sexual Health (effectiveness/BP/notes) when available
# Default to False for speed; PW will be used as a fallback when enabled
USE_PLAYWRIGHT_FOR_SH = False
# Prefer fetching page text via CDP (Playwright) instead of Selenium for speed and to avoid focus/clipboard
USE_CDP_FOR_TEXT = True
# Optional clipboard scraping mode for Sexual Health (select-all + copy and parse)
USE_CLIPBOARD_FOR_TEXT = True
# Global grab-method toggle for T Deficiency, Hair Loss, and Photoaging tabs
# True = use CDP/Playwright (fast, no clipboard needed), False = use clipboard (Ctrl+A/Ctrl+C)
USE_CDP_FOR_GRAB = False
# Start in overlay mode when a CDP browser session is available.
AUTO_START_JS_OVERLAY = True
# Delay (ms) before running an auto grab so the EMR has time to focus after a tab switch
AUTO_GRAB_DELAY_MS = 900

# Width to leave visible when the GUI is hidden off-screen
GUI_HIDDEN_VISIBLE_WIDTH = 65

# Lab configuration - matching the 9 labs from simple_lab_grabber
LABS_CONFIG = {
    "Total PSA": {
        "unit": "ng/mL", 
        "normal_range": "0-4", 
        "patterns": ["Total PSA", "PSA Total", "PSA", "Prostate Specific Antigen", "PSA, Total"],
        "var": "psa"
    },
    "FSH": {
        "unit": "mIU/mL", 
        "normal_range": "1.5-12.4", 
        "patterns": ["FSH", "Follicle Stimulating Hormone", "Follicle-Stimulating Hormone", "FSH, Serum"],
        "var": "fsh"
    },
    "LH": {
        "unit": "mIU/mL", 
        "normal_range": "1.7-8.6", 
        "patterns": ["LH", "Luteinizing Hormone", "Luteinizing-Hormone", "LH, Serum"],
        "var": "lh"
    },
    "Albumin": {
        "unit": "g/dL", 
        "normal_range": "3.5-5.0", 
        "patterns": ["Albumin", "Albumin, Serum", "Serum Albumin"],
        "var": "albumin"
    },
    "Estradiol": {
        "unit": "pg/mL", 
        "normal_range": "7.6-42.6", 
        "patterns": ["Estradiol", "E2", "Estradiol, Serum", "17-Beta Estradiol"],
        "var": "estradiol"
    },
    "Free Testosterone": {
        "unit": "ng/dL", 
        "normal_range": "9.3-26.5", 
        "patterns": ["Free Testosterone", "Free T", "Testosterone Free", "Testosterone, Free", "Free Testosterone, Serum"],
        "var": "free_testosterone"
    },
    "SHBG": {
        "unit": "nmol/L", 
        "normal_range": "16.5-55.9", 
        "patterns": ["SHBG", "Sex Hormone Binding Globulin", "Sex Hormone-Binding Globulin", "SHBG, Serum"],
        "var": "shbg"
    },
    "Total Testosterone": {
        "unit": "ng/dL", 
        "normal_range": "264-916", 
        "patterns": ["Total Testosterone", "Total T", "Testosterone Total", "Testosterone", "Testosterone, Total", "Testosterone, Serum"],
        "var": "total_testosterone"
    },
    "Hematocrit": {
        "unit": "%", 
        "normal_range": "37.5-51.0", 
        "patterns": ["Hematocrit", "HCT", "Hct", "Hematocrit %"],
        "var": "hematocrit"
    },
}

def extract_td_medication_from_text(text: str) -> str:
    """Extract testosterone deficiency medication from grabbed text.
    
    Prioritizes lines that contain both medication name AND dosage/directions.
    Example target: "Enclomiphene / Tadalafil, 12.5mg / 8.5mg (daily)"
    """
    if not text:
        return ""
    
    lines = text.split('\n')
    
    # Pattern to match TD-related medications
    td_med_pattern = re.compile(
        r'(enclomiphene|clomiphene|tadalafil|testosterone|androgen|TRT)',
        re.IGNORECASE
    )
    
    # Pattern to match dosage (e.g., 12.5mg, 25 mg)
    dose_pattern = re.compile(r'\d+\.?\d*\s*mg', re.IGNORECASE)
    
    # Pattern to match frequency/directions
    frequency_pattern = re.compile(
        r'\(?\s*(daily|every other day|twice daily|weekly|as needed|EOD|QD|BID|once daily|per day)\s*\)?',
        re.IGNORECASE
    )
    
    # Lines to skip (headers, warnings, etc.)
    skip_patterns = re.compile(
        r'^(treatment plan|medication interaction|patient preference|patient is already|treatment$|medication$)',
        re.IGNORECASE
    )
    
    # Priority 1: Find lines with medication name + dosage + frequency (best match)
    best_match = ""
    for ln in lines:
        ln_stripped = ln.strip()
        if not ln_stripped or skip_patterns.match(ln_stripped):
            continue
        
        has_med = td_med_pattern.search(ln_stripped)
        has_dose = dose_pattern.search(ln_stripped)
        has_freq = frequency_pattern.search(ln_stripped)
        
        if has_med and has_dose and has_freq:
            # This is ideal - has medication, dose, AND frequency
            best_match = ln_stripped
            break
        elif has_med and has_dose and not best_match:
            # Good match - has medication and dose
            best_match = ln_stripped
    
    # Priority 2: If no match with dose, fall back to any medication line
    if not best_match:
        for ln in lines:
            ln_stripped = ln.strip()
            if not ln_stripped or skip_patterns.match(ln_stripped):
                continue
            if td_med_pattern.search(ln_stripped):
                # Skip very short entries that are likely just category names
                if len(ln_stripped) > 15:
                    best_match = ln_stripped
                    break
    
    # Clean up the medication string
    if best_match:
        # Remove leading bullet points, asterisks, etc.
        best_match = re.sub(r'^[\-\*\•\>]+\s*', '', best_match).strip()
    
    return best_match


def _update_td_med_text(value: str) -> None:
    """Update the TD medication text control with the given value."""
    target = globals().get("frame")
    if not target or not hasattr(target, "td_med_text"):
        return
    target.td_med_text.SetValue(value)

# Performance Anxiety known options (used for text-based fallback parsing)
PA_SITUATION_OPTIONS = [
    # Professional/academic
    "Public speaking",
    "Presentations",
    "Performances",
    "Auditions",
    "Job interviews",
    "Work meetings",
    "Exams",
    "Tests",
    "Class presentations",
    # Social/relationship
    "Dating",
    "Networking",
    "Meeting new people",
    "Social gatherings",
    # Specific performance contexts
    "Sexual performance",
    "Athletic competitions",
    # Travel/other
    "Driving",
    "Flying",
]

PA_SYMPTOM_OPTIONS = [
    # Cardiopulmonary
    "Racing pulse",
    "Heart palpitations",
    "Fast breathing",
    "Shortness of breath",
    # Neuromuscular
    "Trembling",
    "Shaking",
    # Autonomic
    "Sweating",
    "Cold and clammy hands",
    # GI/oral
    "Stomach upset",
    "Nausea",
    "Butterflies",
    "Dry mouth",
    # ENT/vision/vestibular
    "Throat tightness",
    "Blurred vision",
    "Dizziness",
    "Lightheadedness",
    # Chest/temperature sensations
    "Chest tightness",
    "Flushed",
    "Hot flashes",
    "Chills",
    # Common extended phrases seen in EMR
    "Racing pulse you can hear in my ears",
    "Hands, knees, lips, or my voice might tremble",
    "An uneasy feeling in the stomach",
]

# Keywords to detect symptom lines verbatim near the question anchor (captures extended phrases)
PA_SYMPTOM_KEYWORDS = [
    # Cardiopulmonary
    'breath', 'breathing', 'pulse', 'palpit', 'heart rate',
    # Neuromuscular
    'trembl', 'shake',
    # Autonomic
    'sweat', 'clammy', 'cold and clammy',
    # GI/oral
    'stomach', 'nausea', 'butterflies', 'dry mouth',
    # ENT/vision/vestibular
    'throat', 'vision', 'dizz', 'lighthead',
    # Chest/temperature sensations
    'chest', 'flushed', 'hot', 'chills',
]

TEMPLATE_BUTTONS = [
    {"label": "Insert Labs", "dynamic_labs": True},
    {"label": "Rx Note", "rx_note": True},
    {"label": "Referral note", "referral_note": True},
    {"label": "Lab Message", "lab_message": True},
    {"label": "T Follow-up Labs", "template_name": "Testosterone Follow-up Labs"},
    {"label": "TD Follow-up Note", "template_name": "Testosterone Deficiency Follow-up"},
    {"label": "Clinical Matrix", "show_matrix": True},
    {"label": "Clear All", "clear_all": True},
]

# --- END CONFIGURATION SECTION ---

def extract_lab_value_simple(label_config: Dict[str, Any], text: str) -> Dict[str, str]:
    """
    Simplified extraction for EMR format
    Looking for pattern: Lab Name -> Value -> Unit on separate lines
    """
    unit = label_config["unit"]
    patterns = label_config["patterns"]
    normal_range = label_config.get("normal_range", "")
    
    # Split text into lines for easier processing
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    best_comparator_match: Optional[Dict[str, Any]] = None

    for pattern in patterns:
        # Find the lab name line
        for i, line in enumerate(lines):
            if pattern.lower() in line.lower():
                # Look ahead for the value in the next few lines
                for j in range(i+1, min(i+5, len(lines))):
                    next_line = lines[j]
                    
                    # Check if this line contains a number that could be our value
                    # Handle comparison operators like "< 25"
                    value_match = re.search(r'([<>≤≥]?\s*\d+\.?\d*)', next_line)
                    if value_match:
                        potential_value = value_match.group(1).strip()
                        
                        # Check if the unit appears in the next line or same line
                        unit_found = False
                        for k in range(j, min(j+3, len(lines))):
                            if unit.lower() in lines[k].lower():
                                unit_found = True
                                break
                        
                        if unit_found:
                            # Extract just the numeric part for validation
                            numeric_part = re.search(r'(\d+\.?\d*)', potential_value)
                            if numeric_part:
                                try:
                                    float_val = float(numeric_part.group(1))
                                    if 0.01 <= float_val <= 10000:  # Reasonable range
                                        result = {
                                            "value": f"{potential_value} {unit}",
                                            "raw_value": potential_value,
                                            "unit": unit,
                                            "normal_range": normal_range,
                                            "found": True,
                                            "matched_pattern": f"Line-based: {pattern}"
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
        "matched_pattern": "Not found"
    }

def parse_tdcs_score(text: str) -> int:
    """Parse TDCS score from text by counting 'Add +1 to TDCS score' and 'Add +2 to TDCS score'"""
    score = 0
    
    # Count +1 occurrences
    plus_one_pattern = r'Add \+1 to TDCS score'
    plus_one_matches = len(re.findall(plus_one_pattern, text, re.IGNORECASE))
    score += plus_one_matches * 1
    
    # Count +2 occurrences  
    plus_two_pattern = r'Add \+2 to TDCS score'
    plus_two_matches = len(re.findall(plus_two_pattern, text, re.IGNORECASE))
    score += plus_two_matches * 2
    
    print(f"TDCS parsing: Found {plus_one_matches} '+1' and {plus_two_matches} '+2' = Total score: {score}")
    return score

def parse_tdcsc_score(text: str) -> Optional[int]:
    """Parse TDCS-C score based on symptom change responses."""
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

def parse_ed_status(text: str) -> str:
    """Parse ED status from the erection difficulty question"""
    # Look for the specific question about erection difficulty
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

def _extract_response_after_question(question: str, text: str) -> str:
    """Find the line immediately after a question prompt and return it."""
    try:
        # Normalize newlines and search for the question text, allowing an optional 'Response:' line
        pattern = re.escape(question) + r"\s*\n\s*(?:Response:\s*)?([^\n]+)"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    except Exception as e:
        print(f"Question parse error: {e}")
    return ""

def parse_treatment_satisfaction(text: str) -> str:
    """Parse satisfaction response from the questionnaire block."""
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
    """Parse side effects answer from questionnaire."""
    resp = _extract_response_after_question("Q: Have you experienced any side effects?", text)
    if not resp:
        return "No side effects reported"
    if resp.strip().lower().startswith("no"):
        return "No side effects reported"
    return resp

def detect_td_diagnosis(text: str) -> bool:
    """Detect testosterone deficiency / low T in text; returns True if found."""
    if not text:
        return False
    low = text.lower()
    keywords = (
        "testosterone deficiency",
        "t deficiency",
        "low testosterone",
        "low t",
        "hypogonadism",
        "hypogonadal",
    )
    return any(k in low for k in keywords)

def load_templates_from_file():
    """Load templates from external templates.txt file"""
    templates = {}
    
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    templates_path = os.path.join(script_dir, 'templates.txt')
    
    try:
        with open(templates_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Parse templates - simplified format without [END] markers
        current_template = None
        current_content = []
        
        for line in content.split('\n'):
            stripped_line = line.strip()
            if stripped_line.startswith('[') and stripped_line.endswith(']'):
                # Save previous template if exists
                if current_template and current_content:
                    template_text = '\n'.join(current_content).rstrip()  # Keep formatting but remove trailing spaces
                    if template_text:
                        templates[current_template] = template_text
                
                # Start new template
                current_template = stripped_line[1:-1]  # Remove brackets
                current_content = []
            elif current_template is not None:  # Include empty lines for paragraph breaks
                current_content.append(line.rstrip())  # Keep original line but remove trailing spaces
        
        # Save last template
        if current_template and current_content:
            template_text = '\n'.join(current_content).rstrip()  # Keep formatting but remove trailing spaces
            if template_text:
                templates[current_template] = template_text
    
    except FileNotFoundError:
        print(f"Templates file not found: {templates_path}")
        # Fallback to basic templates
        templates = {
            "Insert Labs": "Labs:\n{lab_values_formatted}",
            "Lab Message": "I have reviewed your intake and the labs which are now complete. There is total testosterone of {total_testosterone} ng/dL and free testosterone of {free_testosterone} ng/dL. The other labs done for safety, to rule out possible concerning causes of deficiency, are unremarkable",
            "Testosterone Follow-up Labs": (
                "Hi, my name is Matthew Tomcik, MD, a board-certified family physician licensed in your state.\n\n"
                "I have reviewed your responses here, and the labs which are now complete.\n\n"
                "The total testosterone is now in the normal range at {total_testosterone} ng/dL ([up/down] from [last value total testosterone] in [Month, year]), "
                "while the estradiol, hematocrit, and PSA are [normal/elevated] at {estradiol} pg/mL, {hematocrit} %, and {psa} ng/mL. "
                "Please list each as \"lab name is [normal/abnormal] at [value]\" for any additional tests reviewed.\n\n"
                "I see you've reported [improvement/no change/worsening] in symptoms and [blank/no] side effects [including side effects from the questionnaire], "
                "we'd recommend that you [continue the treatment as-is/increase the dose to ***/decrease to ***] at this time."
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
    except Exception as e:
        print(f"Error loading templates: {e}")
        templates = {"Error": "Could not load templates"}
    
    return templates


OVERLAY_JS = r"""
(() => {
    const existing = document.getElementById('emr-assist-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'emr-assist-overlay';
    overlay.innerHTML = `
    <style>
        #emr-assist-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 100000;
            font-family: "Segoe UI", Arial, sans-serif;
            color: #e8f3ff;
        }
        #emr-assist-overlay * { box-sizing: border-box; }
        .emr-overlay-shell {
            background: rgba(9, 20, 36, 0.92);
            border-bottom: 1px solid rgba(80, 185, 255, 0.55);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
        }
        .emr-overlay-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            flex-wrap: wrap;
        }
        .emr-overlay-title {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #8fc9ff;
            margin-right: 10px;
        }
        .emr-visit-pill {
            min-width: 160px;
            padding: 5px 10px;
            border-radius: 999px;
            background: rgba(28, 55, 84, 0.95);
            color: #f4fbff;
            font-size: 12px;
            font-weight: 600;
        }
        .emr-btn,
        .emr-select {
            height: 30px;
            border-radius: 6px;
            border: 1px solid rgba(96, 171, 232, 0.35);
            background: rgba(17, 34, 53, 0.96);
            color: #e8f3ff;
            font: inherit;
        }
        .emr-btn {
            padding: 0 12px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 600;
        }
        .emr-btn:hover {
            background: rgba(29, 53, 79, 0.98);
            border-color: rgba(112, 194, 255, 0.7);
        }
        .emr-btn-active {
            background: linear-gradient(180deg, #5dd7ff 0%, #1fb2e4 100%);
            color: #062033;
            border-color: #5dd7ff;
        }
        .emr-select {
            min-width: 220px;
            padding: 0 10px;
        }
        .emr-overlay-body {
            display: grid;
            grid-template-columns: minmax(220px, 1fr) minmax(320px, 2fr);
            gap: 12px;
            padding: 0 12px 12px;
        }
        .emr-panel {
            background: rgba(13, 27, 44, 0.9);
            border: 1px solid rgba(80, 145, 205, 0.24);
            border-radius: 10px;
            padding: 10px;
            min-height: 110px;
        }
        .emr-panel-title {
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #7fb7eb;
            margin-bottom: 8px;
        }
        .emr-template-row {
            display: flex;
            gap: 8px;
            align-items: center;
            margin-bottom: 10px;
            flex-wrap: wrap;
        }
        .emr-status {
            min-height: 18px;
            color: #b9d8f5;
            font-size: 12px;
        }
        .emr-vars {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 8px;
            max-height: 220px;
            overflow: auto;
            padding-right: 2px;
        }
        .emr-var-card {
            padding: 8px;
            border-radius: 8px;
            background: rgba(20, 38, 59, 0.92);
            border: 1px solid rgba(88, 139, 185, 0.22);
        }
        .emr-var-key {
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #8eb8db;
            margin-bottom: 4px;
        }
        .emr-var-value {
            font-size: 12px;
            color: #f5fbff;
            white-space: pre-wrap;
            word-break: break-word;
        }
        .emr-empty {
            color: #89a6c1;
            font-size: 12px;
        }
        #emr-assist-overlay.emr-overlay-minimized .emr-overlay-body { display: none; }
        #emr-assist-overlay.emr-overlay-minimized .emr-overlay-shell { background: rgba(9, 20, 36, 0.78); }
    </style>
    <div class="emr-overlay-shell">
        <div class="emr-overlay-bar">
            <div class="emr-overlay-title">EMR Assist Overlay</div>
            <div class="emr-visit-pill" data-role="visit">Visit: Unknown</div>
            <button class="emr-btn" data-action="grab">Grab</button>
            <button class="emr-btn" data-action="detect">Detect</button>
            <button class="emr-btn" data-action="dashboard-auto">Dashboard</button>
            <button class="emr-btn" data-action="invisit-auto">In-Visit</button>
            <button class="emr-btn" data-action="dark">Dark</button>
            <button class="emr-btn" data-action="minimize">Minimize</button>
            <button class="emr-btn" data-action="python">Python UI</button>
            <button class="emr-btn" data-action="close">Close</button>
        </div>
        <div class="emr-overlay-body">
            <div class="emr-panel">
                <div class="emr-panel-title">Templates</div>
                <div class="emr-template-row">
                    <select class="emr-select" data-role="template-select"></select>
                    <button class="emr-btn" data-action="insert-template">Insert</button>
                </div>
                <div class="emr-status" data-role="status">Overlay ready.</div>
            </div>
            <div class="emr-panel">
                <div class="emr-panel-title">Grabbed Variables</div>
                <div class="emr-vars" data-role="vars"></div>
            </div>
        </div>
    </div>`;

    document.documentElement.appendChild(overlay);
    window.__emrOverlayActive = true;

    const state = { visitType: 'Unknown', templates: [], vars: {}, minimized: false };
    const visitEl = overlay.querySelector('[data-role="visit"]');
    const statusEl = overlay.querySelector('[data-role="status"]');
    const varsEl = overlay.querySelector('[data-role="vars"]');
    const templateSelect = overlay.querySelector('[data-role="template-select"]');
    const dashboardBtn = overlay.querySelector('[data-action="dashboard-auto"]');
    const invisitBtn = overlay.querySelector('[data-action="invisit-auto"]');
    const darkBtn = overlay.querySelector('[data-action="dark"]');

    const callHost = (name, ...args) => {
        const fn = window[name];
        if (typeof fn !== 'function') return Promise.resolve();
        try { return Promise.resolve(fn(...args)); } catch (error) {
            console.error('[EMR OVERLAY] Host call failed:', name, error);
            return Promise.resolve();
        }
    };

    const renderTemplates = () => {
        templateSelect.innerHTML = '';
        const items = Array.isArray(state.templates) ? state.templates : [];
        if (!items.length) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = 'No templates for this visit';
            templateSelect.appendChild(opt);
            templateSelect.disabled = true;
            return;
        }
        templateSelect.disabled = false;
        for (const item of items) {
            const opt = document.createElement('option');
            opt.value = String(item || '');
            opt.textContent = String(item || '');
            templateSelect.appendChild(opt);
        }
    };

    const renderVariables = () => {
        varsEl.innerHTML = '';
        const entries = Object.entries(state.vars || {}).filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== '');
        if (!entries.length) {
            const empty = document.createElement('div');
            empty.className = 'emr-empty';
            empty.textContent = 'No grabbed variables yet.';
            varsEl.appendChild(empty);
            return;
        }
        for (const [key, value] of entries) {
            const card = document.createElement('div');
            card.className = 'emr-var-card';
            const keyEl = document.createElement('div');
            keyEl.className = 'emr-var-key';
            keyEl.textContent = key;
            const valueEl = document.createElement('div');
            valueEl.className = 'emr-var-value';
            valueEl.textContent = String(value);
            card.appendChild(keyEl);
            card.appendChild(valueEl);
            varsEl.appendChild(card);
        }
    };

    const setButtonActive = (btn, enabled) => btn && btn.classList.toggle('emr-btn-active', !!enabled);

    overlay.querySelector('[data-action="grab"]').addEventListener('click', () => callHost('__emr_grab'));
    overlay.querySelector('[data-action="detect"]').addEventListener('click', () => callHost('__emr_detect_visit'));
    overlay.querySelector('[data-action="dashboard-auto"]').addEventListener('click', () => callHost('__emr_toggle_autoclicker'));
    overlay.querySelector('[data-action="invisit-auto"]').addEventListener('click', () => callHost('__emr_toggle_invisit_autoclicker'));
    overlay.querySelector('[data-action="dark"]').addEventListener('click', () => callHost('__emr_toggle_dark_mode'));
    overlay.querySelector('[data-action="minimize"]').addEventListener('click', () => window.__emrToggleMinimize());
    overlay.querySelector('[data-action="python"]').addEventListener('click', () => callHost('__emr_switch_to_python'));
    overlay.querySelector('[data-action="close"]').addEventListener('click', () => callHost('__emr_close'));
    overlay.querySelector('[data-action="insert-template"]').addEventListener('click', () => {
        if (!templateSelect.value) return;
        callHost('__emr_insert_template', templateSelect.value, 'overlay');
    });

    window.__emrUpdateVariables = (payload) => {
        state.vars = payload || {};
        renderVariables();
    };
    window.__emrUpdateVisitType = (visitType, templates) => {
        state.visitType = String(visitType || 'Unknown');
        state.templates = Array.isArray(templates) ? templates : [];
        visitEl.textContent = `Visit: ${state.visitType}`;
        renderTemplates();
    };
    window.__emrSetStatus = (message) => { statusEl.textContent = String(message || ''); };
    window.__emrSetAutoclickerState = (enabled) => setButtonActive(dashboardBtn, enabled);
    window.__emrSetInvisitAutoclickerState = (enabled) => setButtonActive(invisitBtn, enabled);
    window.__emrSetDarkMode = (enabled) => setButtonActive(darkBtn, enabled);
    window.__emrToggleMinimize = () => {
        state.minimized = !state.minimized;
        overlay.classList.toggle('emr-overlay-minimized', state.minimized);
        return state.minimized;
    };

    renderTemplates();
    renderVariables();
    return 'overlay_injected';
})();
"""

OVERLAY_REMOVE_JS = """
(() => {
    const overlay = document.getElementById('emr-assist-overlay');
    if (overlay) overlay.remove();
    window.__emrOverlayActive = false;
    return 'overlay_removed';
})();
"""


# ================================================================
# Template Dropdown Configuration System
# ================================================================
# Maps tab names to their JSON config file and template file
import subprocess

TEMPLATE_CONFIG = {
    "T Deficiency": {
        "config_file": "t_deficiency_templates.json",
        "template_file": "templates_t_deficiency.txt",
    },
    "Hair Loss": {
        "config_file": "hair_templates.json",
        "template_file": "templates_hair.txt",
    },
    "Photoaging": {
        "config_file": "photoaging_templates.json",
        "template_file": "templates_photoaging.txt",
    },
    "Sexual Health": {
        "config_file": "sexual_health_templates.json",
        "template_file": "templates_sexual_health.txt",
    },
    "Performance Anxiety": {
        "config_file": "performance_anxiety_templates.json",
        "template_file": "templates_performance_anxiety.txt",
    },
    "Birth Control": {
        "config_file": "birth_control_templates.json",
        "template_file": "templates_birth_control.txt",
    },
}


def _get_script_dir() -> str:
    """Return directory where this script is located."""
    return os.path.dirname(os.path.abspath(__file__))


def get_template_config_path(tab_name: str) -> str:
    """Return full path to a tab's JSON dropdown config file."""
    script_dir = _get_script_dir()
    config_filename = TEMPLATE_CONFIG.get(tab_name, {}).get("config_file", "")
    return os.path.join(script_dir, "template_config", config_filename)


def get_template_file_path(tab_name: str) -> str:
    """Return full path to a tab's template .txt file."""
    script_dir = _get_script_dir()
    template_filename = TEMPLATE_CONFIG.get(tab_name, {}).get("template_file", "")
    return os.path.join(script_dir, "templates", template_filename)


def load_tab_template_list(tab_name: str) -> List[str]:
    """Load the list of template names available for a tab (from JSON config)."""
    config_path = get_template_config_path(tab_name)
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get("templates", [])
    except FileNotFoundError:
        print(f"Template config not found: {config_path}")
        return []
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {config_path}: {e}")
        return []
    except Exception as e:
        print(f"Error loading template config {config_path}: {e}")
        return []


def load_all_templates_unified() -> Dict[str, str]:
    """Load templates from all per-tab files and merge into a single dict.
    
    Also loads from legacy templates.txt for backward compatibility.
    """
    all_templates = {}
    script_dir = _get_script_dir()
    templates_dir = os.path.join(script_dir, "templates")
    
    # First load from per-tab template files
    if os.path.isdir(templates_dir):
        for tab_name, config in TEMPLATE_CONFIG.items():
            template_file = os.path.join(templates_dir, config["template_file"])
            if os.path.exists(template_file):
                try:
                    with open(template_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                    all_templates.update(_parse_template_content(content))
                except Exception as e:
                    print(f"Error loading {template_file}: {e}")
    
    # Then load from legacy templates.txt (overwrites duplicates with legacy versions)
    legacy_path = os.path.join(script_dir, 'templates.txt')
    if os.path.exists(legacy_path):
        try:
            with open(legacy_path, 'r', encoding='utf-8') as f:
                content = f.read()
            all_templates.update(_parse_template_content(content))
        except Exception as e:
            print(f"Error loading legacy templates.txt: {e}")
    
    return all_templates


def _parse_template_content(content: str) -> Dict[str, str]:
    """Parse [Template Name] sections from content string."""
    templates = {}
    current_template = None
    current_content = []
    
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
    
    return templates


def open_in_notepad(file_path: str) -> None:
    """Open a file in notepad.exe for editing."""
    try:
        subprocess.Popen(['notepad.exe', file_path])
    except Exception as e:
        print(f"Error opening {file_path} in notepad: {e}")


panel_title = "EMR Assist"
last_active_window = None
grabbed_vars = {config["var"]: "" for config in LABS_CONFIG.values()}
# Add Sexual Health hair loss sxx variables
grabbed_vars['hair_loss_additional_sxx'] = ""
grabbed_vars['hair_loss_location'] = ""
text_ctrls = {}
tdcs_value = ["—"]  # Store TDCS selection as a mutable list, default to em dash
tdcs_c_value = ["—"]  # Store TDCS-C selection as a mutable list, default to em dash
check_ctrls = {}  # Store checkboxes for each variable: {var: (high_checkbox, low_checkbox)}
medication_value = [""]  # Populated from grabbed data
ed_value = ["—"]  # Default to em dash
td_satisfaction_value = ["—"]  # Parsed questionnaire satisfaction
td_side_effects_value = ["No side effects reported"]  # Parsed questionnaire side effects
diagnoses = [""]  # Will hold the comma-separated diagnoses string
templates = {}  # Will hold loaded templates
selected_template = [""]  # Currently selected template

EMR_BRIDGE_FILENAME = "emr_assist_vars.json"
EMR_BRIDGE_PATH = os.path.join(os.getenv("TEMP") or os.getcwd(), EMR_BRIDGE_FILENAME)

def _build_emr_bridge_payload(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "grabbed_vars": dict(grabbed_vars),
        "tdcs": tdcs_value[0],
        "tdcs_c": tdcs_c_value[0],
        "diagnoses": diagnoses[0],
        "medication": medication_value[0],
        "td_satisfaction": td_satisfaction_value[0],
        "td_side_effects": td_side_effects_value[0],
        "ed": ed_value[0],
    }
    # Also flatten lab variables at the top-level for simple access in AHK
    payload.update(dict(grabbed_vars))
    if extra:
        payload.update(extra)
    return payload

def _write_emr_bridge(payload: Dict[str, Any]) -> None:
    temp_dir = os.path.dirname(EMR_BRIDGE_PATH) or "."
    os.makedirs(temp_dir, exist_ok=True)
    tmp_path = EMR_BRIDGE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp_path, EMR_BRIDGE_PATH)
    dprint(f"EMR bridge wrote: {EMR_BRIDGE_PATH}")


_emr_bridge_hook: Optional[Callable[[Dict[str, Any]], None]] = None

def emit_emr_bridge(extra: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {}
    try:
        payload = _build_emr_bridge_payload(extra)
        _write_emr_bridge(payload)
    except Exception as exc:
        print(f"EMR bridge write failed: {exc}")
    if _emr_bridge_hook is not None:
        try:
            _emr_bridge_hook(payload)
        except Exception:
            pass

# PMH options for Rx note (manual selection only; no auto-grab)
PMH_OPTIONS = [
    "ED",
    "PE",
    "Thyroid disease",
    "BPH",
    "HTN",
    "HLD",
    "sleep apnea",
    "obesity",
    "overweight",
    "diabetes",
]
pmh_selected = set()  # selected PMH items

def build_pmh_text() -> str:
    """Return PMH phrase for Rx note based on user selection.
    If none selected -> 'is noncontributory'; else 'significant for X, Y'.
    """
    try:
        if not pmh_selected:
            return "is noncontributory"
        return "significant for " + ", ".join(sorted(pmh_selected))
    except Exception:
        return "is noncontributory"

# Auto Clicker variables
auto_clicker_x = [2600]  # click coordinates x
auto_clicker_y = [400]   # click coordinates y
auto_clicker_interval = [3]  # seconds between clicks
auto_clicker_enabled = [False]  # clicking state
auto_clicker_last_url = [""]  # last observed URL
auto_clicker_thread = [None]  # thread reference

def window_tracker():
    global last_active_window
    while True:
        win = gw.getActiveWindow()
        if win and panel_title not in win.title:
            last_active_window = win
        time.sleep(0.2)

def _clear_text_selection():
    """Collapse any active selection and return the EMR view to a clean top-of-page state."""
    try:
        if 'frame' in globals() and hasattr(frame, '_reset_emr_view_via_script'):
            if frame._reset_emr_view_via_script():
                print("Selection cleared via CDP script (scroll reset)")
                return
    except Exception as ce:
        print(f"Warning: script-based selection reset failed: {ce}")

    try:
        # Collapse selection without altering field content
        pyautogui.press('right')
        time.sleep(0.05)
        pyautogui.press('left')
        time.sleep(0.05)
        # Ensure focus is out of any context menu
        pyautogui.press('escape')
        time.sleep(0.05)
        # Jump caret to end to guarantee selection cleared, then back to top for a clean view
        pyautogui.hotkey('ctrl', 'end')
        time.sleep(0.08)
        pyautogui.hotkey('ctrl', 'home')
        time.sleep(0.08)
        print("Selection cleared and view reset (Right, Left, Esc, Ctrl+End, Ctrl+Home)")
    except Exception as ce:
        print(f"Warning: clear selection sequence failed: {ce}")

def _get_emr_text_cdp():
    """Get fresh EMR page text via CDP/Playwright using the cached browser grabber.
    Returns the page text string, or None on failure."""
    try:
        grabber = getattr(frame, '_browser_grabber_cache', None)
        if grabber is None or not getattr(grabber, 'driver', None):
            grabber = BrowserEMRGrabber()
            if not grabber.connect_to_chrome():
                return None
            try:
                frame._browser_grabber_cache = grabber
            except Exception:
                pass
        # Clear cached text to ensure we get fresh content
        grabber._cache_body_text = None
        grabber._cache_all_text = None
        text = grabber._get_page_text() or ""
        return text if len(text) > 50 else None
    except Exception as e:
        print(f"CDP text grab failed: {e}")
        return None

def grab_all_labs():
    """Grab all lab values using EMR extraction method"""
    finished = [False]

    def do_grab():
        original = ""
        try:
            new_content = None
            used_clipboard = False

            # --- CDP path (fast, no clipboard/focus interaction needed) ---
            if USE_CDP_FOR_GRAB:
                new_content = _get_emr_text_cdp()
                if new_content:
                    print(f"✅ CDP grab: {len(new_content)} chars")

            # --- Clipboard path (when CDP is disabled or failed) ---
            if not new_content:
                used_clipboard = True
                # If CDP mode was on but failed, need to hide frame for clipboard
                if USE_CDP_FOR_GRAB:
                    frame.Hide()
                    time.sleep(0.1)

                # Store original clipboard
                try:
                    original = pyperclip.paste()
                except Exception:
                    original = ""

                # Clear clipboard with unique marker
                pyperclip.copy("CLEARED_BY_METHOD_2")
                time.sleep(0.3)

                # Click screen center then Ctrl+A, Ctrl+C
                screen_width, screen_height = pyautogui.size()
                center_x, center_y = screen_width // 2, screen_height // 2

                print(f"Clicking screen center ({center_x}, {center_y}) to focus EMR...")
                pyautogui.click(center_x, center_y)
                time.sleep(0.5)

                print("Executing Ctrl+A to select all text...")
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.5)

                print("Executing Ctrl+C to copy text...")
                pyautogui.hotkey("ctrl", "c")
                time.sleep(0.8)

                new_content = pyperclip.paste()

                # If clipboard grab also failed, try CDP as last-resort fallback
                if not (new_content and new_content != "CLEARED_BY_METHOD_2" and new_content != original and len(new_content) > 50):
                    try:
                        grabber = BrowserEMRGrabber()
                        if grabber.connect_to_chrome():
                            fallback_text = grabber._get_page_text() or ""
                            if len(fallback_text) > 50:
                                new_content = fallback_text
                                print(f"✅ Fallback page text used ({len(fallback_text)} chars)")
                    except Exception as fe:
                        print(f"Fallback text grab failed: {fe}")

            if (new_content and
                new_content != "CLEARED_BY_METHOD_2" and
                new_content != original and
                len(new_content) > 50):

                print(f"Successfully grabbed {len(new_content)} characters from EMR")

                # Clear any selection if we used clipboard
                if used_clipboard:
                    _clear_text_selection()
                
                # Parse the grabbed text for all lab values
                for lab_name, lab_config in LABS_CONFIG.items():
                    result = extract_lab_value_simple(lab_config, new_content)
                    var_name = lab_config["var"]
                    
                    if result["found"]:
                        grabbed_vars[var_name] = result["raw_value"]
                        print(f"Found {lab_name}: {result['raw_value']} {result['unit']}")
                    else:
                        grabbed_vars[var_name] = ""
                        print(f"Could not find {lab_name}")
                
                # Parse TDCS score from text
                tdcs_score = parse_tdcs_score(new_content)
                tdcs_value[0] = str(tdcs_score)

                # Parse TDCS-C score from text
                tdcs_c_score = parse_tdcsc_score(new_content)
                tdcs_c_value[0] = str(tdcs_c_score) if tdcs_c_score is not None else "—"
                
                # Parse ED status from text
                ed_status = parse_ed_status(new_content)
                ed_value[0] = "He reports symptoms consistent with ED." if ed_status == "Yes" else "He denies symptoms of ED." if ed_status == "No" else "—"

                # Parse questionnaire satisfaction and side effects
                td_satisfaction_value[0] = parse_treatment_satisfaction(new_content)
                td_side_effects_value[0] = parse_side_effects_response(new_content)

                # Detect testosterone deficiency diagnosis from text (TD tab)
                try:
                    if detect_td_diagnosis(new_content):
                        diagnoses[0] = "Testosterone Deficiency"
                        try:
                            frame.dx_td_cb.SetValue(True)
                        except Exception:
                            pass
                        print("Detected Testosterone Deficiency diagnosis from text")
                except Exception as dx_err:
                    print(f"TD diagnosis detection error: {dx_err}")

                # Extract TD medication from grabbed text
                detected_med = extract_td_medication_from_text(new_content)
                medication_value[0] = detected_med if detected_med else ""
                if detected_med:
                    print(f"Detected TD medication: {detected_med}")
                
                # Update UI on main thread
                wx.CallAfter(update_ui_after_grab)

                emit_emr_bridge({"context": "labs"})
                
                # Restore original clipboard
                try:
                    pyperclip.copy(original)
                except Exception:
                    pass
                
            else:
                # Failure to grab: notify and ensure UI is restored
                print("Failed to grab text from EMR")
                # Restore original clipboard before notifying
                try:
                    pyperclip.copy(original)
                except Exception:
                    pass
                def _show_fail_msg():
                    wx.MessageBox(
                        "Failed to grab text from EMR. Make sure EMR window is active.",
                        "Error",
                        wx.ICON_ERROR,
                    )
                    # Always bring the GUI back
                    frame.Show()
                    frame.Raise()
                wx.CallAfter(_show_fail_msg)
                
        except Exception as e:
            print(f"Error during grab: {e}")
            # Restore original clipboard and UI on exceptions as well
            try:
                pyperclip.copy(original)
            except Exception:
                pass
            def _show_err_msg():
                wx.MessageBox(f"Error during grab: {e}", "Error", wx.ICON_ERROR)
                frame.Show()
                frame.Raise()
            wx.CallAfter(_show_err_msg)
        finally:
            finished[0] = True

    # Only hide frame for clipboard mode (CDP doesn't need screen interaction)
    if not USE_CDP_FOR_GRAB:
        frame.Hide()
    time.sleep(0.1)
    
    # Watchdog: if the worker hangs, restore the UI after a timeout
    def _watchdog():
        if not finished[0]:
            def _restore():
                try:
                    frame.Show()
                    frame.Raise()
                except Exception:
                    pass
                wx.MessageBox(
                    "Variable grab took too long and was canceled. You can try again or focus the EMR window first.",
                    "Grab Timeout",
                    wx.ICON_WARNING,
                )
            wx.CallAfter(_restore)
    timer = threading.Timer(7.0, _watchdog)
    timer.daemon = True
    timer.start()

    # Run grab in thread
    threading.Thread(target=do_grab, daemon=True).start()

def update_ui_after_grab():
    """Update UI with grabbed values"""
    for lab_name, lab_config in LABS_CONFIG.items():
        var_name = lab_config["var"]
        if var_name in text_ctrls:
            text_ctrls[var_name].SetValue(grabbed_vars[var_name])
    
    # Update TDCS and ED text fields
    frame.tdcs_text.SetValue(str(tdcs_value[0]))
    if hasattr(frame, "tdcs_c_text"):
        frame.tdcs_c_text.SetValue(str(tdcs_c_value[0]))
    frame.ed_text.SetValue("Yes" if "symptoms consistent with ED" in ed_value[0] else "No" if "denies symptoms of ED" in ed_value[0] else "—")

    # Update TD medication text field
    if hasattr(frame, "td_med_text"):
        frame.td_med_text.SetValue(medication_value[0] if medication_value[0] else "")

    # Update TD response and side effects text fields
    if hasattr(frame, "td_response_text"):
        frame.td_response_text.SetValue(td_satisfaction_value[0] if td_satisfaction_value[0] and td_satisfaction_value[0] != "—" else "")
    if hasattr(frame, "td_side_effects_text"):
        frame.td_side_effects_text.SetValue(td_side_effects_value[0] if td_side_effects_value[0] else "No side effects reported")
    
    # Show frame again
    frame.Show()
    frame.Raise()

def insert_template_at_cursor():
    """Insert the selected template at the active cursor position"""
    if not selected_template[0]:
        wx.MessageBox("Please select a template first", "No Template Selected", wx.ICON_WARNING)
        return
    
    template_name = selected_template[0]
    if template_name not in templates:
        wx.MessageBox(f"Template '{template_name}' not found", "Template Error", wx.ICON_ERROR)
        return
    
    frame.Hide()
    time.sleep(0.2)
    
    try:
        # Get the template content
        template_content = templates[template_name]
        
        # Prepare variables for template substitution
        template_vars = {}
        
        # Add individual lab variables
        for lab_name, lab_config in LABS_CONFIG.items():
            var_name = lab_config["var"]
            value = grabbed_vars.get(var_name, "")
            template_vars[var_name] = value

        # Default PSA to "not done" when missing
        if not template_vars.get("psa") or not template_vars.get("psa").strip():
            template_vars["psa"] = "- not done -"
        
        # Add formatted lab values
        lab_lines = []
        for lab_name, lab_config in LABS_CONFIG.items():
            var_name = lab_config["var"]
            value = grabbed_vars.get(var_name, "")
            if value.strip():
                high_cb, low_cb = check_ctrls[var_name]
                suffix = ""
                if high_cb.GetValue():
                    suffix = " (high)"
                elif low_cb.GetValue():
                    suffix = " (low)"
                lab_lines.append(f"{lab_name}: {value} {lab_config['unit']}{suffix}")
        
        template_vars["lab_values_formatted"] = "\n".join(lab_lines) if lab_lines else "(none grabbed)"

        # Determine which tab we're inserting for
        current_tab = getattr(frame, '_current_template_tab', 'T Deficiency')

        # ── T Deficiency: read ALL variables from widgets (isolated from other tabs) ──
        if current_tab == "T Deficiency":
            template_vars["tdcs"] = frame.tdcs_text.GetValue().strip() if hasattr(frame, "tdcs_text") else tdcs_value[0]
            template_vars["tdcs_c"] = frame.tdcs_c_text.GetValue().strip() if hasattr(frame, "tdcs_c_text") else tdcs_c_value[0]
            # ED status: widget shows Yes/No/—, template needs full sentence
            ed_widget_val = frame.ed_text.GetValue().strip() if hasattr(frame, "ed_text") else "—"
            if ed_widget_val == "Yes":
                template_vars["ed_status"] = "He reports symptoms consistent with ED."
            elif ed_widget_val == "No":
                template_vars["ed_status"] = "He denies symptoms of ED."
            else:
                template_vars["ed_status"] = ed_widget_val
            # Diagnoses: read from T-tab checkboxes (NOT shared diagnoses[0])
            td_dx_parts = []
            if hasattr(frame, "dx_td_cb") and frame.dx_td_cb.GetValue():
                td_dx_parts.append("Testosterone Deficiency")
            if hasattr(frame, "dx_ed_cb") and frame.dx_ed_cb.GetValue():
                td_dx_parts.append("ED")
            template_vars["diagnoses"] = ", ".join(td_dx_parts) if td_dx_parts else diagnoses[0]
            template_vars["pmh"] = build_pmh_text()
            # Response and side effects: read from visible T-tab widgets
            template_vars["response"] = frame.td_response_text.GetValue().strip() if hasattr(frame, "td_response_text") and frame.td_response_text.GetValue().strip() else td_satisfaction_value[0]
            template_vars["side_effects"] = frame.td_side_effects_text.GetValue().strip() if hasattr(frame, "td_side_effects_text") else td_side_effects_value[0]
            # Medication: read from T-tab widget
            active_med = frame.td_med_text.GetValue().strip() if hasattr(frame, "td_med_text") else ""
            template_vars["medication"] = active_med if active_med else medication_value[0]
            print(f"[TD INSERT DEBUG] tab={current_tab}, medication='{template_vars['medication']}', td_med_text='{active_med}', medication_value[0]='{medication_value[0]}', diagnoses='{template_vars['diagnoses']}', response='{template_vars['response']}', side_effects='{template_vars['side_effects']}'")
        else:
            # Non-T-Deficiency tabs: use globals as before
            template_vars["tdcs"] = tdcs_value[0]
            template_vars["tdcs_c"] = tdcs_c_value[0]
            template_vars["ed_status"] = ed_value[0]
            template_vars["diagnoses"] = diagnoses[0]
            template_vars["pmh"] = build_pmh_text()
            template_vars["response"] = td_satisfaction_value[0]
            template_vars["side_effects"] = td_side_effects_value[0]
            # Determine {medication} from the ACTIVE tab's text field
            active_med = ""
            if current_tab == "Hair Loss":
                active_med = frame.hair_med_text.GetValue().strip() if hasattr(frame, "hair_med_text") else ""
            elif current_tab == "Sexual Health":
                active_med = frame.sexual_health_med_text.GetValue().strip() if hasattr(frame, "sexual_health_med_text") else ""
            elif current_tab == "Photoaging":
                active_med = frame.photoaging_med_text.GetValue().strip() if hasattr(frame, "photoaging_med_text") else ""
            elif current_tab == "Performance Anxiety":
                active_med = frame.pa_med_text.GetValue().strip() if hasattr(frame, "pa_med_text") else ""
            elif current_tab == "Birth Control":
                active_med = frame.bc_med_text.GetValue().strip() if hasattr(frame, "bc_med_text") else ""
            template_vars["medication"] = active_med if active_med else medication_value[0]
        
        # ============================================================
        # Tab-specific variables (Hair, Sexual Health, Photoaging, PA)
        # ============================================================
        
        # --- Hair Loss tab variables ---
        try:
            template_vars["hvar"] = frame.hair_hvar_text.GetValue().strip()
            template_vars["hsx"] = frame.hair_hsx_text.GetValue().strip()
            
            # (medication is resolved from active tab above — no per-tab override here)
            
            # Build objective_section from hair exam checkboxes
            hair_exam_findings = []
            if frame.hair_exam_front_hairline.GetValue():
                hair_exam_findings.append("front hairline")
            if frame.hair_exam_top_crown.GetValue():
                hair_exam_findings.append("top/crown")
            if frame.hair_exam_widening_part.GetValue():
                hair_exam_findings.append("widening of the part")
            if frame.hair_exam_diffuse_thinning.GetValue():
                hair_exam_findings.append("diffuse thinning")
            if frame.hair_exam_confluent.GetValue():
                hair_exam_findings.append("confluent from the front hairline to the crown")
            if frame.hair_exam_near_front.GetValue():
                hair_exam_findings.append("near the front with sparing of the hairline")
            
            if hair_exam_findings:
                exam_text = ", ".join(hair_exam_findings)
                template_vars["objective_section"] = f"O: Images reviewed showing hair loss at {exam_text}\n\n"
            else:
                template_vars["objective_section"] = ""
        except AttributeError:
            # Controls may not exist yet during startup
            template_vars["hvar"] = ""
            template_vars["hsx"] = ""
            template_vars["objective_section"] = ""
        
        # --- Sexual Health tab variables ---
        try:
            bp_val = frame.sexual_health_bp_text.GetValue().strip()
            template_vars["bp"] = bp_val if bp_val else "not recorded"
            
            # (medication is resolved from active tab above — no per-tab override here)
            
            # Response text based on followup/initial
            sh_response = frame.sexual_health_response_choice.GetStringSelection() if hasattr(frame, 'sexual_health_response_choice') else ""
            template_vars["response_text"] = sh_response if sh_response else "a satisfactory response"
            
            # Plan action text
            sh_plan = frame.sexual_health_plan_choice.GetStringSelection() if hasattr(frame, 'sexual_health_plan_choice') else ""
            template_vars["action_text"] = sh_plan if sh_plan else "Continue present treatment"

            # SH Initial visit template variables
            template_vars["age"] = getattr(frame, 'sexual_health_age_text', None) and frame.sexual_health_age_text.GetValue().strip() or ""
            template_vars["visit_type"] = getattr(frame, 'sexual_health_visit_type_text', None) and frame.sexual_health_visit_type_text.GetValue().strip() or "sexual health"
            template_vars["rapidity_of_onset"] = getattr(frame, 'sexual_health_onset_text', None) and frame.sexual_health_onset_text.GetValue().strip() or ""
            template_vars["frequency"] = getattr(frame, 'sexual_health_frequency_text', None) and frame.sexual_health_frequency_text.GetValue().strip() or ""
            template_vars["ed_description"] = getattr(frame, 'sexual_health_ed_description_text', None) and frame.sexual_health_ed_description_text.GetValue().strip() or ""
            template_vars["ed_characterization"] = getattr(frame, 'sexual_health_ed_characterization_text', None) and frame.sexual_health_ed_characterization_text.GetValue().strip() or ""
            template_vars["ehs"] = getattr(frame, 'sexual_health_ehs_text', None) and frame.sexual_health_ehs_text.GetValue().strip() or ""
            template_vars["pep_score"] = getattr(frame, 'sexual_health_pep_text', None) and frame.sexual_health_pep_text.GetValue().strip() or ""
            template_vars["past_ed_treatments"] = getattr(frame, 'sexual_health_past_treatments_text', None) and frame.sexual_health_past_treatments_text.GetValue().strip() or "none reported"
            template_vars["ros_positives"] = getattr(frame, 'sexual_health_ros_pos_text', None) and frame.sexual_health_ros_pos_text.GetValue().strip() or "none"
            template_vars["ros_negatives"] = getattr(frame, 'sexual_health_ros_neg_text', None) and frame.sexual_health_ros_neg_text.GetValue().strip() or ""
        except AttributeError:
            template_vars["bp"] = "not recorded"
            template_vars["response_text"] = "a satisfactory response"
            template_vars["action_text"] = "Continue present treatment"
            template_vars["age"] = ""
            template_vars["visit_type"] = "sexual health"
            template_vars["rapidity_of_onset"] = ""
            template_vars["frequency"] = ""
            template_vars["ed_description"] = ""
            template_vars["ed_characterization"] = ""
            template_vars["ehs"] = ""
            template_vars["pep_score"] = ""
            template_vars["past_ed_treatments"] = "none reported"
            template_vars["ros_positives"] = "none"
            template_vars["ros_negatives"] = ""
        
        # --- Photoaging tab variables ---
        try:
            template_vars["retinoid_history"] = frame.photoaging_retinoid_text.GetValue().strip() if hasattr(frame, 'photoaging_retinoid_text') else ""
            
            # (medication is resolved from active tab above — no per-tab override here)
            
            # Build exam_text from photoaging exam checkboxes
            photoaging_exam_findings = []
            if hasattr(frame, 'photoaging_exam_fine_lines') and frame.photoaging_exam_fine_lines.GetValue():
                photoaging_exam_findings.append("fine lines")
            if hasattr(frame, 'photoaging_exam_wrinkles') and frame.photoaging_exam_wrinkles.GetValue():
                photoaging_exam_findings.append("wrinkles")
            if hasattr(frame, 'photoaging_exam_crows_feet') and frame.photoaging_exam_crows_feet.GetValue():
                photoaging_exam_findings.append("crow's feet")
            if hasattr(frame, 'photoaging_exam_pigmentation') and frame.photoaging_exam_pigmentation.GetValue():
                photoaging_exam_findings.append("pigmentation changes")
            if hasattr(frame, 'photoaging_exam_age_spots') and frame.photoaging_exam_age_spots.GetValue():
                photoaging_exam_findings.append("age spots")
            if hasattr(frame, 'photoaging_exam_melasma') and frame.photoaging_exam_melasma.GetValue():
                photoaging_exam_findings.append("melasma")
            if hasattr(frame, 'photoaging_exam_texture_changes') and frame.photoaging_exam_texture_changes.GetValue():
                photoaging_exam_findings.append("texture changes")
            if hasattr(frame, 'photoaging_exam_enlarged_pores') and frame.photoaging_exam_enlarged_pores.GetValue():
                photoaging_exam_findings.append("enlarged pores")
            if hasattr(frame, 'photoaging_exam_loss_elasticity') and frame.photoaging_exam_loss_elasticity.GetValue():
                photoaging_exam_findings.append("loss of elasticity")
            if hasattr(frame, 'photoaging_exam_inflammation') and frame.photoaging_exam_inflammation.GetValue():
                photoaging_exam_findings.append("signs of inflammation")
            if hasattr(frame, 'photoaging_exam_scarring') and frame.photoaging_exam_scarring.GetValue():
                photoaging_exam_findings.append("acne scarring")
            if hasattr(frame, 'photoaging_exam_normal') and frame.photoaging_exam_normal.GetValue():
                photoaging_exam_findings.append("no significant findings")
            
            if photoaging_exam_findings:
                template_vars["exam_text"] = "Images reviewed showing " + ", ".join(photoaging_exam_findings)
            else:
                template_vars["exam_text"] = "Images reviewed"
        except AttributeError:
            template_vars["retinoid_history"] = ""
            template_vars["exam_text"] = "Images reviewed"
        
        # --- Performance Anxiety tab variables ---
        try:
            # (medication is resolved from active tab above — no per-tab override here)
            
            # Build subject_suffix from situational fears checkboxes
            fears = []
            if hasattr(frame, 'pa_fear_new') and frame.pa_fear_new.GetValue():
                fears.append("with new partners")
            if hasattr(frame, 'pa_fear_condom') and frame.pa_fear_condom.GetValue():
                fears.append("when using a condom")
            if hasattr(frame, 'pa_fear_pressure') and frame.pa_fear_pressure.GetValue():
                fears.append("when feeling pressured")
            template_vars["subject_suffix"] = (" " + ", ".join(fears)) if fears else ""
            
            # Build vitals_line from BP and pulse
            bp_val = frame.pa_bp_text.GetValue().strip() if hasattr(frame, 'pa_bp_text') else ""
            pulse_val = frame.pa_pulse_text.GetValue().strip() if hasattr(frame, 'pa_pulse_text') else ""
            if bp_val or pulse_val:
                vitals_parts = []
                if bp_val:
                    vitals_parts.append(f"BP {bp_val}")
                if pulse_val:
                    vitals_parts.append(f"Pulse {pulse_val}")
                template_vars["vitals_line"] = "O: " + ", ".join(vitals_parts) + "\n\n"
            else:
                template_vars["vitals_line"] = ""
            
            # Response for follow-up (only override when on PA tab to avoid clobbering T tab's response)
            if current_tab == "Performance Anxiety":
                if hasattr(frame, 'pa_response_good_rb') and frame.pa_response_good_rb.GetValue():
                    template_vars["response"] = "a good"
                elif hasattr(frame, 'pa_response_partial_rb') and frame.pa_response_partial_rb.GetValue():
                    template_vars["response"] = "a partial"
                elif hasattr(frame, 'pa_response_poor_rb') and frame.pa_response_poor_rb.GetValue():
                    template_vars["response"] = "a poor"
                else:
                    template_vars["response"] = "a satisfactory"
            
            # Side effects phrase (only for PA tab)
            if current_tab == "Performance Anxiety":
                if hasattr(frame, 'pa_se_none_rb') and frame.pa_se_none_rb.GetValue():
                    template_vars["se_phrase"] = "without side effects"
                elif hasattr(frame, 'pa_se_with_rb') and frame.pa_se_with_rb.GetValue():
                    template_vars["se_phrase"] = "with reported side effects"
                else:
                    template_vars["se_phrase"] = "without side effects"
            else:
                template_vars["se_phrase"] = "without side effects"
        except AttributeError:
            template_vars["subject_suffix"] = ""
            template_vars["vitals_line"] = ""
            template_vars["se_phrase"] = "without side effects"
        
        # Format the template
        try:
            formatted_text = template_content.format(**template_vars)
        except KeyError as e:
            wx.MessageBox(f"Template error - missing variable: {e}", "Template Error", wx.ICON_ERROR)
            frame.Show()
            frame.Raise()
            return

        # Type the text at cursor position
        _type_template_text(formatted_text)

        print(f"Inserted template '{template_name}': {repr(formatted_text[:100])}...")
        
    except Exception as e:
        wx.MessageBox(f"Error inserting template: {e}", "Error", wx.ICON_ERROR)
    
    frame.Show()
    frame.Raise()


def _wait_for_modifier_release(timeout: float = 1.0) -> None:
    """Wait for Ctrl, Alt, Shift keys to be released before typing/pasting."""
    import ctypes
    start = time.time()
    VK_CONTROL = 0x11
    VK_MENU = 0x12  # Alt
    VK_SHIFT = 0x10
    
    while time.time() - start < timeout:
        ctrl = ctypes.windll.user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
        alt = ctypes.windll.user32.GetAsyncKeyState(VK_MENU) & 0x8000
        shift = ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000
        if not (ctrl or alt or shift):
            return  # All released
        time.sleep(0.02)
    print("[WARN] Timeout waiting for modifier key release")


def _type_template_text(text: str, prepend_enter: bool = True, interval: float = 0.005) -> None:
    """Insert template text quickly via clipboard paste, with typing fallback when needed."""

    def _fallback_type() -> None:
        if prepend_enter:
            pyautogui.press("enter")
            time.sleep(0.02)
        pyautogui.typewrite(text, interval=interval)

    if not text:
        if prepend_enter:
            pyautogui.press("enter")
            time.sleep(0.02)
        return

    # Wait for any modifier keys to be released first
    _wait_for_modifier_release()

    cached_clipboard = ""
    clipboard_cached = False
    try:
        cached_clipboard = pyperclip.paste()
        clipboard_cached = True
    except pyperclip.PyperclipException:
        pass

    try:
        pyperclip.copy(text)
        time.sleep(0.05)
        if prepend_enter:
            pyautogui.press("enter")
            time.sleep(0.02)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
    except pyperclip.PyperclipException:
        _fallback_type()
    except Exception:
        try:
            _fallback_type()
        except Exception:
            raise
    finally:
        if clipboard_cached:
            try:
                pyperclip.copy(cached_clipboard)
            except pyperclip.PyperclipException:
                pass

def insert_template(template=None, dynamic_labs=False, rx_note=False, lab_message=False, event=None):
    """Legacy function for backward compatibility with Clear All button"""
    frame.Hide()
    time.sleep(0.2)
    if rx_note:
        # Use external Rx Note template
        tpl = templates.get("Rx Note")
        if tpl:
            # Build lab lines for {lab_values_formatted}
            labs_lines = []
            for lab_name, lab_config in LABS_CONFIG.items():
                var_name = lab_config["var"]
                value = grabbed_vars.get(var_name, "")
                if value.strip():
                    high_cb, low_cb = check_ctrls[var_name]
                    suffix = ""
                    if high_cb.GetValue():
                        suffix = " (high)"
                    elif low_cb.GetValue():
                        suffix = " (low)"
                    labs_lines.append(f"{lab_name}: {value} {lab_config['unit']}{suffix}")
            lab_values_formatted = "\n".join(labs_lines) if labs_lines else "(none grabbed)"

            # Read from T-tab widgets for isolation from other tabs
            _td_med = frame.td_med_text.GetValue().strip() if hasattr(frame, "td_med_text") and frame.td_med_text.GetValue().strip() else medication_value[0]
            _td_tdcs = frame.tdcs_text.GetValue().strip() if hasattr(frame, "tdcs_text") else tdcs_value[0]
            _td_tdcs_c = frame.tdcs_c_text.GetValue().strip() if hasattr(frame, "tdcs_c_text") else tdcs_c_value[0]
            _ed_w = frame.ed_text.GetValue().strip() if hasattr(frame, "ed_text") else ""
            _td_ed = "He reports symptoms consistent with ED." if _ed_w == "Yes" else "He denies symptoms of ED." if _ed_w == "No" else ed_value[0]
            _dx_parts = []
            if hasattr(frame, "dx_td_cb") and frame.dx_td_cb.GetValue():
                _dx_parts.append("Testosterone Deficiency")
            if hasattr(frame, "dx_ed_cb") and frame.dx_ed_cb.GetValue():
                _dx_parts.append("ED")
            _td_dx = ", ".join(_dx_parts) if _dx_parts else diagnoses[0]
            text = tpl.format(
                tdcs=_td_tdcs,
                tdcs_c=_td_tdcs_c,
                ed_status=_td_ed,
                pmh=build_pmh_text(),
                lab_values_formatted=lab_values_formatted,
                diagnoses=_td_dx,
                medication=_td_med,
            )
            print(f"[TD LEGACY RX INSERT] medication='{_td_med}', diagnoses='{_td_dx}'")
        else:
            text = "Rx Note template missing"
    elif lab_message:
        tpl = templates.get("Lab Message")
        if tpl:
            _td_med = frame.td_med_text.GetValue().strip() if hasattr(frame, "td_med_text") and frame.td_med_text.GetValue().strip() else medication_value[0]
            text = tpl.format(
                total_testosterone=grabbed_vars.get('total_testosterone', ''),
                free_testosterone=grabbed_vars.get('free_testosterone', ''),
                medication=_td_med,
            )
            print(f"[TD LEGACY LAB MSG INSERT] medication='{_td_med}'")
        else:
            text = "Lab Message template missing"
    elif dynamic_labs:
        tpl = templates.get("Insert Labs")
        if tpl:
            lines = []
            for lab_name, lab_config in LABS_CONFIG.items():
                var_name = lab_config["var"]
                value = grabbed_vars.get(var_name, "")
                if value.strip():
                    high_cb, low_cb = check_ctrls[var_name]
                    suffix = ""
                    if high_cb.GetValue():
                        suffix = " (high)"
                    elif low_cb.GetValue():
                        suffix = " (low)"
                    lines.append(f"{lab_name}: {value} {lab_config['unit']}{suffix}")
            lab_values_formatted = "\n".join(lines) if lines else "(none grabbed)"
            text = tpl.format(lab_values_formatted=lab_values_formatted)
        else:
            text = "Labs: (template missing)"
    else:
        try:
            _td_med = frame.td_med_text.GetValue().strip() if hasattr(frame, "td_med_text") and frame.td_med_text.GetValue().strip() else medication_value[0]
            _dx_parts = []
            if hasattr(frame, "dx_td_cb") and frame.dx_td_cb.GetValue():
                _dx_parts.append("Testosterone Deficiency")
            if hasattr(frame, "dx_ed_cb") and frame.dx_ed_cb.GetValue():
                _dx_parts.append("ED")
            _td_dx = ", ".join(_dx_parts) if _dx_parts else diagnoses[0]
            text = template.format(**grabbed_vars, tdcs=tdcs_value[0], tdcs_c=tdcs_c_value[0], diagnoses=_td_dx, medication=_td_med, ed=ed_value[0])
        except KeyError as e:
            wx.MessageBox(f"Missing variable: {e}", "Template Error", wx.ICON_ERROR)
            frame.Show()
            frame.Raise()
            return
    _type_template_text(text)
    frame.Show()
    frame.Raise()
    print(f"Inserted template: {repr(text)}")

def clear_all(event=None):
    # Clear lab values and checkboxes
    for var in grabbed_vars:
        grabbed_vars[var] = ""
        text_ctrls[var].SetValue("")
        high_cb, low_cb = check_ctrls[var]
        high_cb.SetValue(False)
        low_cb.SetValue(False)
    
    # Clear T Deficiency diagnosis checkboxes (do NOT touch Sexual Health checkboxes)
    frame.dx_td_cb.SetValue(False)
    frame.dx_ed_cb.SetValue(False)
    diagnoses[0] = ""
    
    # Clear hair loss fields
    try:
        frame.sexual_health_hair_location_text.SetValue("")
        frame.sexual_health_hair_sxx_text.SetValue("")
    except Exception:
        pass

    # Clear SH Initial visit fields
    try:
        frame.sexual_health_age_text.SetValue("")
        frame.sexual_health_visit_type_text.SetValue("")
        frame.sexual_health_onset_text.SetValue("")
        frame.sexual_health_frequency_text.SetValue("")
        frame.sexual_health_ed_description_text.SetValue("")
        frame.sexual_health_ed_characterization_text.SetValue("")
        frame.sexual_health_ehs_text.SetValue("")
        frame.sexual_health_pep_text.SetValue("")
        frame.sexual_health_past_treatments_text.SetValue("")
        frame.sexual_health_ros_pos_text.SetValue("")
        frame.sexual_health_ros_neg_text.SetValue("")
    except Exception:
        pass
    
    # Reset TDCS and ED text fields to em dash
    frame.tdcs_text.SetValue("—")
    if hasattr(frame, "tdcs_c_text"):
        frame.tdcs_c_text.SetValue("—")
    frame.ed_text.SetValue("—")
    tdcs_value[0] = "—"
    tdcs_c_value[0] = "—"
    ed_value[0] = "—"
    # Reset PMH selections and summary
    try:
        pmh_selected.clear()
        if hasattr(frame, 'pmh_summary'):
            frame.update_pmh_summary()
    except Exception:
        pass

    try:
        if hasattr(frame, 'td_med_text'):
            frame.td_med_text.SetValue("")
            medication_value[0] = ""
    except Exception:
        pass

    # Clear T Deficiency response and side effects fields
    try:
        if hasattr(frame, 'td_response_text'):
            frame.td_response_text.SetValue("")
        if hasattr(frame, 'td_side_effects_text'):
            frame.td_side_effects_text.SetValue("No side effects reported")
    except Exception:
        pass
    td_satisfaction_value[0] = "—"
    td_side_effects_value[0] = "No side effects reported"
    
    print("All variables and UI elements cleared.")

def show_clinical_matrix():
    """Display the clinical decision matrix image in a separate window"""
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    image_path = os.path.join(script_dir, 'clincal_decision_matrix.png')
    
    if not os.path.exists(image_path):
        wx.MessageBox(f"Image file not found: {image_path}", "Image Not Found", wx.ICON_ERROR)
        return
    
    try:
        # Create a new frame for the image with a defined size
        window_width, window_height = 2100, 1285  # Define desired window size
        parent_window = None
        try:
            parent_window = frame if isinstance(globals().get("frame"), wx.Window) else None
        except Exception:
            parent_window = None
        frame_style = wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP | wx.FRAME_NO_TASKBAR | wx.FRAME_TOOL_WINDOW
        if parent_window is not None:
            frame_style |= wx.FRAME_FLOAT_ON_PARENT
        image_frame = wx.Frame(parent_window, title="Clinical Decision Matrix",
                               style=frame_style,
                               size=(window_width, window_height))
        panel = wx.Panel(image_frame)
        
        # Load the image
        image = wx.Image(image_path, wx.BITMAP_TYPE_PNG)
        
        # Scale image to fit the window size with some padding
        padding = 60  # Total padding (30px on each side)
        target_width = window_width - padding
        target_height = window_height - padding - 50  # Extra space for title bar and controls
        
        # Calculate scaling to fit within target size while maintaining aspect ratio
        img_width, img_height = image.GetSize()
        scale_w = target_width / img_width
        scale_h = target_height / img_height
        scale = min(scale_w, scale_h)  # Use smaller scale to ensure it fits
        
        # Ensure we don't scale up beyond original size
        if scale > 1.0:
            scale = 1.0
        
        new_width = int(img_width * scale)
        new_height = int(img_height * scale)
        
        # Scale the image
        scaled_image = image.Scale(new_width, new_height, wx.IMAGE_QUALITY_HIGH)
        bitmap = wx.Bitmap(scaled_image)
        image_ctrl = wx.StaticBitmap(panel, bitmap=bitmap)
        
        # Layout - center the image in the window
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(image_ctrl, 0, wx.ALL | wx.CENTER, 20)
        panel.SetSizer(sizer)
        
        # Position window with upper right corner at screen's upper right corner
        display_size = wx.GetDisplaySize()
        x_pos = display_size.width - window_width
        y_pos = 0
        image_frame.SetPosition((x_pos, y_pos))
        
        image_frame.Show()
        
        print(f"Clinical decision matrix displayed - Window: {window_width}x{window_height}, Image: {new_width}x{new_height}")
        
    except Exception as e:
        wx.MessageBox(f"Error loading image: {e}", "Image Load Error", wx.ICON_ERROR)


class BrowserEMRGrabber:
    """Playwright-based EMR data extraction for reliable element grabbing"""
    
    def __init__(self):
        self.driver = None
        self.wait = None
        # Per-instance lightweight caches to avoid repeated expensive scans
        self._cache_latest_segment: Optional[str] = None
        self._cache_body_text: Optional[str] = None
        self._cache_all_text: Optional[str] = None
        # Short-lived caches for section/visit type to avoid repeated frame scans
        self._cache_section_header: Optional[str] = None
        self._cache_section_header_time: float = 0.0
        self._cache_visit_type: Optional[str] = None
        self._cache_visit_type_time: float = 0.0
        # Allowed URL prefixes for EMR
        self.allowed_url_prefixes = [
            'https://emr.forhims.com',
            'http://emr.forhims.com'
        ]
        # Persistent CDP (Playwright) connection cache
        self._pw = None  # Playwright instance from sync_playwright().start()
        self._cdp_browser = None  # Connected Browser via connect_over_cdp
        self._cdp_emr_page = None  # Cached EMR Page object
        self._cdp_last_check: float = 0.0
        self._pw_thread_id = None
        self._driver_thread_id = None
    
    def _reset_text_caches(self):
        """Clear per-page text caches to avoid stale reads between grabs."""
        self._cache_latest_segment = None
        self._cache_body_text = None
        self._cache_all_text = None

    def _teardown_playwright_context(self):
        """Dispose of cached Playwright objects when the owning thread is no longer active."""
        try:
            if self.driver:
                self.driver = None
                self.wait = None
        finally:
            self._driver_thread_id = None
        if self._cdp_browser is not None:
            try:
                self._cdp_browser.close()
            except Exception:
                pass
            self._cdp_browser = None
        self._cdp_emr_page = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._pw_thread_id = None
        self._reset_text_caches()

    def _ensure_playwright_browser(self) -> bool:
        if sync_playwright is None:
            print("Playwright is not available. Please install playwright and run `playwright install chromium`.")
            return False
        current_thread = threading.get_ident()
        if self._pw is not None and self._pw_thread_id not in (None, current_thread):
            self._teardown_playwright_context()
        try:
            if self._pw is None:
                self._pw = sync_playwright().start()  # type: ignore
                self._pw_thread_id = current_thread
            if self._cdp_browser is None:
                cdp_url = f"http://127.0.0.1:{CDP_DEBUG_PORT}"
                self._cdp_browser = self._pw.chromium.connect_over_cdp(cdp_url)
            if self._pw_thread_id is None:
                self._pw_thread_id = current_thread
            return True
        except Exception as exc:
            print(f"❌ Failed to connect to Chrome DevTools via Playwright: {exc}")
            if self._cdp_browser is not None:
                try:
                    self._cdp_browser.close()
                except Exception:
                    pass
                self._cdp_browser = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            self._pw_thread_id = None
            self._driver_thread_id = None
            return False

    def _get_browser_pages(self) -> List[Page]:
        if not self._cdp_browser:
            return []
        pages: List[Page] = []
        try:
            for context in self._cdp_browser.contexts:
                try:
                    pages.extend(context.pages)
                except Exception:
                    continue
        except Exception:
            return []
        return pages

    def _find_emr_page(self) -> Optional[Page]:
        pages = self._get_browser_pages()
        for page in pages:
            try:
                if self._is_emr_url(page.url or ""):
                    return page
            except Exception:
                continue
        return pages[0] if pages else None
        
    def connect_to_chrome(self):
        """Connect to existing Chrome debugging session with retries for transient attach failures."""
        # Reuse existing driver when possible
        current_thread = threading.get_ident()
        if self.driver and self._driver_thread_id not in (None, current_thread):
            self.driver = None
            self.wait = None
            self._driver_thread_id = None
        if self.driver:
            try:
                _ = self.driver.current_url  # Sanity check the session
                if not self.wait:
                    self.wait = WebDriverWait(self.driver, 2.5)
                if self._driver_thread_id is None:
                    self._driver_thread_id = current_thread
                if self._ensure_emr_tab():
                    return True
            except Exception:
                self.driver = None
                self.wait = None
                self._driver_thread_id = None

        last_error = None
        for attempt in range(3):
            try:
                if not self._ensure_playwright_browser():
                    raise TimeoutException("Playwright connection failed")

                page = self._find_emr_page()
                if page is None:
                    raise NoSuchElementException(
                        "Could not find an EMR tab in Chrome. Open https://emr.forhims.com and try again."
                    )

                self.driver = PlaywrightDriverAdapter(self._cdp_browser, page)
                self.wait = WebDriverWait(self.driver, 2.5)
                self._driver_thread_id = current_thread

                if not self._ensure_emr_tab():
                    dprint(f"⚠ Connected to Chrome but no EMR tab selected. Current page: {self.driver.current_url}")
                else:
                    dprint(f"✅ Playwright connected to EMR tab: {self.driver.current_url}")
                return True

            except Exception as e:
                last_error = e
                self.driver = None
                self.wait = None
                self._driver_thread_id = None
                message = str(e).lower()
                transient = any(token in message for token in (
                    "execution context",
                    "disconnected",
                    "target closed",
                    "no such window",
                    "timeout",
                ))
                if transient and attempt < 2:
                    time.sleep(0.6 + 0.3 * attempt)
                    continue
                print(f"❌ Chrome connection failed: {e}")
                return False

        if last_error:
            print(f"❌ Chrome connection failed after retries: {last_error}")
        return False
    
    def disconnect(self):
        """Cleanup - but don't close the browser session"""
        if self.driver:
            print("🔗 Keeping Chrome session open for continued EMR use")
            # Don't call driver.quit() - we want to keep the EMR session active
            self.driver = None
            self.wait = None
            self._driver_thread_id = None
            # Clear caches on disconnect
            self._cache_latest_segment = None
            self._cache_body_text = None
            self._cache_all_text = None
        # Keep Playwright CDP attached for speed; provide an explicit shutdown if needed elsewhere

    def shutdown_playwright(self):
        """Fully stop the Playwright controller (call during application shutdown)."""
        try:
            if self.driver:
                try:
                    if hasattr(self.driver, "_cdp_sessions"):
                        try:
                            self.driver._cdp_sessions.clear()  # type: ignore[attr-defined]
                        except Exception:
                            pass
                finally:
                    self.driver = None
            self.wait = None
            self._driver_thread_id = None
        except Exception:
            pass

        # Drop cached EMR page references so new sessions reconnect cleanly
        self._cdp_emr_page = None
        self._cdp_last_check = 0.0

        # Close the CDP browser connection (this only severs the debugging client, Chrome stays open)
        if self._cdp_browser is not None:
            try:
                self._cdp_browser.close()
            except Exception:
                pass
            self._cdp_browser = None

        # Stop the Playwright driver process to prevent broken pipe errors on exit
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self._pw_thread_id = None

    def fetch_patient_location_summary(self, debug_print=True) -> dict | None:
        summary: dict = {
            "state": None,
            "state_display": None,
            "city": None,
            "distance_miles": None,
            "address": None,
            "detection_method": None,
            "detection_error": None,
        }
        try:
            if not self.driver and not self.connect_to_chrome():
                summary["detection_error"] = "Could not connect to Chrome."
                if debug_print:
                    print(f"[STATE DETECTION] {summary}")
                return summary
            if not self.driver:
                summary["detection_error"] = "No active browser session."
                if debug_print:
                    print(f"[STATE DETECTION] {summary}")
                return summary
            if not self._ensure_emr_tab():
                summary["detection_error"] = "Not on EMR tab."
                if debug_print:
                    print(f"[STATE DETECTION] {summary}")
                return summary

            self._switch_to_default()
            wait = WebDriverWait(self.driver, 6)

            # Try primary selector (original method)
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='patientSummarySidebar']")))
            except TimeoutException:
                summary["detection_error"] = "Sidebar not found."
                if debug_print:
                    print(f"[STATE DETECTION] {summary}")
                return summary

            # Try original XPath
            try:
                state_elem = wait.until(
                    EC.presence_of_element_located(
                        (
                            By.XPATH,
                            "//div[@data-testid='patientSummarySidebar']//div[contains(concat(' ', normalize-space(@class), ' '), ' w-[25%] ')]/div[contains(concat(' ', normalize-space(@class), ' '), ' font-medium ')]",
                        )
                    )
                )
                raw_state = (state_elem.text or "").strip()
                normalized = raw_state.upper()
                summary["state_display"] = raw_state or normalized or None
                summary["state"] = normalized or None
                summary["detection_method"] = "sidebar_xpath"
            except TimeoutException:
                state_elem = None
                raw_state = None
                normalized = None

            # Fallback: try alternative selectors if state not found
            if not normalized:
                # Try to find any element with state abbreviation (2 letters) in sidebar
                try:
                    sidebar = self.driver.find_element(By.CSS_SELECTOR, "div[data-testid='patientSummarySidebar']")
                    sidebar_text = sidebar.text
                    # Look for state abbreviation (e.g., NY, CA, WI, etc.)
                    state_match = re.search(r"\b([A-Z]{2})\b", sidebar_text)
                    if state_match:
                        summary["state"] = state_match.group(1)
                        summary["state_display"] = state_match.group(1)
                        summary["detection_method"] = "sidebar_text_regex"
                except Exception as e:
                    summary["detection_error"] = f"Sidebar text fallback error: {e}"

            # Fallback: try parsing visible page text for state
            if not summary["state"]:
                try:
                    page_text = self.driver.find_element(By.TAG_NAME, 'body').text
                    # Look for state abbreviation (2 uppercase letters) after a city name
                    state_match = re.search(r",\s*([A-Z]{2})\b", page_text)
                    if state_match:
                        summary["state"] = state_match.group(1)
                        summary["state_display"] = state_match.group(1)
                        summary["detection_method"] = "body_text_regex"
                except Exception as e:
                    summary["detection_error"] = f"Body text fallback error: {e}"

            # If still not found, try to find any state name (e.g., Wisconsin)
            if not summary["state"]:
                try:
                    page_text = self.driver.find_element(By.TAG_NAME, 'body').text
                    for state_name in [
                        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming"
                    ]:
                        if state_name in page_text:
                            summary["state"] = state_name.upper()[:2]
                            summary["state_display"] = state_name
                            summary["detection_method"] = "body_text_state_name"
                            break
                except Exception as e:
                    summary["detection_error"] = f"Body text state name fallback error: {e}"

            # If state is WI or WISCONSIN, try to get city/address as before
            normalized = summary.get("state", None)
            if normalized in ("WI", "WISCONSIN"):
                summary["state"] = "WI"
                address_row_xpath = "(//div[contains(@class,'py-3') and contains(@class,'text-xs') and contains(@class,'text-black')])[3]"
                try:
                    edit_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#editPatient")))
                    edit_button.click()
                    reveal_button = wait.until(
                        EC.element_to_be_clickable((By.XPATH, f"{address_row_xpath}//button"))
                    )
                    reveal_button.click()
                    def address_ready(driver_obj):
                        try:
                            text_value = driver_obj.find_element(By.XPATH, address_row_xpath).text.strip()
                        except (StaleElementReferenceException, NoSuchElementException):
                            return False
                        return text_value and "click to reveal address" not in text_value.lower()
                    WebDriverWait(self.driver, 6).until(address_ready)
                    address_text = self.driver.find_element(By.XPATH, address_row_xpath).text.strip()
                    normalized_address = " ".join(address_text.split())
                    summary["address"] = normalized_address
                    match = re.search(r",\s*([A-Za-z\s]+),\s*WI\b", normalized_address)
                    if match:
                        summary["city"] = match.group(1).strip()
                except TimeoutException:
                    summary["detection_error"] = "Timeout getting WI address/city."
                except Exception as e:
                    summary["detection_error"] = f"WI address/city error: {e}"
                finally:
                    self._switch_to_default()
                    self._close_patient_edit_modal()
                if summary.get("city") and Nominatim and geodesic:
                    try:
                        geolocator = Nominatim(user_agent=GEOCODER_USER_AGENT)
                        patient_coords = geocode_address(geolocator, f"{summary['city']}, WI")
                        target_coords = get_chippewa_falls_coords(geolocator)
                        if patient_coords and target_coords:
                            summary["distance_miles"] = geodesic(patient_coords, target_coords).miles
                    except Exception:
                        pass
            if debug_print:
                print(f"[STATE DETECTION] {summary}")
            return summary
        except Exception as exc:
            summary["detection_error"] = f"Location summary fetch error: {exc}"
            print(f"Location summary fetch error: {exc}")
            return summary
        finally:
            try:
                self._switch_to_default()
            except Exception:
                pass

    # --- Frame/element helpers ---
    def _switch_to_default(self):
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass

    def _switch_to_frame_with_element(self, by, selector, timeout_each=1.5, max_depth=3):
        """Try default context, then recursively descend into iframes until element is found.
        Leaves the driver focused in the frame where the element exists. Returns True if found, else False.
        """
        if not self.driver:
            return False

        self._switch_to_default()

        # Try default content first
        try:
            WebDriverWait(self.driver, max(0.5, timeout_each)).until(
                EC.presence_of_element_located((by, selector))
            )
            return True
        except Exception:
            pass

        # Depth-first search through iframes
        def dfs(depth):
            if depth > max_depth:
                return False
            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for idx, frame in enumerate(frames):
                try:
                    self.driver.switch_to.frame(frame)
                except Exception:
                    # Skip frames we cannot switch into
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass
                    continue
                try:
                    WebDriverWait(self.driver, max(0.5, timeout_each)).until(
                        EC.presence_of_element_located((by, selector))
                    )
                    # Found in this frame; stay here
                    return True
                except Exception:
                    # Try nested frames
                    if dfs(depth + 1):
                        return True
                # Not found in this branch; go back up and continue
                try:
                    self.driver.switch_to.parent_frame()
                except Exception:
                    pass
            return False

        return dfs(0)

    def _find_element_in_frames_by_xpath(self, xpath: str, timeout_each=1.5, max_depth=3):
        """Return the first WebElement matching xpath across default content and nested iframes.
        Keeps driver focused where the element is found. Returns None if not found."""
        if not self.driver:
            return None
        self._switch_to_default()
        try:
            el = WebDriverWait(self.driver, max(0.5, timeout_each)).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
            return el
        except Exception:
            pass

        def dfs_get(depth):
            if depth > max_depth:
                return None
            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for frame in frames:
                try:
                    self.driver.switch_to.frame(frame)
                except Exception:
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass
                    continue
                try:
                    el = WebDriverWait(self.driver, max(0.5, timeout_each)).until(
                        EC.presence_of_element_located((By.XPATH, xpath))
                    )
                    return el
                except Exception:
                    nested = dfs_get(depth + 1)
                    if nested is not None:
                        return nested
                finally:
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass
            return None

        self._switch_to_default()
        return dfs_get(0)

    def _is_emr_url(self, url: str) -> bool:
        try:
            return any(url.startswith(prefix) for prefix in self.allowed_url_prefixes)
        except Exception:
            return False

    def _ensure_emr_tab(self) -> bool:
        """Ensure the driver is focused on a tab whose URL starts with emr.forhims.com.
        Returns True if switched/found, False otherwise.
        """
        try:
            # 1) If we're already on EMR, done
            current = self.driver.current_url
            if self._is_emr_url(current):
                return True

            # 2) Try all window handles
            handles = self.driver.window_handles
            for h in handles:
                try:
                    self.driver.switch_to.window(h)
                    url = self.driver.current_url
                    if self._is_emr_url(url):
                        print(f"🔀 Switched to EMR tab via window handle: {url}")
                        return True
                except Exception:
                    continue

            # 3) Try CDP to find and activate target
            try:
                targets = self.driver.execute_cdp_cmd('Target.getTargets', {})
                for t in targets.get('targetInfos', []):
                    url = t.get('url', '')
                    target_id = t.get('targetId')
                    if target_id and self._is_emr_url(url):
                        # Activate the EMR target
                        self.driver.execute_cdp_cmd('Target.activateTarget', {'targetId': target_id})
                        time.sleep(0.2)
                        # Re-check handles
                        for h in self.driver.window_handles:
                            try:
                                self.driver.switch_to.window(h)
                                if self._is_emr_url(self.driver.current_url):
                                    print(f"🔀 Activated and switched to EMR tab: {self.driver.current_url}")
                                    return True
                            except Exception:
                                continue
                        break
            except Exception as e:
                print(f"CDP target selection failed: {e}")

            return False
        except Exception as e:
            print(f"ensure_emr_tab error: {e}")
            return False
    
    def _close_patient_edit_modal(self) -> None:
        """Exit the patient edit modal via the close icon (fallback to Escape)."""
        if not self.driver:
            return
        clicked = False
        for attempt in range(2):
            if self._click_modal_close_button():
                clicked = True
            if attempt == 0:
                time.sleep(0.35)
        if clicked:
            return
        try:
            if self.driver.execute_script(self._modal_close_script()):
                return
        except Exception:
            pass
        try:
            page = getattr(self.driver, 'page', None)
            if page is not None and hasattr(page, 'keyboard'):
                page.keyboard.press('Escape')
                time.sleep(0.1)
        except Exception:
            pass

    def _click_modal_close_button(self) -> bool:
        if not self.driver:
            return False
        page = getattr(self.driver, 'page', None)
        if page is None:
            return False
        selectors = [
            'button[aria-label="Close modal icon"]',
            'button[data-testid="closeModalButton"]',
            'button[aria-label="Close"]',
            'svg.svg-inline--fa.fa-xmark[aria-label="Close modal icon"]'
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = locator.count()
                if count > 0:
                    locator.nth(0).click()
                    return True
            except Exception:
                continue
        return False

    def _modal_close_script(self) -> str:
        return """(() => {
            const selectors = [
                'button[aria-label="Close modal icon"]',
                'button[data-testid="closeModalButton"]',
                'button[aria-label="Close"]'
            ];
            for (const sel of selectors) {
                const button = document.querySelector(sel);
                if (button) {
                    button.click();
                    return true;
                }
            }
            const svg = document.querySelector('svg.svg-inline--fa.fa-xmark[aria-label="Close modal icon"]');
            if (svg) {
                const btn = svg.closest('button');
                if (btn) {
                    btn.click();
                    return true;
                }
            }
            return false;
        })();"""

    def grab_sexual_health_data(self):
        """
    Browser-driven Sexual Health data extraction
    Much more reliable than keyboard automation
        """
        if not self.driver:
            if not self.connect_to_chrome():
                return None
        
        try:
            # Ensure we're on an EMR tab before extracting
            if not self._ensure_emr_tab():
                print(f"❌ No EMR tab found. Please open https://emr.forhims.com in your browser (debugging port {CDP_DEBUG_PORT}) and try again.")
                print(f"📍 Current page: {self.driver.current_url}")
                return None

            # Always clear cached text snapshots before a new grab to prevent stale values
            self._reset_text_caches()

            overall_start = time.perf_counter()
            dprint("🔍 Starting browser-based Sexual Health grab…")
            dprint(f"📍 Current page: {self.driver.current_url}")
            
            # Define EMR element selectors for Sexual Health data
            selectors = {
                'medication_title': get_selector('sexual_health', 'medication', engine='selenium', selector_type='css') or '[data-testid="medication-title"]',
                'medication_text': get_selector('sexual_health', 'medication_detail', engine='selenium', selector_type='css') or '[data-testid="medication-text"]',
                'treatment_plan': get_selector('sexual_health', 'treatment_plan', engine='selenium', selector_type='css') or '[data-testid="proposedTreatmentPlan"]',
                'current_dose': get_selector('sexual_health', 'current_dose', engine='selenium', selector_type='css') or '[data-testid="treatmentPlan"]',
            }
            
            # Additional selectors to try if main ones don't work
            fallback_selectors = {
                'medication_alt1': '.medication-name, .med-name, [class*="medication"]',
                'medication_alt2': '[class*="dose"], [class*="dosage"]',
                'treatment_alt': '[class*="treatment"], [class*="plan"]',
            }
            
            extracted_data = {}
            
            # Try to extract medication information
            t0 = time.perf_counter()
            medication = self._extract_medication(selectors, fallback_selectors)
            if medication:
                extracted_data['medication'] = medication
                dprint(f"   ✅ Extracted medication: '{medication}' ({(time.perf_counter()-t0)*1000:.0f} ms)")

            t1 = time.perf_counter()
            med_detail = self._extract_medication_detail_text()
            if med_detail:
                extracted_data['intake_med_detail'] = med_detail
                dprint(f"   ✅ Extracted medication detail: '{med_detail}' ({(time.perf_counter()-t1)*1000:.0f} ms)")

            intake_med_plain = self._strip_frequency_suffix(medication) if medication else ""
            if intake_med_plain:
                extracted_data['intake_med_name'] = intake_med_plain
            
            # Try to extract effectiveness information (prefer fast text parsing)
            t2 = time.perf_counter()
            # Keep text gathering fast: avoid iframe walks unless Hybrid mode is selected
            try:
                if USE_PLAYWRIGHT_FOR_SH:
                    pre_text = self._get_all_text_across_frames(max_frames=4)
                else:
                    pre_text = self._get_page_text() or ""
            except Exception:
                pre_text = self._get_page_text() or ""
            effectiveness = self._extract_effectiveness(full_text=pre_text)
            if effectiveness:
                extracted_data['effectiveness'] = effectiveness
                dprint(f"   ✅ Extracted effectiveness: '{effectiveness}' ({(time.perf_counter()-t2)*1000:.0f} ms)")
            
            # Try to extract blood pressure
            t3 = time.perf_counter()
            blood_pressure = self._extract_blood_pressure()
            if blood_pressure:
                extracted_data['blood_pressure'] = blood_pressure
                dprint(f"   ✅ Extracted blood pressure: '{blood_pressure}' ({(time.perf_counter()-t3)*1000:.0f} ms)")
            
            # Try to extract diagnoses from notes
            t4 = time.perf_counter()
            diagnoses = self._extract_diagnoses()
            if diagnoses:
                extracted_data['diagnoses'] = diagnoses
                dprint(f"   ✅ Extracted diagnoses: {diagnoses} ({(time.perf_counter()-t4)*1000:.0f} ms)")

            t5 = time.perf_counter()
            current_med_name, current_med_detail = self._extract_current_treatment_summary()
            if current_med_name:
                extracted_data['current_med_name'] = current_med_name
                dprint(f"   ✅ Current medication: '{current_med_name}' ({(time.perf_counter()-t5)*1000:.0f} ms)")
            if current_med_detail:
                extracted_data['current_med_detail'] = current_med_detail
                dprint(f"   ✅ Current detail: '{current_med_detail}'")
            
            # Extract hair loss data if present
            t6 = time.perf_counter()
            hair_loss_location = self._extract_hair_loss_location()
            if hair_loss_location:
                extracted_data['hair_loss_location'] = hair_loss_location
                dprint(f"   ✅ Extracted hair loss location: '{hair_loss_location}' ({(time.perf_counter()-t6)*1000:.0f} ms)")
            
            t7 = time.perf_counter()
            hair_loss_additional_sxx = self._extract_hair_loss_additional_sxx()
            if hair_loss_additional_sxx:
                extracted_data['hair_loss_additional_sxx'] = hair_loss_additional_sxx
                dprint(f"   ✅ Extracted hair loss additional sxx: '{hair_loss_additional_sxx}' ({(time.perf_counter()-t7)*1000:.0f} ms)")

            # --- SH Initial visit fields ---
            full_text = self._get_page_text()

            t8 = time.perf_counter()
            patient_age = self._extract_patient_age(full_text=full_text)
            if patient_age:
                extracted_data['patient_age'] = patient_age
                dprint(f"   ✅ Extracted patient age: '{patient_age}' ({(time.perf_counter()-t8)*1000:.0f} ms)")

            t9 = time.perf_counter()
            visit_type = self._extract_visit_type_from_header()
            if visit_type:
                extracted_data['visit_type'] = visit_type
                dprint(f"   ✅ Extracted visit type: '{visit_type}' ({(time.perf_counter()-t9)*1000:.0f} ms)")

            t10 = time.perf_counter()
            ed_onset = self._extract_ed_onset(full_text=full_text)
            if ed_onset:
                extracted_data['rapidity_of_onset'] = ed_onset
                dprint(f"   ✅ Extracted ED onset: '{ed_onset}' ({(time.perf_counter()-t10)*1000:.0f} ms)")

            t11 = time.perf_counter()
            ed_frequency = self._extract_ed_frequency(full_text=full_text)
            if ed_frequency:
                extracted_data['frequency'] = ed_frequency
                dprint(f"   ✅ Extracted ED frequency: '{ed_frequency}' ({(time.perf_counter()-t11)*1000:.0f} ms)")

            t12 = time.perf_counter()
            ed_description = self._extract_ed_description(full_text=full_text)
            if ed_description:
                extracted_data['ed_description'] = ed_description
                dprint(f"   ✅ Extracted ED description: '{ed_description}' ({(time.perf_counter()-t12)*1000:.0f} ms)")

            t13 = time.perf_counter()
            ed_characterization = self._extract_ed_characterization(full_text=full_text)
            if ed_characterization:
                extracted_data['ed_characterization'] = ed_characterization
                dprint(f"   ✅ Extracted ED characterization: '{ed_characterization}' ({(time.perf_counter()-t13)*1000:.0f} ms)")

            t14 = time.perf_counter()
            ehs = self._extract_ehs_scores(full_text=full_text)
            if ehs:
                extracted_data['ehs'] = ehs
                dprint(f"   ✅ Extracted EHS: '{ehs}' ({(time.perf_counter()-t14)*1000:.0f} ms)")

            t15 = time.perf_counter()
            pep = self._extract_pep_score(full_text=full_text)
            if pep:
                extracted_data['pep_score'] = pep
                dprint(f"   ✅ Extracted PEP score: '{pep}' ({(time.perf_counter()-t15)*1000:.0f} ms)")

            t16 = time.perf_counter()
            past_treatments = self._extract_past_ed_treatments(full_text=full_text)
            if past_treatments:
                extracted_data['past_ed_treatments'] = past_treatments
                dprint(f"   ✅ Extracted past treatments: '{past_treatments}' ({(time.perf_counter()-t16)*1000:.0f} ms)")

            t17 = time.perf_counter()
            ros_pos = self._extract_ros_positives(full_text=full_text)
            extracted_data['ros_positives'] = ros_pos if ros_pos else "none"
            ros_neg = self._extract_ros_negatives(ros_pos)
            extracted_data['ros_negatives'] = ros_neg
            dprint(f"   ✅ Extracted ROS: +'{ros_pos}' / -'{ros_neg}' ({(time.perf_counter()-t17)*1000:.0f} ms)")

            if full_text:
                extracted_data['full_text'] = full_text
            
            dprint(f"✅ Browser grab completed in {(time.perf_counter()-overall_start)*1000:.0f} ms. Found {len(extracted_data)} data types.")
            return extracted_data
            
        except Exception as e:
            print(f"❌ Browser grab error: {e}")
            return None

    def _parse_intake_timestamp(self, text: str) -> Optional[datetime]:
        """Parse intake timestamp strings like 'August 20, 2024 at 3:15 PM'."""
        if not text:
            return None
        cleaned = text.strip()
        patterns = [
            "%B %d, %Y at %I:%M %p",
            "%b %d, %Y at %I:%M %p",
            "%m/%d/%Y %I:%M %p",
        ]
        for fmt in patterns:
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        return None

    def _get_latest_intake_text_segment(self) -> Optional[str]:
        """Return the body text segment that belongs to the most recent intake form."""
        if self._cache_latest_segment is not None:
            return self._cache_latest_segment
        try:
            self._switch_to_default()
        except Exception:
            pass

        page_text = self._get_page_text() or ""
        if not page_text:
            return None

        try:
            date_nodes = self.driver.find_elements(By.CSS_SELECTOR, INTAKE_FORM_DATE_SELECTOR)
        except Exception:
            date_nodes = []

        spans: List[Tuple[datetime, int, str]] = []
        for node in date_nodes:
            try:
                raw = (node.get_attribute("innerText") or node.text or "").strip()
            except Exception:
                continue
            if not raw:
                continue
            stamp = self._parse_intake_timestamp(raw)
            if not stamp:
                continue
            idx = page_text.find(raw)
            if idx == -1:
                continue
            spans.append((stamp, idx, raw))

        if not spans:
            # Cache full page as a segment fallback
            self._cache_latest_segment = page_text
            return self._cache_latest_segment

        spans.sort(key=lambda item: item[0], reverse=True)
        latest_stamp, latest_idx, latest_label = spans[0]
        cutoff = len(page_text)
        for _, idx, _ in spans[1:]:
            if idx > latest_idx and idx < cutoff:
                cutoff = idx

        segment = page_text[latest_idx:cutoff].strip()
        if segment:
            self._cache_latest_segment = segment
            return self._cache_latest_segment
        self._cache_latest_segment = page_text
        return self._cache_latest_segment
    
    def _extract_medication(self, selectors, fallback_selectors):
        """Extract medication quickly via DOM queries with Playwright; fall back to text parsing when needed.
        The returned string appends a simple frequency suffix (", daily" or ", as-needed").
        """
        print("   🔍 Looking for medication-title element (including iframes)...")

        latest_segment = self._get_latest_intake_text_segment() or ""
        latest_segment_lower = latest_segment.lower()

        group = 'sexual_health'
        if isinstance(selectors, dict):
            group = selectors.get('group', group)

        selector = get_selector(group, 'medication', engine='selenium', selector_type='css') or '[data-testid="medication-title"]'
        # Include common brand/generic names and combo components
        med_keywords = [
            'viagra', 'cialis', 'sildenafil', 'tadalafil', 'vardenafil', 'stendra', 'avanafil', 'levitra',
            'generic viagra', 'generic cialis', 'atorvastatin'
        ]

        try:
            # Ensure we're in the right frame where the element lives
            if not self._switch_to_frame_with_element(By.CSS_SELECTOR, selector, timeout_each=1.5, max_depth=3):
                print("   ❌ medication-title not found in any frame")
                # Before returning, ensure we reset context
                self._switch_to_default()
                return ""

            # Found; get the element and read all text (innerText preserves line breaks better)
            element = self.driver.find_element(By.CSS_SELECTOR, selector)
            full_text = (element.get_attribute('innerText') or element.text or '').strip()
            print(f"   ✅ medication-title text: '{full_text}'")

            if full_text:
                # Normalize NBSP and build lines
                norm_text = full_text.replace('\xa0', ' ')
                lines = [line.strip() for line in norm_text.split('\n') if line.strip()]
                # Regex detector for doses per month
                doses_re = re.compile(r"(\d+)\s*doses?\s*per\s*month", re.IGNORECASE)
                # Global flag and value based on any instance of the phrase in the element
                doses_match_elem = doses_re.search(norm_text)
                doses_in_elem = int(doses_match_elem.group(1)) if doses_match_elem else None
                print(f"      ⏱ doses_in_elem: {doses_in_elem}")

                # Also try to inspect a nearby container (parent) for the frequency line
                doses_in_container = None
                try:
                    container = element.find_element(By.XPATH, "ancestor::*[self::div or self::section or self::article][1]")
                    container_text = (container.get_attribute('innerText') or container.text or '').replace('\xa0', ' ')
                    dm = doses_re.search(container_text)
                    doses_in_container = int(dm.group(1)) if dm else None
                    print(f"      ⏱ doses_in_container: {doses_in_container}")
                except Exception as ce:
                    print(f"      ⚠️ Container scan failed: {ce}")

                # Fallback to page body if needed
                doses_in_body = None
                if doses_in_elem is None and doses_in_container is None:
                    try:
                        source = latest_segment if latest_segment else (self._get_page_text() or '')
                        body_text = source.replace('\xa0', ' ')
                        dm_body = doses_re.search(body_text)
                        doses_in_body = int(dm_body.group(1)) if dm_body else None
                        print(f"      ⏱ doses_in_body: {doses_in_body}")
                    except Exception as be:
                        print(f"      ⚠️ Body scan failed: {be}")

                # Decide frequency from the best-available value
                def compute_frequency(next_line: str) -> str:
                    nl = (next_line or '').lower()
                    # Next-line probe
                    next_line_val = None
                    try:
                        m = doses_re.search(nl)
                        next_line_val = int(m.group(1)) if m else None
                    except Exception:
                        next_line_val = None
                    # Prefer element value, then container, then next-line, then body
                    for v, src in [
                        (doses_in_elem, 'elem'),
                        (doses_in_container, 'container'),
                        (next_line_val, 'next-line'),
                        (doses_in_body, 'body'),
                    ]:
                        if v is not None:
                            print(f"      🔎 frequency source: {src} -> {v} doses/month")
                            return ", daily" if v >= 30 else ", as-needed"
                    # Default if nothing detected
                    print("      🔎 frequency source: none -> default as-needed")
                    return ", as-needed"

                def with_frequency(base_line: str, idx: int) -> str:
                    # Use computed frequency from multiple scopes
                    next_line = lines[idx + 1] if (idx + 1) < len(lines) else ""
                    suffix = compute_frequency(next_line)
                    cleaned_line = self._sanitize_medication_line(base_line) or base_line.strip()
                    result = f"{cleaned_line}{suffix}"
                    print(f"      ➕ Appending frequency (next='{next_line}'): '{result}'")
                    return result

                # Primary pass: lines with known keywords and mg
                for idx, line in enumerate(lines):
                    lower = line.lower()
                    found_keywords = [kw for kw in med_keywords if kw in lower]
                    has_mg = 'mg' in lower
                    has_numbers = any(ch.isdigit() for ch in line)
                    if latest_segment_lower and lower not in latest_segment_lower:
                        continue
                    print(f"      Checking line: '{line}' -> kws={found_keywords}, mg={has_mg}, nums={has_numbers}")
                    if found_keywords and has_mg and has_numbers:
                        result = with_frequency(line, idx)
                        print(f"   🎯 MATCH! Returning: '{result}'")
                        return result

                # Fallback: first line with 'mg' and numbers
                for idx, line in enumerate(lines):
                    lower = line.lower()
                    if latest_segment_lower and lower not in latest_segment_lower:
                        continue
                    if 'mg' in lower and any(ch.isdigit() for ch in line):
                        result = with_frequency(line, idx)
                        print(f"   🎯 Fallback MATCH (no keyword): Returning: '{result}'")
                        return result

            print("   ⚠️ medication-title element present but no matching line found")
        except Exception as e:
            print(f"   ❌ Medication extraction error: {e}")
        finally:
            # Always reset back to default content for subsequent operations
            self._switch_to_default()

        # If Selenium path didn't return, try Playwright as a fallback (can be slower)
        if USE_PLAYWRIGHT_FOR_MED and sync_playwright is not None:
            try:
                med_pl = self._extract_medication_playwright(group=group)
                if med_pl:
                    return med_pl
            except Exception as e:
                print(f"   ⚠️ Playwright medication grab failed: {e}")

        # As a last resort, list visible medication-related elements in any frame for debugging
        try:
            print("   🔍 Listing elements with 'medication' in data-testid (any frame)...")
            # Search default first
            self._switch_to_default()
            elems = self.driver.find_elements(By.CSS_SELECTOR, '[data-testid*="medication"]')
            print(f"   Default context: {len(elems)} elements found")

            # Then scan top-level iframes for hints
            frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            print(f"   Top-level iframes: {len(frames)}")
            for idx, fr in enumerate(frames[:5]):
                try:
                    self.driver.switch_to.frame(fr)
                    sub = self.driver.find_elements(By.CSS_SELECTOR, '[data-testid*="medication"]')
                    print(f"     Frame {idx}: {len(sub)} elements with 'medication'")
                    for el in sub[:3]:
                        tid = el.get_attribute('data-testid') or 'no-testid'
                        txt = (el.text or '').strip()
                        txt = (txt[:100] + '...') if len(txt) > 100 else txt
                        print(f"       - {tid}: '{txt}'")
                except Exception as fe:
                    print(f"     Frame {idx} scan error: {fe}")
                finally:
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass
        except Exception as e2:
            print(f"   ❌ Debug listing error: {e2}")

        return ""

    def _extract_medication_playwright(self, group: str = 'sexual_health') -> str:
        """Use Playwright (connected to existing Chrome via CDP) to read
        [data-testid="medication-title"]. Returns an empty string if not found.

        Also attempts to infer frequency from nearby text: if "30 doses per month"
        appears in the medication text block or page content, appends ", daily",
        otherwise appends ", as-needed".
        """
        try:
            if sync_playwright is None:
                return ""
            with sync_playwright() as p:  # type: ignore
                cdp_url = f"http://127.0.0.1:{CDP_DEBUG_PORT}"
                browser = p.chromium.connect_over_cdp(cdp_url)
                try:
                    target_page = None
                    # Find an EMR page among existing contexts/pages
                    for context in browser.contexts:
                        for page in context.pages:
                            url = page.url or ""
                            if self._is_emr_url(url):
                                target_page = page
                                break
                        if target_page:
                            break

                    if not target_page:
                        print("   ⚠️ Playwright: EMR page not found in existing Chrome session")
                        return ""

                    sel = get_selector(group, 'medication', engine='playwright', selector_type='css') or '[data-testid="medication-title"]'
                    locator = target_page.locator(sel)
                    text_val = ""
                    try:
                        # Prefer inner_text for preserving spacing/line breaks
                        text_val = locator.first.inner_text(timeout=1500).strip()
                    except Exception:
                        # Try text_content as fallback
                        try:
                            text_val = (locator.first.text_content(timeout=1500) or "").strip()
                        except Exception:
                            text_val = ""

                    if not text_val:
                        return ""

                    # Determine frequency from medication-text block or page HTML
                    freq_suffix = ", as-needed"
                    try:
                        detail_sel = get_selector(group, 'medication_detail', engine='playwright', selector_type='css') or '[data-testid="medication-text"]'
                        med_text_loc = target_page.locator(detail_sel)
                        med_text_lower = ""
                        try:
                            med_text_lower = (med_text_loc.first.inner_text(timeout=800) or "").lower()
                        except Exception:
                            med_text_lower = (target_page.content() or "").lower()
                        if "30 doses per month" in med_text_lower:
                            freq_suffix = ", daily"
                    except Exception:
                        pass

                    cleaned_text = self._sanitize_medication_line(text_val) or text_val.strip()
                    result = f"{cleaned_text}{freq_suffix}"
                    print(f"   ✅ Playwright medication-title: '{result}'")
                    return result
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"   ❌ Playwright connection/use error: {e}")
            return ""
    
    def _extract_medication_detail_text(self) -> str:
        selector = '[data-testid="medication-text"]'
        try:
            if not self._switch_to_frame_with_element(By.CSS_SELECTOR, selector, timeout_each=1.0, max_depth=3):
                self._switch_to_default()
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, selector)
                    return (el.get_attribute('innerText') or el.text or '').strip()
                except Exception:
                    return ""

            element = self.driver.find_element(By.CSS_SELECTOR, selector)
            return (element.get_attribute('innerText') or element.text or '').strip()
        except Exception as e:
            print(f"   ❌ Medication detail extraction error: {e}")
            return ""
        finally:
            try:
                self._switch_to_default()
            except Exception:
                pass

    def _extract_current_treatment_summary(self) -> Tuple[Optional[str], Optional[str]]:
        try:
            self._switch_to_default()
        except Exception:
            pass

        try:
            containers = self.driver.find_elements(By.CSS_SELECTOR, 'div.css-1hj5o6h')
        except Exception:
            containers = []

        for container in containers:
            try:
                rows = container.find_elements(By.CSS_SELECTOR, 'div.css-1rynq56')
            except Exception:
                continue
            if not rows:
                continue
            name = (rows[0].get_attribute('innerText') or rows[0].text or '').strip()
            detail = ''
            if len(rows) > 1:
                detail = (rows[1].get_attribute('innerText') or rows[1].text or '').strip()
            if name:
                return name, detail

        return None, None

    @staticmethod
    def _strip_frequency_suffix(value: Optional[str]) -> str:
        if not value:
            return ""
        trimmed = value.strip()
        lowered = trimmed.lower()
        for suffix in (', daily', ', as-needed'):
            if lowered.endswith(suffix):
                return trimmed[: -len(suffix)]
        return trimmed

    @staticmethod
    def _sanitize_medication_line(value: Optional[str]) -> str:
        if not value:
            return ""
        cleaned = value.replace('\xa0', ' ')
        cleaned = re.sub(r'\b(prn|as\s*[-]?\s*needed)\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'a\s*s(?:\s*[-]\s*)?n\s*e\s*e\s*d\s*e\s*d', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r',\s*(\d[\d\.]*\s*(?:mg|mcg|g|ml|units?|tabs?|caps?|puffs?|sprays?))', r', \1', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+,\s*', ', ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned)
        cleaned = cleaned.replace(' ,', ',')
        cleaned = cleaned.strip(' ,')
        return cleaned.strip()

    def _extract_effectiveness(self, full_text: Optional[str] = None):
        """Extract treatment effectiveness (Yes/No).
        Fast-first approach across multiple question phrasings; returns 'Yes'/'No' or None.
        """
        try:
            dprint("   🔍 Extracting treatment effectiveness…")
            t_start = time.perf_counter()

            def normalize_yes_no(value: Optional[str]) -> Optional[str]:
                if not value:
                    return None
                stripped = str(value).strip()
                if not stripped:
                    return None
                match = re.match(r'^\W*(yes|no)\b', stripped, re.IGNORECASE)
                if match:
                    return match.group(1).capitalize()
                condensed = re.sub(r'[^a-z]', '', stripped.lower())
                if condensed.startswith('yes'):
                    return 'Yes'
                if condensed.startswith('no'):
                    return 'No'
                return None

            # Fast text-anchored path across aliases
            try:
                latest_segment = self._get_latest_intake_text_segment()
            except Exception:
                latest_segment = None
            text_source = latest_segment or full_text or self._get_page_text() or ""
            if text_source:
                lines = [ln.strip() for ln in text_source.splitlines()]
                low = [ln.lower() for ln in lines]
                for alias in EFFECTIVENESS_QUESTION_ALIASES:
                    q = alias.lower()
                    anchors = [i for i, l in enumerate(low) if q in l]
                    if not anchors:
                        continue
                    start = anchors[0]
                    for i in range(start + 1, min(len(lines), start + 1 + 40)):
                        normalized = normalize_yes_no(lines[i])
                        if normalized:
                            return normalized

            # 1) Prefer chips/answers to the right of the question label
            for alias in EFFECTIVENESS_QUESTION_ALIASES:
                answers = self._get_selected_answers_by_question(alias)
                if answers:
                    for a in answers:
                        normalized = normalize_yes_no(a)
                        if normalized:
                            return normalized

            # 2) Try XPath anchored on the question text and grab the first Yes/No following it
            for alias in EFFECTIVENESS_QUESTION_ALIASES:
                q = alias.lower()
                question_xpath = (
                    "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '" + q + "')]"
                )
                question_el = self._find_element_in_frames_by_xpath(question_xpath, timeout_each=0.6, max_depth=2)
                if question_el is not None:
                    try:
                        answer = question_el.find_element(By.XPATH, "following::div[normalize-space(text())='Yes' or normalize-space(text())='No'][1]")
                        ans_txt = (answer.text or '').strip()
                        dprint(f"      Answer via question anchor: '{ans_txt}'")
                        normalized = normalize_yes_no(ans_txt)
                        if normalized:
                            return normalized
                    except Exception:
                        pass
                    try:
                        script = """
const start = arguments[0];
let node = start;
while (node && node !== document.body) {
  const candidates = node.querySelectorAll('[aria-pressed="true"],[aria-selected="true"],[role="radio"][aria-checked="true"],[role="switch"][aria-checked="true"],[data-state="on"],[data-selected="true"],[data-testid*="selected"]');
  for (const cand of candidates) {
    const txt = (cand.innerText || cand.textContent || '').trim();
    if (txt) {
      return txt;
    }
  }
  node = node.parentElement;
}
return null;
"""
                        attr_txt = self.driver.execute_script(script, question_el)
                        normalized = normalize_yes_no(attr_txt)
                        if normalized:
                            return normalized
                    except Exception:
                        pass

            # 2b) Attribute-based fallback for selected chips/buttons that expose aria state
            try:
                chip_candidates = self.driver.find_elements(
                    By.XPATH,
                    "//*[(@aria-pressed='true' or @aria-selected='true' or contains(@class,'selected')) and normalize-space(.)!='']"
                )
            except Exception:
                chip_candidates = []
            for el in chip_candidates[:40]:
                try:
                    txt = (el.get_attribute('innerText') or el.text or '').strip()
                except Exception:
                    continue
                normalized = normalize_yes_no(txt)
                if normalized:
                    return normalized

            # 3) Positional fallback: collect short texts to the right of the question; prefer Yes/No
            if (time.perf_counter() - t_start) > 0.9:
                return None
            for alias in EFFECTIVENESS_QUESTION_ALIASES:
                try:
                    near = self._get_right_half_visible_texts_near_question(alias)
                except Exception:
                    near = []
                for t in near:
                    normalized = normalize_yes_no(t)
                    if normalized:
                        return normalized

            # 4) Generic fallback: global Yes/No near treatment/effectiveness context
            yn_el = self._find_element_in_frames_by_xpath(
                "//div[(normalize-space(text())='Yes' or normalize-space(text())='No')][ancestor::*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'treatment') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'effectiv')]][1]",
                timeout_each=0.5,
                max_depth=2
            )
            if yn_el is not None:
                txt = (yn_el.text or '').strip()
                dprint(f"      Fallback Yes/No: '{txt}'")
                normalized = normalize_yes_no(txt)
                if normalized:
                    return normalized

            # 4b) Playwright fallback (off by default unless enabled)
            if USE_PLAYWRIGHT_FOR_SH and sync_playwright is not None:
                try:
                    val = self._extract_effectiveness_playwright()
                    if val:
                        dprint("      ✅ Effectiveness via Playwright fallback")
                        return val
                except Exception as e:
                    dprint(f"      ⚠️ Playwright effectiveness read failed: {e}")

            # 5) Last resort: scan common chip class across frames and pick a visible 'Yes'/'No'
            class_selectors = ['div.css-1rynq56.r-cqee49.r-b88u0q']
            for sel in class_selectors:
                try:
                    if self._switch_to_frame_with_element(By.CSS_SELECTOR, sel, timeout_each=0.5, max_depth=2):
                        elems = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        for el in elems[:120]:
                            try:
                                if not el.is_displayed():
                                    continue
                            except Exception:
                                pass
                            txt = (el.get_attribute('innerText') or el.text or '').strip()
                            normalized = normalize_yes_no(txt)
                            if normalized:
                                return normalized
                except Exception:
                    pass
                finally:
                    try:
                        self._switch_to_default()
                    except Exception:
                        pass

            # 6) Global chip harvest as an absolute fallback
            try:
                chips = self._collect_selected_chip_texts(max_depth=2)
            except Exception:
                chips = []
            for chip_txt in chips:
                normalized = normalize_yes_no(chip_txt)
                if normalized:
                    return normalized

        except Exception as e:
            dprint(f"   ❌ Effectiveness extraction error: {e}")
        
        return None

    def _extract_effectiveness_playwright(self) -> Optional[str]:
        """Use Playwright (existing Chrome via CDP) to read the Yes/No chip near the effectiveness question."""
        try:
            if sync_playwright is None:
                return None
            with sync_playwright() as p:  # type: ignore
                cdp_url = f"http://127.0.0.1:{CDP_DEBUG_PORT}"
                browser = p.chromium.connect_over_cdp(cdp_url)
                try:
                    target_page = None
                    for context in browser.contexts:
                        for page in context.pages:
                            url = page.url or ""
                            if self._is_emr_url(url):
                                target_page = page
                                break
                        if target_page:
                            break
                    if not target_page:
                        return None

                    # Try each known alias; also include provided concrete labels
                    aliases = list(EFFECTIVENESS_QUESTION_ALIASES) + [
                        "How’s your sexual health treatment going so far?",
                        "Are you getting the results you want?",
                    ]
                    for alias in aliases:
                        try:
                            # Anchor on the question text (case-insensitive) via XPath, then find the row and right column chips
                            q_xpath = (
                                "xpath=//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '"
                                + alias.lower()
                                + "')]"
                            )
                            qloc = target_page.locator(q_xpath).first
                            if not qloc or qloc.count() == 0:
                                continue
                            # Climb to row, then to right column
                            row = qloc.locator(
                                "xpath=ancestor::div[contains(@class,'py-4')][1]"
                            )
                            right = row.locator(
                                "css=div.flex-1.w-auto, div.flex-1 >> nth=-1"
                            )
                            chip = right.locator("css=div.css-1rynq56.r-cqee49.r-b88u0q").first
                            txt = (chip.text_content(timeout=1000) or "").strip()
                            if txt and txt.lower() in ("yes", "no"):
                                return txt.capitalize()
                        except Exception:
                            continue

                    # Fallback: provided selector path without anchoring
                    try:
                        chip2 = target_page.locator(".flex-1 > .css-1hj5o6h > .css-1rynq56").first
                        txt2 = (chip2.text_content(timeout=1000) or "").strip()
                        if txt2 and txt2.lower() in ("yes", "no"):
                            return txt2.capitalize()
                    except Exception:
                        pass
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception:
            return None
        return None

    # --- Robust DOM click for "Get next task" ---
    def _collect_get_next_task_selectors(self) -> Tuple[List[str], List[str]]:
        """Gather unique CSS and XPath selectors for the next-task button."""
        css_selectors: List[str] = []
        xpath_selectors: List[str] = []

        def push(target_list: List[str], value: Optional[str]) -> None:
            if not value:
                return
            cleaned = value.strip()
            if cleaned and cleaned not in target_list:
                target_list.append(cleaned)

        for engine in ("selenium", "playwright"):
            try:
                push(css_selectors, get_selector('emr', 'get_next_task', engine=engine, selector_type='css'))
            except Exception:
                pass
            try:
                push(xpath_selectors, get_selector('emr', 'get_next_task', engine=engine, selector_type='xpath'))
            except Exception:
                pass
            try:
                selector_list = get_selector_list('emr', 'get_next_task', engine=engine)
                if selector_list:
                    for entry in selector_list:
                        sel_type = (entry or {}).get('type')
                        sel_value = (entry or {}).get('selector')
                        if sel_type == 'css':
                            push(css_selectors, sel_value)
                        elif sel_type == 'xpath':
                            push(xpath_selectors, sel_value)
            except Exception:
                pass

        fallback_css = [
            '[data-testid="getNextTaskButton"]',
            'button[data-testid="getNextTaskButton"]',
            'div[data-testid="getNextTaskButton"]',
            '#getNextTask',
        ]
        fallback_xpath = [
            '//*[@data-testid="getNextTaskButton" and @role="button"]',
            '//*[@id="getNextTask"]/ancestor::*[@role="button"][1]',
        ]

        for sel in fallback_css:
            push(css_selectors, sel)
        for xp in fallback_xpath:
            push(xpath_selectors, xp)

        return css_selectors, xpath_selectors

    def click_get_next_task(self, max_retries: int = 3) -> bool:
        """Attempt to click the 'Get next task' button in the EMR UI.
        Returns True on success, False on failure.
        """
        if not self.driver:
            if not self.connect_to_chrome():
                return False

        try:
            if not self._ensure_emr_tab():
                return False

            # Try Playwright first if available and preferred
            if USE_PLAYWRIGHT_FOR_MED and sync_playwright is not None:
                try:
                    playwright_success = self._click_get_next_task_playwright()
                    if playwright_success:
                        return True
                    print("   ⚠️ Playwright primary click failed; trying alternate strategy")
                except Exception as e:
                    print(f"   ⚠️ Playwright click error; trying alternate strategy: {e}")

            wait = WebDriverWait(self.driver, 4)

            selectors: List[Tuple[str, str]] = []
            css_selectors, xpath_selectors = self._collect_get_next_task_selectors()
            for sel in css_selectors:
                selectors.append((By.CSS_SELECTOR, sel))
            for xp in xpath_selectors:
                selectors.append((By.XPATH, xp))

            def try_click(el) -> bool:
                try:
                    el.click()
                    return True
                except Exception:
                    pass
                try:
                    el.scroll_into_view()
                    el.click()
                    return True
                except Exception:
                    pass
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                    self.driver.execute_script("arguments[0].click();", el)
                    return True
                except Exception:
                    return False

            # Try default context and then dive into iframes
            for attempt in range(max_retries):
                # Default content
                self._switch_to_default()
                for by, sel in selectors:
                    try:
                        el = wait.until(EC.presence_of_element_located((by, sel)))
                        if try_click(el):
                            # Clear caches when navigation to next task is triggered
                            try:
                                self._reset_text_caches()
                            except Exception:
                                pass
                            return True
                    except Exception:
                        continue

                # Look through top-level iframes
                try:
                    frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
                except Exception:
                    frames = []
                for fr in frames:
                    try:
                        self.driver.switch_to.frame(fr)
                        for by, sel in selectors:
                            try:
                                el = wait.until(EC.presence_of_element_located((by, sel)))
                                if try_click(el):
                                    try:
                                        self._reset_text_caches()
                                    except Exception:
                                        pass
                                    return True
                            except Exception:
                                continue
                    except Exception:
                        pass
                    finally:
                        try:
                            self.driver.switch_to.parent_frame()
                        except Exception:
                            pass

            return False
        except Exception as e:
            print(f"click_get_next_task error: {e}")
            return False

    @staticmethod
    def _build_cdp_click_expression(css_selectors: List[str], xpath_selectors: List[str]) -> str:
        css_json = json.dumps(css_selectors)
        xpath_json = json.dumps(xpath_selectors)
        return (
            """
(() => {
  const cssSelectors = __CSS__;
  const xpathSelectors = __XPATH__;

  const tryClick = (node) => {
    if (!node) return false;
    try {
      node.scrollIntoView({block: 'center', behavior: 'instant'});
    } catch (e) {}
    try {
      node.click();
      return true;
    } catch (e) {}
    try {
      const evt = new MouseEvent('click', {bubbles: true, cancelable: true, composed: true});
      node.dispatchEvent(evt);
      return true;
    } catch (e) {}
    return false;
  };

  for (const sel of cssSelectors) {
    try {
      const el = document.querySelector(sel);
      if (el && tryClick(el)) return true;
    } catch (e) {}
  }

  for (const xp of xpathSelectors) {
    try {
      const res = document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      for (let i = 0; i < res.snapshotLength; i += 1) {
        const node = res.snapshotItem(i);
        if (node && tryClick(node)) return true;
      }
    } catch (e) {}
  }

  return false;
})();
"""
        ).replace("__CSS__", css_json).replace("__XPATH__", xpath_json)

    def click_get_next_task_cdp(self) -> bool:
        """Use CDP Runtime.evaluate to activate the next-task button."""
        if not self.driver and not self.connect_to_chrome():
            return False

        try:
            if not self._ensure_emr_tab():
                return False

            css_selectors, xpath_selectors = self._collect_get_next_task_selectors()
            if not css_selectors and not xpath_selectors:
                return False

            expr = self._build_cdp_click_expression(css_selectors, xpath_selectors)
            result = self.driver.execute_cdp_cmd(
                'Runtime.evaluate',
                {
                    'expression': expr,
                    'returnByValue': True,
                    'awaitPromise': True,
                    'userGesture': True,
                },
            )
            success = bool(((result or {}).get('result', {}) or {}).get('value'))
            if success:
                try:
                    self._reset_text_caches()
                except Exception:
                    pass
            return success
        except Exception as e:
            print(f"click_get_next_task_cdp error: {e}")
            return False

    def _click_get_next_task_playwright(self) -> bool:
        """Use Playwright to click the 'Get next task' button.
        Returns True on success, False on failure.
        """
        try:
            if sync_playwright is None:
                return False
                
            with sync_playwright() as p:  # type: ignore
                cdp_url = f"http://127.0.0.1:{CDP_DEBUG_PORT}"
                browser = p.chromium.connect_over_cdp(cdp_url)
                try:
                    target_page = None
                    # Find an EMR page among existing contexts/pages
                    for context in browser.contexts:
                        for page in context.pages:
                            url = page.url or ""
                            if self._is_emr_url(url):
                                target_page = page
                                break
                        if target_page:
                            break

                    if not target_page:
                        print("   ⚠️ Playwright: EMR page not found in existing Chrome session")
                        return False

                    # Get selector from grab_points.py for Playwright
                    try:
                        playwright_selector = get_selector('emr', 'get_next_task', engine='playwright', selector_type='css')
                        if not playwright_selector:
                            # Fallback to generic selector
                            playwright_selector = '[data-testid="getNextTaskButton"]'
                    except Exception:
                        playwright_selector = '[data-testid="getNextTaskButton"]'

                    # Try to click the button
                    locator = target_page.locator(playwright_selector)
                    locator.first.click(timeout=3000)
                    print(f"   ✅ Playwright clicked 'Get next task' button using selector: {playwright_selector}")
                    return True
                    
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"   ❌ Playwright 'Get next task' click error: {e}")
            return False

            # Try default context and then dive into iframes
            for attempt in range(max_retries):
                # Default content
                self._switch_to_default()
                for by, sel in selectors:
                    try:
                        el = wait.until(EC.presence_of_element_located((by, sel)))
                        if try_click(el):
                            return True
                    except Exception:
                        continue

                # Look through top-level iframes
                try:
                    frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
                except Exception:
                    frames = []
                for fr in frames:
                    try:
                        self.driver.switch_to.frame(fr)
                        for by, sel in selectors:
                            try:
                                el = wait.until(EC.presence_of_element_located((by, sel)))
                                if try_click(el):
                                    return True
                            except Exception:
                                continue
                    except Exception:
                        pass
                    finally:
                        try:
                            self.driver.switch_to.parent_frame()
                        except Exception:
                            pass

            return False
        except Exception as e:
            print(f"click_get_next_task error: {e}")
            return False
    
    def _extract_blood_pressure(self):
        """Extract blood pressure information from the provided CSS class.
        Prefer exact element text like '90-139/50-80' or a reading like '120/80'.
        """
        try:
            # 0) Fast text path with strict time budget to avoid slow DOM traversals
            fast_start = time.perf_counter()
            def _bp_from_text(text: str) -> Optional[str]:
                if not text:
                    return None
                sentinel = "None of the above - Patient is not required to report BP"
                if sentinel in text:
                    return 'nr'
                # Plausible BP regexes
                dash = r"[-\u2012\u2013\u2014\u2212]"
                range_pat = re.compile(rf"\b(\d{{2,3}})\s*{dash}\s*(\d{{2,3}})\s*/\s*(\d{{2,3}})\s*{dash}\s*(\d{{2,3}})\b")
                single_pat = re.compile(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b(?!\s*/\s*\d{2,4})")
                # Quick scan lines near likely anchors
                lines = [ln.strip() for ln in text.splitlines()]
                low = [ln.lower() for ln in lines]
                anchors = []
                for i, l in enumerate(low):
                    if ('blood pressure' in l) or (' bp' in l) or ('bp:' in l) or ('blood-pressure' in l):
                        anchors.append(i)
                windows = []
                for a in anchors or [0]:
                    windows.append((max(0, a), min(len(lines), a + 80)))
                for start, end in windows[:3]:
                    segment = "\n".join(lines[start:end])
                    m = range_pat.search(segment)
                    if m:
                        s1, s2, d1, d2 = map(int, m.groups())
                        if (70 <= s1 <= 250 and 70 <= s2 <= 250 and 30 <= d1 <= 150 and 30 <= d2 <= 150 and s1 <= s2 and d1 <= d2):
                            return f"{s1}-{s2}/{d1}-{d2}"
                    m = single_pat.search(segment)
                    if m:
                        s, d = map(int, m.groups())
                        if 70 <= s <= 250 and 30 <= d <= 150 and s > d:
                            return f"{s}/{d}"
                return None

            if FAST_MODE_BP:
                # Prefer cached latest segment; avoid frame traversal in Text-only mode
                try:
                    pre_text = self._get_latest_intake_text_segment()
                except Exception:
                    pre_text = None
                if not pre_text:
                    try:
                        if USE_PLAYWRIGHT_FOR_SH:
                            pre_text = self._get_all_text_across_frames(max_frames=3)
                        else:
                            pre_text = self._get_page_text() or ''
                    except Exception:
                        pre_text = ''
                quick = _bp_from_text(pre_text)
                # Optional broader check on full body text if latest segment didn't have it
                if not quick:
                    try:
                        body_text = self._get_page_text() or ''
                    except Exception:
                        body_text = ''
                    if body_text and body_text != pre_text:
                        quick = _bp_from_text(body_text)
                if quick:
                    print(f"   ✅ BP (fast text): '{quick}' ({(time.perf_counter()-fast_start)*1000:.0f} ms)")
                    return quick
                # Playwright fallback before giving up in fast mode
                if USE_PLAYWRIGHT_FOR_SH and sync_playwright is not None:
                    try:
                        val = self._extract_blood_pressure_playwright()
                        if val:
                            print(f"   ✅ BP (Playwright): '{val}'")
                            return val
                    except Exception as e:
                        print(f"   ⚠️ BP Playwright fallback failed: {e}")
                # In fast mode, skip slow DOM scans entirely
                elapsed_ms = (time.perf_counter()-fast_start)*1000
                print(f"   ⏭️ BP fast mode (no match in text after {elapsed_ms:.0f} ms) → returning 'nr'")
                return 'nr'

            print("   🔍 Extracting blood pressure via class selector…")
            latest_segment = self._get_latest_intake_text_segment() or ''
            latest_segment_lower = latest_segment.lower()
            class_selectors = [
                'div.css-1rynq56.r-cqee49.r-b88u0q',  # Provided example
            ]
            sentinel = "None of the above - Patient is not required to report BP"

            # Patterns: range (e.g., 90-139/50-80) or single reading (e.g., 120/80), allow units like mmHg
            dash = r"[-\u2012\u2013\u2014\u2212]"  # common dashes
            range_pat_inline = re.compile(rf"\b(\d{{2,3}})\s*{dash}\s*(\d{{2,3}})\s*/\s*(\d{{2,3}})\s*{dash}\s*(\d{{2,3}})\b", re.IGNORECASE)
            single_pat_inline = re.compile(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b(?!\s*/\s*\d{2,4})", re.IGNORECASE)

            def normalize_bp_range(m):
                s1, s2, d1, d2 = map(int, m.groups())
                if (70 <= s1 <= 250 and 70 <= s2 <= 250 and 30 <= d1 <= 150 and 30 <= d2 <= 150 and s1 <= s2 and d1 <= d2):
                    return f"{s1}-{s2}/{d1}-{d2}"
                return None

            def normalize_bp_single(m):
                s, d = map(int, m.groups())
                if 70 <= s <= 250 and 30 <= d <= 150 and s > d:
                    return f"{s}/{d}"
                return None

            def pick_bp_text(candidates):
                # First pass: prefer candidates that also appear in the latest intake segment
                prioritized = []
                secondary = []
                for t in candidates:
                    st = (t or '').strip()
                    if not st:
                        continue
                    # Sentinel meaning BP not required
                    if st.strip() == sentinel:
                        return 'nr'
                    target = prioritized if (latest_segment_lower and st.lower() in latest_segment_lower) else secondary
                    # Inline match anywhere in the string
                    m = range_pat_inline.search(st)
                    if m:
                        norm = normalize_bp_range(m)
                        if norm:
                            target.append(norm)
                            continue
                    m = single_pat_inline.search(st)
                    if m:
                        norm = normalize_bp_single(m)
                        if norm:
                            target.append(norm)
                            continue
                # Return by priority
                if prioritized:
                    return prioritized[0]
                if secondary:
                    return secondary[0]
                return None

            # 0) Anchored on the BP question: use selected chips near the label
            for q in [
                'what was your last blood pressure reading',
                'what is your current blood pressure',
                'blood pressure reading',
            ]:
                try:
                    answers = self._get_selected_answers_by_question(q)
                except Exception:
                    answers = []
                if answers:
                    chosen = pick_bp_text(answers)
                    if chosen:
                        print(f"   ✅ Blood pressure (answers near question '{q}'): '{chosen}'")
                        return chosen
                # Positional right-half text near question
                try:
                    near = self._get_right_half_visible_texts_near_question(q)
                except Exception:
                    near = []
                if near:
                    chosen = pick_bp_text(near)
                    if chosen:
                        print(f"   ✅ Blood pressure (positional near '{q}'): '{chosen}'")
                        return chosen

            # 1) Try within a frame that contains the selector
            for sel in class_selectors:
                try:
                    if self._switch_to_frame_with_element(By.CSS_SELECTOR, sel, timeout_each=1.0, max_depth=3):
                        elems = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        texts = [(e.get_attribute('innerText') or e.text or '').strip() for e in elems]
                        # First try prioritizing by latest segment, then fallback to any
                        texts_pref = [t for t in texts if t and latest_segment_lower and t.lower() in latest_segment_lower]
                        texts_any = [t for t in texts if t]
                        texts = texts_pref or texts_any
                        print(f"      Found {len(texts)} candidates in-frame for '{sel}' -> {texts[:3]}")
                        chosen = pick_bp_text(texts)
                        if chosen:
                            print(f"   ✅ Blood pressure: '{chosen}'")
                            return chosen
                except Exception as e:
                    print(f"      Selector '{sel}' search error: {e}")
                finally:
                    self._switch_to_default()

            # 2) Broader scan: default content then top-level iframes
            try:
                self._switch_to_default()
                elems = self.driver.find_elements(By.CSS_SELECTOR, 'div.css-1rynq56.r-cqee49.r-b88u0q')
                texts = [(e.get_attribute('innerText') or e.text or '').strip() for e in elems]
                texts_pref = [t for t in texts if t and latest_segment_lower and t.lower() in latest_segment_lower]
                texts = texts_pref or [t for t in texts if t]
                chosen = pick_bp_text(texts)
                if chosen:
                    print(f"   ✅ Blood pressure (default): '{chosen}'")
                    return chosen
            except Exception:
                pass

            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for fr in frames[:4]:
                try:
                    self.driver.switch_to.frame(fr)
                    elems = self.driver.find_elements(By.CSS_SELECTOR, 'div.css-1rynq56.r-cqee49.r-b88u0q')
                    texts = [(e.get_attribute('innerText') or e.text or '').strip() for e in elems]
                    texts_pref = [t for t in texts if t and latest_segment_lower and t.lower() in latest_segment_lower]
                    texts = texts_pref or [t for t in texts if t]
                    chosen = pick_bp_text(texts)
                    if chosen:
                        print(f"   ✅ Blood pressure (frame): '{chosen}'")
                        return chosen
                except Exception:
                    pass
                finally:
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass

            # 3) Fallback to body text search. First handle sentinel and page-type logic,
            #    then try regex with date/physiology guards to avoid false positives like '11/09'.
            body = latest_segment if latest_segment else (self._get_page_text() or '')
            if sentinel in body:
                print("   ✅ BP sentinel text found in body; returning 'nr'")
                return 'nr'

            # If we're on a Sexual Health page/template but there is no BP content at all,
            # ensure we explicitly return 'nr' BEFORE trying to mine numeric patterns.
            # Use cached visit type (set elsewhere) to avoid expensive detection here
            try:
                visit = self._cache_visit_type if (time.time() - self._cache_visit_type_time) < 10 else None
            except Exception:
                visit = None
            low_body = body.lower()
            # Look for common BP keywords/tokens
            bp_token_present = (
                ("blood pressure" in low_body) or
                ("blood-pressure" in low_body) or
                ("bloodpressure" in low_body) or
                (" bp " in low_body) or
                ("bp:" in low_body) or
                (" bp:" in low_body)
            )
            if (visit == 'Sexual Health') and not bp_token_present:
                print("   ✅ No BP questions or values detected on Sexual Health page; defaulting to 'nr'")
                return 'nr'

            # Try range-like first with plausibility checks
            range_pat = re.compile(r"\b(\d{2,3})\s*-\s*(\d{2,3})\s*/\s*(\d{2,3})\s*-\s*(\d{2,3})\b")
            for m in range_pat.finditer(body):
                s1, s2, d1, d2 = map(int, m.groups())
                if (70 <= s1 <= 250 and 70 <= s2 <= 250 and 30 <= d1 <= 150 and 30 <= d2 <= 150
                        and s1 <= s2 and d1 <= d2):
                    val = m.group(0).strip()
                    print(f"   🎯 BP range candidate accepted: '{val}'")
                    return val
                else:
                    print(f"   ↩︎ Ignoring implausible BP range '{m.group(0)}'")

            # Then single reading like 120/80, but avoid dates like 11/09/1975 using negative lookahead
            single_pat = re.compile(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b(?!\s*/\s*\d{2,4})")
            for m in single_pat.finditer(body):
                s, d = map(int, m.groups())
                if 70 <= s <= 250 and 30 <= d <= 150 and s > d:
                    val = m.group(0).strip()
                    print(f"   🎯 BP single candidate accepted: '{val}'")
                    return val
                else:
                    print(f"   ↩︎ Ignoring implausible BP single '{m.group(0)}'")

        except Exception as e:
            print(f"   ❌ Blood pressure extraction error: {e}")

        return 'nr'  # Default value if nothing matched

    def _extract_blood_pressure_playwright(self) -> Optional[str]:
        """Use Playwright to read the selected BP chip near the BP question."""
        try:
            if sync_playwright is None:
                return None
            with sync_playwright() as p:  # type: ignore
                cdp_url = f"http://127.0.0.1:{CDP_DEBUG_PORT}"
                browser = p.chromium.connect_over_cdp(cdp_url)
                try:
                    target_page = None
                    for context in browser.contexts:
                        for page in context.pages:
                            url = page.url or ""
                            if self._is_emr_url(url):
                                target_page = page
                                break
                        if target_page:
                            break
                    if not target_page:
                        return None

                    # Anchor on question and fetch right-hand chip text
                    q = "What was your last blood pressure reading?"
                    try:
                        qloc = target_page.locator(
                            "xpath=//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '" + q.lower() + "')]"
                        ).first
                        row = qloc.locator("xpath=ancestor::div[contains(@class,'py-4')][1]")
                        right = row.locator("css=div.flex-1.w-auto, div.flex-1 >> nth=-1")
                        chip = right.locator("css=div.css-1rynq56.r-cqee49.r-b88u0q").first
                        txt = (chip.text_content(timeout=1000) or "").strip()
                        if txt:
                            return txt
                    except Exception:
                        pass

                    # Fallback: grab first visible chip text that looks like BP
                    try:
                        chips = target_page.locator("div.css-1rynq56.r-cqee49.r-b88u0q")
                        for i in range(min(chips.count(), 20)):
                            t = (chips.nth(i).text_content(timeout=500) or "").strip()
                            if not t:
                                continue
                            if re.search(r"\d{2,3}\s*/\s*\d{2,3}", t) or "90/50-80" in t or "139/90" in t:
                                return t
                    except Exception:
                        pass
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception:
            return None
        return None
    
    def _extract_diagnoses(self):
        """Extract Sexual Health diagnoses with robust mapping and negation handling.
        Returns a list containing any of: 'ED', 'PE', 'PE-like ejaculatory dysfunction'.
        Priority:
        - Selected chips near likely diagnosis/assessment questions
        - Text parsing on latest intake or shallow page text across frames
        """
        try:
            # Canonical specs: synonyms/patterns and simple negations
            specs = [
                {
                    'canonical': 'ED',
                    'syn': [
                        r"\bED\b",
                        r"\bE\.D\.\b",
                        r"erectile\s+dysfunction",
                        r"erectile\s+disorder",
                        r"impotence",
                        r"erection[^\n\r]{0,30}dysfunction",
                    ],
                    'neg': [
                        r"denies?[^\n\r]{0,30}(?:ed|erectile\s+(?:dysfunction|disorder)|impotence)",
                        r"no[^\n\r]{0,20}(?:ed|erectile\s+(?:dysfunction|disorder)|impotence)",
                        r"without[^\n\r]{0,20}(?:ed|erectile\s+(?:dysfunction|disorder)|impotence)",
                    ],
                    'codes': ['n52', 'n52.9'],
                },
                {
                    'canonical': 'PE',
                    'syn': [
                        r"\bPE\b",
                        r"\bP\.E\.\b",
                        r"premature\s+ejaculation",
                        r"early\s+ejaculation",
                        r"rapid\s+ejaculation",
                        r"climax[^\n\r]{0,20}(?:too\s+)?soon",
                        r"premature\s+climax",
                    ],
                    'neg': [
                        r"denies?[^\n\r]{0,30}(?:pe|premature\s+ejaculation|early\s+ejaculation)",
                        r"no[^\n\r]{0,20}(?:pe|premature\s+ejaculation)",
                        r"without[^\n\r]{0,20}(?:pe|premature\s+ejaculation)",
                    ],
                    'codes': ['f52.4', 'f52.3', 'f52.8'],
                },
                {
                    'canonical': 'PE-like ejaculatory dysfunction',
                    'syn': [
                        r"pe-like\s+ejaculatory\s+dysfunction",
                        r"ejaculatory\s+dysfunction",
                        r"climax[^\n\r]{0,30}dysfunction",
                        r"ejaculatory\s+disorder",
                        r"ejaculatory\s+dysfunction\s+\(pe-like\)",
                    ],
                    'neg': [
                        r"denies?[^\n\r]{0,30}ejaculatory\s+(?:dysfunction|disorder)",
                        r"no[^\n\r]{0,20}ejaculatory\s+(?:dysfunction|disorder)",
                        r"without[^\n\r]{0,20}ejaculatory\s+(?:dysfunction|disorder)",
                    ],
                    'codes': ['f52.21', 'f52.22', 'f52.6'],
                },
                {
                    'canonical': 'Hair Loss',
                    'syn': [
                        r"\bhair loss\b",
                        r"male\s+pattern\s+hair\s+loss",
                        r"\bmhpl\b",
                        r"androgenic\s+alopecia",
                    ],
                    'neg': [
                        r"denies?[^\n\r]{0,30}hair\s+loss",
                        r"no[^\n\r]{0,20}hair\s+loss",
                        r"without[^\n\r]{0,20}hair\s+loss",
                    ],
                    'codes': ['l64', 'l64.0', 'l64.1'],
                },
            ]

            def map_to_canonical(texts: list[str]) -> list[str]:
                out: list[str] = []
                seen: set[str] = set()
                cleaned = [(t.strip(), t.strip().lower()) for t in texts if t and t.strip()]
                if not cleaned:
                    return out
                for spec in specs:
                    canon = spec['canonical']
                    code_seen = False
                    code_without_neg = False
                    syn_without_neg = False
                    for original, lower in cleaned:
                        has_code = any(code in lower for code in spec.get('codes', []))
                        has_syn = any(re.search(p, original, re.IGNORECASE) for p in spec['syn'])
                        if not has_code and not has_syn:
                            continue
                        has_neg = any(re.search(p, original, re.IGNORECASE) for p in spec['neg'])
                        if has_code:
                            code_seen = True
                            if not has_neg:
                                code_without_neg = True
                        if has_syn and not has_neg:
                            syn_without_neg = True
                    if syn_without_neg or code_without_neg or code_seen:
                        key = canon.lower()
                        if key not in seen:
                            out.append(canon)
                            seen.add(key)
                return out

            # 1) Prefer selected chips near diagnosis/assessment questions
            question_aliases = [
                'diagnosis',
                'diagnoses',
                'assessment',
                'condition',
                'primary diagnosis',
                'final diagnosis',
                'visit diagnosis',
                'visit diagnoses',
                'selected diagnoses',
                'icd',
                'icd-10',
                'assessment & plan',
                'assessment and plan',
            ]
            try:
                chip_texts: list[str] = []
                for q in question_aliases:
                    ans = self._get_selected_answers_by_question(q)
                    if ans:
                        print(f"[DX] Question '{q}' answers: {ans}")
                        chip_texts.extend(ans)
                if chip_texts:
                    mapped = map_to_canonical(chip_texts)
                    print(f"[DX] Mapped diagnoses from question chips: {mapped}")
                    if mapped:
                        return mapped
            except Exception:
                pass

            # 1b) Page-wide chip sweep when anchors fail
            try:
                chip_texts_any = self._collect_selected_chip_texts()
                if chip_texts_any:
                    print(f"[DX] Page-wide chip texts: {chip_texts_any}")
                    mapped_any = map_to_canonical(chip_texts_any)
                    print(f"[DX] Mapped diagnoses from page-wide chips: {mapped_any}")
                    if mapped_any:
                        return mapped_any
            except Exception:
                pass

            # 1c) Selected checkbox/option labels across frames
            try:
                checkbox_labels = self._collect_selected_checkbox_labels()
                if checkbox_labels:
                    print(f"[DX] Selected checkbox labels: {checkbox_labels}")
                    mapped_labels = map_to_canonical(checkbox_labels)
                    print(f"[DX] Mapped diagnoses from checkbox labels: {mapped_labels}")
                    if mapped_labels:
                        return mapped_labels
            except Exception:
                pass

            # 1d) Explicit clinical notes extraction (S/O/A/P blocks)
            try:
                # Ensure clinical notes are visible by clicking the Notes tab/header if present
                try:
                    self._click_notes_tab_if_present()
                except Exception as e:
                    print(f"[DX] Notes tab click skipped: {e}")

                note_texts = self._collect_note_texts()
                if note_texts:
                    print(f"[DX] Clinical note texts found: {len(note_texts)} entries")
                    mapped_notes = map_to_canonical(note_texts)
                    print(f"[DX] Mapped diagnoses from notes: {mapped_notes}")
                    if mapped_notes:
                        return mapped_notes
            except Exception:
                pass

            # 2) Text parsing using latest intake segment or shallow page text across frames
            try:
                text = self._get_latest_intake_text_segment()
            except Exception:
                text = None
            if not text:
                try:
                    # Skip iframe traversal in Text-only mode for speed
                    text = (self._get_all_text_across_frames(max_frames=3) if USE_PLAYWRIGHT_FOR_SH else (self._get_page_text() or ''))
                except Exception:
                    text = ''

            if text:
                print(f"[DX] Fallback text preview: {text[:160]!r}")
                text_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                mapped = map_to_canonical(text_lines + [text])
                print(f"[DX] Mapped diagnoses from fallback text: {mapped}")
                if mapped:
                    return mapped

            # 3) Playwright fallback (notes scan via data-testid)
            if USE_PLAYWRIGHT_FOR_SH and sync_playwright is not None:
                try:
                    mapped_pl = self._extract_diagnoses_playwright()
                    if mapped_pl:
                        return mapped_pl
                except Exception:
                    pass

        except Exception as e:
            print(f"   ❌ Diagnosis extraction error: {e}")
        return []

    def _extract_diagnoses_playwright(self) -> list[str]:
        """Use Playwright to read recent note contents and map 'A:' lines to canonical diagnoses."""
        try:
            if sync_playwright is None:
                return []
            with sync_playwright() as p:  # type: ignore
                cdp_url = f"http://127.0.0.1:{CDP_DEBUG_PORT}"
                browser = p.chromium.connect_over_cdp(cdp_url)
                try:
                    target_page = None
                    for context in browser.contexts:
                        for page in context.pages:
                            url = page.url or ""
                            if self._is_emr_url(url):
                                target_page = page
                                break
                        if target_page:
                            break
                    if not target_page:
                        return []
                    # Collect note texts
                    notes = []
                    try:
                        loc = target_page.locator("[data-testid^='note-content-']")
                        count = loc.count()
                        for i in range(min(count, 10)):
                            txt = (loc.nth(i).text_content(timeout=800) or "").strip()
                            if txt:
                                notes.append(txt)
                    except Exception:
                        pass
                    if not notes:
                        return []
                    blob = "\n".join(notes)
                    # Extract 'A:' lines
                    assessments = []
                    for line in blob.splitlines():
                        l = line.strip()
                        if l.lower().startswith('a:'):
                            assessments.append(l[2:].strip())
                    search_texts = assessments if assessments else [blob]
                    # Map using same canonicalization as main method
                    specs = [
                        {
                            'canonical': 'ED',
                            'syn': [r"\bED\b", r"\bE\.D\.", r"erectile\s+dysfunction", r"impotence", r"erection[^\n\r]{0,30}dysfunction"],
                            'neg': [r"denies?[^\n\r]{0,30}(?:ed|erectile\s+dysfunction|impotence)", r"no[^\n\r]{0,20}(?:ed|erectile\s+dysfunction|impotence)", r"without[^\n\r]{0,20}(?:ed|erectile\s+dysfunction|impotence)"],
                        },
                        {
                            'canonical': 'PE',
                            'syn': [r"\bPE\b", r"\bP\.E\.", r"premature\s+ejaculation", r"early\s+ejaculation", r"rapid\s+ejaculation", r"climax[^\n\r]{0,20}(?:too\s+)?soon"],
                            'neg': [r"denies?[^\n\r]{0,30}(?:pe|premature\s+ejaculation|early\s+ejaculation)", r"no[^\n\r]{0,20}(?:pe|premature\s+ejaculation)", r"without[^\n\r]{0,20}(?:pe|premature\s+ejaculation)"],
                        },
                        {
                            'canonical': 'PE-like ejaculatory dysfunction',
                            'syn': [r"pe-like\s+ejaculatory\s+dysfunction", r"ejaculatory\s+dysfunction", r"climax[^\n\r]{0,30}dysfunction", r"ejaculatory\s+disorder"],
                            'neg': [r"denies?[^\n\r]{0,30}ejaculatory\s+(?:dysfunction|disorder)", r"no[^\n\r]{0,20}ejaculatory\s+(?:dysfunction|disorder)", r"without[^\n\r]{0,20}ejaculatory\s+(?:dysfunction|disorder)"],
                        },
                        {
                            'canonical': 'Hair Loss',
                            'syn': [r"\bhair loss\b", r"male\s+pattern\s+hair\s+loss", r"\bmhpl\b", r"androgenic\s+alopecia"],
                            'neg': [r"denies?[^\n\r]{0,30}hair\s+loss", r"no[^\n\r]{0,20}hair\s+loss", r"without[^\n\r]{0,20}hair\s+loss"],
                        },
                    ]
                    out: list[str] = []
                    seen = set()
                    for text in search_texts:
                        for spec in specs:
                            if any(re.search(p, text, re.IGNORECASE) for p in spec['neg']):
                                continue
                            if any(re.search(p, text, re.IGNORECASE) for p in spec['syn']):
                                k = spec['canonical'].lower()
                                if k not in seen:
                                    out.append(spec['canonical'])
                                    seen.add(k)
                    return out
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception:
            return []
        return []
    
    def _extract_hair_loss_location(self) -> str:
        """Extract hair loss location from the EMR page.
        Uses fallback selector to find the specific element for hair loss location.
        """
        try:
            self._switch_to_default()
            
            # Possible hair loss location options
            location_patterns = [
                "Thinning at the hairline",
                "Thinning on the top of the head",
                "Bald patches, smooth and hairless not at the top of the head",
                "Redness and irritation found at sites of hair loss",
                "I'll take a photo of my head instead"
            ]
            
            # Try the specific fallback selector first
            fallback_selector = "div:nth-of-type(1) > div:nth-of-type(4) > div > div:nth-of-type(2) > div:nth-of-type(1) > div > div > div > div > div > div > div:nth-of-type(4) > div:nth-of-type(2) > div:nth-of-type(2) > div > div:nth-of-type(44) > div > div:nth-of-type(2) > div:nth-of-type(1) > div:nth-of-type(1)"
            
            try:
                element = self.driver.find_element(By.CSS_SELECTOR, fallback_selector)
                text = (element.get_attribute('innerText') or element.text or '').strip()
                if text in location_patterns:
                    print(f"   ✅ Found hair loss location via fallback selector: '{text}'")
                    return text
            except Exception as e:
                print(f"   ⚠️ Fallback selector failed: {e}")
            
            # If fallback fails, search all elements with the generic class
            elements = self.driver.find_elements(By.CSS_SELECTOR, "div.css-1rynq56.r-cqee49.r-b88u0q")
            print(f"   🔍 Searching {len(elements)} elements for hair loss location...")
            
            # Collect all matching text
            matches = []
            for element in elements:
                try:
                    text = (element.get_attribute('innerText') or element.text or '').strip()
                    if text in location_patterns:
                        matches.append(text)
                        print(f"   ✅ Found match: '{text}'")
                except Exception:
                    continue
            
            if matches:
                # Return comma-separated list of unique matches
                result = ", ".join(sorted(set(matches)))
                print(f"   ✅ Hair loss location extracted: '{result}'")
                return result
            
            print(f"   ⚠️ No hair loss location found")
            return ""
        except Exception as e:
            print(f"   ❌ Hair loss location extraction error: {e}")
            return ""
        finally:
            try:
                self._switch_to_default()
            except Exception:
                pass
    
    def _extract_hair_loss_additional_sxx(self) -> str:
        """Extract additional hair loss symptoms from the EMR page.
        Uses fallback selector to find the specific element for hair loss additional symptoms.
        """
        try:
            self._switch_to_default()
            
            # Possible additional symptom options
            symptom_patterns = [
                "No, none of these",
                "Burning or pain",
                "Patches of rough, scaly skin or scarring",
                "Pustules or crusting"
            ]
            
            # Try the specific fallback selector first
            fallback_selector = "div:nth-of-type(1) > div:nth-of-type(4) > div > div:nth-of-type(2) > div:nth-of-type(1) > div > div > div > div > div > div > div:nth-of-type(4) > div:nth-of-type(2) > div:nth-of-type(2) > div > div:nth-of-type(43) > div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > div"
            
            try:
                element = self.driver.find_element(By.CSS_SELECTOR, fallback_selector)
                text = (element.get_attribute('innerText') or element.text or '').strip()
                if text == "No, none of these":
                    print(f"   ✅ Found 'No, none of these' via fallback selector")
                    return "none, denies burning, pain, patches of rough scaly skin, scarring, pustules, and crusting"
                elif text in symptom_patterns:
                    print(f"   ✅ Found hair loss additional sxx via fallback selector: '{text}'")
                    return text
            except Exception as e:
                print(f"   ⚠️ Fallback selector failed: {e}")
            
            # If fallback fails, search all elements with the generic class
            elements = self.driver.find_elements(By.CSS_SELECTOR, "div.css-1rynq56.r-cqee49.r-b88u0q")
            print(f"   🔍 Searching {len(elements)} elements for hair loss additional sxx...")
            
            # Special case: if "No, none of these" is found, return the special text
            none_of_these_found = False
            other_symptoms = []
            
            for element in elements:
                try:
                    text = (element.get_attribute('innerText') or element.text or '').strip()
                    if text == "No, none of these":
                        none_of_these_found = True
                        print(f"   ✅ Found: '{text}'")
                    elif text in symptom_patterns:
                        other_symptoms.append(text)
                        print(f"   ✅ Found symptom: '{text}'")
                except Exception:
                    continue
            
            # If "No, none of these" was selected
            if none_of_these_found and not other_symptoms:
                result = "none, denies burning, pain, patches of rough scaly skin, scarring, pustules, and crusting"
                print(f"   ✅ Hair loss additional sxx extracted: '{result}'")
                return result
            
            # Otherwise return comma-separated list of symptoms
            if other_symptoms:
                result = ", ".join(sorted(set(other_symptoms)))
                print(f"   ✅ Hair loss additional sxx extracted: '{result}'")
                return result
            
            print(f"   ⚠️ No hair loss additional sxx found")
            return ""
        except Exception as e:
            print(f"   ❌ Hair loss additional sxx extraction error: {e}")
            return ""
        finally:
            try:
                self._switch_to_default()
            except Exception:
                pass

    # ====================================================================
    # SH Initial Visit extractors
    # ====================================================================

    def _extract_patient_age(self, full_text: Optional[str] = None) -> str:
        """Extract patient age from EMR page text.
        Looks for patterns like 'Age: 35', '35 year old', '35 yo', 'DOB: ...' then computes age.
        Returns age string like '35 year old male' or '' if not found.
        """
        try:
            text = full_text or self._get_page_text() or ""
            if not text:
                return ""
            # Pattern 1: explicit "Age: XX" or "Age XX"
            m = re.search(r'\bage[:\s]+(\d{2,3})\b', text, re.IGNORECASE)
            if m:
                return f"{m.group(1)} year old male"
            # Pattern 2: "XX year old" or "XX y/o" or "XX yo"
            m = re.search(r'\b(\d{2,3})\s*(?:year[- ]old|y/?o)\b', text, re.IGNORECASE)
            if m:
                return f"{m.group(1)} year old male"
            # Pattern 3: DOB-based calculation
            dob_patterns = [
                r'(?:DOB|date\s+of\s+birth|born)[:\s]+(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})',
                r'(?:DOB|date\s+of\s+birth|born)[:\s]+(\w+)\s+(\d{1,2}),?\s+(\d{4})',
            ]
            from datetime import date as _date
            for pat in dob_patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    try:
                        groups = m.groups()
                        if groups[0].isdigit():
                            month, day, year = int(groups[0]), int(groups[1]), int(groups[2])
                        else:
                            import calendar
                            month_names = {name.lower(): num for num, name in enumerate(calendar.month_name) if num}
                            month_abbrs = {name.lower(): num for num, name in enumerate(calendar.month_abbr) if num}
                            month = month_names.get(groups[0].lower()) or month_abbrs.get(groups[0].lower()[:3]) or 1
                            day, year = int(groups[1]), int(groups[2])
                        dob = _date(year, month, day)
                        today = _date.today()
                        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                        if 10 <= age <= 120:
                            return f"{age} year old male"
                    except Exception:
                        pass
            return ""
        except Exception as e:
            print(f"   ❌ Patient age extraction error: {e}")
            return ""

    def _extract_visit_type_from_header(self) -> str:
        """Determine if this is a 'sexual health' or 'premature ejaculation' visit.
        Uses section header and page text to auto-detect.
        """
        try:
            header = self._get_section_header() or ""
            low = header.lower()
            if "premature ejaculation" in low or "pe " in low:
                return "premature ejaculation"
            if "sexual health" in low or "erectile" in low:
                return "sexual health"
            # Fallback: check page text
            text = self._get_page_text() or ""
            text_low = text.lower()
            # Count occurrences to decide
            pe_count = len(re.findall(r'premature\s+ejaculation', text_low))
            ed_count = len(re.findall(r'erectile\s+dysfunction', text_low))
            if pe_count > ed_count and pe_count >= 2:
                return "premature ejaculation"
            return "sexual health"
        except Exception as e:
            print(f"   ❌ Visit type extraction error: {e}")
            return "sexual health"

    def _extract_ed_onset(self, full_text: Optional[str] = None) -> str:
        """Extract rapidity of onset from intake questionnaire.
        Q: 'How did your symptoms begin?' or 'onset' related questions.
        Maps answer to 'gradual' or 'sudden'.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]
            # Look for onset question anchors
            onset_anchors = [
                "how did your symptoms begin",
                "how did your ed symptoms begin",
                "how did your erection problems begin",
                "onset of symptoms",
                "onset of your symptoms",
            ]
            for anchor in onset_anchors:
                indices = [i for i, l in enumerate(low) if anchor in l]
                for idx in indices:
                    for j in range(idx + 1, min(len(lines), idx + 15)):
                        answer = lines[j].lower().strip()
                        if not answer:
                            continue
                        if "gradual" in answer or "slowly" in answer or "over time" in answer:
                            return "gradual"
                        if "sudden" in answer or "abrupt" in answer or "overnight" in answer:
                            return "sudden"
            # Try chip-based approach
            for anchor in onset_anchors:
                answers = self._get_selected_answers_by_question(anchor)
                if answers:
                    combined = " ".join(answers).lower()
                    if "gradual" in combined or "slowly" in combined:
                        return "gradual"
                    if "sudden" in combined or "abrupt" in combined:
                        return "sudden"
            return ""
        except Exception as e:
            print(f"   ❌ ED onset extraction error: {e}")
            return ""

    def _extract_ed_frequency(self, full_text: Optional[str] = None) -> str:
        """Extract ED frequency description from intake questionnaire.
        Q: 'How often do you have difficulty...' or 'frequency' related questions.
        Returns the raw answer text like 'sometimes', 'most of the time', etc.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]
            freq_anchors = [
                "how often do you have difficulty",
                "how often do you experience",
                "how frequently",
                "frequency of your",
                "how often are you able to get",
            ]
            for anchor in freq_anchors:
                indices = [i for i, l in enumerate(low) if anchor in l]
                for idx in indices:
                    for j in range(idx + 1, min(len(lines), idx + 10)):
                        answer = lines[j].strip()
                        if not answer or len(answer) < 3:
                            continue
                        # Skip if it's another question
                        if answer.endswith("?"):
                            break
                        return answer.lower()
            # Try chip-based
            for anchor in freq_anchors:
                answers = self._get_selected_answers_by_question(anchor)
                if answers:
                    return answers[0].strip().lower()
            return ""
        except Exception as e:
            print(f"   ❌ ED frequency extraction error: {e}")
            return ""

    def _extract_ed_description(self, full_text: Optional[str] = None) -> str:
        """Extract the type/description of ED from the intake form.
        Q: 'Which of the following best describes your erection problem?'
        Returns text like 'erectile dysfunction' or 'premature ejaculation'.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]
            desc_anchors = [
                "which of the following best describes",
                "what best describes your erection",
                "describe your erection problem",
                "what type of erection problem",
                "which best describes your",
            ]
            for anchor in desc_anchors:
                indices = [i for i, l in enumerate(low) if anchor in l]
                for idx in indices:
                    for j in range(idx + 1, min(len(lines), idx + 10)):
                        answer = lines[j].strip()
                        if not answer or len(answer) < 3:
                            continue
                        if answer.endswith("?"):
                            break
                        return answer.lower()
            # Try chip answers
            for anchor in desc_anchors:
                answers = self._get_selected_answers_by_question(anchor)
                if answers:
                    return answers[0].strip().lower()
            return ""
        except Exception as e:
            print(f"   ❌ ED description extraction error: {e}")
            return ""

    def _extract_ed_characterization(self, full_text: Optional[str] = None) -> str:
        """Extract characterization of ED (getting vs maintaining erections).
        Q: 'Do you have difficulty getting an erection, maintaining...'
        Returns text like 'difficulty getting an erection', 'difficulty maintaining an erection', 'both'.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]
            char_anchors = [
                "do you have difficulty getting an erection",
                "difficulty getting or maintaining",
                "getting an erection, maintaining",
                "is your difficulty in getting",
            ]
            for anchor in char_anchors:
                indices = [i for i, l in enumerate(low) if anchor in l]
                for idx in indices:
                    for j in range(idx + 1, min(len(lines), idx + 10)):
                        answer = lines[j].strip()
                        if not answer or len(answer) < 3:
                            continue
                        if answer.endswith("?"):
                            break
                        al = answer.lower()
                        if "both" in al or ("getting" in al and "maintaining" in al):
                            return "difficulty getting and maintaining an erection"
                        if "getting" in al:
                            return "difficulty getting an erection"
                        if "maintaining" in al or "keeping" in al:
                            return "difficulty maintaining an erection"
                        return al
            # Try chip answers
            for anchor in char_anchors:
                answers = self._get_selected_answers_by_question(anchor)
                if answers:
                    combined = " ".join(answers).lower()
                    if "both" in combined or ("getting" in combined and "maintaining" in combined):
                        return "difficulty getting and maintaining an erection"
                    if "getting" in combined:
                        return "difficulty getting an erection"
                    if "maintaining" in combined or "keeping" in combined:
                        return "difficulty maintaining an erection"
                    return answers[0].strip().lower()
            return ""
        except Exception as e:
            print(f"   ❌ ED characterization extraction error: {e}")
            return ""

    def _extract_ehs_scores(self, full_text: Optional[str] = None) -> str:
        """Extract best EHS (Erection Hardness Score) from intake questionnaire.
        Looks for EHS questions across 3 contexts (masturbation, nocturnal, partner).
        EHS scale: 0 = no erection, 1 = larger but not hard, 2 = hard but not enough,
                   3 = hard enough but not fully, 4 = completely hard.
        Returns the best (highest) score as string like '3' or ''.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]

            ehs_anchors = [
                "how would you rate the hardness",
                "how hard was your erection",
                "erection hardness score",
                "ehs score",
                "rate your erection hardness",
                "on a scale.*hardness",
            ]

            # EHS answer mapping: text → numeric score
            ehs_map = {
                "no erection": 0,
                "penis is larger but not hard": 1,
                "not hard enough for penetration": 2,
                "hard enough for penetration but not completely hard": 3,
                "completely hard and fully rigid": 4,
                "fully rigid": 4,
                "completely hard": 4,
            }

            scores = []
            for anchor in ehs_anchors:
                for i, l in enumerate(low):
                    if re.search(anchor, l):
                        # Search next lines for an answer
                        for j in range(i + 1, min(len(lines), i + 15)):
                            ans = lines[j].strip()
                            ans_low = ans.lower()
                            if not ans_low or ans_low.endswith("?"):
                                continue
                            # Direct numeric
                            m = re.match(r'^(\d)$', ans_low)
                            if m and 0 <= int(m.group(1)) <= 4:
                                scores.append(int(m.group(1)))
                                break
                            # Text mapping
                            for key, val in ehs_map.items():
                                if key in ans_low:
                                    scores.append(val)
                                    break
                            else:
                                continue
                            break

            # Also try chip-based extraction
            for anchor in ehs_anchors:
                if not re.search(r'[.*]', anchor):
                    answers = self._get_selected_answers_by_question(anchor)
                    if answers:
                        for ans in answers:
                            ans_low = ans.strip().lower()
                            m = re.match(r'^(\d)$', ans_low)
                            if m and 0 <= int(m.group(1)) <= 4:
                                scores.append(int(m.group(1)))
                            for key, val in ehs_map.items():
                                if key in ans_low:
                                    scores.append(val)

            if scores:
                best = max(scores)
                return str(best)
            return ""
        except Exception as e:
            print(f"   ❌ EHS extraction error: {e}")
            return ""

    def _extract_pep_score(self, full_text: Optional[str] = None) -> str:
        """Extract PEP (Premature Ejaculation Profile) score from intake questionnaire.
        PEP = average of 4 questions (each scored 0-4):
          Q1) Control over ejaculation           (direct:  poor=1, fair=2, good=3 …)
          Q2) Ejaculation-related distress        (reverse: quite a bit=1, not at all=4 …)
          Q3) Satisfaction with sex life           (direct:  fair=2, good=3 …)
          Q4) Relationship difficulty from PE      (reverse: quite a bit=1, not at all=4 …)
        Severity: ≤2 = Severe, 2-3 = Moderate, 3-3.5 = Mild
        Returns e.g. '1.2 (Severe)' or ''.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]

            # Each PEP question: (list-of-anchor-regexes, score_map_dict)
            # Direct scoring: better answer → higher score
            direct_map = {
                "very poor": 0, "no control": 0,
                "poor": 1,
                "fair": 2,
                "good": 3, "satisfied": 3,
                "very good": 4, "very satisfied": 4, "excellent": 4, "complete": 4,
            }
            # Reverse scoring: worse answer → lower score (more distress/difficulty = lower)
            reverse_map = {
                "extremely": 0, "very much": 0,
                "quite a bit": 1, "much": 1, "a lot": 1,
                "somewhat": 2, "moderate": 2, "moderately": 2,
                "a little": 3, "slightly": 3, "a little bit": 3,
                "not at all": 4, "none": 4, "not at all difficult": 4,
            }

            pep_questions = [
                # Q1: Control over ejaculation (direct)
                {
                    "anchors": [
                        r"control over ejaculation",
                        r"how (?:was|is) your control over",
                        r"how much control do you have",
                    ],
                    "score_map": direct_map,
                },
                # Q2: Distress about ejaculation (reverse)
                {
                    "anchors": [
                        r"distress(?:ed)?.*(?:ejaculat|how fast)",
                        r"how fast you ejaculat.*distress",
                        r"how much distress",
                        r"bothered.*(?:ejaculat|how fast)",
                    ],
                    "score_map": reverse_map,
                },
                # Q3: Satisfaction with sex life (direct)
                {
                    "anchors": [
                        r"satisf(?:ied|action).*(?:sex|sexual|intercourse)",
                        r"how satisfied (?:were|are) you",
                    ],
                    "score_map": direct_map,
                },
                # Q4: Relationship difficulty from PE (reverse)
                {
                    "anchors": [
                        r"(?:difficulty|difficult).*relationship",
                        r"cause difficulty in your relationship",
                        r"interpersonal difficulty",
                        r"relationship.*(?:difficulty|problem)",
                    ],
                    "score_map": reverse_map,
                },
            ]

            def _score_answer(answer_text: str, score_map: dict) -> Optional[float]:
                """Map an answer string to a numeric PEP score."""
                ans_low = answer_text.strip().lower()
                if not ans_low:
                    return None
                # Direct numeric
                m = re.match(r'^(\d(?:\.\d)?)$', ans_low)
                if m:
                    v = float(m.group(1))
                    if 0 <= v <= 5:
                        return v
                # Embedded number like "(3) Moderate"
                m = re.search(r'\((\d)\)', ans_low)
                if m:
                    v = float(m.group(1))
                    if 0 <= v <= 5:
                        return v
                # Text mapping — try longest match first to avoid partial hits
                for key in sorted(score_map.keys(), key=len, reverse=True):
                    if key in ans_low:
                        return float(score_map[key])
                return None

            individual_scores = []
            for q in pep_questions:
                found = False
                for anchor in q["anchors"]:
                    if found:
                        break
                    for i, l in enumerate(low):
                        if re.search(anchor, l):
                            # Search next lines for an answer
                            for j in range(i + 1, min(len(lines), i + 10)):
                                ans = lines[j].strip()
                                if not ans or ans.lower().endswith("?"):
                                    continue
                                score = _score_answer(ans, q["score_map"])
                                if score is not None:
                                    individual_scores.append(score)
                                    dprint(f"      PEP Q{len(individual_scores)}: anchor='{anchor}' answer='{ans}' → {score}")
                                    found = True
                                    break
                            if found:
                                break

            if individual_scores:
                avg = sum(individual_scores) / len(individual_scores)
                avg_rounded = round(avg, 2)
                if avg_rounded <= 2.0:
                    severity = "Severe"
                elif avg_rounded <= 3.0:
                    severity = "Moderate"
                elif avg_rounded <= 3.5:
                    severity = "Mild"
                else:
                    severity = "Minimal"
                return f"{avg_rounded} ({severity})"
            return ""
        except Exception as e:
            print(f"   ❌ PEP score extraction error: {e}")
            return ""

    def _extract_past_ed_treatments(self, full_text: Optional[str] = None) -> str:
        """Extract prior ED treatments from intake questionnaire.
        Q: 'Have you tried any treatments before?' / 'previous treatments'
        Returns comma-separated list of treatments or 'none reported'.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]

            tx_anchors = [
                "have you tried any",
                "previous treatments",
                "prior treatments",
                "tried any ed treatments",
                "treatments have you tried",
                "which treatments have you",
                "have you previously tried",
                "have you used any",
            ]

            for anchor in tx_anchors:
                indices = [i for i, l in enumerate(low) if anchor in l]
                for idx in indices:
                    answers = []
                    for j in range(idx + 1, min(len(lines), idx + 20)):
                        ans = lines[j].strip()
                        if not ans:
                            continue
                        if ans.endswith("?"):
                            break
                        # Stop if we hit another section header
                        if re.match(r'^[A-Z][a-z]+.*:$', ans):
                            break
                        answers.append(ans)
                    if answers:
                        # Look for "none" type answers
                        combined = " ".join(answers).lower()
                        if "none" in combined and "no" in combined and len(answers) == 1:
                            return "none reported"
                        if combined.strip() in ("none", "no", "n/a"):
                            return "none reported"
                        return ", ".join(answers)

            # Try chip answers
            for anchor in tx_anchors:
                answers = self._get_selected_answers_by_question(anchor)
                if answers:
                    combined = " ".join(answers).lower()
                    if combined.strip() in ("none", "no", "n/a", "none of the above"):
                        return "none reported"
                    return ", ".join(a.strip() for a in answers if a.strip())
            return ""
        except Exception as e:
            print(f"   ❌ Past ED treatments extraction error: {e}")
            return ""

    def _extract_ros_positives(self, full_text: Optional[str] = None) -> str:
        """Extract ROS (Review of Systems) pertinent positives from the intake questionnaire.
        Looks for checked/selected symptom checkboxes that the patient endorsed.
        Returns comma-separated list of positive findings.
        """
        try:
            text = full_text or self._get_latest_intake_text_segment() or self._get_page_text() or ""
            if not text:
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            low = [ln.lower() for ln in lines]

            # ROS question anchors
            ros_anchors = [
                "do you currently experience any of the following",
                "review of systems",
                "are you currently experiencing",
                "check all that apply",
                "do you experience any of",
                "select all symptoms",
            ]

            # Known ROS symptom options in the SH questionnaire
            ros_symptoms = {
                "exertional chest pain": "exertional chest pain",
                "chest pain": "exertional chest pain",
                "irregular heart beats": "irregular heart beats",
                "irregular heartbeats": "irregular heart beats",
                "arrhythmia": "irregular heart beats",
                "unexplained fainting": "unexplained fainting or dizziness",
                "dizziness": "unexplained fainting or dizziness",
                "fainting": "unexplained fainting or dizziness",
                "low libido": "low libido",
                "decreased libido": "low libido",
                "reduced sex drive": "low libido",
            }

            positives = []
            for anchor in ros_anchors:
                indices = [i for i, l in enumerate(low) if anchor in l]
                for idx in indices:
                    for j in range(idx + 1, min(len(lines), idx + 30)):
                        ans = lines[j].strip()
                        ans_low = ans.lower()
                        if not ans_low:
                            continue
                        if ans_low.endswith("?"):
                            break
                        # Check for "none of the above" which means all negative
                        if "none of the above" in ans_low or "none of these" in ans_low:
                            break
                        for key, canonical in ros_symptoms.items():
                            if key in ans_low and canonical not in positives:
                                positives.append(canonical)

            # Also try chip answers for ROS
            for anchor in ros_anchors:
                answers = self._get_selected_answers_by_question(anchor)
                if answers:
                    for a in answers:
                        al = a.strip().lower()
                        if "none" in al:
                            continue
                        for key, canonical in ros_symptoms.items():
                            if key in al and canonical not in positives:
                                positives.append(canonical)

            if positives:
                return ", ".join(positives)
            return "none"
        except Exception as e:
            print(f"   ❌ ROS positives extraction error: {e}")
            return ""

    def _extract_ros_negatives(self, ros_positives: str = "") -> str:
        """Generate ROS pertinent negatives by subtracting positives from standard denial set.
        Standard denials: exertional chest pain, irregular heart beats,
                         unexplained fainting or dizziness, low libido.
        Returns text like 'denies exertional chest pain, irregular heart beats'.
        """
        try:
            standard_denials = [
                "exertional chest pain",
                "irregular heart beats",
                "unexplained fainting or dizziness",
                "low libido",
            ]
            positives_low = (ros_positives or "").lower()
            negatives = [d for d in standard_denials if d.lower() not in positives_low]
            if negatives:
                return "denies " + ", ".join(negatives)
            return "no additional negatives"
        except Exception as e:
            print(f"   ❌ ROS negatives generation error: {e}")
            return ""

    def _extract_medication_from_text(self, text: str) -> str:
        """Heuristic medication extractor for Sexual Health from plain text.
        Primary rule: pick the med line immediately after a header line that reads 'Treatment'.
        Fallback: first med-like line (contains mg and digits, ideally with known med keyword).
        Appends a frequency suffix derived from nearby/overall text ('doses per month').
        Returns empty string if nothing plausible is found.
        """
        try:
            if not text:
                return ""
            # Known sexual health meds/brands
            med_keywords = [
                'viagra', 'cialis', 'sildenafil', 'tadalafil', 'vardenafil', 'stendra', 'avanafil', 'levitra',
                'generic viagra', 'generic cialis'
            ]
            raw_lines = text.splitlines()
            lines = [ln.strip() for ln in raw_lines if ln.strip()]
            doses_re = re.compile(r"(\d+)\s*doses?\s*per\s*month", re.IGNORECASE)
            header_re = re.compile(r"^treatment\b", re.IGNORECASE)

            def is_med_like(line: str) -> bool:
                ll = line.lower()
                if 'mg' not in ll:
                    return False
                if not re.search(r"\d", line):
                    return False
                # Prefer lines with known med keywords but allow generic mg lines
                return True if any(kw in ll for kw in med_keywords) else True
            # Pass A: prefer the med line that immediately follows a 'Treatment' header
            chosen = None
            chosen_idx = -1
            treatment_indices = [i for i, l in enumerate(lines) if header_re.search(l)]
            if treatment_indices:
                # Use the last 'Treatment' header (closest to the section we want)
                start = treatment_indices[-1] + 1
                for i in range(start, min(start + 6, len(lines))):
                    candidate = lines[i].strip()
                    if not candidate:
                        continue
                    # Skip obvious non-med informational lines
                    if re.match(r"^(current dose|treatment plan|medication|dose|notes)\b", candidate, re.IGNORECASE):
                        continue
                    if is_med_like(candidate):
                        chosen = candidate
                        chosen_idx = i
                        break
            # Pass B: fallback to first med-like line anywhere
            if not chosen:
                for i, line in enumerate(lines):
                    if is_med_like(line):
                        chosen = line.strip()
                        chosen_idx = i
                        break
            if not chosen:
                return ""
            # Determine frequency from nearby lines or whole text
            def infer_freq() -> str:
                # Prefer next few lines near the chosen index
                nearby = "\n".join(lines[chosen_idx+1: chosen_idx+4]) if chosen_idx >= 0 else ''
                m = doses_re.search(nearby) or doses_re.search(text)
                if m:
                    try:
                        v = int(m.group(1))
                        return ", daily" if v >= 30 else ", as-needed"
                    except Exception:
                        return ", as-needed"
                return ", as-needed"
            base_line = self._sanitize_medication_line(chosen) or chosen.strip()
            return f"{base_line}{infer_freq()}"
        except Exception:
            return ""
    
    def _get_page_text(self):
        """Get all text content from the page as fallback"""
        if self._cache_body_text is not None:
            return self._cache_body_text
        try:
            # Prefer CDP-based innerText for speed and to avoid Selenium overhead
            if USE_CDP_FOR_TEXT and sync_playwright is not None:
                try:
                    txt = self._get_text_via_cdp(include_frames=False) or ''
                    if txt:
                        self._cache_body_text = txt
                        return self._cache_body_text
                except Exception:
                    pass
            # Fallback to Selenium JS evaluation
            try:
                self._cache_body_text = self.driver.execute_script(
                    "return document.body && (document.body.innerText || document.body.textContent) || ''"
                )
            except Exception:
                self._cache_body_text = self.driver.find_element(By.TAG_NAME, 'body').text
            return self._cache_body_text
        except Exception as e:
            print(f"   ❌ Could not get page text: {e}")
            return None

    def _get_all_text_across_frames(self, max_frames: int = 6) -> str:
        """Return concatenated visible text from default document and top-level iframes (shallow).
        Keeps it simple and fast; used for coarse parsing fallbacks.
        """
        if self._cache_all_text is not None:
            return self._cache_all_text
        # Prefer CDP-based text collection across same-origin frames to avoid Selenium frame switching
        if USE_CDP_FOR_TEXT and sync_playwright is not None:
            try:
                txt = self._get_text_via_cdp(include_frames=True, max_frames=max_frames) or ''
                self._cache_all_text = txt
                return self._cache_all_text
            except Exception:
                pass
        parts = []
        try:
            self._switch_to_default()
            try:
                t = (self.driver.find_element(By.TAG_NAME, 'body').text or '').strip()
            except Exception:
                t = ''
            if t:
                parts.append(t)
            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for fr in frames[:max_frames]:
                try:
                    self.driver.switch_to.frame(fr)
                    tt = (self.driver.find_element(By.TAG_NAME, 'body').text or '').strip()
                    if tt:
                        parts.append(tt)
                except Exception:
                    pass
                finally:
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass
        except Exception as e:
            print(f"   ❌ Frame text collection error: {e}")
        finally:
            self._switch_to_default()
        self._cache_all_text = "\n\n".join(parts)
        return self._cache_all_text

    def _get_text_via_cdp(self, include_frames: bool = False, max_frames: int = 6) -> Optional[str]:
        """Use Playwright over CDP to fetch document.body innerText/textContent from the EMR page,
        optionally aggregating same-origin iframes. Avoids Selenium operations for speed.
        Returns concatenated text or None on failure.
        """
        try:
            if sync_playwright is None:
                return None
            page = self._get_cdp_emr_page()
            if page is None:
                return None

            parts: list[str] = []
            try:
                main_txt = page.evaluate(
                    "() => (document.body && (document.body.innerText || document.body.textContent)) || ''"
                ) or ""
                if main_txt:
                    parts.append(main_txt)
            except Exception:
                pass

            if include_frames:
                try:
                    frames = page.frames
                except Exception:
                    frames = []
                count = 0
                for fr in frames:
                    try:
                        if fr == page.main_frame:
                            continue
                    except Exception:
                        pass
                    try:
                        txt = fr.evaluate(
                            "() => (document.body && (document.body.innerText || document.body.textContent)) || ''"
                        ) or ""
                        if txt:
                            parts.append(txt)
                            count += 1
                            if count >= max_frames:
                                break
                    except Exception:
                        continue

            return "\n\n".join(parts)
        except Exception:
            return None

    def _get_cdp_emr_page(self):
        """Ensure a persistent CDP connection and return the EMR Playwright Page.
        Reuses the attached browser across calls for speed.
        """
        try:
            if sync_playwright is None:
                return None
            current_thread = threading.get_ident()
            if self._pw is not None and self._pw_thread_id not in (None, current_thread):
                self._teardown_playwright_context()
            if not self._ensure_playwright_browser():
                return None

            # If we have a cached page and it's still on EMR, reuse it
            try:
                if self._cdp_emr_page is not None:
                    url = self._cdp_emr_page.url or ""
                    if self._is_emr_url(url):
                        return self._cdp_emr_page
            except Exception:
                self._cdp_emr_page = None

            # Otherwise, find an EMR page among contexts
            try:
                for context in self._cdp_browser.contexts:
                    for page in context.pages:
                        url = page.url or ""
                        if self._is_emr_url(url):
                            self._cdp_emr_page = page
                            return page
            except Exception:
                return None
            return None
        except Exception:
            return None

    def _parse_pa_options_from_text(self, text: str, question_substring: str, options: list[str]) -> list[str]:
        """Given full page text and a question substring, collect matching options that appear nearby.
        Strategy:
        - Split into lines; find indices of lines containing the question substring (case-insensitive).
        - For the first hit, scan forward up to N lines (window) and collect any option whose lowercase token is found
          in a line; prefer full string matches ignoring case; dedupe preserving order.
        """
        if not text:
            return []
        lines = [ln.strip() for ln in text.splitlines()]
        low = [ln.lower() for ln in lines]
        q = question_substring.strip().lower()
        anchors = [i for i, l in enumerate(low) if q in l]
        if not anchors:
            return []
        start = anchors[0]
        window = 60  # generous forward scan
        chosen: list[str] = []
        seen = set()
        # Precompute lowercase options and simple tokens
        opt_pairs = [(opt, opt.lower()) for opt in options]
        for i in range(start + 1, min(len(lines), start + 1 + window)):
            ltxt = lines[i]
            lwr = low[i]
            if not ltxt:
                continue
            for opt, opt_low in opt_pairs:
                if opt_low in lwr:
                    key = opt_low.strip()
                    if key and key not in seen:
                        chosen.append(opt)
                        seen.add(key)
        return chosen

    def _collect_symptom_lines_from_text(self, text: str, symptom_question_substring: str) -> list[str]:
        """Capture full symptom phrases by scanning lines after the symptom question for any line containing
        symptom keywords; return unique lines in order. This complements options parsing for variants.
        """
        if not text:
            return []
        lines = [ln.strip() for ln in text.splitlines()]
        low = [ln.lower() for ln in lines]
        q = symptom_question_substring.strip().lower()
        anchors = [i for i, l in enumerate(low) if q in l]
        if not anchors:
            return []
        start = anchors[0]
        window = 80  # a bit larger for free-text style items
        picked: list[str] = []
        seen = set()

        def is_symptom_line(s: str) -> bool:
            ls = s.lower()
            # Exclude navigation/controls lines
            if any(tok in ls for tok in ['show unselected', 'expand', 'collapse', 'yes', 'no', 'none of the above']):
                return False
            # Must contain a symptom keyword
            return any(k in ls for k in PA_SYMPTOM_KEYWORDS)

        for i in range(start + 1, min(len(lines), start + 1 + window)):
            ltxt = lines[i].strip()
            if not ltxt:
                continue
            if is_symptom_line(ltxt):
                # Keep full phrase, but avoid duplicates by lowercase
                key = ltxt.lower()
                if key not in seen:
                    picked.append(ltxt)
                    seen.add(key)
        return picked

    def _map_lines_to_known_options(self, lines: list[str], options: list[str]) -> list[str]:
        """Map arbitrary lines to canonical options by case-insensitive containment of the option phrase.
        Returns deduped canonical option strings in the order they first appear in the provided lines.
        """
        out: list[str] = []
        seen = set()
        opt_pairs = [(opt, opt.lower()) for opt in options]
        for ln in lines:
            ll = ln.lower()
            for opt, opt_low in opt_pairs:
                if opt_low in ll:
                    k = opt_low
                    if k not in seen:
                        out.append(opt)
                        seen.add(k)
        return out

    def _get_selected_answers_by_question(self, question_text_lower_contains: str, timeout_each=1.2, max_depth=2):
        """Given a question label (lowercase substring), return visible selected answers (chips) on the right column.
        Uses the common EMR layout: a flex row with the label on the left and selected options on the right, where
        selected answers appear as divs with classes 'css-1rynq56 r-cqee49 r-b88u0q'. Searches across iframes.
        """
        try:
            # Build an XPath that finds any element whose normalized lowercase text contains the question substring
            qt = question_text_lower_contains.strip()
            xpath = (
                "//*["
                "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '" + qt + "')"
                "]"
            )
            print(f"   🔎 Seeking question: '{question_text_lower_contains}'")
            el = self._find_element_in_frames_by_xpath(xpath, timeout_each=timeout_each, max_depth=max_depth)
            if el is None:
                print("      ↩︎ Question not found in any frame")
                return []
            right_col = None
            # Ascend to the flex row container of this question
            try:
                row = el.find_element(By.XPATH, "ancestor::div[contains(@class,'flex') and contains(@class,'py-4')][1]")
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", row)
                except Exception:
                    pass
                try:
                    right_col = row.find_element(By.XPATH, ".//div[contains(@class,'flex-1') and contains(@class,'w-auto')]")
                except Exception:
                    # Relaxed right column
                    try:
                        right_col = row.find_element(By.XPATH, ".//div[contains(@class,'flex-1')][last()]")
                    except Exception:
                        right_col = None
            except Exception:
                # Fallback path: locate left column and then its following-sibling as right column
                try:
                    left_col = el.find_element(By.XPATH, "ancestor::div[contains(@class,'pr-4') and contains(@class,'flex-1')][1]")
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", left_col)
                    except Exception:
                        pass
                    right_col = left_col.find_element(By.XPATH, "following-sibling::div[contains(@class,'flex-1')][1]")
                    print("      🔁 Used sibling-based right column detection")
                except Exception:
                    print("      ↩︎ Could not locate flex row ancestor or sibling right column")
                    return []

            # Within the right column, find the selected chips
            chips = []
            if right_col is not None:
                try:
                    chips = right_col.find_elements(
                        By.XPATH,
                        ".//div[@dir='auto' and contains(@class,'css-1rynq56') and contains(@class,'r-cqee49') and contains(@class,'r-b88u0q') and not(contains(@class,'r-v258g'))]"
                    )
                except Exception:
                    chips = []

            # If still nothing, climb ancestors from the question element to find a container that contains chips
            if not chips:
                try:
                    container = el
                    for _ in range(10):
                        try:
                            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", container)
                        except Exception:
                            pass
                        try:
                            test_chips = container.find_elements(
                                By.XPATH,
                                ".//div[@dir='auto' and contains(@class,'css-1rynq56') and contains(@class,'r-cqee49') and contains(@class,'r-b88u0q') and not(contains(@class,'r-v258g'))]"
                            )
                        except Exception:
                            test_chips = []
                        if test_chips:
                            chips = test_chips
                            print("      🔁 Found chips in ancestor container")
                            break
                        # Heuristic: if parent has multiple direct div children, use the last one as right column
                        try:
                            direct_children = container.find_elements(By.XPATH, "./div")
                        except Exception:
                            direct_children = []
                        if len(direct_children) >= 2:
                            candidate_right = direct_children[-1]
                            try:
                                test_chips = candidate_right.find_elements(
                                    By.XPATH,
                                    ".//div[@dir='auto' and contains(@class,'css-1rynq56') and contains(@class,'r-cqee49') and contains(@class,'r-b88u0q') and not(contains(@class,'r-v258g'))]"
                                )
                            except Exception:
                                test_chips = []
                            if test_chips:
                                chips = test_chips
                                print("      🔁 Used last-child-as-right-column heuristic")
                                break
                        # Move up to parent
                        try:
                            container = container.find_element(By.XPATH, "..")
                        except Exception:
                            break
                except Exception as ce:
                    print(f"      ⚠️ Ancestor scan error: {ce}")
            vals = []
            for c in chips:
                try:
                    if not c.is_displayed():
                        continue
                except Exception:
                    pass
                txt = (c.get_attribute('innerText') or c.text or '').strip()
                if txt:
                    vals.append(txt)
            print(f"      ✅ Found {len(vals)} selected answers: {vals[:5]}")
            # Deduplicate preserving order
            seen = set()
            out = []
            for v in vals:
                k = v.strip()
                k2 = k.lower()
                if k and k2 not in seen:
                    out.append(k)
                    seen.add(k2)
            if out:
                return out

            # Ultimate fallback: build a page-wide map of question->answers by scanning visible blocks
            try:
                print("      🔎 Page-wide fallback scan for question/answers")
                self._switch_to_default()
                blocks = []
                try:
                    blocks = self.driver.find_elements(By.XPATH, "//div[contains(@class,'flex') and contains(@class,'py-4')]")
                except Exception:
                    blocks = []
                best = []
                for b in blocks[:80]:
                    try:
                        q = b.find_element(By.XPATH, ".//span[contains(@class,'css-1qaijid') or contains(@class,'css-1rynq56')]")
                        qtxt = (q.get_attribute('innerText') or q.text or '').strip().lower()
                    except Exception:
                        qtxt = ''
                    if not qtxt or question_text_lower_contains not in qtxt:
                        continue
                    # Prefer explicitly the right-hand container if present
                    try:
                        right = b.find_element(By.XPATH, ".//div[contains(@class,'flex-1') and contains(@class,'w-auto')] | .//div[contains(@class,'flex-1')][last()]")
                    except Exception:
                        right = b
                    try:
                        chips = right.find_elements(By.XPATH, ".//div[@dir='auto' and contains(@class,'css-1rynq56') and contains(@class,'r-cqee49') and (contains(@class,'r-b88u0q') or contains(@class,'r-v258g'))]")
                    except Exception:
                        chips = []
                    vals2 = []
                    for c in chips:
                        try:
                            # Only consider visible chips within the row's right-hand area
                            if not c.is_displayed():
                                continue
                        except Exception:
                            pass
                        txt2 = (c.get_attribute('innerText') or c.text or '').strip()
                        if txt2:
                            vals2.append(txt2)
                    if vals2:
                        best = vals2
                        print(f"      ✅ Fallback block matched with {len(best)} answers")
                        break
                if best:
                    # Deduplicate
                    seen = set()
                    out2 = []
                    for v in best:
                        k = v.strip().lower()
                        if v and k not in seen:
                            out2.append(v.strip())
                            seen.add(k)
                    return out2
            except Exception as e2:
                print(f"      ⚠️ Page-wide fallback error: {e2}")

            return []
        except Exception as e:
            print(f"   ❌ _get_selected_answers_by_question error: {e}")
            return []

    def _collect_selected_chip_texts(self, max_depth: int = 3) -> list[str]:
        """Gather visible chip texts across frames when question anchors are missing."""
        if not self.driver:
            return []

        collected: list[str] = []
        seen: set[str] = set()

        def harvest_current_frame() -> None:
            try:
                chips = self.driver.find_elements(
                    By.XPATH,
                    "//div[@dir='auto' and contains(@class,'css-1rynq56') and contains(@class,'r-cqee49') and contains(@class,'r-b88u0q') and not(contains(@class,'r-v258g'))]",
                )
            except Exception:
                chips = []
            for chip in chips:
                try:
                    if not chip.is_displayed():
                        continue
                except Exception:
                    pass
                try:
                    txt = (chip.get_attribute('innerText') or chip.text or '').strip()
                except Exception:
                    txt = ''
                if not txt:
                    continue
                key = txt.lower()
                if key not in seen:
                    collected.append(txt)
                    seen.add(key)

        def dfs(depth: int) -> None:
            if depth > max_depth:
                return
            harvest_current_frame()
            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for frame in frames:
                try:
                    self.driver.switch_to.frame(frame)
                except Exception:
                    continue
                dfs(depth + 1)
                try:
                    self.driver.switch_to.parent_frame()
                except Exception:
                    pass

        try:
            self._switch_to_default()
        except Exception:
            pass
        dfs(0)
        try:
            self._switch_to_default()
        except Exception:
            pass
        return collected

    def _collect_note_texts(self, max_notes: int = 12, max_depth: int = 3) -> list[str]:
        """Collect clinical note bodies ([data-testid^='note-content-']) across frames."""
        if not self.driver:
            return []

        texts: list[str] = []
        seen_ids: set[str] = set()

        def harvest_current_frame() -> bool:
            try:
                nodes = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid^='note-content-']")
            except Exception:
                nodes = []
            for node in nodes:
                try:
                    tid = (node.get_attribute('data-testid') or '').strip()
                except Exception:
                    tid = ''
                if tid and tid in seen_ids:
                    continue
                try:
                    raw = (node.get_attribute('innerText') or node.text or '').strip()
                except Exception:
                    raw = ''
                if not raw:
                    continue
                if tid:
                    seen_ids.add(tid)
                texts.append(raw)
                print(f"[DX] Note text collected (tid={tid or 'no-id'}): {raw[:120]!r}")
                if len(texts) >= max_notes:
                    return True
            return False

        def dfs(depth: int) -> bool:
            if depth > max_depth:
                return False
            if harvest_current_frame():
                return True
            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for frame in frames:
                try:
                    self.driver.switch_to.frame(frame)
                except Exception:
                    continue
                if dfs(depth + 1):
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass
                    return True
                try:
                    self.driver.switch_to.parent_frame()
                except Exception:
                    pass
            return False

        try:
            self._switch_to_default()
        except Exception:
            pass
        dfs(0)
        try:
            self._switch_to_default()
        except Exception:
            pass

        if not texts:
            try:
                script = """
                const collect = (root) => {
                    const out = [];
                    if (!root) return out;
                    const nodes = root.querySelectorAll('[data-testid^="note-content-"]');
                    nodes.forEach(node => {
                        const txt = (node.innerText || node.textContent || '').trim();
                        if (txt) out.push(txt);
                    });
                    const frames = root.querySelectorAll('iframe');
                    frames.forEach(frame => {
                        try {
                            const doc = frame.contentDocument;
                            if (doc) {
                                collect(doc).forEach(t => out.push(t));
                            }
                        } catch (e) {}
                    });
                    return out;
                };
                return collect(document);
                """
                results = self.driver.execute_script(script) or []
                for idx, entry in enumerate(results):
                    if not isinstance(entry, str):
                        continue
                    cleaned = entry.strip()
                    if not cleaned:
                        continue
                    if cleaned in texts:
                        continue
                    texts.append(cleaned)
                    print(f"[DX] Script note text collected #{idx}: {cleaned[:120]!r}")
                    if len(texts) >= max_notes:
                        break
            except Exception as e:
                print(f"[DX] Script note collection failed: {e}")
        return texts

    def _get_right_half_visible_texts_near_question(self, question_text_lower_contains: str, y_window: int = 900):
        """Ultra-relaxed fallback: find the question, then gather any visible short texts to the right side of it.
        Heuristics:
        - Anchor on the question element position (within the frame it lives in).
        - Define an x-threshold as max(question.right+10, viewportWidth*0.55).
        - Collect visible div/span/button nodes with short text whose rect center-x is beyond the threshold and whose
          center-y is within [question.top-60, question.bottom+y_window].
        - Exclude obvious UI verbs like 'Show unselected answers', 'Expand', 'Collapse', 'Edit', 'Save', 'Cancel'.
        Returns a de-duplicated list of strings in DOM order.
        """
        try:
            qt = question_text_lower_contains.strip()
            xpath = (
                "//*["
                "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '" + qt + "')"
                "]"
            )
            print(f"   🔎 Positional fallback seeking question: '{question_text_lower_contains}'")
            el = self._find_element_in_frames_by_xpath(xpath, timeout_each=1.2, max_depth=3)
            if el is None:
                print("      ↩︎ Question not found for positional fallback")
                return []

            # Compute geometry in the current frame context
            try:
                rect = self.driver.execute_script(
                    "const r=arguments[0].getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};",
                    el,
                )
            except Exception:
                rect = None
            if not rect:
                return []
            try:
                vpw = self.driver.execute_script("return window.innerWidth || document.documentElement.clientWidth || 0;") or 0
            except Exception:
                vpw = 0
            x_threshold = max(float(rect.get('right', 0)) + 10.0, float(vpw) * 0.55)
            y_min = float(rect.get('top', 0)) - 60.0
            y_max = float(rect.get('bottom', 0)) + float(y_window)

            # Broad candidate query within this frame
            try:
                candidates = self.driver.find_elements(
                    By.XPATH,
                    "//*[self::div or self::span or self::button][(@dir='auto') or contains(@class,'css-1rynq56') or contains(@class,'r-cqee49') or contains(@class,'chip') or contains(@class,'tag')]",
                )
            except Exception:
                candidates = []

            banned_tokens = [
                'show unselected', 'show more', 'collapse', 'expand', 'edit', 'save', 'cancel', 'close',
                'previous', 'next', 'back', 'submit', 'clear', 'search', 'filter', 'sort'
            ]

            out = []
            seen = set()
            for c in candidates[:400]:
                try:
                    if not c.is_displayed():
                        continue
                except Exception:
                    continue
                txt = (c.get_attribute('innerText') or c.text or '').strip()
                if not txt:
                    continue
                # Skip if it's literally the question text or very long UI sentences
                low = txt.lower()
                if any(bt in low for bt in banned_tokens):
                    continue
                if len(txt) > 80 or len(txt.split()) > 14:
                    continue
                # Geometry filter
                try:
                    r = self.driver.execute_script(
                        "const r=arguments[0].getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};",
                        c,
                    )
                except Exception:
                    r = None
                if not r:
                    continue
                cx = float(r.get('left', 0)) + float(r.get('width', 0)) / 2.0
                cy = float(r.get('top', 0)) + float(r.get('height', 0)) / 2.0
                if cx <= x_threshold:
                    continue
                if cy < y_min or cy > y_max:
                    continue
                key = low.strip()
                if key and key not in seen:
                    out.append(txt.strip())
                    seen.add(key)

            if out:
                print(f"      ✅ Positional fallback collected {len(out)} texts: {out[:6]}")
            else:
                print("      ↩︎ Positional fallback found 0 texts")
            return out
        except Exception as e:
            print(f"   ❌ Positional fallback error: {e}")
            return []

    # --- Performance Anxiety helpers ---
    def _extract_pulse(self):
        """Extract pulse/HR value.
        Target values prioritized as one of: '60-100 bpm', 'Less than 60 bpm', 'More than 100 bpm'.
        Prefer exact numeric entered near the pulse question if present; otherwise fall back to phrases or generic numeric.
        Returns 'nr' if unavailable.
        """
        try:
            print("   🔍 Extracting pulse/HR…")
            preferred_phrases = [
                '60-100 bpm',
                'Less than 60 bpm',
                'More than 100 bpm'
            ]

            # 0) Try text-window anchored on the pulse question for an exact numeric
            try:
                full = self._get_all_text_across_frames() or ''
            except Exception:
                full = self._get_page_text() or ''
            if full:
                lines = [ln.strip() for ln in full.splitlines()]
                low = [ln.lower() for ln in lines]
                q = 'what is your current pulse'.lower()
                anchors = [i for i, l in enumerate(low) if q in l]
                if anchors:
                    start = anchors[0]
                    window = 30
                    # First, within window, if preferred phrases appear, return canonical phrase
                    for i in range(start + 1, min(len(lines), start + 1 + window)):
                        l = lines[i]
                        for phrase in preferred_phrases:
                            if phrase.lower() in l.lower():
                                print(f"   ✅ Pulse/HR (anchored phrase): '{phrase}'")
                                return phrase
                    # Else, collect first plausible integer (30-220) possibly followed by 'bpm'
                    num_pat = re.compile(r"\b(\d{2,3})\b")
                    for i in range(start + 1, min(len(lines), start + 1 + window)):
                        l = lines[i]
                        for m in num_pat.finditer(l):
                            try:
                                val = int(m.group(1))
                            except Exception:
                                continue
                            if 30 <= val <= 220:
                                print(f"   ✅ Pulse/HR (anchored numeric): '{val}'")
                                return str(val)

            def pick_pulse_text(candidates):
                # 1) Exact phrase match first
                for t in candidates:
                    tt = (t or '').strip()
                    for phrase in preferred_phrases:
                        if phrase.lower() in tt.lower():
                            return phrase
                # 2) Fallback to numeric HR patterns
                pat = re.compile(r"(?:^|\b)(?:hr|pulse|heart\s*rate)\s*[:\-\s]*([0-9]{2,3})(?:\b|\s)", re.IGNORECASE)
                for t in candidates:
                    m = pat.search(t or "")
                    if m:
                        try:
                            val = int(m.group(1))
                        except Exception:
                            continue
                        if 30 <= val <= 220:
                            return str(val)
                return None

            # Try common class like BP
            try:
                self._switch_to_default()
                elems = self.driver.find_elements(By.CSS_SELECTOR, 'div.css-1rynq56.r-cqee49.r-b88u0q')
                texts = [(e.get_attribute('innerText') or e.text or '').strip() for e in elems]
                chosen = pick_pulse_text(texts)
                if chosen:
                    print(f"   ✅ Pulse/HR (default): '{chosen}'")
                    return chosen
            except Exception:
                pass

            # Try frames as well
            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for fr in frames[:10]:
                try:
                    self.driver.switch_to.frame(fr)
                    elems = self.driver.find_elements(By.CSS_SELECTOR, 'div.css-1rynq56.r-cqee49.r-b88u0q')
                    texts = [(e.get_attribute('innerText') or e.text or '').strip() for e in elems]
                    chosen = pick_pulse_text(texts)
                    if chosen:
                        print(f"   ✅ Pulse/HR (frame): '{chosen}'")
                        return chosen
                except Exception:
                    pass
                finally:
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass

            # Fallback: body text search for preferred phrases first, then numeric
            body = self._get_page_text() or ''
            # Check phrases
            for phrase in preferred_phrases:
                if phrase.lower() in body.lower():
                    print(f"   ✅ Pulse/HR (body phrase): '{phrase}'")
                    return phrase
            # Then numeric
            chosen = None
            try:
                for m in re.finditer(r"(?:^|\b)(?:hr|pulse|heart\s*rate)\s*[:\-\s]*([0-9]{2,3})(?:\b|\s)", body, re.IGNORECASE):
                    try:
                        val = int(m.group(1))
                    except Exception:
                        continue
                    if 30 <= val <= 220:
                        chosen = str(val)
                        break
            except Exception:
                pass
            if chosen:
                print(f"   ✅ Pulse/HR (body): '{chosen}'")
                return chosen
        except Exception as e:
            print(f"   ❌ Pulse extraction error: {e}")
        return 'nr'

    def _collect_selected_checkbox_labels(self, max_frames: int = 10):
        """Collect text labels for selected options/checkboxes across default content and frames.
        Considers: role=checkbox with aria-checked=true or data-state=checked, role=option with aria-selected=true,
        aria-pressed=true, and elements with data-testid containing 'selected'.
        """
        results = []

        def collect_here():
            try:
                # Multiple strategies
                xpaths = [
                    "//*[@role='checkbox' and (@aria-checked='true' or @data-state='checked')]",
                    "//*[@role='option' and @aria-selected='true']",
                    "//*[@aria-pressed='true']",
                    "//*[contains(@data-state,'checked') and (self::div or self::button or self::span)]",
                    "//*[contains(@data-testid,'selected') and (self::div or self::button or self::span)]",
                ]
                elements = []
                for xp in xpaths:
                    try:
                        elements.extend(self.driver.find_elements(By.XPATH, xp))
                    except Exception:
                        continue
                for el in elements:
                    # Prefer aria-label
                    label = (el.get_attribute('aria-label') or '').strip()
                    if not label:
                        txt = (el.get_attribute('innerText') or el.text or '').strip()
                        label = txt
                    if not label:
                        # Try sibling/ancestor text
                        try:
                            sib = el.find_element(By.XPATH, "following-sibling::*[self::div or self::span or self::label][normalize-space(text())!=''][1]")
                            label = (sib.text or '').strip()
                        except Exception:
                            try:
                                anc = el.find_element(By.XPATH, "ancestor::*[self::label or self::div or self::span][normalize-space(text())!=''][1]")
                                label = (anc.text or '').strip()
                            except Exception:
                                label = ''
                    # If label has comma-separated items, split
                    if label:
                        parts = [p.strip() for p in label.split(',') if p.strip()]
                        if parts:
                            results.extend(parts)
            except Exception:
                pass

        try:
            self._switch_to_default()
            collect_here()
            try:
                frames = self.driver.find_elements(By.TAG_NAME, 'iframe')
            except Exception:
                frames = []
            for fr in frames[:max_frames]:
                try:
                    self.driver.switch_to.frame(fr)
                    collect_here()
                except Exception:
                    pass
                finally:
                    try:
                        self.driver.switch_to.parent_frame()
                    except Exception:
                        pass
        except Exception:
            pass
        # Deduplicate, preserve order
        seen = set()
        ordered = []
        for t in results:
            k = t.strip()
            if k and k.lower() not in seen:
                ordered.append(k)
                seen.add(k.lower())
        return ordered

    def grab_performance_anxiety_data(self):
        """Extract Performance Anxiety related fields: medication, situations/fears, somatic symptoms, BP, pulse."""
        if not self.driver:
            if not self.connect_to_chrome():
                return None

        try:
            if not self._ensure_emr_tab():
                print("❌ No EMR tab found for Performance Anxiety grab.")
                return None

            data = {}

            # Medication
            try:
                med = self._extract_medication({}, {})
            except Exception:
                med = ''
            if med:
                data['medication'] = med

            # BP and Pulse
            try:
                data['blood_pressure'] = self._extract_blood_pressure() or 'nr'
            except Exception:
                data['blood_pressure'] = 'nr'
            try:
                data['pulse'] = self._extract_pulse() or 'nr'
            except Exception:
                data['pulse'] = 'nr'

            # Situations and Somatic Symptoms via text-based parsing only
            try:
                full_text = self._get_all_text_across_frames()
            except Exception:
                full_text = self._get_page_text() or ''

            # Situations
            situations = self._parse_pa_options_from_text(
                full_text,
                'what situational fears make you nervous or anxious',
                PA_SITUATION_OPTIONS,
            )
            print(f"   📌 Situations (text-parse): {len(situations)} -> {situations[:5]}")

            # Symptoms: options + keyword-line mapped to known options
            parsed_symptoms = self._parse_pa_options_from_text(
                full_text,
                'do you experience any of the following symptoms when you are anxious',
                PA_SYMPTOM_OPTIONS,
            )
            variant_lines = self._collect_symptom_lines_from_text(
                full_text,
                'do you experience any of the following symptoms when you are anxious',
            )
            variant_mapped = self._map_lines_to_known_options(variant_lines, PA_SYMPTOM_OPTIONS)
            # Merge and dedupe preserving order
            symptoms = []
            seen_sym = set()
            for s in parsed_symptoms + variant_mapped:
                k = s.strip().lower()
                if k and k not in seen_sym:
                    symptoms.append(s.strip())
                    seen_sym.add(k)
            print(f"   📌 Symptoms (text-parse): {len(symptoms)} -> {symptoms[:5]}")

            # Final fallback: parse full text across frames near question anchors using known option lists
            if not situations or not symptoms:
                try:
                    full = self._get_all_text_across_frames()
                except Exception:
                    full = self._get_page_text() or ''
                if not situations:
                    parsed_situations = self._parse_pa_options_from_text(
                        full,
                        'what situational fears make you nervous or anxious',
                        PA_SITUATION_OPTIONS,
                    )
                    if parsed_situations:
                        situations = parsed_situations
                        print(f"   🧭 Text-parse Situations: {len(situations)} -> {situations[:5]}")
                parsed_symptoms = self._parse_pa_options_from_text(
                    full,
                    'do you experience any of the following symptoms when you are anxious',
                    PA_SYMPTOM_OPTIONS,
                )
                variant_lines = self._collect_symptom_lines_from_text(
                    full,
                    'do you experience any of the following symptoms when you are anxious',
                )
                # Only keep keyword lines that map to known options; this drops instructional text
                variant_mapped = self._map_lines_to_known_options(variant_lines, PA_SYMPTOM_OPTIONS)
                if parsed_symptoms or variant_mapped:
                    # Merge option hits + keyword-line hits + any existing
                    merged = []
                    seenm = set()
                    for s in (symptoms or []) + parsed_symptoms + variant_mapped:
                        k = s.strip().lower()
                        if k and k not in seenm:
                            merged.append(s.strip())
                            seenm.add(k)
                    symptoms = merged
                    print(f"   🧭 Text-parse+Keywords Symptoms: {len(symptoms)} -> {symptoms[:6]}")

            if situations:
                data['situations_text'] = ", ".join(situations)
            if symptoms:
                data['symptoms_text'] = ", ".join(symptoms)

            return data
        except Exception as e:
            print(f"❌ Performance Anxiety grab error: {e}")
            return None

    def _infer_visit_type_from_text(self) -> Optional[str]:
        """Fallback: scan full page text across frames for known visit-type keywords."""
        try:
            text = self._get_all_text_across_frames()
            if not text:
                text = self._get_page_text()
        except Exception:
            text = None
        if not text:
            return None
        low = text.lower()
        for keyword, label in VISIT_TYPE_FALLBACK_KEYWORDS:
            if keyword in low:
                print(f"   🧭 Text fallback visit type via '{keyword}': '{label}'")
                return label
        return None

    def _click_notes_tab_if_present(self):
        """Click the clinical Notes tab/header so note content is loaded/visible."""
        try:
            if USE_CDP_FOR_TEXT and sync_playwright is not None:
                # Fast CDP click
                try:
                    script = """
                        const selectors = [
                          'div.css-1hj5o6h div.css-1rynq56',
                          '[data-testid="notes-tab"]',
                          'div[role="button"][aria-label*="Notes"]'
                        ];
                        for (const sel of selectors) {
                          const el = document.querySelector(sel);
                          if (el && el.offsetParent !== null) {
                            el.click();
                            return true;
                          }
                        }
                        return false;
                    """
                    hit = self.driver.execute_cdp_cmd("Runtime.evaluate", {"expression": script, "returnByValue": True})
                    if hit and hit.get("result", {}).get("value"):
                        return True
                except Exception:
                    pass

            # Selenium fallback
            selectors = [
                (By.CSS_SELECTOR, "div.css-1hj5o6h div.css-1rynq56"),
                (By.CSS_SELECTOR, "[data-testid='notes-tab']"),
                (By.XPATH, "//div[contains(@class,'css-1rynq56') and contains(., 'Notes')]")
            ]
            for how, sel in selectors:
                try:
                    el = self.driver.find_element(how, sel)
                    if el and el.is_displayed():
                        el.click()
                        time.sleep(0.2)
                        return True
                except Exception:
                    continue
        except Exception as e:
            print(f"[DX] Notes tab click error: {e}")
        return False

    def _detect_emr_dashboard(self) -> Optional[str]:
        """Detect the EMR home dashboard either by exact URL or by the welcome header."""
        current_url = ""
        try:
            if self.driver:
                current_url = (self.driver.current_url or "").strip()
        except Exception:
            current_url = ""

        if not current_url and self._cdp_browser is not None:
            try:
                page = self._find_emr_page()
                if page:
                    current_url = (page.url or "").strip()
            except Exception:
                current_url = ""

        normalized = current_url.rstrip("/")
        if normalized in EMR_DASHBOARD_BASE_URLS and normalized:
            print("   [VISIT DETECT] EMR dashboard detected via exact URL match")
            return "EMR Dashboard"

        if not self.driver:
            return None

        for sel in EMR_DASHBOARD_WELCOME_SELECTORS:
            try:
                self._switch_to_default()
            except Exception:
                pass
            try:
                element = self.driver.find_element(By.CSS_SELECTOR, sel)
            except Exception:
                continue

            try:
                text_val = (element.get_attribute('innerText') or element.text or '').strip()
            except Exception:
                text_val = ''

            if text_val and text_val.lower().startswith("welcome"):
                print(f"   [VISIT DETECT] EMR dashboard detected via selector '{sel}' -> '{text_val}'")
                return "EMR Dashboard"

        return None

    def _get_section_header(self) -> str | None:
        """Try to read the main section header like 'Hair Loss', 'Sexual Health', 'Testosterone', 'Photoaging'
        from the element described by the user or via a text-based XPath. Searches across iframes.
        """
        try:
            # Serve from cache if fresh (10s)
            if (time.time() - getattr(self, '_cache_section_header_time', 0)) < 10 and self._cache_section_header:
                return self._cache_section_header
            # First, try the exact class path the user provided
            header_selectors = [
                'div.css-1rynq56.r-cqee49.r-1kfrs79',
                # Slightly looser variations in case one class changes
                'div.r-cqee49.r-1kfrs79',
                'div.css-1rynq56.r-cqee49',
            ]
            for sel in header_selectors:
                try:
                    if self._switch_to_frame_with_element(By.CSS_SELECTOR, sel, timeout_each=0.8, max_depth=2):
                        el = self.driver.find_element(By.CSS_SELECTOR, sel)
                        txt = (el.get_attribute('innerText') or el.text or '').strip()
                        self._switch_to_default()
                        if txt:
                            print(f"   🏷️ Section header via CSS '{sel}': '{txt}'")
                            self._cache_section_header = txt
                            self._cache_section_header_time = time.time()
                            return txt
                except Exception:
                    self._switch_to_default()
                    continue

            # Next, try XPath matching the known section names ignoring case and whitespace
            options = ["hair loss", "sexual health", "testosterone", "photoaging"]
            preds = " or ".join([
                f"normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='{opt}'" for opt in options
            ])
            xpath = f"//*[self::div or self::span or self::h1 or self::h2][{preds}]"
            el = self._find_element_in_frames_by_xpath(xpath, timeout_each=0.8, max_depth=2)
            if el is not None:
                txt = (el.get_attribute('innerText') or el.text or '').strip()
                print(f"   🏷️ Section header via XPath: '{txt}'")
                self._cache_section_header = txt
                self._cache_section_header_time = time.time()
                return txt

            # Final fallback: scan page text for keywords
            inferred = self._infer_visit_type_from_text()
            if inferred:
                self._cache_section_header = inferred
                self._cache_section_header_time = time.time()
                return inferred
        except Exception as e:
            print(f"   ❌ Section header detection error: {e}")
        finally:
            self._switch_to_default()
        return None

    def detect_visit_type(self) -> str | None:
        """Return canonical visit type name based on section header text.
        Returns one of: 'Hair Loss', 'Sexual Health', 'Testosterone', 'Photoaging' or None.
        """
        # Serve from cache if fresh (10s)
        if (time.time() - getattr(self, '_cache_visit_type_time', 0)) < 10 and self._cache_visit_type:
            return self._cache_visit_type

        dashboard = self._detect_emr_dashboard()
        if dashboard:
            self._cache_visit_type = dashboard
            self._cache_visit_type_time = time.time()
            return dashboard

        txt = self._get_section_header()
        if not txt:
            return None
        low = txt.strip().lower()
        mapping = {
            'hair loss': 'Hair Loss',
            'sexual health': 'Sexual Health',
            'testosterone': 'T Deficiency',
            'photoaging': 'Photoaging',
            'performance anxiety': 'Performance Anxiety',
            't deficiency': 'T Deficiency',
            'td/ed': 'T Deficiency',
        }
        for key, val in mapping.items():
            if key in low:
                self._cache_visit_type = val
                self._cache_visit_type_time = time.time()
                return val
        return None

    def grab_birth_control_data(self) -> Optional[Dict[str, Any]]:
        if not self.driver:
            if not self.connect_to_chrome():
                return None

        try:
            if not self._ensure_emr_tab():
                print("❌ No EMR tab found for Birth Control grab.")
                return None

            data: Dict[str, Any] = {}

            def _get_selector_candidates(group: str, key: str) -> List[str]:
                selectors: List[str] = []
                try:
                    selectors.extend(
                        get_selector_list(group, key, engine="selenium", selector_type="css_list")
                    )
                except Exception:
                    pass
                try:
                    primary = get_selector(group, key, engine="selenium", selector_type="css")
                    if primary:
                        selectors.append(primary)
                except Exception:
                    pass
                deduped: List[str] = []
                for sel in selectors:
                    if sel and sel not in deduped:
                        deduped.append(sel)
                return deduped

            def _read_text_for_selectors(selectors: List[str]) -> str:
                if not selectors:
                    return ""
                for sel in selectors:
                    try:
                        self._switch_to_default()
                        if self._switch_to_frame_with_element(By.CSS_SELECTOR, sel, timeout_each=0.8, max_depth=3):
                            el = self.driver.find_element(By.CSS_SELECTOR, sel)
                        else:
                            el = self.driver.find_element(By.CSS_SELECTOR, sel)
                        text_val = (el.get_attribute('innerText') or el.text or '').strip()
                        if text_val:
                            return text_val
                    except Exception:
                        continue
                    finally:
                        try:
                            self._switch_to_default()
                        except Exception:
                            pass
                return ""

            try:
                med = self._extract_medication({'group': 'birth_control'}, {}) or ''
            except Exception:
                med = ''
            if med:
                data['medication'] = med
            else:
                try:
                    raw_med_text = _read_text_for_selectors(_get_selector_candidates('birth_control', 'medication'))
                except Exception:
                    raw_med_text = ''
                if raw_med_text:
                    med_from_text = self._extract_medication_from_text(raw_med_text) or raw_med_text.strip()
                    if med_from_text:
                        data['medication'] = med_from_text
                        med = med_from_text

            try:
                lmp_selectors = _get_selector_candidates('birth_control', 'lmp')
                lmp_text = _read_text_for_selectors(lmp_selectors)
                if lmp_text:
                    data['lmp'] = lmp_text
            except Exception:
                pass

            try:
                bp_val = self._extract_blood_pressure() or 'nr'
            except Exception:
                bp_val = 'nr'

            try:
                systolic_text = _read_text_for_selectors(_get_selector_candidates('birth_control', 'systolic_bp'))
                diastolic_text = _read_text_for_selectors(_get_selector_candidates('birth_control', 'diastolic_bp'))
                systolic = re.search(r"\b(\d{2,3})\b", systolic_text or '')
                diastolic = re.search(r"\b(\d{2,3})\b", diastolic_text or '')
                if systolic and diastolic:
                    bp_val = f"{systolic.group(1)}/{diastolic.group(1)}"
            except Exception:
                pass

            data['blood_pressure'] = bp_val or 'nr'

            try:
                full_text = self._get_all_text_across_frames()
            except Exception:
                full_text = self._get_page_text() or ''

            lines = [ln.strip() for ln in (full_text.splitlines() if full_text else [])]
            low_lines = [ln.lower() for ln in lines]

            def find_answer(keywords: List[str], limit: int = 8) -> List[str]:
                for idx, low in enumerate(low_lines):
                    if all(kw in low for kw in keywords):
                        answers: List[str] = []
                        for j in range(idx + 1, min(len(lines), idx + 1 + limit)):
                            candidate = lines[j].strip()
                            if not candidate:
                                continue
                            cand_low = candidate.lower()
                            if '?' in candidate and cand_low.count(' ') > 2:
                                break
                            if cand_low.startswith('status:'):
                                continue
                            answers.append(candidate)
                            if cand_low in {'yes', 'no', 'none', 'n/a'}:
                                break
                        return answers
                return []

            lmp_answer = find_answer(['last menstrual period'])
            if lmp_answer:
                data['lmp'] = lmp_answer[0]

            side_effects_answer = find_answer(['side effect'])
            side_effects_text = ''
            if side_effects_answer:
                joined = ', '.join(side_effects_answer)
                if any(ans.lower() in {'no', 'none', 'none reported'} for ans in side_effects_answer):
                    side_effects_text = 'none'
                else:
                    side_effects_text = joined
            data['side_effects_text'] = side_effects_text or 'none'

            pmh_candidates = set()
            diag_answers = find_answer(['diagnosed with the following'])
            condition_answers = find_answer(['have you had any of the following conditions'])
            for answer_group in (diag_answers, condition_answers):
                for entry in answer_group:
                    low = entry.lower()
                    if low in {'none', 'no', 'n/a'}:
                        continue
                    for key, synonyms in BIRTH_CONTROL_PMH_KEYWORDS.items():
                        if any(term in low for term in synonyms):
                            pmh_candidates.add(key)

            other_diag_answers = find_answer(['any other medical conditions'])
            pmh_other = ''
            if other_diag_answers:
                first = other_diag_answers[0].strip()
                if first and first.lower() not in {'no', 'none', 'n/a'}:
                    pmh_other = first

            if pmh_candidates:
                data['pmh_list'] = sorted(pmh_candidates)
            if pmh_other:
                data['pmh_other'] = pmh_other

            low_text = '\n'.join(low_lines)
            initial = True
            if 'follow-up' in low_text or 'follow up' in low_text or 'refill' in low_text:
                initial = False
            elif 'are you seeking birth control' in low_text or 'intake forms' in low_text:
                initial = True
            data['initial_visit'] = initial

            med_change_answers = find_answer(['changes in your medical history'])
            if med_change_answers:
                first = med_change_answers[0].strip()
                if first:
                    data['med_history_changes'] = 'none' if first.lower() in {'no', 'none', 'n/a'} else first

            return data
        except Exception as exc:
            print(f"❌ Birth Control grab error: {exc}")
            return None


SeleniumEMRGrabber = BrowserEMRGrabber


# ================================================================
# Template Popup Window (Ctrl+Alt+T)
# ================================================================

class TemplatePopup(wx.Frame):
    """Popup window for quick template selection triggered by Ctrl+Alt+T.
    
    Uses wx.Frame instead of PopupTransientWindow for better dropdown support.
    Auto-dismisses when losing focus or when Insert/Escape is pressed.
    """
    
    # Track active popup instance to prevent duplicates
    _active_popup = None
    
    def __init__(self, parent, tab_name: str, on_insert_callback: Callable[[str], None]):
        # Close any existing popup first
        if TemplatePopup._active_popup:
            try:
                TemplatePopup._active_popup.Destroy()
            except Exception:
                pass
            TemplatePopup._active_popup = None
        
        super().__init__(
            parent, 
            style=wx.FRAME_NO_TASKBAR | wx.STAY_ON_TOP | wx.FRAME_TOOL_WINDOW | wx.BORDER_SIMPLE
        )
        
        TemplatePopup._active_popup = self
        self.tab_name = tab_name
        self.on_insert_callback = on_insert_callback
        self._dropdown_open = False
        
        # Create panel with padding
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(250, 250, 250))
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Header label
        header = wx.StaticText(panel, label=f"Template: {tab_name}")
        header.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(header, 0, wx.ALL, 8)
        
        # Dropdown row
        dropdown_row = wx.BoxSizer(wx.HORIZONTAL)
        
        # Load templates for current tab
        template_list = load_tab_template_list(tab_name)
        self.dropdown = wx.Choice(panel, choices=template_list, size=(250, -1))
        if template_list:
            self.dropdown.SetSelection(0)
        dropdown_row.Add(self.dropdown, 1, wx.ALL | wx.EXPAND, 5)
        
        # "..." button for config
        config_btn = wx.Button(panel, label="...", size=(30, -1))
        config_btn.SetToolTip("Edit dropdown list")
        config_btn.Bind(wx.EVT_BUTTON, self.on_edit_config)
        dropdown_row.Add(config_btn, 0, wx.ALL, 5)
        
        # Refresh button
        refresh_btn = wx.Button(panel, label="↻", size=(30, -1))
        refresh_btn.SetToolTip("Refresh list")
        refresh_btn.Bind(wx.EVT_BUTTON, self.on_refresh)
        dropdown_row.Add(refresh_btn, 0, wx.ALL, 5)
        
        main_sizer.Add(dropdown_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        
        # Insert button row
        insert_btn = wx.Button(panel, label="Insert Template", size=(200, 30))
        insert_btn.SetBackgroundColour(wx.Colour(200, 230, 200))
        insert_btn.SetToolTip("Insert selected template (closes popup)")
        insert_btn.Bind(wx.EVT_BUTTON, self.on_insert)
        main_sizer.Add(insert_btn, 0, wx.ALL | wx.ALIGN_CENTER, 8)
        
        panel.SetSizer(main_sizer)
        main_sizer.Fit(panel)
        
        # Fit frame to panel size
        self.SetClientSize(panel.GetSize())
        
        # Bind events for auto-close behavior
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        
        # Track dropdown state to prevent premature close
        self.dropdown.Bind(wx.EVT_CHOICE, self.on_dropdown_select)
        
        # Allow keyboard focus
        self.dropdown.SetFocus()
    
    def on_activate(self, event):
        """Close popup when it loses focus (user clicks elsewhere)."""
        if not event.GetActive():
            # Delay slightly to allow dropdown interactions
            wx.CallLater(100, self._check_and_close)
        event.Skip()
    
    def _check_and_close(self):
        """Check if we should close (not if dropdown is open)."""
        if self and self.IsShown():
            # Check if any child has focus
            focused = wx.Window.FindFocus()
            if focused and self.IsDescendant(focused):
                return  # Don't close, a child control has focus
            self._close_popup()
    
    def on_key(self, event):
        """Handle Escape key to close popup."""
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._close_popup()
        elif event.GetKeyCode() == wx.WXK_RETURN:
            self.on_insert(None)
        else:
            event.Skip()
    
    def on_dropdown_select(self, event):
        """Handle dropdown selection."""
        event.Skip()
    
    def on_edit_config(self, event):
        config_path = get_template_config_path(self.tab_name)
        if os.path.exists(config_path):
            open_in_notepad(config_path)
        # Close popup after opening config
        self._close_popup()

    def _close_popup(self):
        """Explicitly close and destroy the popup."""
        try:
            TemplatePopup._active_popup = None
            self.Show(False)
            wx.CallAfter(self.Destroy)
        except Exception:
            pass

    def on_refresh(self, event):
        new_list = load_tab_template_list(self.tab_name)
        self.dropdown.Clear()
        self.dropdown.AppendItems(new_list)
        if new_list:
            self.dropdown.SetSelection(0)
        print(f"Popup refreshed: {len(new_list)} templates")
    
    def on_insert(self, event):
        selection = self.dropdown.GetStringSelection()
        print(f"[POPUP] Insert clicked, selection: '{selection}'")
        
        # Store callback reference before closing
        callback = self.on_insert_callback
        
        # Close popup FIRST so focus returns to target app
        self._close_popup()
        
        # Then run the insert after a short delay to let focus settle
        if selection:
            def do_insert():
                try:
                    # Wait for any modifier keys to be released
                    import time
                    time.sleep(0.15)
                    callback(selection)
                    print(f"[POPUP] Callback completed for: {selection}")
                except Exception as e:
                    print(f"[POPUP] Callback error: {e}")
                    import traceback
                    traceback.print_exc()
            
            wx.CallLater(50, do_insert)
    
    def Popup(self):
        """Show the popup (compatibility with PopupTransientWindow API)."""
        self.Show()
        self.Raise()
    
    def position_on_screen(self, x: int, y: int):
        """Position popup at (x, y) but adjust to stay fully on screen."""
        popup_size = self.GetSize()
        display = wx.Display(wx.Display.GetFromPoint(wx.Point(x, y)))
        screen_rect = display.GetClientArea()
        
        # Adjust X if popup would go off right edge
        if x + popup_size.width > screen_rect.x + screen_rect.width:
            x = screen_rect.x + screen_rect.width - popup_size.width - 5
        
        # Adjust Y if popup would go off bottom edge
        if y + popup_size.height > screen_rect.y + screen_rect.height:
            y = screen_rect.y + screen_rect.height - popup_size.height - 5
        
        # Ensure not off left or top edge
        x = max(screen_rect.x + 5, x)
        y = max(screen_rect.y + 5, y)
        
        self.SetPosition(wx.Point(x, y))


class MyFrame(wx.Frame):
    @property
    def _selenium_grabber_cache(self):
        """Backwards-compatible access to the browser grabber cache."""
        return getattr(self, "_browser_grabber_cache", None)

    @_selenium_grabber_cache.setter
    def _selenium_grabber_cache(self, value):
        self._browser_grabber_cache = value

    def _ensure_browser_grabber(self) -> tuple[Optional[BrowserEMRGrabber], bool]:
        """Return the cached BrowserEMRGrabber, creating one if needed.

        Returns a tuple of (grabber instance or None, created flag).
        """
        grabber = getattr(self, "_cdp_browser_grabber", None)
        if grabber is not None:
            return grabber, False
        try:
            grabber = BrowserEMRGrabber()
            self._cdp_browser_grabber = grabber
            return grabber, True
        except Exception as exc:
            print(f"Browser grabber creation error: {exc}")
            return None, False
    def __init__(self):
        global templates
        
        # Load templates first
        templates = load_templates_from_file()
        
        super().__init__(
            None,
            title=panel_title,
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX) | wx.STAY_ON_TOP
        )
        
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Use a Notebook with multi-line tabs so all tab selectors can wrap into two rows
        self.notebook = wx.Notebook(panel, style=wx.NB_TOP | wx.NB_MULTILINE)

        # Visit type status line just under the tab strip (updates live)
        self.visit_type_text = wx.StaticText(panel, label="Visit type: Unknown")
        self.visit_type_text.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        self.patient_location_text = wx.StaticText(panel, label="Location: —")
        self.patient_location_text.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        self._last_visit_tab = None
        self._last_location_summary = None
        self._location_worker = None
        self._cdp_browser_grabber = None
        self._custom_cdp_hotkey_handles = []
        self._custom_cdp_hotkeys_active = []
        self._custom_cdp_hotkeys_registered = False
        self._gui_hidden = False
        self._gui_toggle_hotkey_handle = None
        self._gui_toggle_hotkey_id = None
        self._gui_toggle_hotkey_method = None
        self._gui_visible_sliver = GUI_HIDDEN_VISIBLE_WIDTH
        self._js_overlay_active = False
        self._js_overlay_visit_type = None
        self._js_overlay_dark_mode = False
        self._js_overlay_cmd_queue = queue.Queue()
        self._js_overlay_thread = None
        self._js_overlay_page = None
        self._js_overlay_show_error_on_fail = True

        # --- Tab 1: Main tools (recreate existing UI on this panel) ---
        tab1 = scrolled.ScrolledPanel(self.notebook, style=wx.VSCROLL)
        tab1.SetupScrolling(scroll_x=False, scroll_y=True)
        tab1_sizer = wx.BoxSizer(wx.VERTICAL)

        # Template dropdown row at top of tab
        self.tab1_template_dropdown = self.create_template_dropdown_row_inline(tab1, tab1_sizer, "T Deficiency")

        # Grab All Labs button at the top (Tab 1)
        grab_btn = wx.Button(tab1, label="Grab", size=(200, 40))
        grab_btn.Bind(wx.EVT_BUTTON, lambda event: grab_all_labs())
        grab_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        tab1_sizer.Add(grab_btn, 0, wx.ALL | wx.CENTER, 10)

        # Grab mode toggle for Tab 1
        gm_row1 = wx.BoxSizer(wx.HORIZONTAL)
        gm_label1 = wx.StaticText(tab1, label="Grab mode:", size=(120, -1))
        self.grab_mode_choice_tab1 = wx.Choice(tab1, choices=["CDP / Playwright (fast)", "Clipboard (select all + copy)"])
        self.grab_mode_choice_tab1.SetSelection(0 if USE_CDP_FOR_GRAB else 1)
        self.grab_mode_choice_tab1.Bind(wx.EVT_CHOICE, lambda evt: self._on_grab_mode_change(evt))
        self.grab_mode_choice_tab1.SetToolTip("CDP: reads browser DOM directly (fast, no screen interaction). Clipboard: hides GUI, Ctrl+A/Ctrl+C from EMR.")
        gm_row1.Add(gm_label1, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        gm_row1.Add(self.grab_mode_choice_tab1, 0, wx.ALL, 5)
        tab1_sizer.Add(gm_row1, 0, wx.EXPAND)

        # TDCS text field
        tdcs_row = wx.BoxSizer(wx.HORIZONTAL)
        tdcs_label = wx.StaticText(tab1, label="TDCS Score:", size=(120, -1))
        self.tdcs_text = wx.TextCtrl(tab1, size=(100, -1))
        self.tdcs_text.SetValue("—")  # Default to em dash
        tdcs_row.Add(tdcs_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        tdcs_row.Add(self.tdcs_text, 0, wx.ALL, 5)
        tab1_sizer.Add(tdcs_row, 0, wx.EXPAND)

        # TDCS-C text field
        tdcs_c_row = wx.BoxSizer(wx.HORIZONTAL)
        tdcs_c_label = wx.StaticText(tab1, label="TDCS-C Score:", size=(120, -1))
        self.tdcs_c_text = wx.TextCtrl(tab1, size=(100, -1))
        self.tdcs_c_text.SetValue("—")  # Default to em dash
        tdcs_c_row.Add(tdcs_c_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        tdcs_c_row.Add(self.tdcs_c_text, 0, wx.ALL, 5)
        tab1_sizer.Add(tdcs_c_row, 0, wx.EXPAND)

        # ED Status text field  
        ed_row = wx.BoxSizer(wx.HORIZONTAL)
        ed_label = wx.StaticText(tab1, label="ED Status:", size=(120, -1))
        self.ed_text = wx.TextCtrl(tab1, size=(200, -1))
        self.ed_text.SetValue("—")  # Default to em dash
        ed_row.Add(ed_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        ed_row.Add(self.ed_text, 0, wx.ALL, 5)
        tab1_sizer.Add(ed_row, 0, wx.EXPAND)

        # Create lab value displays (text field + checkboxes only, no grab buttons)
        for lab_name, lab_config in LABS_CONFIG.items():
            row = wx.BoxSizer(wx.HORIZONTAL)
            label = wx.StaticText(tab1, label=lab_name, size=(120, -1))
            txt = wx.TextCtrl(tab1, size=(200, -1))
            high_cb = wx.CheckBox(tab1, label="High")
            low_cb = wx.CheckBox(tab1, label="Low")
            
            var_name = lab_config["var"]
            text_ctrls[var_name] = txt
            check_ctrls[var_name] = (high_cb, low_cb)
            
            row.Add(label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            row.Add(txt, 1, wx.ALL | wx.EXPAND, 5)
            row.Add(high_cb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            row.Add(low_cb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            tab1_sizer.Add(row, 0, wx.EXPAND)

        # Medication text field (populated from grabbed data)
        med_row = wx.BoxSizer(wx.HORIZONTAL)
        med_label = wx.StaticText(tab1, label="Medication:", size=(120, -1))
        self.td_med_text = wx.TextCtrl(tab1, size=(350, -1))
        med_row.Add(med_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        med_row.Add(self.td_med_text, 0, wx.ALL, 5)
        tab1_sizer.Add(med_row, 0, wx.EXPAND)

        # Response text field (populated from grabbed questionnaire data)
        resp_row = wx.BoxSizer(wx.HORIZONTAL)
        resp_label = wx.StaticText(tab1, label="Response:", size=(120, -1))
        self.td_response_text = wx.TextCtrl(tab1, size=(350, -1))
        self.td_response_text.SetHint("e.g. satisfied, much better, no change")
        resp_row.Add(resp_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        resp_row.Add(self.td_response_text, 0, wx.ALL, 5)
        tab1_sizer.Add(resp_row, 0, wx.EXPAND)

        # Side Effects text field (populated from grabbed questionnaire data)
        se_row = wx.BoxSizer(wx.HORIZONTAL)
        se_label = wx.StaticText(tab1, label="Side Effects:", size=(120, -1))
        self.td_side_effects_text = wx.TextCtrl(tab1, size=(350, -1))
        self.td_side_effects_text.SetValue("No side effects reported")
        se_row.Add(se_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        se_row.Add(self.td_side_effects_text, 0, wx.ALL, 5)
        tab1_sizer.Add(se_row, 0, wx.EXPAND)

        # Dx label
        dx_label = wx.StaticText(tab1, label="Dx:")
        tab1_sizer.Add(dx_label, 0, wx.LEFT | wx.TOP, 10)

        # Dx checkboxes
        dx_row = wx.BoxSizer(wx.HORIZONTAL)
        self.dx_td_cb = wx.CheckBox(tab1, label="Testosterone Deficiency")
        self.dx_ed_cb = wx.CheckBox(tab1, label="ED")
        self.dx_td_cb.Bind(wx.EVT_CHECKBOX, self.on_dx_checkbox)
        self.dx_ed_cb.Bind(wx.EVT_CHECKBOX, self.on_dx_checkbox)
        dx_row.Add(self.dx_td_cb, 0, wx.ALL, 5)
        dx_row.Add(self.dx_ed_cb, 0, wx.ALL, 5)
        tab1_sizer.Add(dx_row, 0, wx.EXPAND)

        # PMH selector for Rx Note (manual multi-select)
        pmh_box = wx.StaticBox(tab1, label="PMH (for Rx Note)")
        pmh_sizer = wx.StaticBoxSizer(pmh_box, wx.VERTICAL)
        pmh_row = wx.BoxSizer(wx.HORIZONTAL)
        self.pmh_select_btn = wx.Button(tab1, label="Select PMH…")
        self.pmh_select_btn.Bind(wx.EVT_BUTTON, lambda evt: self.open_pmh_dialog())
        pmh_row.Add(self.pmh_select_btn, 0, wx.ALL, 5)
        self.pmh_summary = wx.StaticText(tab1, label="Current: none (noncontributory)")
        pmh_row.Add(self.pmh_summary, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        pmh_sizer.Add(pmh_row, 0, wx.EXPAND)
        tab1_sizer.Add(pmh_sizer, 0, wx.EXPAND | wx.ALL, 5)
        # Initialize PMH summary based on current selection
        self.update_pmh_summary()

        # Template buttons section (show only intended static buttons for this tab)
        template_label = wx.StaticText(tab1, label="Templates:")
        tab1_sizer.Add(template_label, 0, wx.LEFT | wx.TOP, 10)

        # Create template buttons
        for tbtn in TEMPLATE_BUTTONS:
            btn = wx.Button(tab1, label=tbtn["label"])
            # Optional tooltips for static buttons that map to hotkeys
            tooltip_text = None
            if tbtn.get("dynamic_labs"):
                btn.Bind(wx.EVT_BUTTON, partial(insert_template, dynamic_labs=True))
            elif tbtn.get("rx_note"):
                # Prefer template if present; fallback to legacy builder
                def _do_rx_btn():
                    try:
                        if 'Rx Note' in templates:
                            self.insert_specific_template('Rx Note')
                        elif 'Rx note' in templates:
                            self.insert_specific_template('Rx note')
                        elif 'RxNote' in templates:
                            self.insert_specific_template('RxNote')
                        else:
                            insert_template(rx_note=True)
                    except Exception:
                        insert_template(rx_note=True)
                btn.Bind(wx.EVT_BUTTON, lambda event: _do_rx_btn())
                tooltip_text = "Rx note (Ctrl+Alt+N)"
            elif tbtn.get("lab_message"):
                # Prefer Lab Message template; fallback to legacy builder
                def _do_labmsg_btn():
                    try:
                        if 'Lab Message' in templates:
                            self.insert_specific_template('Lab Message')
                        else:
                            insert_template(lab_message=True)
                    except Exception:
                        insert_template(lab_message=True)
                btn.Bind(wx.EVT_BUTTON, lambda event: _do_labmsg_btn())
                tooltip_text = "Lab Message (Ctrl+Alt+F)"
            elif tbtn.get("referral_note"):
                btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_specific_template('Referral note'))
                tooltip_text = "Referral note (Ctrl+Alt+C)"
            elif tbtn.get("template_name"):
                tpl_name = tbtn["template_name"]
                btn.Bind(wx.EVT_BUTTON, lambda event, name=tpl_name: self.insert_specific_template(name))
                tooltip_text = tpl_name
            elif tbtn.get("clear_all"):
                btn.Bind(wx.EVT_BUTTON, clear_all)
            elif tbtn.get("show_matrix"):
                btn.Bind(wx.EVT_BUTTON, lambda event: show_clinical_matrix())
            else:
                btn.Bind(wx.EVT_BUTTON, partial(insert_template, tbtn.get("template")))
            if tooltip_text:
                btn.SetToolTip(tooltip_text)
            tab1_sizer.Add(btn, 0, wx.ALL, 5)

        # Finish Tab 1
        tab1.SetSizer(tab1_sizer)
        tab1.Layout()

        # --- Tab 2: Hair Loss tools ---
        tab2 = wx.Panel(self.notebook)
        tab2_sizer = wx.BoxSizer(wx.VERTICAL)

        # Template dropdown row at top of tab
        self.tab2_template_dropdown = self.create_template_dropdown_row_inline(tab2, tab2_sizer, "Hair Loss")

        # Grab button at the top (matching Tab 1 style)
        grab_hair_btn = wx.Button(tab2, label="Grab", size=(200, 40))
        grab_hair_btn.Bind(wx.EVT_BUTTON, lambda event: self.grab_hair())
        grab_hair_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        # Tooltip: Global F4 triggers Grab for the active tab
        grab_hair_btn.SetToolTip("Grab (F4)")
        tab2_sizer.Add(grab_hair_btn, 0, wx.ALL | wx.CENTER, 10)

        # Grab mode toggle for Tab 2
        gm_row2 = wx.BoxSizer(wx.HORIZONTAL)
        gm_label2 = wx.StaticText(tab2, label="Grab mode:", size=(120, -1))
        self.grab_mode_choice_tab2 = wx.Choice(tab2, choices=["CDP / Playwright (fast)", "Clipboard (select all + copy)"])
        self.grab_mode_choice_tab2.SetSelection(0 if USE_CDP_FOR_GRAB else 1)
        self.grab_mode_choice_tab2.Bind(wx.EVT_CHOICE, lambda evt: self._on_grab_mode_change(evt))
        self.grab_mode_choice_tab2.SetToolTip("CDP: reads browser DOM directly (fast, no screen interaction). Clipboard: hides GUI, Ctrl+A/Ctrl+C from EMR.")
        gm_row2.Add(gm_label2, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        gm_row2.Add(self.grab_mode_choice_tab2, 0, wx.ALL, 5)
        tab2_sizer.Add(gm_row2, 0, wx.EXPAND)

        # Parsed fields
        med_row = wx.BoxSizer(wx.HORIZONTAL)
        med_label = wx.StaticText(tab2, label="Medication:", size=(120, -1))
        self.hair_med_text = wx.TextCtrl(tab2, size=(350, -1))
        med_row.Add(med_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        med_row.Add(self.hair_med_text, 0, wx.ALL, 5)
        tab2_sizer.Add(med_row, 0, wx.EXPAND)

        # Hair loss symptoms (HSX)
        hsx_row = wx.BoxSizer(wx.HORIZONTAL)
        hsx_label = wx.StaticText(tab2, label="Hair loss symptoms:", size=(120, -1))
        self.hair_hsx_text = wx.TextCtrl(tab2, size=(350, -1))
        hsx_row.Add(hsx_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        hsx_row.Add(self.hair_hsx_text, 0, wx.ALL, 5)
        tab2_sizer.Add(hsx_row, 0, wx.EXPAND)

        # Hair loss response (HVAR)
        hvar_row = wx.BoxSizer(wx.HORIZONTAL)
        hvar_label = wx.StaticText(tab2, label="Hair loss response:", size=(120, -1))
        self.hair_hvar_text = wx.TextCtrl(tab2, size=(350, -1))
        hvar_row.Add(hvar_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        hvar_row.Add(self.hair_hvar_text, 0, wx.ALL, 5)
        tab2_sizer.Add(hvar_row, 0, wx.EXPAND)

        # Hair loss exam findings
        hair_exam_label = wx.StaticText(tab2, label="Exam findings (from images):")
        tab2_sizer.Add(hair_exam_label, 0, wx.LEFT | wx.TOP, 10)
        
        # Hair exam checkboxes - organized in rows
        hair_exam_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Row 1
        hair_exam_row1 = wx.BoxSizer(wx.HORIZONTAL)
        self.hair_exam_front_hairline = wx.CheckBox(tab2, label="front hairline")
        self.hair_exam_top_crown = wx.CheckBox(tab2, label="top/crown") 
        self.hair_exam_widening_part = wx.CheckBox(tab2, label="widening of the part")
        hair_exam_row1.Add(self.hair_exam_front_hairline, 0, wx.ALL, 5)
        hair_exam_row1.Add(self.hair_exam_top_crown, 0, wx.ALL, 5)
        hair_exam_row1.Add(self.hair_exam_widening_part, 0, wx.ALL, 5)
        hair_exam_sizer.Add(hair_exam_row1, 0, wx.EXPAND)
        
        # Row 2  
        hair_exam_row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.hair_exam_diffuse_thinning = wx.CheckBox(tab2, label="diffuse thinning")
        self.hair_exam_confluent = wx.CheckBox(tab2, label="confluent from the front hairline to the crown")
        hair_exam_row2.Add(self.hair_exam_diffuse_thinning, 0, wx.ALL, 5)
        hair_exam_row2.Add(self.hair_exam_confluent, 0, wx.ALL, 5)
        hair_exam_sizer.Add(hair_exam_row2, 0, wx.EXPAND)
        
        # Row 3
        hair_exam_row3 = wx.BoxSizer(wx.HORIZONTAL)
        self.hair_exam_near_front = wx.CheckBox(tab2, label="near the front with sparing of the hairline")
        hair_exam_row3.Add(self.hair_exam_near_front, 0, wx.ALL, 5)
        hair_exam_sizer.Add(hair_exam_row3, 0, wx.EXPAND)
        
        tab2_sizer.Add(hair_exam_sizer, 0, wx.EXPAND)

        # Hair loss template buttons
        hair_template_label = wx.StaticText(tab2, label="Templates:")
        tab2_sizer.Add(hair_template_label, 0, wx.LEFT | wx.TOP, 10)
        
        hair_btn_row = wx.BoxSizer(wx.HORIZONTAL)
        insert_hair_btn = wx.Button(tab2, label="Insert Hair Note (Follow-up)")
        insert_hair_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_hair_note(followup=True))
        # Tooltip: System-wide when Hair Loss tab is selected
        insert_hair_btn.SetToolTip("Insert Hair Note (Follow-up) (Ctrl+Alt+F)")
        hair_btn_row.Add(insert_hair_btn, 0, wx.ALL, 5)

        insert_initial_btn = wx.Button(tab2, label="Insert Hair Note (Initial)")
        insert_initial_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_hair_note(initial=True))
        # Tooltip: System-wide when Hair Loss tab is selected
        insert_initial_btn.SetToolTip("Insert Hair Note (Initial) (Ctrl+Alt+N)")
        hair_btn_row.Add(insert_initial_btn, 0, wx.ALL, 5)
        
        tab2_sizer.Add(hair_btn_row, 0, wx.EXPAND)

        # Limited Check-in note button on its own row
        hair_btn_row2 = wx.BoxSizer(wx.HORIZONTAL)
        limited_checkin_btn = wx.Button(tab2, label="Limited Check-in note")
        limited_checkin_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_hair_limited_checkin_note())
        # Tooltip: System-wide when Hair Loss tab is selected
        limited_checkin_btn.SetToolTip("Limited Check-in note (Ctrl+Alt+C)")
        hair_btn_row2.Add(limited_checkin_btn, 0, wx.ALL, 5)
        print("✅ LIMITED CHECK-IN NOTE BUTTON CREATED SUCCESSFULLY!")  # Debug output
        
        tab2_sizer.Add(hair_btn_row2, 0, wx.EXPAND)

        tab2.SetSizer(tab2_sizer)
        tab2.Layout()

        # --- Tab 3: Photoaging tools ---
        tab3 = wx.Panel(self.notebook)
        tab3_sizer = wx.BoxSizer(wx.VERTICAL)

        # Template dropdown row at top of tab
        self.tab3_template_dropdown = self.create_template_dropdown_row_inline(tab3, tab3_sizer, "Photoaging")

        # Grab button at the top (matching other tabs)
        grab_photoaging_btn = wx.Button(tab3, label="Grab", size=(200, 40))
        grab_photoaging_btn.Bind(wx.EVT_BUTTON, lambda event: self.grab_photoaging())
        grab_photoaging_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        tab3_sizer.Add(grab_photoaging_btn, 0, wx.ALL | wx.CENTER, 10)

        # Grab mode toggle for Tab 3
        gm_row3 = wx.BoxSizer(wx.HORIZONTAL)
        gm_label3 = wx.StaticText(tab3, label="Grab mode:", size=(120, -1))
        self.grab_mode_choice_tab3 = wx.Choice(tab3, choices=["CDP / Playwright (fast)", "Clipboard (select all + copy)"])
        self.grab_mode_choice_tab3.SetSelection(0 if USE_CDP_FOR_GRAB else 1)
        self.grab_mode_choice_tab3.Bind(wx.EVT_CHOICE, lambda evt: self._on_grab_mode_change(evt))
        self.grab_mode_choice_tab3.SetToolTip("CDP: reads browser DOM directly (fast, no screen interaction). Clipboard: hides GUI, Ctrl+A/Ctrl+C from EMR.")
        gm_row3.Add(gm_label3, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        gm_row3.Add(self.grab_mode_choice_tab3, 0, wx.ALL, 5)
        tab3_sizer.Add(gm_row3, 0, wx.EXPAND)

        # Parsed fields for photoaging
        # Medication field
        photoaging_med_row = wx.BoxSizer(wx.HORIZONTAL)
        photoaging_med_label = wx.StaticText(tab3, label="Medication:", size=(120, -1))
        self.photoaging_med_text = wx.TextCtrl(tab3, size=(350, -1))
        photoaging_med_row.Add(photoaging_med_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        photoaging_med_row.Add(self.photoaging_med_text, 0, wx.ALL, 5)
        tab3_sizer.Add(photoaging_med_row, 0, wx.EXPAND)

        # Skin goals field
        skin_goals_row = wx.BoxSizer(wx.HORIZONTAL)
        skin_goals_label = wx.StaticText(tab3, label="Skin goals:", size=(120, -1))
        self.photoaging_goals_text = wx.TextCtrl(tab3, size=(350, -1))
        skin_goals_row.Add(skin_goals_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        skin_goals_row.Add(self.photoaging_goals_text, 0, wx.ALL, 5)
        tab3_sizer.Add(skin_goals_row, 0, wx.EXPAND)

        # Previous retinoid use field
        retinoid_history_row = wx.BoxSizer(wx.HORIZONTAL)
        retinoid_history_label = wx.StaticText(tab3, label="Retinoid history:", size=(120, -1))
        self.photoaging_retinoid_text = wx.TextCtrl(tab3, size=(350, -1))
        retinoid_history_row.Add(retinoid_history_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        retinoid_history_row.Add(self.photoaging_retinoid_text, 0, wx.ALL, 5)
        tab3_sizer.Add(retinoid_history_row, 0, wx.EXPAND)

        # Photoaging exam findings
        photoaging_exam_label = wx.StaticText(tab3, label="Exam findings (from images):")
        tab3_sizer.Add(photoaging_exam_label, 0, wx.LEFT | wx.TOP, 10)
        
        # Photoaging exam checkboxes - organized in rows
        photoaging_exam_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Row 1
        photoaging_exam_row1 = wx.BoxSizer(wx.HORIZONTAL)
        self.photoaging_exam_fine_lines = wx.CheckBox(tab3, label="Fine lines")
        self.photoaging_exam_wrinkles = wx.CheckBox(tab3, label="Wrinkles")
        self.photoaging_exam_crows_feet = wx.CheckBox(tab3, label="Crow's feet")
        photoaging_exam_row1.Add(self.photoaging_exam_fine_lines, 0, wx.ALL, 5)
        photoaging_exam_row1.Add(self.photoaging_exam_wrinkles, 0, wx.ALL, 5)
        photoaging_exam_row1.Add(self.photoaging_exam_crows_feet, 0, wx.ALL, 5)
        photoaging_exam_sizer.Add(photoaging_exam_row1, 0, wx.EXPAND)
        
        # Row 2
        photoaging_exam_row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.photoaging_exam_pigmentation = wx.CheckBox(tab3, label="Pigmentation changes")
        self.photoaging_exam_age_spots = wx.CheckBox(tab3, label="Age spots/sun spots")
        self.photoaging_exam_melasma = wx.CheckBox(tab3, label="Melasma")
        photoaging_exam_row2.Add(self.photoaging_exam_pigmentation, 0, wx.ALL, 5)
        photoaging_exam_row2.Add(self.photoaging_exam_age_spots, 0, wx.ALL, 5)
        photoaging_exam_row2.Add(self.photoaging_exam_melasma, 0, wx.ALL, 5)
        photoaging_exam_sizer.Add(photoaging_exam_row2, 0, wx.EXPAND)
        
        # Row 3
        photoaging_exam_row3 = wx.BoxSizer(wx.HORIZONTAL)
        self.photoaging_exam_texture_changes = wx.CheckBox(tab3, label="Texture changes")
        self.photoaging_exam_enlarged_pores = wx.CheckBox(tab3, label="Enlarged pores")
        self.photoaging_exam_loss_elasticity = wx.CheckBox(tab3, label="Loss of elasticity")
        photoaging_exam_row3.Add(self.photoaging_exam_texture_changes, 0, wx.ALL, 5)
        photoaging_exam_row3.Add(self.photoaging_exam_enlarged_pores, 0, wx.ALL, 5)
        photoaging_exam_row3.Add(self.photoaging_exam_loss_elasticity, 0, wx.ALL, 5)
        photoaging_exam_sizer.Add(photoaging_exam_row3, 0, wx.EXPAND)
        
        # Row 4
        photoaging_exam_row4 = wx.BoxSizer(wx.HORIZONTAL)
        self.photoaging_exam_inflammation = wx.CheckBox(tab3, label="Signs of inflammation")
        self.photoaging_exam_scarring = wx.CheckBox(tab3, label="Acne scarring")
        self.photoaging_exam_normal = wx.CheckBox(tab3, label="No significant findings")
        photoaging_exam_row4.Add(self.photoaging_exam_inflammation, 0, wx.ALL, 5)
        photoaging_exam_row4.Add(self.photoaging_exam_scarring, 0, wx.ALL, 5)
        photoaging_exam_row4.Add(self.photoaging_exam_normal, 0, wx.ALL, 5)
        photoaging_exam_sizer.Add(photoaging_exam_row4, 0, wx.EXPAND)
        
        tab3_sizer.Add(photoaging_exam_sizer, 0, wx.EXPAND)

        # Photoaging template buttons
        photoaging_template_label = wx.StaticText(tab3, label="Templates:")
        tab3_sizer.Add(photoaging_template_label, 0, wx.LEFT | wx.TOP, 10)
        
        photoaging_btn_row = wx.BoxSizer(wx.HORIZONTAL)
        insert_photoaging_btn = wx.Button(tab3, label="Insert Photoaging Note")
        insert_photoaging_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_photoaging_note())
        photoaging_btn_row.Add(insert_photoaging_btn, 0, wx.ALL, 5)
        
        tab3_sizer.Add(photoaging_btn_row, 0, wx.EXPAND)

        tab3.SetSizer(tab3_sizer)
        tab3.Layout()

        # --- Tab 4: Sexual Health tools ---
        tab4 = wx.Panel(self.notebook)
        tab4_sizer = wx.BoxSizer(wx.VERTICAL)

        # Template dropdown row at top of tab
        self.tab4_template_dropdown = self.create_template_dropdown_row_inline(tab4, tab4_sizer, "Sexual Health")

        # Grab button at the top (matching other tabs)
        grab_sexual_health_btn = wx.Button(tab4, label="Grab", size=(200, 40))
        grab_sexual_health_btn.Bind(wx.EVT_BUTTON, lambda event: self.grab_sexual_health())
        grab_sexual_health_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        # Tooltip: Global F4 triggers Grab for the active tab
        grab_sexual_health_btn.SetToolTip("Grab (F4)")
        tab4_sizer.Add(grab_sexual_health_btn, 0, wx.ALL | wx.CENTER, 10)

        # Grab mode toggle for Sexual Health (unified CDP vs Clipboard)
        gm_row4 = wx.BoxSizer(wx.HORIZONTAL)
        gm_label4 = wx.StaticText(tab4, label="Grab mode:", size=(120, -1))
        self.grab_mode_choice_tab4 = wx.Choice(tab4, choices=["CDP / Playwright (fast)", "Clipboard (select all + copy)"])
        self.grab_mode_choice_tab4.SetSelection(0 if USE_CDP_FOR_GRAB else 1)
        self.grab_mode_choice_tab4.Bind(wx.EVT_CHOICE, lambda evt: self._on_grab_mode_change(evt))
        self.grab_mode_choice_tab4.SetToolTip("CDP: reads browser DOM directly (fast, no screen interaction). Clipboard: hides GUI, Ctrl+A/Ctrl+C from EMR.")
        gm_row4.Add(gm_label4, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        gm_row4.Add(self.grab_mode_choice_tab4, 0, wx.ALL, 5)
        tab4_sizer.Add(gm_row4, 0, wx.EXPAND)

        # Parsed fields for sexual health
        # Medication field
        sexual_health_med_row = wx.BoxSizer(wx.HORIZONTAL)
        sexual_health_med_label = wx.StaticText(tab4, label="Medication:", size=(120, -1))
        self.sexual_health_med_text = wx.TextCtrl(tab4, size=(350, -1))
        sexual_health_med_row.Add(sexual_health_med_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sexual_health_med_row.Add(self.sexual_health_med_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sexual_health_med_row, 0, wx.EXPAND)

        # Effectiveness field
        effectiveness_row = wx.BoxSizer(wx.HORIZONTAL)
        effectiveness_label = wx.StaticText(tab4, label="Effective?", size=(120, -1))
        self.sexual_health_effectiveness_text = wx.TextCtrl(tab4, size=(350, -1))
        effectiveness_row.Add(effectiveness_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        effectiveness_row.Add(self.sexual_health_effectiveness_text, 0, wx.ALL, 5)
        tab4_sizer.Add(effectiveness_row, 0, wx.EXPAND)

        # Blood pressure field
        bp_row = wx.BoxSizer(wx.HORIZONTAL)
        bp_label = wx.StaticText(tab4, label="BP", size=(120, -1))
        self.sexual_health_bp_text = wx.TextCtrl(tab4, size=(350, -1))
        bp_row.Add(bp_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        bp_row.Add(self.sexual_health_bp_text, 0, wx.ALL, 5)
        tab4_sizer.Add(bp_row, 0, wx.EXPAND)

        # --- SH Initial visit fields (auto-populated by Grab, editable) ---
        sh_initial_label = wx.StaticText(tab4, label="── Initial Visit ──")
        sh_initial_label.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_BOLD))
        tab4_sizer.Add(sh_initial_label, 0, wx.LEFT | wx.TOP, 10)

        sh_age_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_age_label = wx.StaticText(tab4, label="Age:", size=(120, -1))
        self.sexual_health_age_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_age_row.Add(sh_age_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_age_row.Add(self.sexual_health_age_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_age_row, 0, wx.EXPAND)

        sh_visit_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_visit_label = wx.StaticText(tab4, label="Visit type:", size=(120, -1))
        self.sexual_health_visit_type_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_visit_row.Add(sh_visit_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_visit_row.Add(self.sexual_health_visit_type_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_visit_row, 0, wx.EXPAND)

        sh_onset_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_onset_label = wx.StaticText(tab4, label="Onset:", size=(120, -1))
        self.sexual_health_onset_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_onset_row.Add(sh_onset_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_onset_row.Add(self.sexual_health_onset_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_onset_row, 0, wx.EXPAND)

        sh_freq_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_freq_label = wx.StaticText(tab4, label="Frequency:", size=(120, -1))
        self.sexual_health_frequency_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_freq_row.Add(sh_freq_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_freq_row.Add(self.sexual_health_frequency_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_freq_row, 0, wx.EXPAND)

        sh_ed_desc_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_ed_desc_label = wx.StaticText(tab4, label="ED description:", size=(120, -1))
        self.sexual_health_ed_description_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_ed_desc_row.Add(sh_ed_desc_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_ed_desc_row.Add(self.sexual_health_ed_description_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_ed_desc_row, 0, wx.EXPAND)

        sh_ed_char_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_ed_char_label = wx.StaticText(tab4, label="ED character.:", size=(120, -1))
        self.sexual_health_ed_characterization_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_ed_char_row.Add(sh_ed_char_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_ed_char_row.Add(self.sexual_health_ed_characterization_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_ed_char_row, 0, wx.EXPAND)

        sh_ehs_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_ehs_label = wx.StaticText(tab4, label="EHS:", size=(120, -1))
        self.sexual_health_ehs_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_ehs_row.Add(sh_ehs_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_ehs_row.Add(self.sexual_health_ehs_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_ehs_row, 0, wx.EXPAND)

        sh_pep_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_pep_label = wx.StaticText(tab4, label="PEP score:", size=(120, -1))
        self.sexual_health_pep_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_pep_row.Add(sh_pep_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_pep_row.Add(self.sexual_health_pep_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_pep_row, 0, wx.EXPAND)

        sh_past_tx_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_past_tx_label = wx.StaticText(tab4, label="Past treatments:", size=(120, -1))
        self.sexual_health_past_treatments_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_past_tx_row.Add(sh_past_tx_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_past_tx_row.Add(self.sexual_health_past_treatments_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_past_tx_row, 0, wx.EXPAND)

        sh_ros_pos_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_ros_pos_label = wx.StaticText(tab4, label="ROS (+):", size=(120, -1))
        self.sexual_health_ros_pos_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_ros_pos_row.Add(sh_ros_pos_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_ros_pos_row.Add(self.sexual_health_ros_pos_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_ros_pos_row, 0, wx.EXPAND)

        sh_ros_neg_row = wx.BoxSizer(wx.HORIZONTAL)
        sh_ros_neg_label = wx.StaticText(tab4, label="ROS (-):", size=(120, -1))
        self.sexual_health_ros_neg_text = wx.TextCtrl(tab4, size=(350, -1))
        sh_ros_neg_row.Add(sh_ros_neg_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sh_ros_neg_row.Add(self.sexual_health_ros_neg_text, 0, wx.ALL, 5)
        tab4_sizer.Add(sh_ros_neg_row, 0, wx.EXPAND)

        # Response text selector (used in SH Follow-up template for {response_text})
        response_row = wx.BoxSizer(wx.HORIZONTAL)
        response_label = wx.StaticText(tab4, label="Response:", size=(120, -1))
        self.sexual_health_response_choice = wx.Choice(tab4, choices=[
            "a satisfactory response",
            "a good response",
            "an excellent response",
            "a partial response",
            "a poor response",
            "no improvement",
        ])
        self.sexual_health_response_choice.SetSelection(0)
        response_row.Add(response_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        response_row.Add(self.sexual_health_response_choice, 0, wx.ALL, 5)
        tab4_sizer.Add(response_row, 0, wx.EXPAND)

        # Hair loss location field
        hair_location_row = wx.BoxSizer(wx.HORIZONTAL)
        hair_location_label = wx.StaticText(tab4, label="Hair loss location:", size=(120, -1))
        self.sexual_health_hair_location_text = wx.TextCtrl(tab4, size=(350, -1))
        hair_location_row.Add(hair_location_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        hair_location_row.Add(self.sexual_health_hair_location_text, 0, wx.ALL, 5)
        tab4_sizer.Add(hair_location_row, 0, wx.EXPAND)

        # Hair loss additional symptoms field
        hair_sxx_row = wx.BoxSizer(wx.HORIZONTAL)
        hair_sxx_label = wx.StaticText(tab4, label="Hair loss sxx:", size=(120, -1))
        self.sexual_health_hair_sxx_text = wx.TextCtrl(tab4, size=(350, -1))
        hair_sxx_row.Add(hair_sxx_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        hair_sxx_row.Add(self.sexual_health_hair_sxx_text, 0, wx.ALL, 5)
        tab4_sizer.Add(hair_sxx_row, 0, wx.EXPAND)

        # Change request focus selector
        change_focus_row = wx.BoxSizer(wx.HORIZONTAL)
        change_focus_label = wx.StaticText(tab4, label="Change focus:", size=(120, -1))
        self.sexual_health_change_choice = wx.Choice(tab4, choices=["Cadence", "Dose number", "Medication"])
        self.sexual_health_change_choice.SetSelection(0)
        change_focus_row.Add(change_focus_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        change_focus_row.Add(self.sexual_health_change_choice, 0, wx.ALL, 5)
        tab4_sizer.Add(change_focus_row, 0, wx.EXPAND)

        # Change details text field
        change_detail_row = wx.BoxSizer(wx.HORIZONTAL)
        change_detail_label = wx.StaticText(tab4, label="Change details:", size=(120, -1))
        self.sexual_health_change_detail = wx.TextCtrl(tab4, size=(350, -1))
        change_detail_row.Add(change_detail_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        change_detail_row.Add(self.sexual_health_change_detail, 0, wx.ALL, 5)
        tab4_sizer.Add(change_detail_row, 0, wx.EXPAND)

        # Plan action selector
        plan_action_row = wx.BoxSizer(wx.HORIZONTAL)
        plan_action_label = wx.StaticText(tab4, label="Plan action:", size=(120, -1))
        self.sexual_health_plan_choice = wx.Choice(tab4, choices=["Continue present treatment", "Start treatment", "Change treatment to"])
        self.sexual_health_plan_choice.SetSelection(0)
        plan_action_row.Add(plan_action_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        plan_action_row.Add(self.sexual_health_plan_choice, 0, wx.ALL, 5)
        tab4_sizer.Add(plan_action_row, 0, wx.EXPAND)

        # Sexual health diagnosis checkboxes
        sexual_health_dx_label = wx.StaticText(tab4, label="Diagnosis:")
        tab4_sizer.Add(sexual_health_dx_label, 0, wx.LEFT | wx.TOP, 10)
        
        sexual_health_dx_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.sexual_health_dx_ed = wx.CheckBox(tab4, label="ED")
        self.sexual_health_dx_pe = wx.CheckBox(tab4, label="PE")
        self.sexual_health_dx_pe_like = wx.CheckBox(tab4, label="PE-like ejaculatory dysfunction")
        self.sexual_health_dx_hair_loss = wx.CheckBox(tab4, label="Hair Loss")
        sexual_health_dx_sizer.Add(self.sexual_health_dx_ed, 0, wx.ALL, 5)
        sexual_health_dx_sizer.Add(self.sexual_health_dx_pe, 0, wx.ALL, 5)
        sexual_health_dx_sizer.Add(self.sexual_health_dx_pe_like, 0, wx.ALL, 5)
        sexual_health_dx_sizer.Add(self.sexual_health_dx_hair_loss, 0, wx.ALL, 5)
        tab4_sizer.Add(sexual_health_dx_sizer, 0, wx.EXPAND)

        self._suppress_sexual_health_dx_event = False
        for _cb in (self.sexual_health_dx_ed, self.sexual_health_dx_pe, self.sexual_health_dx_pe_like, self.sexual_health_dx_hair_loss):
            _cb.Bind(wx.EVT_CHECKBOX, self.on_sexual_health_dx_checkbox)

        # Sexual health template buttons
        sexual_health_template_label = wx.StaticText(tab4, label="Templates:")
        tab4_sizer.Add(sexual_health_template_label, 0, wx.LEFT | wx.TOP, 10)
        
        # Row 1: follow-up and plan note
        sexual_health_btn_row = wx.BoxSizer(wx.HORIZONTAL)
        insert_sexual_health_btn = wx.Button(tab4, label="Insert Follow-up Note")
        insert_sexual_health_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_sexual_health_note())
        sexual_health_btn_row.Add(insert_sexual_health_btn, 0, wx.ALL, 5)
        # New brief template button (Dx + Plan)
        insert_sexual_health_brief_btn = wx.Button(tab4, label="Insert Plan Note")
        insert_sexual_health_brief_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_sexual_health_brief_template())
        sexual_health_btn_row.Add(insert_sexual_health_brief_btn, 0, wx.ALL, 5)

        # Row 2: specific change buttons replacing the old single 'Change Template'
        sexual_health_btn_row2 = wx.BoxSizer(wx.HORIZONTAL)
        sh_change_cadence_btn = wx.Button(tab4, label="Change cadence")
        sh_change_cadence_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_sh_change_cadence())
        sexual_health_btn_row2.Add(sh_change_cadence_btn, 0, wx.ALL, 5)
        sh_change_number_btn = wx.Button(tab4, label="Change number")
        sh_change_number_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_sh_change_number())
        sexual_health_btn_row2.Add(sh_change_number_btn, 0, wx.ALL, 5)
        sh_change_med_btn = wx.Button(tab4, label="Change med")
        sh_change_med_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_sh_change_medication())
        sexual_health_btn_row2.Add(sh_change_med_btn, 0, wx.ALL, 5)
        
        # Row 3: Hair loss button
        sexual_health_btn_row3 = wx.BoxSizer(wx.HORIZONTAL)
        insert_hair_info_btn = wx.Button(tab4, label="Insert Hair Info")
        insert_hair_info_btn.Bind(wx.EVT_BUTTON, lambda event: self.insert_hair_info())
        insert_hair_info_btn.SetToolTip("Insert Hair Info (Ctrl+Alt+K)")
        sexual_health_btn_row3.Add(insert_hair_info_btn, 0, wx.ALL, 5)

        # --- Keyboard shortcuts for Tab 4 (Sexual Health) ---
        # Where to change shortcuts:
        #   1) Edit the tooltip strings below to reflect the keys you want to use.
        #   2) Edit the 'accel_specs' list below to change the actual key bindings.
        #      Each entry is a tuple: (flags, key, command_id)
        #      Examples:
        #        - Ctrl+Alt+F: (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('F'), self.ID_SH_FOLLOWUP)
        #        - Ctrl+Shift+N: (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('N'), self.ID_SH_FOLLOWUP)
        #        - Function key F5: (wx.ACCEL_NORMAL, wx.WXK_F5, self.ID_SH_FOLLOWUP)

        # Show shortcuts in tooltips for quick discoverability
        insert_sexual_health_btn.SetToolTip("Insert Follow-up Note (Ctrl+Alt+F)")
        insert_sexual_health_brief_btn.SetToolTip("Insert Plan Note (Ctrl+Alt+N)")
        sh_change_cadence_btn.SetToolTip("Insert 'change cadence' template")
        sh_change_number_btn.SetToolTip("Insert 'change number' template")
        sh_change_med_btn.SetToolTip("Insert 'change medication' template")

        # Unique command IDs used by the accelerator table
        # Using NewControlId provides stable integers appropriate for accelerators and EVT_MENU
        self.ID_SH_FOLLOWUP = getattr(self, "ID_SH_FOLLOWUP", wx.Window.NewControlId())
        self.ID_SH_PLAN = getattr(self, "ID_SH_PLAN", wx.Window.NewControlId())
        self.ID_SH_CHANGE = getattr(self, "ID_SH_CHANGE", wx.Window.NewControlId())
        self.ID_SH_HAIR_INFO = getattr(self, "ID_SH_HAIR_INFO", wx.Window.NewControlId())

        # Route accelerator events to the same handlers as the buttons
        tab4.Bind(wx.EVT_MENU, lambda evt: self.insert_sexual_health_note(), id=self.ID_SH_FOLLOWUP)
        tab4.Bind(wx.EVT_MENU, lambda evt: self.insert_sexual_health_brief_template(), id=self.ID_SH_PLAN)
        tab4.Bind(wx.EVT_MENU, lambda evt: self.insert_sexual_health_change_template(), id=self.ID_SH_CHANGE)
        tab4.Bind(wx.EVT_MENU, lambda evt: self.insert_hair_info(), id=self.ID_SH_HAIR_INFO)

        # Define the accelerator table here. Change these to customize the key combos.
        accel_specs = [
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('F'), self.ID_SH_FOLLOWUP),  # Follow-up Note
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('N'), self.ID_SH_PLAN),      # Plan Note
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('C'), self.ID_SH_CHANGE),    # Change Template
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('K'), self.ID_SH_HAIR_INFO), # Hair Info
        ]
        accel_entries = []
        for flags, key, cmd_id in accel_specs:
            ent = wx.AcceleratorEntry()
            ent.Set(flags, key, cmd_id)
            accel_entries.append(ent)
        # Attach the accelerator table to Tab 4 so shortcuts work when this tab has focus
        tab4.SetAcceleratorTable(wx.AcceleratorTable(accel_entries))
        
        tab4_sizer.Add(sexual_health_btn_row, 0, wx.EXPAND)
        tab4_sizer.Add(sexual_health_btn_row2, 0, wx.EXPAND)
        tab4_sizer.Add(sexual_health_btn_row3, 0, wx.EXPAND)

        tab4.SetSizer(tab4_sizer)
        tab4.Layout()

        # Initialize click counters and default click method before building UI
        self.browser_click_count = 0
        self.xy_click_count = 0
        self.cdp_click_count = 0
        self.clicker_method_mode = "cdp"

        # --- Tab 5: Auto Clicker ---
        tab5 = wx.Panel(self.notebook)
        tab5_sizer = wx.BoxSizer(wx.VERTICAL)

        # Auto Clicker title
        clicker_title = wx.StaticText(tab5, label="Auto Clicker & URL Monitor")
        clicker_title.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        tab5_sizer.Add(clicker_title, 0, wx.ALL | wx.CENTER, 10)

        # Control buttons - vertical layout
        clicker_controls = wx.BoxSizer(wx.VERTICAL)
        
        self.clicker_start_btn = wx.Button(tab5, label="Start Autoclick: Dashboard", size=(280, 40))
        self.clicker_start_btn.Bind(wx.EVT_BUTTON, lambda event: self.toggle_auto_clicker())
        self.clicker_start_btn.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        
        self.clicker_test_btn = wx.Button(tab5, label="Detect Browser", size=(280, 30))
        self.clicker_test_btn.Bind(wx.EVT_BUTTON, lambda event: self.test_chrome_connection())
        
        # Detect Tab button uses browser-based header detection to switch tabs
        self.detect_tab_btn = wx.Button(tab5, label="Detect Tab", size=(280, 30))
        self.detect_tab_btn.Bind(wx.EVT_BUTTON, lambda event: self.detect_visit_type_and_switch_tab())

        # Quick Next Task button - clicks floating button then "Get Next Task"
        self.quick_next_btn = wx.Button(tab5, label="Quick Next Task", size=(280, 30))
        self.quick_next_btn.Bind(wx.EVT_BUTTON, lambda event: self.quick_next_task())
        
        # Start Autoclick: In-Visit - runs quick_next_task repeatedly at interval until URL changes
        self.clicker_invisit_btn = wx.Button(tab5, label="Start Autoclick: In-Visit", size=(280, 40))
        self.clicker_invisit_btn.Bind(wx.EVT_BUTTON, lambda event: self.toggle_invisit_clicker())
        self.clicker_invisit_btn.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        
        # Auto-Refresh Page - refreshes current tab every 3 minutes ±30%
        self.page_refresh_btn = wx.Button(tab5, label="Start Auto-Refresh Page", size=(280, 40))
        self.page_refresh_btn.Bind(wx.EVT_BUTTON, lambda event: self.toggle_page_refresh())
        self.page_refresh_btn.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        
        self.clicker_status_text = wx.StaticText(tab5, label="Status: STOPPED")
        self.clicker_status_text.SetForegroundColour(wx.Colour(255, 0, 0))  # Red

        clicker_controls.Add(self.clicker_start_btn, 0, wx.ALL | wx.EXPAND, 5)
        clicker_controls.Add(self.clicker_test_btn, 0, wx.ALL | wx.EXPAND, 5)
        clicker_controls.Add(self.detect_tab_btn, 0, wx.ALL | wx.EXPAND, 5)
        clicker_controls.Add(self.quick_next_btn, 0, wx.ALL | wx.EXPAND, 5)
        clicker_controls.Add(self.clicker_invisit_btn, 0, wx.ALL | wx.EXPAND, 5)
        clicker_controls.Add(self.page_refresh_btn, 0, wx.ALL | wx.EXPAND, 5)
        self.js_overlay_btn = wx.Button(tab5, label="Switch to JS Overlay", size=(280, 34))
        self.js_overlay_btn.Bind(wx.EVT_BUTTON, self.inject_js_overlay)
        clicker_controls.Add(self.js_overlay_btn, 0, wx.ALL | wx.EXPAND, 5)
        clicker_controls.Add(self.clicker_status_text, 0, wx.ALL | wx.CENTER, 8)
        tab5_sizer.Add(clicker_controls, 0, wx.EXPAND | wx.ALL, 10)

        # Click method and last-click info
        info_row = wx.BoxSizer(wx.HORIZONTAL)
        self.clicker_method_text = wx.StaticText(tab5, label="Click method: CDP (0)")
        self.clicker_method_text.SetForegroundColour(wx.Colour(30, 144, 255))  # DodgerBlue for CDP
        self.clicker_last_text = wx.StaticText(tab5, label="Last click: —")
        info_row.Add(self.clicker_method_text, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        info_row.AddSpacer(20)
        info_row.Add(self.clicker_last_text, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        tab5_sizer.Add(info_row, 0, wx.EXPAND | wx.LEFT, 12)

        # Notification toggle: popup vs beep
        notif_row = wx.BoxSizer(wx.HORIZONTAL)
        self.popup_toggle = wx.CheckBox(tab5, label="Popup on new task (instead of beep)")
        self.popup_toggle.SetValue(True)
        self.popup_toggle.Bind(wx.EVT_CHECKBOX, self.on_popup_toggle)
        notif_row.Add(self.popup_toggle, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        tab5_sizer.Add(notif_row, 0, wx.EXPAND | wx.LEFT, 12)

        # App-wide status label moved into Auto Clicker tab
        self.grab_status_text = wx.StaticText(tab5, label="Ready")
        self.grab_status_text.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        tab5_sizer.Add(self.grab_status_text, 0, wx.LEFT | wx.BOTTOM, 8)

        # Settings
        settings_box = wx.StaticBox(tab5, label="Click Settings")
        settings_sizer = wx.StaticBoxSizer(settings_box, wx.VERTICAL)
        
        # Coordinates
        coord_row = wx.BoxSizer(wx.HORIZONTAL)
        coord_label = wx.StaticText(tab5, label="Click Position:")
        self.clicker_x_text = wx.TextCtrl(tab5, size=(80, -1), value="2600")
        self.clicker_y_text = wx.TextCtrl(tab5, size=(80, -1), value="400")
        coord_row.Add(coord_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        coord_row.Add(wx.StaticText(tab5, label="X:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        coord_row.Add(self.clicker_x_text, 0, wx.ALL, 5)
        coord_row.Add(wx.StaticText(tab5, label="Y:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        coord_row.Add(self.clicker_y_text, 0, wx.ALL, 5)
        settings_sizer.Add(coord_row, 0, wx.EXPAND)
        
        # Interval
        interval_row = wx.BoxSizer(wx.HORIZONTAL)
        interval_label = wx.StaticText(tab5, label="Interval (seconds):")
        self.clicker_interval_text = wx.TextCtrl(tab5, size=(80, -1), value="3")
        interval_row.Add(interval_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        interval_row.Add(self.clicker_interval_text, 0, wx.ALL, 5)
        settings_sizer.Add(interval_row, 0, wx.EXPAND)

        # Click method selection
        method_row = wx.BoxSizer(wx.HORIZONTAL)
        method_row.Add(wx.StaticText(tab5, label="Click via:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.method_browser_rb = wx.RadioButton(tab5, label="Browser", style=wx.RB_GROUP)
        self.method_xy_rb = wx.RadioButton(tab5, label="X/Y screen")
        self.method_cdp_rb = wx.RadioButton(tab5, label="CDP")
        self.method_cdp_rb.SetValue(True)
        for rb in (self.method_browser_rb, self.method_xy_rb, self.method_cdp_rb):
            rb.Bind(wx.EVT_RADIOBUTTON, self.on_click_method_changed)
            method_row.Add(rb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.on_click_method_changed()
        settings_sizer.Add(method_row, 0, wx.EXPAND)

        tab5_sizer.Add(settings_sizer, 0, wx.EXPAND | wx.ALL, 10)

        custom_hotkey_box = wx.StaticBox(tab5, label="CDP Hotkeys")
        custom_hotkey_sizer = wx.StaticBoxSizer(custom_hotkey_box, wx.VERTICAL)
        custom_note = wx.StaticText(
            tab5,
            label="Edit the CUSTOM_CDP_HOTKEYS list near the auto-hide helpers to change these shortcuts.")
        custom_note.Wrap(520)
        custom_hotkey_sizer.Add(custom_note, 0, wx.ALL | wx.EXPAND, 6)
        if self.CUSTOM_CDP_HOTKEYS:
            for entry in self.CUSTOM_CDP_HOTKEYS:
                hotkey_label = entry.get("hotkey", "")
                description = entry.get("description") or ""
                line = f"{hotkey_label}"
                if description:
                    line = f"{line} — {description}"
                custom_hotkey_sizer.Add(wx.StaticText(tab5, label=line), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        else:
            custom_hotkey_sizer.Add(wx.StaticText(tab5, label="No custom CDP hotkeys defined."), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        tab5_sizer.Add(custom_hotkey_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # URL Monitor
        url_box = wx.StaticBox(tab5, label="URL Monitor")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)
        
        self.clicker_url_text = wx.StaticText(tab5, label="Current URL: Not detected")
        self.clicker_url_text.Wrap(600)
        url_sizer.Add(self.clicker_url_text, 0, wx.ALL | wx.EXPAND, 5)
        
        tab5_sizer.Add(url_sizer, 1, wx.EXPAND | wx.ALL, 10)

        # Instructions
        instructions = wx.StaticText(tab5, 
            label=f"Instructions:\n• Set click coordinates and interval\n• Click 'Start Clicking' to begin\n• Clicking auto-stops when URL changes\n• Chrome/Thorium must be running with remote debugging (--remote-debugging-port={CDP_DEBUG_PORT})")
        instructions.Wrap(600)
        tab5_sizer.Add(instructions, 0, wx.ALL | wx.EXPAND, 10)
        
        # Initialize in-visit clicker state
        self.invisit_running = False
        self.invisit_thread = None

        tab5.SetSizer(tab5_sizer)
        tab5.Layout()

        # --- Tab 6: Performance Anxiety ---
        tab6 = wx.Panel(self.notebook)
        tab6_sizer = wx.BoxSizer(wx.VERTICAL)

        # Template dropdown row at top of tab
        self.tab6_template_dropdown = self.create_template_dropdown_row_inline(tab6, tab6_sizer, "Performance Anxiety")

        # Grab button
        pa_grab_btn = wx.Button(tab6, label="Grab", size=(200, 40))
        pa_grab_btn.Bind(wx.EVT_BUTTON, lambda evt: self.grab_performance_anxiety())
        pa_grab_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        tab6_sizer.Add(pa_grab_btn, 0, wx.ALL | wx.CENTER, 10)

        # Grab mode toggle for Tab 6
        gm_row6 = wx.BoxSizer(wx.HORIZONTAL)
        gm_label6 = wx.StaticText(tab6, label="Grab mode:", size=(120, -1))
        self.grab_mode_choice_tab6 = wx.Choice(tab6, choices=["CDP / Playwright (fast)", "Clipboard (select all + copy)"])
        self.grab_mode_choice_tab6.SetSelection(0 if USE_CDP_FOR_GRAB else 1)
        self.grab_mode_choice_tab6.Bind(wx.EVT_CHOICE, lambda evt: self._on_grab_mode_change(evt))
        self.grab_mode_choice_tab6.SetToolTip("CDP: reads browser DOM directly (fast, no screen interaction). Clipboard: hides GUI, Ctrl+A/Ctrl+C from EMR.")
        gm_row6.Add(gm_label6, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        gm_row6.Add(self.grab_mode_choice_tab6, 0, wx.ALL, 5)
        tab6_sizer.Add(gm_row6, 0, wx.EXPAND)

        # Performance Anxiety form fields
        pa_intro = wx.StaticText(tab6, label="Performance Anxiety")
        tab6_sizer.Add(pa_intro, 0, wx.ALL, 6)

        # Medication (from EMR)
        pa_med_row = wx.BoxSizer(wx.HORIZONTAL)
        pa_med_row.Add(wx.StaticText(tab6, label="Medication:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.pa_med_text = wx.TextCtrl(tab6, size=(420, -1))
        pa_med_row.Add(self.pa_med_text, 0, wx.ALL, 5)
        tab6_sizer.Add(pa_med_row, 0, wx.EXPAND)

        # Situations/fears (from EMR)
        pa_sit_row = wx.BoxSizer(wx.HORIZONTAL)
        pa_sit_row.Add(wx.StaticText(tab6, label="Situations:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.pa_situations_text = wx.TextCtrl(tab6, size=(420, 60), style=wx.TE_MULTILINE)
        pa_sit_row.Add(self.pa_situations_text, 0, wx.ALL, 5)
        tab6_sizer.Add(pa_sit_row, 0, wx.EXPAND)

        # Somatic symptoms (from EMR)
        pa_sym_row = wx.BoxSizer(wx.HORIZONTAL)
        pa_sym_row.Add(wx.StaticText(tab6, label="Symptoms:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.pa_symptoms_text = wx.TextCtrl(tab6, size=(420, 60), style=wx.TE_MULTILINE)
        pa_sym_row.Add(self.pa_symptoms_text, 0, wx.ALL, 5)
        tab6_sizer.Add(pa_sym_row, 0, wx.EXPAND)

        # Vitals
        pa_vitals_row = wx.BoxSizer(wx.HORIZONTAL)
        pa_vitals_row.Add(wx.StaticText(tab6, label="BP:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.pa_bp_text = wx.TextCtrl(tab6, size=(100, -1))
        pa_vitals_row.Add(self.pa_bp_text, 0, wx.ALL, 5)
        pa_vitals_row.Add(wx.StaticText(tab6, label="Pulse:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.pa_pulse_text = wx.TextCtrl(tab6, size=(120, -1))
        pa_vitals_row.Add(self.pa_pulse_text, 0, wx.ALL, 5)
        tab6_sizer.Add(pa_vitals_row, 0, wx.EXPAND)

        # Follow-up controls
        pa_followup_box = wx.StaticBox(tab6, label="Follow-up Settings")
        pa_followup = wx.StaticBoxSizer(pa_followup_box, wx.VERTICAL)
        # Response
        resp_row = wx.BoxSizer(wx.HORIZONTAL)
        resp_row.Add(wx.StaticText(tab6, label="Response:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.pa_resp_good_rb = wx.RadioButton(tab6, label="good", style=wx.RB_GROUP)
        self.pa_resp_bad_rb = wx.RadioButton(tab6, label="bad")
        self.pa_resp_good_rb.SetValue(True)
        resp_row.Add(self.pa_resp_good_rb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        resp_row.Add(self.pa_resp_bad_rb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        pa_followup.Add(resp_row, 0, wx.EXPAND)
        # Side effects
        se_row = wx.BoxSizer(wx.HORIZONTAL)
        se_row.Add(wx.StaticText(tab6, label="Side effects:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.pa_se_without_rb = wx.RadioButton(tab6, label="without", style=wx.RB_GROUP)
        self.pa_se_with_rb = wx.RadioButton(tab6, label="with")
        self.pa_se_without_rb.SetValue(True)
        se_row.Add(self.pa_se_without_rb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        se_row.Add(self.pa_se_with_rb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        pa_followup.Add(se_row, 0, wx.EXPAND)
        tab6_sizer.Add(pa_followup, 0, wx.EXPAND | wx.ALL, 6)

        # Note insertion buttons
        pa_btn_row = wx.BoxSizer(wx.HORIZONTAL)
        pa_insert_initial_btn = wx.Button(tab6, label="Insert Initial Note")
        pa_insert_follow_btn = wx.Button(tab6, label="Insert Follow-up Note")
        pa_insert_initial_btn.Bind(wx.EVT_BUTTON, lambda evt: self.insert_performance_anxiety_note(initial=True))
        pa_insert_follow_btn.Bind(wx.EVT_BUTTON, lambda evt: self.insert_performance_anxiety_note(followup=True))
        pa_btn_row.Add(pa_insert_initial_btn, 0, wx.ALL, 5)
        pa_btn_row.Add(pa_insert_follow_btn, 0, wx.ALL, 5)
        tab6_sizer.Add(pa_btn_row, 0, wx.EXPAND)

        tab6.SetSizer(tab6_sizer)
        tab6.Layout()

        # --- Tab 7: Birth Control ---
        tab7 = wx.Panel(self.notebook)
        tab7_sizer = wx.BoxSizer(wx.VERTICAL)

        # Template dropdown row at top of tab
        self.tab7_template_dropdown = self.create_template_dropdown_row_inline(tab7, tab7_sizer, "Birth Control")

        bc_grab_btn = wx.Button(tab7, label="Grab", size=(200, 40))
        bc_grab_btn.Bind(wx.EVT_BUTTON, lambda evt: self.grab_birth_control())
        bc_grab_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        bc_grab_btn.SetToolTip("Grab Birth Control data (F4)")
        tab7_sizer.Add(bc_grab_btn, 0, wx.ALL | wx.CENTER, 10)

        # Grab mode toggle for Tab 7
        gm_row7 = wx.BoxSizer(wx.HORIZONTAL)
        gm_label7 = wx.StaticText(tab7, label="Grab mode:", size=(120, -1))
        self.grab_mode_choice_tab7 = wx.Choice(tab7, choices=["CDP / Playwright (fast)", "Clipboard (select all + copy)"])
        self.grab_mode_choice_tab7.SetSelection(0 if USE_CDP_FOR_GRAB else 1)
        self.grab_mode_choice_tab7.Bind(wx.EVT_CHOICE, lambda evt: self._on_grab_mode_change(evt))
        self.grab_mode_choice_tab7.SetToolTip("CDP: reads browser DOM directly (fast, no screen interaction). Clipboard: hides GUI, Ctrl+A/Ctrl+C from EMR.")
        gm_row7.Add(gm_label7, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        gm_row7.Add(self.grab_mode_choice_tab7, 0, wx.ALL, 5)
        tab7_sizer.Add(gm_row7, 0, wx.EXPAND)

        form_grid = wx.FlexGridSizer(rows=0, cols=2, hgap=10, vgap=8)
        form_grid.AddGrowableCol(1, 1)

        form_grid.Add(wx.StaticText(tab7, label="Medication:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.bc_med_text = wx.TextCtrl(tab7, size=(400, -1))
        form_grid.Add(self.bc_med_text, 0, wx.ALL | wx.EXPAND, 4)

        form_grid.Add(wx.StaticText(tab7, label="Last menstrual period:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.bc_lmp_text = wx.TextCtrl(tab7, size=(200, -1))
        form_grid.Add(self.bc_lmp_text, 0, wx.ALL | wx.EXPAND, 4)

        form_grid.Add(wx.StaticText(tab7, label="Blood pressure:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.bc_bp_text = wx.TextCtrl(tab7, size=(200, -1))
        form_grid.Add(self.bc_bp_text, 0, wx.ALL | wx.EXPAND, 4)

        form_grid.Add(wx.StaticText(tab7, label="Side effects:"), 0, wx.ALL | wx.ALIGN_TOP, 4)
        self.bc_side_effects_text = wx.TextCtrl(tab7, size=(400, 80), style=wx.TE_MULTILINE | wx.TE_WORDWRAP)
        form_grid.Add(self.bc_side_effects_text, 0, wx.ALL | wx.EXPAND, 4)
        form_grid.AddGrowableRow(3)

        form_grid.Add(wx.StaticText(tab7, label="Visit type:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        visit_row = wx.BoxSizer(wx.HORIZONTAL)
        self.bc_initial_rb = wx.RadioButton(tab7, label="Initial visit", style=wx.RB_GROUP)
        self.bc_followup_rb = wx.RadioButton(tab7, label="Follow-up visit")
        self.bc_initial_rb.SetValue(True)
        visit_row.Add(self.bc_initial_rb, 0, wx.RIGHT, 10)
        visit_row.Add(self.bc_followup_rb, 0)
        form_grid.Add(visit_row, 0, wx.ALL | wx.EXPAND, 4)

        form_grid.Add(wx.StaticText(tab7, label="Med history changes:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.bc_history_changes_text = wx.TextCtrl(tab7, size=(400, -1))
        form_grid.Add(self.bc_history_changes_text, 0, wx.ALL | wx.EXPAND, 4)

        tab7_sizer.Add(form_grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        pmh_box = wx.StaticBox(tab7, label="Past medical history")
        pmh_sizer = wx.StaticBoxSizer(pmh_box, wx.VERTICAL)
        pmh_grid = wx.GridSizer(rows=2, cols=3, hgap=12, vgap=4)
        self.bc_pmh_checkboxes = {}
        for key, display in BIRTH_CONTROL_PMH_OPTIONS:
            cb = wx.CheckBox(tab7, label=display)
            self.bc_pmh_checkboxes[key] = cb
            pmh_grid.Add(cb, 0, wx.ALL, 2)
        pmh_sizer.Add(pmh_grid, 0, wx.EXPAND | wx.ALL, 4)

        other_row = wx.BoxSizer(wx.HORIZONTAL)
        other_row.Add(wx.StaticText(tab7, label="Other:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 2)
        self.bc_pmh_other_text = wx.TextCtrl(tab7, size=(320, -1))
        other_row.Add(self.bc_pmh_other_text, 1, wx.ALL | wx.EXPAND, 2)
        pmh_sizer.Add(other_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        tab7_sizer.Add(pmh_sizer, 0, wx.EXPAND | wx.ALL, 12)

        bc_btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.bc_insert_followup_btn = wx.Button(tab7, label="Insert Follow-up Note")
        self.bc_insert_initial_btn = wx.Button(tab7, label="Insert Initial Note")
        self.bc_insert_followup_btn.Bind(wx.EVT_BUTTON, lambda evt: self.insert_birth_control_followup_note())
        self.bc_insert_initial_btn.Bind(wx.EVT_BUTTON, lambda evt: self.insert_birth_control_initial_note())
        self.bc_insert_followup_btn.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.bc_insert_initial_btn.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.bc_insert_followup_btn.SetToolTip("Insert Follow-up Note (Ctrl+Alt+F)")
        self.bc_insert_initial_btn.SetToolTip("Insert Initial Note (Ctrl+Alt+N)")
        bc_btn_row.Add(self.bc_insert_followup_btn, 0, wx.ALL, 6)
        bc_btn_row.Add(self.bc_insert_initial_btn, 0, wx.ALL, 6)
        tab7_sizer.Add(bc_btn_row, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        # Birth Control tab accelerators
        self.ID_BC_FOLLOWUP = getattr(self, "ID_BC_FOLLOWUP", wx.Window.NewControlId())
        self.ID_BC_INITIAL = getattr(self, "ID_BC_INITIAL", wx.Window.NewControlId())
        self.ID_BC_GRAB = getattr(self, "ID_BC_GRAB", wx.Window.NewControlId())
        tab7.Bind(wx.EVT_MENU, lambda evt: self.insert_birth_control_followup_note(), id=self.ID_BC_FOLLOWUP)
        tab7.Bind(wx.EVT_MENU, lambda evt: self.insert_birth_control_initial_note(), id=self.ID_BC_INITIAL)
        tab7.Bind(wx.EVT_MENU, lambda evt: self.grab_birth_control(), id=self.ID_BC_GRAB)
        bc_accel_entries = []
        for flags, key, cmd_id in [
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('F'), self.ID_BC_FOLLOWUP),
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('N'), self.ID_BC_INITIAL),
            (wx.ACCEL_NORMAL, wx.WXK_F4, self.ID_BC_GRAB),
        ]:
            entry = wx.AcceleratorEntry()
            entry.Set(flags, key, cmd_id)
            bc_accel_entries.append(entry)
        tab7.SetAcceleratorTable(wx.AcceleratorTable(bc_accel_entries))

        tab7.SetSizer(tab7_sizer)
        tab7.Layout()

        # Add tabs to notebook
        self.notebook.AddPage(tab1, "T Deficiency")
        self.notebook.AddPage(tab2, "Hair Loss")
        self.notebook.AddPage(tab3, "Photoaging")
        self.notebook.AddPage(tab4, "Sexual Health")
        self.notebook.AddPage(tab5, "Auto Clicker")
        self.notebook.AddPage(tab6, "Performance Anxiety")
        self.notebook.AddPage(tab7, "Birth Control")

        # Record index for T Deficiency, Sexual Health and Hair Loss tabs so we can toggle global hotkeys based on selection
        try:
            self._tab_index_t_def = None
            self._tab_index_sexual_health = None
            self._tab_index_hair_loss = None
            self._tab_index_performance_anxiety = None
            self._tab_index_birth_control = None
            for i in range(self.notebook.GetPageCount()):
                page = self.notebook.GetPage(i)
                if page is tab1:
                    self._tab_index_t_def = i
                if page is tab4:
                    self._tab_index_sexual_health = i
                if page is tab2:
                    self._tab_index_hair_loss = i
                if page is tab6:
                    self._tab_index_performance_anxiety = i
                if page is tab7:
                    self._tab_index_birth_control = i
        except Exception:
            self._tab_index_t_def = 0       # Fallback to known order
            self._tab_index_sexual_health = 3  # Fallback to known order
            self._tab_index_hair_loss = 1      # Fallback to known order
            self._tab_index_performance_anxiety = 5
            self._tab_index_birth_control = 6

        # Add the visit-type line just under the tabs area, then the notebook
        top_info_sizer = wx.BoxSizer(wx.HORIZONTAL)
        info_label_sizer = wx.BoxSizer(wx.VERTICAL)
        info_label_sizer.Add(self.visit_type_text, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)
        info_label_sizer.Add(self.patient_location_text, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        top_info_sizer.Add(info_label_sizer, 1, wx.EXPAND)
        
        # Edit Templates button (opens template file for current tab)
        self.edit_templates_btn = wx.Button(panel, label="Edit Templates")
        self.edit_templates_btn.SetToolTip("Edit template text file for current tab (opens in Notepad)")
        self.edit_templates_btn.Bind(wx.EVT_BUTTON, self.on_edit_templates_global)
        top_info_sizer.Add(self.edit_templates_btn, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        
        self.detect_visit_btn = wx.Button(panel, label="Detect Visit")
        self.detect_visit_btn.SetToolTip("Detect the active EMR visit type and switch to that tab")
        self.detect_visit_btn.Bind(wx.EVT_BUTTON, lambda evt: self.detect_visit_type_and_switch_tab())
        top_info_sizer.Add(self.detect_visit_btn, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        
        main_sizer.Add(top_info_sizer, 0, wx.EXPAND)
        
        # Track current tab for Edit Templates button
        self._current_template_tab = "T Deficiency"
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_notebook_page_changed)
        
        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)

        # Initialize auto-refresh state
        self.auto_refresh_enabled = False
        self.auto_refresh_thread = None
        
        # Initialize page refresh state
        self.page_refresh_running = False
        self.page_refresh_thread = None

        # Initialize URL monitoring for automatic tab switching (disabled)
        self.url_monitoring_enabled = False
        self.current_url = ""
        self.url_monitor_thread = None
        # CDP visit-type monitor state
        self._cdp_monitor_thread = None
        self._cdp_last_urls = {}
        self._cdp_monitor_running = True
        self._cdp_driver = None
        self._cdp_driver_thread_id = None
        self._cdp_active_url = None
        self._cdp_last_visit_key = None
        self._cdp_last_grab_key = None
        self._cdp_last_display = None
        # Start CDP visit-type monitor (no flicker)
        self._start_cdp_visit_monitor()
        
        # URL monitoring disabled per user request

        panel.SetSizer(main_sizer)
        panel.Layout()
        
        # Get screen dimensions and set window to full height
        display_size = wx.GetDisplaySize()
        screen_height = display_size.GetHeight()
        
        self.Fit()
        self.SetMinSize((600, 800))
        self.SetSize((700, screen_height - 100))  # Full height minus taskbar space

        # Initialize a persistent HTTP session for speed
        try:
            self.requests_session = requests.Session()
        except Exception:
            self.requests_session = None
        # Notification mode and popup reference
        self.notify_with_popup = True
        self.new_task_popup = None
        self._browser_grabber_cache = None

        # Delay used before inserting text via GLOBAL hotkeys (in milliseconds).
        # Increase this if characters are occasionally missed by Chrome/EMR on paste.
        # Where to change: adjust self.global_insert_delay_ms below (e.g., 150–350 ms)
        self.global_insert_delay_ms = 200

        # Setup global F4 hotkey for grab functionality (now with Playwright support)
        self.setup_global_hotkeys()
        self._register_custom_cdp_hotkeys()

        # Bind close event to cleanup hotkeys
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def _canonicalize_visit_type(self, visit: Optional[str]) -> Optional[str]:
        if not visit:
            return None
        text = str(visit).strip()
        if not text:
            return None
        low = text.lower()
        if "emr dashboard" in low or low == "dashboard":
            return "EMR Dashboard"
        if "testosterone" in low and "T Deficiency" in VISIT_TAB_INDICES:
            return "T Deficiency"
        if "low t" in low and "T Deficiency" in VISIT_TAB_INDICES:
            return "T Deficiency"
        if "birth control" in low and "Birth Control" in VISIT_TAB_INDICES:
            return "Birth Control"
        if "contraception" in low and "Birth Control" in VISIT_TAB_INDICES:
            return "Birth Control"
        # Direct match to known labels
        for label in VISIT_TAB_INDICES:
            if low == label.lower():
                return label
        # Substring match against known labels
        for label in VISIT_TAB_INDICES:
            if label.lower() in low:
                return label
        # Keyword fallbacks (GUI-specific list)
        for keyword, mapped in VISIT_TYPE_FALLBACK_KEYWORDS:
            if keyword in low and mapped in VISIT_TAB_INDICES:
                return mapped
        # CDP keyword list has additional aliases
        for keyword, mapped in CDP_VISIT_TYPE_KEYWORDS:
            if keyword in low and mapped in VISIT_TAB_INDICES:
                return mapped
        return None

    def _reset_emr_view_via_script(self) -> bool:
        """Clear selection and scroll to top using the shared Playwright connection."""
        grabber = getattr(self, "_browser_grabber_cache", None)
        created = False
        try:
            if grabber is None:
                grabber = BrowserEMRGrabber()
                self._browser_grabber_cache = grabber
                created = True

            if not grabber.connect_to_chrome():
                if created:
                    self._browser_grabber_cache = None
                return False

            if not grabber._ensure_emr_tab():
                return False

            driver = getattr(grabber, "driver", None)
            if not driver:
                return False

            try:
                grabber._switch_to_default()
            except Exception:
                pass

            script = """
(() => {
    try {
        const sel = window.getSelection && window.getSelection();
        if (sel && sel.removeAllRanges) {
            sel.removeAllRanges();
        }
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') {
            active.blur();
        }
        if (typeof window.scrollTo === 'function') {
            window.scrollTo({left: 0, top: 0, behavior: 'instant'});
        }
        return true;
    } catch (err) {
        return false;
    }
})();
"""

            result = driver.execute_script(script)
            return bool(result) if result is not None else True
        except Exception as exc:
            print(f"Reset EMR view error: {exc}")
            return False
        finally:
            if created and getattr(self, "_browser_grabber_cache", None) is not grabber:
                try:
                    grabber.disconnect()
                except Exception:
                    pass
                try:
                    grabber.shutdown_playwright()
                except Exception:
                    pass

    def _format_patient_location_label(self, summary: Optional[Dict[str, Any]]) -> str:
        base = "Location: —"
        if not summary:
            return base

        state_display = (summary.get("state_display") or summary.get("state") or "").strip()
        city = (summary.get("city") or "").strip()
        state_code = (summary.get("state") or state_display or "").strip()
        distance = summary.get("distance_miles")

        if not state_display:
            return base

        if city and state_code.upper() == "WI":
            if distance is not None:
                return f"Location: {city}, WI - {distance:.1f} mi from Chippewa Falls, WI"
            return f"Location: {city}, WI"

        return f"Location: {state_display}"

    def _update_patient_location_label(self, summary: Optional[Dict[str, Any]]) -> None:
        label = self._format_patient_location_label(summary)
        self.patient_location_text.SetLabel(label)
        self._last_location_summary = summary

    def refresh_patient_location_async(self) -> None:
        if self._location_worker and self._location_worker.is_alive():
            return

        def worker():
            try:
                grabber = BrowserEMRGrabber()
            except Exception as exc:
                print(f"Location refresh init error: {exc}")
                self._location_worker = None
                return

            try:
                if not grabber.connect_to_chrome():
                    print("Location refresh error: could not attach to Chrome")
                    return

                summary = grabber.fetch_patient_location_summary(debug_print=True)
                print(f"[PATIENT LOCATION REFRESH] {summary}")
                wx.CallAfter(self._update_patient_location_label, summary)
            except Exception as exc:
                print(f"Location refresh error: {exc}")
            finally:
                try:
                    grabber.disconnect()
                except Exception:
                    pass
                try:
                    grabber.shutdown_playwright()
                except Exception:
                    pass
                self._location_worker = None

        self._location_worker = threading.Thread(target=worker, daemon=True)
        self._location_worker.start()

    def _schedule_tab_switch(self, visit_label: Optional[str], status_msg: Optional[str] = None) -> bool:
        canonical = self._canonicalize_visit_type(visit_label)
        print(f"[TAB SWITCH] Called with visit_label={visit_label!r}, canonical={canonical!r}, status_msg={status_msg!r}")
        if not canonical:
            print(f"[TAB SWITCH] No canonical visit type for label: {visit_label!r}")
            return False
        target_tab = VISIT_TAB_INDICES.get(canonical)
        if canonical == "EMR Dashboard":
            target_tab = AUTO_CLICKER_TAB_INDEX

        if target_tab is None:
            print(f"[TAB SWITCH] No tab index for canonical visit type: {canonical!r}")
            return False

        def apply_switch():
            current = self.notebook.GetSelection()
            print(f"[TAB SWITCH] Current tab: {current}, Target tab: {target_tab}")
            if current != target_tab:
                print(f"[TAB SWITCH] Switching to tab {target_tab} for visit type {canonical}")
                self.notebook.SetSelection(target_tab)
            else:
                print(f"[TAB SWITCH] Already on correct tab {target_tab}")
            if status_msg and hasattr(self, "grab_status_text"):
                self.grab_status_text.SetLabel(status_msg)
            self._last_visit_tab = canonical

        wx.CallAfter(apply_switch)
        return True

    def _queue_auto_grab(self, canonical: str, source_url: Optional[str]) -> None:
        if not canonical:
            return

        if canonical == "EMR Dashboard":
            return

        key = ((source_url or ""), canonical)
        if self._cdp_last_grab_key == key:
            return

        self._cdp_last_grab_key = key

        if hasattr(self, "grab_status_text"):
            wx.CallAfter(self.grab_status_text.SetLabel, f"Auto-grabbing {canonical} data...")

        def dispatch():
            if getattr(self, "_is_closing", False):
                return
            try:
                if canonical == "Sexual Health":
                    self.grab_sexual_health()
                elif canonical == "Hair Loss":
                    self.grab_hair()
                elif canonical == "Photoaging":
                    self.grab_photoaging()
                elif canonical == "Performance Anxiety":
                    self.grab_performance_anxiety()
                elif canonical == "Birth Control":
                    self.grab_birth_control()
                else:
                    grab_all_labs()
                print(f"[CDP MONITOR] Auto grab dispatched for {canonical} (url={source_url})")
            except Exception as exc:
                print(f"[CDP MONITOR] Auto grab error for {canonical}: {exc}")

        wx.CallAfter(wx.CallLater, AUTO_GRAB_DELAY_MS, dispatch)

    # --- CDP visit-type monitor methods (no flicker) ---
    def _cdp_connect_driver(self):
        current_thread = threading.get_ident()
        if self._cdp_driver is not None:
            if getattr(self, "_cdp_driver_thread_id", None) not in (None, current_thread):
                self._cdp_driver = None
                self._cdp_driver_thread_id = None
            else:
                return

        grabber, _ = self._ensure_browser_grabber()
        if grabber is None:
            print("CDP connect error: browser grabber unavailable")
            return

        last_error = None
        for attempt in range(3):
            try:
                if not grabber._ensure_playwright_browser():
                    raise TimeoutException("Playwright connection failed")

                page = grabber._find_emr_page()
                if page is None:
                    pages = grabber._get_browser_pages()
                    page = pages[0] if pages else None
                if page is None:
                    raise NoSuchElementException(
                        "Could not locate any Chrome page to attach to via CDP."
                    )

                self._cdp_driver = PlaywrightDriverAdapter(grabber._cdp_browser, page)
                self._cdp_driver_thread_id = current_thread
                return
            except Exception as e:
                last_error = e
                self._cdp_driver = None
                self._cdp_driver_thread_id = None
                message = str(e).lower()
                transient = any(
                    token in message
                    for token in (
                        "execution context",
                        "disconnected",
                        "target closed",
                        "timeout",
                    )
                )
                if transient and attempt < 2:
                    time.sleep(0.6 + 0.3 * attempt)
                    continue
                print(f"CDP connect error: {e}")
                if grabber is getattr(self, "_cdp_browser_grabber", None):
                    # Drop cached grabber so the next attempt can start fresh
                    self._cdp_browser_grabber = None
                return

        if last_error:
            print(f"CDP connect error: {last_error}")
            if grabber is getattr(self, "_cdp_browser_grabber", None):
                self._cdp_browser_grabber = None

    def _cdp_list_targets(self):
        try:
            res = self._cdp_driver.execute_cdp_cmd("Target.getTargets", {})
            all_targets = (res or {}).get("targetInfos", []) or []
            # Return ALL page targets - we'll verify actual URL after attaching
            page_targets = [ti for ti in all_targets if ti.get("type") == "page"]
            page_targets.sort(key=lambda ti: 0 if self._cdp_is_emr(ti) else 1)
            return page_targets
        except Exception:
            return []

    def _cdp_is_emr(self, target) -> bool:
        url = ""
        if isinstance(target, str):
            url = target
        elif isinstance(target, dict):
            if target.get("type") != "page":
                return False
            url = target.get("url", "") or ""
        else:
            return False

        url = url.strip()
        if not url:
            return False

        normalized = url.rstrip("/")
        if normalized in EMR_DASHBOARD_BASE_URLS:
            return True

        prefix_https = CDP_EMR_PATIENT_URL_PREFIX
        prefix_http = prefix_https.replace("https://", "http://")
        return url.startswith(prefix_https) or url.startswith(prefix_http)

    def _cdp_eval(self, session_id: str, expression: str, context_id=None):
        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": False,
            "includeCommandLineAPI": True,
            "sessionId": session_id,
        }
        if context_id is not None:
            params["contextId"] = context_id
        try:
            res = self._cdp_driver.execute_cdp_cmd("Runtime.evaluate", params)
            return (res.get("result", {}) or {}).get("value")
        except Exception:
            return None

    def _cdp_log(self, msg: str):
        try:
            if not CDP_DEBUG_VERBOSE:
                return
            ts = time.strftime('%H:%M:%S')
            line = f"[CDP {ts}] {msg}"
            print(line)
        except Exception:
            pass

    def _cdp_get_frame_contexts(self, session_id: str) -> List[int]:
        ctxs: List[int] = []
        try:
            ft = self._cdp_driver.execute_cdp_cmd("Page.getFrameTree", {"sessionId": session_id})
            def walk(node):
                frame = (node or {}).get("frame", {})
                fid = frame.get("id")
                if fid:
                    try:
                        cw = self._cdp_driver.execute_cdp_cmd(
                            "Page.createIsolatedWorld",
                            {"frameId": fid, "worldName": "visit_type_probe", "sessionId": session_id},
                        )
                        cid = cw.get("executionContextId")
                        if isinstance(cid, int):
                            ctxs.append(cid)
                    except Exception:
                        pass
                for ch in (node or {}).get("childFrames", []) or []:
                    walk(ch)
            root = (ft or {}).get("frameTree")
            if root:
                walk(root)
        except Exception:
            pass
        return ctxs

    def _cdp_detect_visit_text(self, target_id: str) -> Optional[tuple]:
        """
        Attach to target and detect visit type.
        Returns: (title_text, actual_url) tuple if successful, None if failed or wrong page
        """
        try:
            # Attach to target
            att = self._cdp_driver.execute_cdp_cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session_id = att.get("sessionId")
            if not session_id:
                return None
            
            try:
                # Verify actual URL
                expr_url = "window.location.href"
                actual_url = self._cdp_eval(session_id, expr_url)
                
                if not actual_url:
                    return None
                    
                # Check if this is actually an EMR visit page
                if not self._cdp_is_emr(actual_url):
                    self._cdp_log(f"Skipping non-EMR page: {actual_url}")
                    return None
                
                # Try to get visit type text
                contexts = self._cdp_get_frame_contexts(session_id)
                for ctx in contexts:
                    txt = self._cdp_try_get_header_text(session_id, ctx)
                    if txt:
                        return (txt, actual_url)
                
                return None
                
            finally:
                try:
                    self._cdp_driver.execute_cdp_cmd("Target.detachFromTarget", {"sessionId": session_id})
                except Exception:
                    pass
                    
        except Exception as e:
            return None

    def _start_cdp_visit_monitor(self):
        """Background thread that monitors visit type via CDP"""
        def worker():
            last_text = None
            last_url = None
            
            while self._cdp_monitor_active:
                try:
                    # Get all page targets
                    targets = self._cdp_list_targets()
                    
                    # Try each target until we find an EMR visit page
                    found = False
                    for t in targets:
                        result = self._cdp_detect_visit_text(t['targetId'])
                        if result:
                            text, url = result
                            found = True
                            
                            # Update display if changed
                            if text != last_text or url != last_url:
                                last_text = text
                                last_url = url
                                wx.CallAfter(self._update_visit_display, text)
                                self._cdp_log(f"Visit type detected: {text} | URL: {url}")
                            break
                    
                    if not found:
                        if last_text is not None:
                            last_text = None
                            last_url = None
                            wx.CallAfter(self._update_visit_display, None)
                    
                    time.sleep(2)
                    
                except Exception as e:
                    self._cdp_log(f"Monitor error: {e}")
                    time.sleep(5)
        
        self._cdp_monitor_thread = threading.Thread(target=worker, daemon=True)
        self._cdp_monitor_thread.start()

    def _cdp_get_frame_contexts(self, session_id: str) -> List[int]:
        """Get execution contexts for all frames"""
        ctxs: List[int] = []
        try:
            ft = self._cdp_driver.execute_cdp_cmd("Page.getFrameTree", {"sessionId": session_id})
            def walk(node):
                frame = (node or {}).get("frame", {})
                fid = frame.get("id")
                if fid:
                    try:
                        cw = self._cdp_driver.execute_cdp_cmd(
                            "Page.createIsolatedWorld",
                            {"frameId": fid, "worldName": "visit_type_probe", "sessionId": session_id},
                        )
                        cid = cw.get("executionContextId")
                        if isinstance(cid, int):
                            ctxs.append(cid)
                    except Exception:
                        pass
                for ch in (node or {}).get("childFrames", []) or []:
                    walk(ch)
            root = (ft or {}).get("frameTree")
            if root:
                walk(root)
        except Exception:
            pass
        return ctxs

    def _cdp_try_get_header_text(self, session_id: str, context_id: int = None) -> Optional[str]:
        """Extract visit header text using the module-level CDP_HEADER_EXPRESSION cache after EMR-target-first prioritization."""
        val = self._cdp_eval(session_id, CDP_HEADER_EXPRESSION, context_id=context_id)
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None

    def _cdp_try_get_body_snapshot(self, session_id: str, limit: int = 6000) -> Optional[str]:
        expr = f"""
(() => {{
    try {{
        const body = document && document.body;
        if (!body) return null;
        let txt = body.innerText || body.textContent || '';
        if (!txt) return null;
        txt = txt.replace(/\s+/g, ' ').trim();
        if (!txt) return null;
        return txt.slice(0, {limit});
    }} catch (e) {{
        return null;
    }}
}})();
"""
        val = self._cdp_eval(session_id, expr, context_id=None)
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None

    def _cdp_try_get_dashboard_welcome(self, session_id: str, context_id=None) -> Optional[str]:
        sels_json = json.dumps(EMR_DASHBOARD_WELCOME_SELECTORS)
        expr = (
            """
(() => {
  const sels = __SELS__;

  const read = (node) => {
    if (!node) return null;
    try {
      const raw = (node.innerText || node.textContent || '').trim();
      if (!raw) return null;
      const low = raw.toLowerCase();
      if (!low.startsWith('welcome')) return null;
      return raw;
    } catch (e) {
      return null;
    }
  };

  for (const sel of sels) {
    try {
      const node = document.querySelector(sel);
      const hit = read(node);
      if (hit) return hit;
    } catch (e) {}
  }

  return null;
})();
"""
        ).replace("__SELS__", sels_json)
        val = self._cdp_eval(session_id, expr, context_id=context_id)
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None

    def _cdp_get_frame_contexts(self, session_id: str) -> List[int]:
        ctxs: List[int] = []
        try:
            ft = self._cdp_driver.execute_cdp_cmd("Page.getFrameTree", {"sessionId": session_id})
            def walk(node):
                frame = (node or {}).get("frame", {})
                fid = frame.get("id")
                if fid:
                    try:
                        cw = self._cdp_driver.execute_cdp_cmd(
                            "Page.createIsolatedWorld",
                            {"frameId": fid, "worldName": "visit_type_probe", "sessionId": session_id},
                        )
                        cid = cw.get("executionContextId")
                        if isinstance(cid, int):
                            ctxs.append(cid)
                    except Exception:
                        pass
                for ch in (node or {}).get("childFrames", []) or []:
                    walk(ch)
            root = (ft or {}).get("frameTree")
            if root:
                walk(root)
        except Exception:
            pass
        return ctxs

    def _cdp_detect_visit_text(self, target_id: str) -> Optional[tuple]:
        """
        Attach to target and detect visit type.
        Returns: (title_text, actual_url) tuple if successful, None if failed or wrong page
        """
        try:
            self._cdp_log(f"Attaching to target {target_id}…")
            attach = self._cdp_driver.execute_cdp_cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session_id = attach.get("sessionId")
            if not session_id:
                self._cdp_log("Failed to obtain sessionId on attach")
                return None
        except Exception:
            self._cdp_log("Exception during Target.attachToTarget")
            return None
        try:
            try:
                self._cdp_driver.execute_cdp_cmd("Runtime.enable", {"sessionId": session_id})
            except Exception:
                pass
            
            # Verify we're attached to the correct page by checking window.location.href
            try:
                actual_url = self._cdp_eval(session_id, "window.location.href", context_id=None)
            except Exception:
                actual_url = None

            if not actual_url or not self._cdp_is_emr(actual_url):
                return None

            normalized_url = actual_url.rstrip("/") if isinstance(actual_url, str) else ""
            if normalized_url in EMR_DASHBOARD_BASE_URLS:
                welcome = self._cdp_try_get_dashboard_welcome(session_id, context_id=None)
                if not welcome:
                    ctxs = self._cdp_get_frame_contexts(session_id)
                    for ctx in ctxs:
                        welcome = self._cdp_try_get_dashboard_welcome(session_id, context_id=ctx)
                        if welcome:
                            break
                if welcome:
                    self._cdp_log(f"Dashboard welcome detected: {welcome}")
                else:
                    self._cdp_log("Dashboard detected via URL (no welcome header found)")
                return ("EMR Dashboard", actual_url)
            
            # main world
            self._cdp_log("Probing main world for header text…")
            t = self._cdp_try_get_header_text(session_id, context_id=None)
            if t:
                self._cdp_log("Found header text in main world")
                return (t, actual_url)
            # frames
            ctxs = self._cdp_get_frame_contexts(session_id)
            self._cdp_log(f"Probing {len(ctxs)} frame contexts…")
            for ctx in ctxs:
                t = self._cdp_try_get_header_text(session_id, context_id=ctx)
                if t:
                    self._cdp_log("Found header text in a frame context")
                    return (t, actual_url)
            # Fallback: grab a trimmed body snapshot to classify visit type heuristically
            body_txt = self._cdp_try_get_body_snapshot(session_id)
            if body_txt:
                self._cdp_log("Using body snapshot fallback for visit classification")
                return (body_txt, actual_url)
        finally:
            try:
                self._cdp_driver.execute_cdp_cmd("Target.detachFromTarget", {"sessionId": session_id})
                self._cdp_log("Detached from target")
            except Exception:
                pass
        return None

    def _classify_visit_type(self, text: str) -> str:
        if not text:
            return "Unknown"
        lines = [ln.strip() for ln in str(text).splitlines() if ln and ln.strip()]
        ignore = {"dashboard", "home", "inbox", "settings"}
        # First pass: per-line keyword match, ignoring nav words
        for line in lines:
            l = line.lower().strip()
            if l in ignore:
                continue
            for key, label in CDP_VISIT_TYPE_KEYWORDS:
                if key in l:
                    return label
        # Second pass: any-line keyword match across the whole text
        low_all = str(text).lower()
        for key, label in CDP_VISIT_TYPE_KEYWORDS:
            if key in low_all:
                return label
        for key, label in VISIT_TYPE_FALLBACK_KEYWORDS:
            if key in low_all:
                return label
        # Final fallback: first non-ignored line
        for line in lines:
            if line.lower().strip() not in ignore:
                return line
        return "Unknown"

    def _start_cdp_visit_monitor(self):
        def loop():
            self._cdp_log("Starting CDP visit monitor loop…")
            while self._cdp_monitor_running and not getattr(self, "_is_closing", False):
                try:
                    self._cdp_connect_driver()
                    if not self._cdp_driver:
                        time.sleep(2)
                        continue

                    targets = self._cdp_list_targets()
                    detected = None
                    for ti in targets:
                        tid = ti.get("targetId")
                        if not tid:
                            continue
                        detected = self._cdp_detect_visit_text(tid)
                        if detected:
                            break

                    if detected:
                        txt, actual_url = detected
                        vt_raw = self._classify_visit_type(txt)
                        canonical = self._canonicalize_visit_type(vt_raw)
                        display = canonical or vt_raw or "Unknown"

                        url_changed = actual_url != self._cdp_active_url
                        if url_changed:
                            self._cdp_log(f"URL change: {self._cdp_active_url} -> {actual_url}")
                            self._cdp_active_url = actual_url
                            self._cdp_last_grab_key = None

                        if display != self._cdp_last_display:
                            self._cdp_last_display = display
                            wx.CallAfter(self.visit_type_text.SetLabel, f"Visit type: {display}")

                        key = (actual_url, canonical or display)
                        if key != self._cdp_last_visit_key:
                            self._cdp_last_visit_key = key
                            self._cdp_log(f"Visit type: {display} (from: {txt})")
                            wx.CallAfter(self.refresh_patient_location_async)
                            if canonical:
                                status_msg = f"Auto-detected: {display}"
                                switched = self._schedule_tab_switch(canonical, status_msg)
                                if switched:
                                    self._queue_auto_grab(canonical, actual_url)
                        elif url_changed:
                            wx.CallAfter(self.refresh_patient_location_async)
                    else:
                        if self._cdp_active_url is not None:
                            self._cdp_log("EMR visit page no longer found")
                            self._cdp_active_url = None
                            self._cdp_last_visit_key = None
                            self._cdp_last_display = None
                            self._cdp_last_grab_key = None
                            wx.CallAfter(self.visit_type_text.SetLabel, "Visit type: Unknown")

                    time.sleep(CDP_POLL_INTERVAL_SEC)
                except Exception as exc:
                    self._cdp_log(f"CDP monitor error: {exc}")
                    self._cdp_driver = None
                    self._cdp_driver_thread_id = None
                    time.sleep(2)

        if self._cdp_monitor_thread is None or not self._cdp_monitor_thread.is_alive():
            self._cdp_monitor_thread = threading.Thread(target=loop, daemon=True)
            self._cdp_monitor_thread.start()

    def _on_grab_mode_change(self, evt):
        """Shared handler for the grab mode dropdown on all tabs.
        Updates the global USE_CDP_FOR_GRAB and syncs all dropdowns."""
        global USE_CDP_FOR_GRAB
        sel = evt.GetEventObject().GetSelection()
        USE_CDP_FOR_GRAB = (sel == 0)
        mode_label = "CDP / Playwright (fast)" if USE_CDP_FOR_GRAB else "Clipboard (select all + copy)"
        print(f"[Grab mode] Set to: {mode_label}")
        # Sync all tab dropdowns to the same selection
        for widget_name in ('grab_mode_choice_tab1', 'grab_mode_choice_tab2', 'grab_mode_choice_tab3',
                            'grab_mode_choice_tab4', 'grab_mode_choice_tab6', 'grab_mode_choice_tab7'):
            widget = getattr(self, widget_name, None)
            if widget is not None and widget is not evt.GetEventObject():
                widget.SetSelection(sel)

    def grab_hair(self):
        """Grab the active EMR text and parse for medication and response lines for hair-loss."""
        def do_grab():
            try:
                original = ""
                new_content = None
                used_clipboard = False

                # --- CDP path (fast, no clipboard/focus interaction) ---
                if USE_CDP_FOR_GRAB:
                    new_content = _get_emr_text_cdp()
                    if new_content:
                        print(f"✅ Hair CDP grab: {len(new_content)} chars")

                # --- Clipboard path (when CDP disabled or failed) ---
                if not new_content:
                    used_clipboard = True
                    try:
                        original = pyperclip.paste()
                    except:
                        pass

                    # Hide, focus EMR, select all and copy
                    frame.Hide()
                    time.sleep(0.1)
                    screen_width, screen_height = pyautogui.size()
                    center_x, center_y = screen_width // 2, screen_height // 2
                    pyautogui.click(center_x, center_y)
                    time.sleep(0.3)
                    pyautogui.hotkey('ctrl', 'a')
                    time.sleep(0.2)
                    pyautogui.hotkey('ctrl', 'c')
                    time.sleep(0.5)

                    new_content = pyperclip.paste()

                if not new_content or len(new_content) < 20:
                    wx.CallAfter(lambda: wx.MessageBox('Failed to grab text for hair-loss parsing. Make sure EMR window is active and contains text.', 'Error', wx.ICON_ERROR))
                    if used_clipboard:
                        pyperclip.copy(original)
                    wx.CallAfter(lambda: frame.Show())
                    return

                # Clear any selection if we used clipboard
                if used_clipboard:
                    _clear_text_selection()

                # Parse medication heuristics and other fields
                med = ''
                hvar = ''
                hsx = ''
                lines = [ln.strip() for ln in new_content.splitlines() if ln.strip()]

                header_pattern = re.compile(
                    r'^(current dose|treatment plan|treatment|medication|current treatment|meds|dose|photos|notes|click an image|intake forms|patient selected|responses?|instructions)\b',
                    re.IGNORECASE,
                )
                treatment_header_re = re.compile(r'^treatment\s*[:\-]*$', re.IGNORECASE)
                treatment_inline_re = re.compile(r'^treatment\s*[:\-]\s*(.+)$', re.IGNORECASE)
                med_keywords_re = re.compile(r'finasteride|minoxidil|ketoconazole|biotin|spray|solution|cmpd|compound|%|topical', re.IGNORECASE)
                frequency_hint_re = re.compile(
                    r'\b(daily|weekly|monthly|every|q\d+h|nightly|bedtime|hs|qhs|qam|qpm|bid|tid|qid|once|twice|per\s+\w+|dose[s]?\s+per|each\s+\w+)\b',
                    re.IGNORECASE,
                )

                treatment_line: Optional[str] = None
                treatment_frequency_hint: Optional[str] = None

                def find_next_non_header_line(start_idx: int) -> Tuple[Optional[int], Optional[str]]:
                    idx = start_idx
                    while idx < len(lines):
                        candidate = lines[idx].strip()
                        if not candidate:
                            idx += 1
                            continue
                        if header_pattern.match(candidate):
                            idx += 1
                            continue
                        if re.match(r'^(photos|notes|click an image|intake forms)\b', candidate, re.IGNORECASE):
                            idx += 1
                            continue
                        return idx, candidate
                    return None, None

                for idx, ln in enumerate(lines):
                    inline_match = treatment_inline_re.match(ln)
                    if inline_match:
                        candidate = inline_match.group(1).strip()
                        if candidate:
                            treatment_line = candidate
                        break
                    if treatment_header_re.match(ln):
                        next_idx, candidate = find_next_non_header_line(idx + 1)
                        if candidate:
                            treatment_line = candidate
                            if next_idx is not None:
                                freq_idx, freq_candidate = find_next_non_header_line(next_idx + 1)
                                if freq_candidate and frequency_hint_re.search(freq_candidate):
                                    treatment_frequency_hint = freq_candidate
                        break

                # Capture HSX: collect specific hair loss option phrases
                hsx_symptoms = []
                specific_symptoms = [
                    r'general thinning or shedding',
                    r'thinning at temples',
                    r'thinning at the hairline',
                    r'thinning on the top of the head',
                    r'bald patches, smooth and hairless not at the top of the head',
                    r'redness and irritation found at sites of hair loss',
                    r"i'll take a photo of my head instead"
                ]
                
                # Look through all lines for matching symptoms - extract exact phrases
                for line in lines:
                    line_clean = line.strip()
                    if line_clean and not line_clean.startswith('Patient selected'):
                        for pattern in specific_symptoms:
                            if re.search(pattern, line_clean, re.IGNORECASE):
                                # Extract just the exact phrase, no parenthetical text
                                match = re.search(pattern, line_clean, re.IGNORECASE)
                                if match:
                                    symptom_text = match.group(0)
                                    if symptom_text not in hsx_symptoms:
                                        hsx_symptoms.append(symptom_text)
                
                # Join all found symptoms with commas
                hsx = ', '.join(hsx_symptoms)

                def find_next_med_like(start_idx, lookahead=8):
                    """Return the first med-like line after start_idx or None."""
                    for k in range(start_idx, min(start_idx + lookahead, len(lines))):
                        candidate = lines[k].strip()
                        if not candidate:
                            continue
                        # skip explicit section labels / headers
                        if header_pattern.match(candidate):
                            continue
                        # If candidate clearly contains med keywords, percent signs, or topical/spray wording, take it
                        if med_keywords_re.search(candidate) or re.search(r'\b(topical|spray|cmpd|compound|solution)\b', candidate, re.IGNORECASE) or '%' in candidate:
                            return candidate
                        # otherwise, if it contains multiple words and punctuation (likely a med description), accept as fallback
                        if len(candidate.split()) >= 3 and re.search(r'[A-Za-z0-9]', candidate):
                            return candidate
                    return None

                treatment_found = False
                for i, ln in enumerate(lines):
                    if treatment_header_re.search(ln):
                        candidate = find_next_med_like(i+1, lookahead=8)
                        if candidate:
                            med = candidate.strip().strip('*').strip()
                            treatment_found = True
                            break

                # If we found the treatment med above, ensure frequency appended later; otherwise fall back to header-based search
                if not med:
                    for i, ln in enumerate(lines):
                        if re.search(r'^(treatment|medication|current treatment|meds)[:\-\s]', ln, re.IGNORECASE) or header_pattern.search(ln):
                            # find next med-like line after this header
                            candidate = find_next_med_like(i+1, lookahead=6)
                            if candidate:
                                med = candidate.strip().strip('*').strip()
                            else:
                                med = ''
                            break

                # Fallback: search for known hair meds
                if not med:
                    for ln in lines:
                        if re.search(r'finasteride|minoxidil|dutasteride|spironolactone|topical', ln, re.IGNORECASE):
                            med = ln
                            break

                if treatment_line:
                    med = treatment_line.strip().strip('*').strip()

                # Normalise medication string: if it looks like a header (e.g., 'Current Dose') skip ahead
                if med and header_pattern.search(med):
                    # find the next non-header line
                    for k in range(lines.index(med)+1, len(lines)):
                        if not header_pattern.search(lines[k]) and not re.match(r'^(photos|notes|comments)\b', lines[k], re.IGNORECASE):
                            med = lines[k]
                            break

                # Append frequency 'daily' if not present in med or nearby treatment hint
                append_daily = True
                if med and frequency_hint_re.search(med):
                    append_daily = False
                if append_daily and treatment_frequency_hint and frequency_hint_re.search(treatment_frequency_hint):
                    append_daily = False
                if append_daily and med:
                    med = med.rstrip(' .') + ' daily'

                # Look for response to treatment question
                treatment_response_found = False
                for i, ln in enumerate(lines):
                    if re.search(r'how has your treatment affected your hair loss', ln, re.IGNORECASE):
                        # Take the next non-empty line as response
                        for j in range(i+1, min(i+4, len(lines))):
                            candidate = lines[j].strip()
                            if candidate and len(candidate) > 3:
                                hvar = candidate
                                treatment_response_found = True
                                break
                        break

                # Fallback: look for treatment response patterns
                if not hvar:
                    response_patterns = [
                        r'good response|excellent response|positive response',
                        r'no change|no improvement|same|stable',
                        r'worse|getting worse|declining',
                        r'improved|better|improvement|some improvement',
                        r'minimal.*response|slight.*improvement',
                        r'significant.*improvement|much better',
                        r'side effects|stopped.*due',
                        r'continued.*improvement|ongoing.*improvement'
                    ]
                    
                    for ln in lines:
                        for pattern in response_patterns:
                            if re.search(pattern, ln, re.IGNORECASE):
                                hvar = ln.strip()
                                break
                        if hvar:
                            break

                # Additional fallback: look for any line that mentions hair in a progress context
                if not hvar:
                    for ln in lines:
                        if (re.search(r'hair.*(?:better|worse|same|improved|stable|thicker|thinner)', ln, re.IGNORECASE) or
                            re.search(r'(?:better|worse|same|improved|stable|thicker|thinner).*hair', ln, re.IGNORECASE)):
                            hvar = ln.strip()
                            break

                # Update fields on main thread
                wx.CallAfter(self.hair_med_text.SetValue, med)
                wx.CallAfter(self.hair_hvar_text.SetValue, hvar)
                wx.CallAfter(self.hair_hsx_text.SetValue, hsx)

                emit_emr_bridge({
                    "context": "hair",
                    "hair_med": med,
                    "hair_response": hvar,
                    "hair_symptoms": hsx,
                })

                # Restore original clipboard if we used it
                if used_clipboard:
                    pyperclip.copy(original)
                    wx.CallAfter(lambda: frame.Show())

            except Exception as e:
                if used_clipboard:
                    pyperclip.copy(original)
                wx.CallAfter(lambda: wx.MessageBox(f'Error during hair grab: {e}', 'Error', wx.ICON_ERROR))
                if used_clipboard:
                    wx.CallAfter(lambda: frame.Show())

        threading.Thread(target=do_grab, daemon=True).start()

    def insert_hair_note(self, followup=False, initial=False):
        """Insert a hair-loss note. followup=True inserts the follow-up template; initial=True inserts the initial-visit template."""
        med = self.hair_med_text.GetValue().strip()
        hvar = self.hair_hvar_text.GetValue().strip()
        hsx = self.hair_hsx_text.GetValue().strip()

        # Collect hair exam findings
        hair_exam_findings = []
        if self.hair_exam_front_hairline.GetValue():
            hair_exam_findings.append("front hairline")
        if self.hair_exam_top_crown.GetValue():
            hair_exam_findings.append("top/crown")
        if self.hair_exam_widening_part.GetValue():
            hair_exam_findings.append("widening of the part")
        if self.hair_exam_diffuse_thinning.GetValue():
            hair_exam_findings.append("diffuse thinning")
        if self.hair_exam_confluent.GetValue():
            hair_exam_findings.append("confluent from the front hairline to the crown")
        if self.hair_exam_near_front.GetValue():
            hair_exam_findings.append("near the front with sparing of the hairline")
        
        # Only include O: section if exam findings are selected
        objective_section = ""
        if hair_exam_findings:
            exam_text = ", ".join(hair_exam_findings)
            objective_section = f"O: Images reviewed showing hair loss at {exam_text}\n\n"

        if initial:
            if not hsx and not med:
                wx.MessageBox('No HSX or medication captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
                return
            note = (
                f"S: Reports hair loss described as: '{hsx}'\n"
                "Denies smooth bald spots or redness/crusting around hair follicles.\n\n"
                f"{objective_section}"
                "A: Hair loss consistent with androgenic alopecia\n\n"
                f"P: Start treatment with {med}.\n"
                "Prescription written, follow-up per routine."
            )
        else:
            # default to follow-up style
            if not med and not hvar:
                wx.MessageBox('No medication or response captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
                return
            note = (
                f"S: Reports response to treatment without notable side effects. Endorses: '{hvar}'\n"
                f"{objective_section}"
                "A: Androgenic alopecia\n"
                f"P: Continue present treatment with {med}.\n"
                "Prescription written, follow-up per routine."
            )

        # Hide and paste/type note
        frame.Hide()
        time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f'Error inserting hair note: {e}', 'Insert Error', wx.ICON_ERROR)
        frame.Show()

    def insert_hair_limited_checkin_note(self):
        """Insert a limited check-in note for hair loss follow-up visits."""
        med = self.hair_med_text.GetValue().strip()
        
        if not med:
            wx.MessageBox('No medication captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
            return
        
        note = (
            "S: Reports good response to treatment without side effects.\n"
            "A: Androgenic alopecia\n"
            f"P: Continue present treatment with {med}.\n"
            "Prescription written, follow-up per routine."
        )

        # Hide and paste/type note
        frame.Hide()
        time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f'Error inserting limited check-in note: {e}', 'Insert Error', wx.ICON_ERROR)
        frame.Show()
        frame.Raise()

    def grab_photoaging(self):
        """Grab the active EMR text and parse for photoaging-related information."""
        def do_grab():
            try:
                original = ""
                new_content = None
                used_clipboard = False

                # --- CDP path (fast, no clipboard/focus interaction) ---
                if USE_CDP_FOR_GRAB:
                    new_content = _get_emr_text_cdp()
                    if new_content:
                        print(f"✅ Photoaging CDP grab: {len(new_content)} chars")

                # --- Clipboard path (when CDP disabled or failed) ---
                if not new_content:
                    used_clipboard = True
                    try:
                        original = pyperclip.paste()
                    except:
                        pass

                    # Hide, focus EMR, select all and copy
                    frame.Hide()
                    time.sleep(0.1)
                    screen_width, screen_height = pyautogui.size()
                    center_x, center_y = screen_width // 2, screen_height // 2
                    pyautogui.click(center_x, center_y)
                    time.sleep(0.3)
                    pyautogui.hotkey('ctrl', 'a')
                    time.sleep(0.2)
                    pyautogui.hotkey('ctrl', 'c')
                    time.sleep(0.5)
                    new_content = pyperclip.paste()

                if not new_content or len(new_content) < 20:
                    wx.CallAfter(lambda: wx.MessageBox('Failed to grab text for photoaging parsing. Make sure EMR window is active and contains text.', 'Error', wx.ICON_ERROR))
                    if used_clipboard:
                        pyperclip.copy(original)
                        wx.CallAfter(lambda: frame.Show())
                    return

                if used_clipboard:
                    _clear_text_selection()

                # Parse photoaging-related fields
                lines = [ln.strip() for ln in new_content.splitlines() if ln.strip()]
                
                med = ''
                skin_goals = ''
                retinoid_history = ''
                
                # Parse medication - look for Tretinoin compounds
                med_keywords_re = re.compile(r'tretinoin|niacinamide|azelaic.*acid|retinoid|aging.*rx|custom.*formula', re.IGNORECASE)
                
                # Look for treatment/medication section - prioritize actual medication composition
                for i, ln in enumerate(lines):
                    if re.search(r'^(treatment|aging rx|patient preference)\b', ln, re.IGNORECASE):
                        # Look ahead for medication line, prefer lines with percentages and specific compounds
                        best_candidate = ''
                        for j in range(i+1, min(i+8, len(lines))):
                            candidate = lines[j].strip()
                            if len(candidate.split()) >= 3:
                                # Prioritize lines with percentages and compound names (actual medication)
                                if re.search(r'\d+\.?\d*%.*tretinoin|tretinoin.*\d+\.?\d*%', candidate, re.IGNORECASE):
                                    med = candidate
                                    break
                                # Secondary: lines with multiple drug names and percentages
                                elif re.search(r'%.*%', candidate) and med_keywords_re.search(candidate):
                                    best_candidate = candidate
                                # Fallback: general medication-like lines
                                elif med_keywords_re.search(candidate) and not best_candidate:
                                    best_candidate = candidate
                        
                        # Use best candidate if no perfect match found
                        if not med and best_candidate:
                            med = best_candidate
                        if med:
                            break
                
                # Parse skin goals - look for "Patient's skin care goals" section
                for i, ln in enumerate(lines):
                    if re.search(r'patient.*skin.*care.*goals?', ln, re.IGNORECASE):
                        # Take the next non-empty line
                        for j in range(i+1, min(i+4, len(lines))):
                            candidate = lines[j].strip()
                            if candidate and not re.match(r'^(patient|treatment|aging)', candidate, re.IGNORECASE):
                                skin_goals = candidate
                                break
                        break
                
                # Parse retinoid history - look for previous retinoid use
                retinoid_found = False
                for ln in lines:
                    if re.search(r'retin-a|tretinoin|retinoid', ln, re.IGNORECASE) and not re.search(r'^treatment|^aging', ln, re.IGNORECASE):
                        # Check if this looks like a historical reference
                        if re.search(r'used|previously|past|been|few years|remember', ln, re.IGNORECASE):
                            retinoid_history = "Previously used Retin-A"
                            retinoid_found = True
                            break
                        elif re.search(r'prescription retinoid', ln, re.IGNORECASE):
                            retinoid_history = "Has used prescription retinoids"
                            retinoid_found = True
                            break
                
                # If no specific retinoid history found, check for general skincare use
                if not retinoid_found:
                    for ln in lines:
                        if re.search(r'over-the-counter.*product|men.*skin.*cream|skincare', ln, re.IGNORECASE):
                            retinoid_history = "Has used OTC skincare products"
                            break
                
                # Append daily frequency to medication if not present
                if med and not re.search(r'\b(daily|bi-monthly|monthly|weekly)\b', med, re.IGNORECASE):
                    med = med.rstrip(' .') + ' daily'
                
                print(f"Parsed photoaging data: Med='{med}', Goals='{skin_goals}', History='{retinoid_history}'")
                
                # Update fields on main thread
                wx.CallAfter(self.photoaging_med_text.SetValue, med)
                wx.CallAfter(self.photoaging_goals_text.SetValue, skin_goals)
                wx.CallAfter(self.photoaging_retinoid_text.SetValue, retinoid_history)

                emit_emr_bridge({
                    "context": "photoaging",
                    "photoaging_med": med,
                    "photoaging_goals": skin_goals,
                    "photoaging_retinoid_history": retinoid_history,
                })

                # Restore original clipboard if we used it
                if used_clipboard:
                    pyperclip.copy(original)
                    wx.CallAfter(lambda: frame.Show())

            except Exception as e:
                if used_clipboard:
                    pyperclip.copy(original)
                wx.CallAfter(lambda: wx.MessageBox(f'Error during photoaging grab: {e}', 'Error', wx.ICON_ERROR))
                if used_clipboard:
                    wx.CallAfter(lambda: frame.Show())

        threading.Thread(target=do_grab, daemon=True).start()

    def grab_performance_anxiety(self):
        """Collect Performance Anxiety fields and populate the tab. Respects USE_CDP_FOR_GRAB toggle."""
        def do_grab():
            try:
                original = ""
                used_clipboard = False
                clip_text = None

                # --- CDP path ---
                if USE_CDP_FOR_GRAB:
                    grabber = None
                    try:
                        if self._browser_grabber_cache is None:
                            self._browser_grabber_cache = BrowserEMRGrabber()
                            self._browser_grabber_cache.connect_to_chrome()
                        grabber = self._browser_grabber_cache
                    except Exception:
                        grabber = BrowserEMRGrabber()
                        grabber.connect_to_chrome()

                    if grabber and grabber.driver:
                        data = grabber.grab_performance_anxiety_data() or {}
                        med = data.get('medication', '')
                        situations = data.get('situations_text', '')
                        symptoms = data.get('symptoms_text', '')
                        bp = data.get('blood_pressure', 'nr')
                        pulse = data.get('pulse', 'nr')

                        wx.CallAfter(self.pa_med_text.SetValue, med)
                        wx.CallAfter(self.pa_situations_text.SetValue, situations)
                        wx.CallAfter(self.pa_symptoms_text.SetValue, symptoms)
                        wx.CallAfter(self.pa_bp_text.SetValue, bp)
                        wx.CallAfter(self.pa_pulse_text.SetValue, pulse)

                        emit_emr_bridge({
                            "context": "performance_anxiety",
                            "pa_med": med,
                            "pa_situations": situations,
                            "pa_symptoms": symptoms,
                            "pa_bp": bp,
                            "pa_pulse": pulse,
                        })

                        self.refresh_patient_location_async()
                        return
                    else:
                        print("CDP grab failed for PA, falling through to clipboard...")

                # --- Clipboard fallback path ---
                used_clipboard = True
                try:
                    original = pyperclip.paste()
                except Exception:
                    original = ""

                frame.Hide()
                time.sleep(0.1)
                screen_width, screen_height = pyautogui.size()
                center_x, center_y = screen_width // 2, screen_height // 2
                pyautogui.click(center_x, center_y)
                time.sleep(0.3)
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.2)
                pyautogui.hotkey('ctrl', 'c')
                time.sleep(0.5)

                clip_text = pyperclip.paste() or ''
                _clear_text_selection()

                if not clip_text or len(clip_text) < 30:
                    wx.CallAfter(lambda: wx.MessageBox(
                        'Clipboard grab failed for Performance Anxiety. Ensure EMR window is focused.',
                        'Clipboard Error', wx.ICON_WARNING))
                    pyperclip.copy(original)
                    wx.CallAfter(lambda: frame.Show())
                    return

                # Parse from clipboard text using a disconnected grabber
                grabber = BrowserEMRGrabber()
                grabber._cache_body_text = clip_text
                grabber._cache_all_text = clip_text

                med = grabber._extract_medication_from_text(clip_text)
                bp = 'nr'
                pulse = 'nr'

                # BP from text
                bp_match = re.search(r'(?:blood\s*pressure|bp)[:\s]*(\d{2,3}\s*/\s*\d{2,3})', clip_text, re.IGNORECASE)
                if bp_match:
                    bp = bp_match.group(1).replace(' ', '')

                # Pulse from text
                pulse_match = re.search(r'(?:pulse|heart\s*rate)[:\s]*(\d{2,3})', clip_text, re.IGNORECASE)
                if pulse_match:
                    pulse = pulse_match.group(1)

                # PA situations and symptoms from text
                situations_list = grabber._parse_pa_options_from_text(
                    clip_text,
                    'what situational fears make you nervous or anxious',
                    PA_SITUATION_OPTIONS,
                )
                symptoms_list = grabber._parse_pa_options_from_text(
                    clip_text,
                    'do you experience any of the following symptoms when you are anxious',
                    PA_SYMPTOM_OPTIONS,
                )
                situations = ', '.join(situations_list) if situations_list else ''
                symptoms = ', '.join(symptoms_list) if symptoms_list else ''

                wx.CallAfter(self.pa_med_text.SetValue, med)
                wx.CallAfter(self.pa_situations_text.SetValue, situations)
                wx.CallAfter(self.pa_symptoms_text.SetValue, symptoms)
                wx.CallAfter(self.pa_bp_text.SetValue, bp)
                wx.CallAfter(self.pa_pulse_text.SetValue, pulse)

                emit_emr_bridge({
                    "context": "performance_anxiety",
                    "pa_med": med,
                    "pa_situations": situations,
                    "pa_symptoms": symptoms,
                    "pa_bp": bp,
                    "pa_pulse": pulse,
                })

                pyperclip.copy(original)
                wx.CallAfter(lambda: frame.Show())
                self.refresh_patient_location_async()

            except Exception as e:
                if used_clipboard:
                    try:
                        pyperclip.copy(original)
                    except Exception:
                        pass
                    wx.CallAfter(lambda: frame.Show())
                wx.CallAfter(lambda: wx.MessageBox(f'Error during Performance Anxiety grab: {e}', 'Error', wx.ICON_ERROR))

        threading.Thread(target=do_grab, daemon=True).start()

    def grab_birth_control(self):
        """Collect Birth Control tab data via the browser grabber and populate fields.
        Respects USE_CDP_FOR_GRAB toggle."""

        def do_grab():
            original = ""
            used_clipboard = False
            try:
                # --- CDP path ---
                if USE_CDP_FOR_GRAB:
                    grabber = None
                    try:
                        if self._browser_grabber_cache is None:
                            self._browser_grabber_cache = BrowserEMRGrabber()
                            self._browser_grabber_cache.connect_to_chrome()
                        grabber = self._browser_grabber_cache
                    except Exception:
                        grabber = BrowserEMRGrabber()
                        grabber.connect_to_chrome()

                    if grabber and grabber.driver:
                        data = grabber.grab_birth_control_data() or {}

                        med = data.get('medication', '')
                        lmp = data.get('lmp', '')
                        bp = data.get('blood_pressure', 'nr')
                        side_effects = data.get('side_effects_text', '')
                        pmh = data.get('pmh_list', [])
                        pmh_other = data.get('pmh_other', '')
                        initial_visit = data.get('initial_visit', True)
                        med_history_changes = data.get('med_history_changes', '')

                        wx.CallAfter(self.bc_med_text.SetValue, med)
                        wx.CallAfter(self.bc_lmp_text.SetValue, lmp)
                        wx.CallAfter(self.bc_bp_text.SetValue, bp)
                        wx.CallAfter(self.bc_side_effects_text.SetValue, side_effects or 'none')
                        wx.CallAfter(self.bc_history_changes_text.SetValue, med_history_changes)
                        wx.CallAfter(self.bc_initial_rb.SetValue, bool(initial_visit))
                        wx.CallAfter(self.bc_followup_rb.SetValue, not bool(initial_visit))
                        wx.CallAfter(self._apply_birth_control_pmh, pmh, pmh_other)

                        emit_emr_bridge({
                            "context": "birth_control",
                            "bc_med": med,
                            "bc_lmp": lmp,
                            "bc_bp": bp,
                            "bc_side_effects": side_effects,
                            "bc_pmh_list": pmh,
                            "bc_pmh_other": pmh_other,
                            "bc_initial_visit": bool(initial_visit),
                            "bc_med_history_changes": med_history_changes,
                        })

                        if hasattr(self, "grab_status_text"):
                            wx.CallAfter(self.grab_status_text.SetLabel, "Updated: Birth Control")

                        self.refresh_patient_location_async()
                        return
                    else:
                        print("CDP grab failed for BC, falling through to clipboard...")

                # --- Clipboard fallback path ---
                used_clipboard = True
                try:
                    original = pyperclip.paste()
                except Exception:
                    original = ""

                frame.Hide()
                time.sleep(0.1)
                screen_width, screen_height = pyautogui.size()
                center_x, center_y = screen_width // 2, screen_height // 2
                pyautogui.click(center_x, center_y)
                time.sleep(0.3)
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.2)
                pyautogui.hotkey('ctrl', 'c')
                time.sleep(0.5)

                clip_text = pyperclip.paste() or ''
                _clear_text_selection()

                if not clip_text or len(clip_text) < 30:
                    wx.CallAfter(lambda: wx.MessageBox(
                        'Clipboard grab failed for Birth Control. Ensure EMR window is focused.',
                        'Clipboard Error', wx.ICON_WARNING))
                    pyperclip.copy(original)
                    wx.CallAfter(lambda: frame.Show())
                    return

                # Parse from clipboard text using the same logic as grab_birth_control_data
                grabber = BrowserEMRGrabber()
                grabber._cache_body_text = clip_text
                grabber._cache_all_text = clip_text

                med = grabber._extract_medication_from_text(clip_text)
                bp = 'nr'
                lmp = ''
                side_effects = 'none'
                pmh_candidates = set()
                pmh_other = ''
                initial_visit = True
                med_history_changes = ''

                lines = [ln.strip() for ln in clip_text.splitlines() if ln.strip()]
                low_lines = [ln.lower() for ln in lines]

                def find_answer(keywords, limit=8):
                    for idx, low in enumerate(low_lines):
                        if all(kw in low for kw in keywords):
                            answers = []
                            for j in range(idx + 1, min(len(lines), idx + 1 + limit)):
                                candidate = lines[j].strip()
                                if not candidate:
                                    continue
                                cand_low = candidate.lower()
                                if '?' in candidate and cand_low.count(' ') > 2:
                                    break
                                if cand_low.startswith('status:'):
                                    continue
                                answers.append(candidate)
                                if cand_low in {'yes', 'no', 'none', 'n/a'}:
                                    break
                            return answers
                    return []

                # BP from text
                bp_match = re.search(r'(?:blood\s*pressure|bp)[:\s]*(\d{2,3}\s*/\s*\d{2,3})', clip_text, re.IGNORECASE)
                if bp_match:
                    bp = bp_match.group(1).replace(' ', '')

                # LMP
                lmp_answer = find_answer(['last menstrual period'])
                if lmp_answer:
                    lmp = lmp_answer[0]

                # Side effects
                se_answer = find_answer(['side effect'])
                if se_answer:
                    if any(a.lower() in {'no', 'none', 'none reported'} for a in se_answer):
                        side_effects = 'none'
                    else:
                        side_effects = ', '.join(se_answer)

                # PMH
                diag_answers = find_answer(['diagnosed with the following'])
                condition_answers = find_answer(['have you had any of the following conditions'])
                for answer_group in (diag_answers, condition_answers):
                    for entry in answer_group:
                        low = entry.lower()
                        if low in {'none', 'no', 'n/a'}:
                            continue
                        for key, synonyms in BIRTH_CONTROL_PMH_KEYWORDS.items():
                            if any(term in low for term in synonyms):
                                pmh_candidates.add(key)

                other_diag_answers = find_answer(['any other medical conditions'])
                if other_diag_answers:
                    first = other_diag_answers[0].strip()
                    if first and first.lower() not in {'no', 'none', 'n/a'}:
                        pmh_other = first

                # Visit type
                low_text = '\n'.join(low_lines)
                if 'follow-up' in low_text or 'follow up' in low_text or 'refill' in low_text:
                    initial_visit = False

                # Med history changes
                mhc_answer = find_answer(['changes in your medical history'])
                if mhc_answer:
                    first = mhc_answer[0].strip()
                    if first:
                        med_history_changes = 'none' if first.lower() in {'no', 'none', 'n/a'} else first

                pmh = sorted(pmh_candidates) if pmh_candidates else []

                wx.CallAfter(self.bc_med_text.SetValue, med)
                wx.CallAfter(self.bc_lmp_text.SetValue, lmp)
                wx.CallAfter(self.bc_bp_text.SetValue, bp)
                wx.CallAfter(self.bc_side_effects_text.SetValue, side_effects or 'none')
                wx.CallAfter(self.bc_history_changes_text.SetValue, med_history_changes)
                wx.CallAfter(self.bc_initial_rb.SetValue, bool(initial_visit))
                wx.CallAfter(self.bc_followup_rb.SetValue, not bool(initial_visit))
                wx.CallAfter(self._apply_birth_control_pmh, pmh, pmh_other)

                emit_emr_bridge({
                    "context": "birth_control",
                    "bc_med": med,
                    "bc_lmp": lmp,
                    "bc_bp": bp,
                    "bc_side_effects": side_effects,
                    "bc_pmh_list": pmh,
                    "bc_pmh_other": pmh_other,
                    "bc_initial_visit": bool(initial_visit),
                    "bc_med_history_changes": med_history_changes,
                })

                if hasattr(self, "grab_status_text"):
                    wx.CallAfter(self.grab_status_text.SetLabel, "Updated: Birth Control")

                pyperclip.copy(original)
                wx.CallAfter(lambda: frame.Show())
                self.refresh_patient_location_async()

            except Exception as exc:
                print(f"Birth Control grab thread error: {exc}")
                if used_clipboard:
                    try:
                        pyperclip.copy(original)
                    except Exception:
                        pass
                    wx.CallAfter(lambda: frame.Show())
                wx.CallAfter(lambda err=exc: wx.MessageBox(f'Error during Birth Control grab: {err}', 'Error', wx.ICON_ERROR))

        threading.Thread(target=do_grab, daemon=True).start()

    def insert_performance_anxiety_note(self, initial=False, followup=False):
        """Generate and type a Performance Anxiety note."""
        med = self.pa_med_text.GetValue().strip()
        situations = self.pa_situations_text.GetValue().strip()
        symptoms = self.pa_symptoms_text.GetValue().strip()
        bp = self.pa_bp_text.GetValue().strip() or 'nr'
        pulse = self.pa_pulse_text.GetValue().strip() or 'nr'

        vitals_line = ''
        if bp != 'nr' or pulse != 'nr':
            parts = []
            if bp != 'nr':
                parts.append(f"BP {bp}")
            if pulse != 'nr':
                parts.append(f"Pulse {pulse}")
            vitals_line = "O: " + ", ".join(parts) + "\n\n"

        if initial:
            if not med and not (situations or symptoms):
                wx.MessageBox('No medication or symptom/situation data captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
                return
            subj_bits = []
            if situations:
                subj_bits.append(f"situations: {situations}")
            if symptoms:
                subj_bits.append(f"somatic symptoms: {symptoms}")
            subj = "; ".join(subj_bits) if subj_bits else ""
            note = (
                f"S: Reports performance anxiety {('(' + subj + ')') if subj else ''}.\n\n"
                f"{vitals_line}"
                "A: Performance anxiety\n\n"
                f"P: Start treatment with {med}."
            )
        else:
            # follow-up note
            resp = 'good' if hasattr(self, 'pa_resp_good_rb') and self.pa_resp_good_rb.GetValue() else 'bad'
            se = 'without' if hasattr(self, 'pa_se_without_rb') and self.pa_se_without_rb.GetValue() else 'with'
            se_phrase = 'without side effects' if se == 'without' else 'with side effects'
            if not med:
                wx.MessageBox('No medication captured. Use Grab or enter manually.', 'Nothing to Insert', wx.ICON_WARNING)
                return
            note = (
                f"S: Reports {resp} response to treatment {se_phrase}.\n\n"
                f"{vitals_line}"
                "A: Performance anxiety\n\n"
                f"P: Continue present treatment with {med}."
            )

        frame.Hide()
        time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f'Error inserting Performance Anxiety note: {e}', 'Insert Error', wx.ICON_ERROR)
        frame.Show()
        frame.Raise()

    def _apply_birth_control_pmh(self, selected: Optional[List[str]], other: Optional[str]) -> None:
        try:
            chosen = set(selected or [])
            for key, cb in getattr(self, 'bc_pmh_checkboxes', {}).items():
                cb.SetValue(key in chosen)
            if hasattr(self, 'bc_pmh_other_text'):
                self.bc_pmh_other_text.SetValue(other or '')
        except Exception:
            pass

    def _collect_birth_control_pmh(self) -> Tuple[List[str], str]:
        selected: List[str] = []
        try:
            for key, cb in getattr(self, 'bc_pmh_checkboxes', {}).items():
                if cb.GetValue():
                    selected.append(key)
        except Exception:
            selected = []
        other = ''
        try:
            other = (self.bc_pmh_other_text.GetValue() or '').strip()
        except Exception:
            other = ''
        return selected, other

    def _format_birth_control_pmh(self) -> str:
        selected, other = self._collect_birth_control_pmh()
        items: List[str] = []
        items.extend(selected)
        if other:
            items.append(other)
        return ", ".join(items) if items else "none"

    def _normalize_birth_control_side_effects(self) -> Tuple[str, bool]:
        try:
            raw = (self.bc_side_effects_text.GetValue() or '').strip()
        except Exception:
            raw = ''
        if not raw:
            return "none", True
        low = raw.lower()
        if low in {"none", "no", "none reported", "denies", "without"}:
            return "none", True
        formatted = raw
        if not low.startswith("includes"):
            formatted = f"includes {raw}".strip()
        doing_well = False
        if any(token in low for token in ("doing well", "well", "good")) and "not" not in low:
            doing_well = True
        return formatted, doing_well

    def _resolve_birth_control_plan_phrase(self, doing_well: bool) -> Tuple[str, str]:
        if doing_well:
            return "is", "Continue treatment with"
        return "is not", "Change treatment to"

    def insert_birth_control_initial_note(self):
        med = (self.bc_med_text.GetValue() or '').strip()
        bp = (self.bc_bp_text.GetValue() or '').strip() or 'nr'
        pmh_summary = self._format_birth_control_pmh()
        if not med:
            wx.MessageBox('No medication captured. Use Grab or enter manually.', 'Nothing to Insert', wx.ICON_WARNING)
            return

        note = (
            "S: Patient looking to initiate oral contraceptive.\n"
            f"PMH: {pmh_summary}\n"
            f"O: BP: {bp}\n"
            "A: Contraceptive management\n"
            f"P: Start treatment with {med}\n"
            "Prescription written, follow-up per routine."
        )

        frame.Hide(); time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as exc:
            wx.MessageBox(f'Error inserting Birth Control initial note: {exc}', 'Insert Error', wx.ICON_ERROR)
        finally:
            frame.Show(); frame.Raise()

    def insert_birth_control_followup_note(self):
        med = (self.bc_med_text.GetValue() or '').strip()
        bp = (self.bc_bp_text.GetValue() or '').strip() or 'nr'
        med_history_changes = (self.bc_history_changes_text.GetValue() or '').strip() or 'none'
        side_effects_text, wellbeing_hint = self._normalize_birth_control_side_effects()
        doing_well = wellbeing_hint and side_effects_text == 'none'
        if side_effects_text != 'none' and wellbeing_hint:
            doing_well = True
        if side_effects_text != 'none' and not wellbeing_hint:
            doing_well = False
        if not med:
            wx.MessageBox('No medication captured. Use Grab or enter manually.', 'Nothing to Insert', wx.ICON_WARNING)
            return

        is_phrase, plan_phrase = self._resolve_birth_control_plan_phrase(doing_well)

        note = (
            "S: The patient reports {} doing well on treatment.\n"
            "Side effects: {}\n"
            "Changes in medical history: {}\n"
            "O: BP: {}\n"
            "A: Contraceptive management\n"
            "P: {} {}\n"
            "Prescription written, follow-up per routine."
        ).format(is_phrase, side_effects_text, med_history_changes, bp, plan_phrase, med)

        frame.Hide(); time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as exc:
            wx.MessageBox(f'Error inserting Birth Control follow-up note: {exc}', 'Insert Error', wx.ICON_ERROR)
        finally:
            frame.Show(); frame.Raise()

    def grab_sexual_health_old(self):
        """OLD: Grab the active EMR text and parse for sexual health information."""
        def do_grab():
            try:
                original = ""
                try:
                    original = pyperclip.paste()
                except:
                    pass

                # Hide, focus EMR, select all and copy like other grab methods
                frame.Hide()
                time.sleep(0.1)
                screen_width, screen_height = pyautogui.size()
                center_x, center_y = screen_width // 2, screen_height // 2
                pyautogui.click(center_x, center_y)
                time.sleep(0.3)
                
                # Try multiple approaches to get all content
                attempts = [
                    # Attempt 1: Standard ctrl+a
                    lambda: (pyautogui.hotkey('ctrl', 'a'), time.sleep(0.2), pyautogui.hotkey('ctrl', 'c'), time.sleep(0.3)),
                    # Attempt 2: Try clicking in different areas and ctrl+a
                    lambda: (pyautogui.click(center_x, center_y - 100), time.sleep(0.2), pyautogui.hotkey('ctrl', 'a'), time.sleep(0.2), pyautogui.hotkey('ctrl', 'c'), time.sleep(0.3)),
                    # Attempt 3: Try pressing Tab to navigate to text content then ctrl+a
                    lambda: (pyautogui.press('tab'), time.sleep(0.1), pyautogui.press('tab'), time.sleep(0.1), pyautogui.hotkey('ctrl', 'a'), time.sleep(0.2), pyautogui.hotkey('ctrl', 'c'), time.sleep(0.3)),
                    # Attempt 4: Try ctrl+home then ctrl+shift+end to select all
                    lambda: (pyautogui.hotkey('ctrl', 'home'), time.sleep(0.2), pyautogui.hotkey('ctrl', 'shift', 'end'), time.sleep(0.2), pyautogui.hotkey('ctrl', 'c'), time.sleep(0.3))
                ]
                
                best_content = ""
                best_length = 0
                
                for i, attempt in enumerate(attempts):
                    try:
                        # Execute the attempt
                        attempt()
                        
                        # Check what we got
                        test_content = pyperclip.paste()
                        print(f"Sexual Health Grab Attempt {i+1}: Got {len(test_content)} characters")
                        print(f"First 100 chars: {repr(test_content[:100])}")
                        
                        # Keep the longest/best content
                        if len(test_content) > best_length and len(test_content) > 20:
                            best_content = test_content
                            best_length = len(test_content)
                            print(f"New best content from attempt {i+1}: {len(test_content)} chars")
                            
                            # If we got a substantial amount of text, we can break early
                            if len(test_content) > 500:
                                print(f"Got substantial content ({len(test_content)} chars), using this")
                                break
                                
                    except Exception as e:
                        print(f"Attempt {i+1} failed: {e}")
                        continue
                
                new_content = best_content

                if not new_content or len(new_content) < 20:
                    print("Standard attempts failed, trying Notes tab approach...")
                    # Final attempt: Try to find and click on Notes tab, then grab content
                    try:
                        # Look for "Notes" tab or similar - try common locations
                        notes_tab_attempts = [
                            # Try clicking on potential Notes tab locations
                            (center_x - 200, center_y - 300),  # Top left area
                            (center_x - 100, center_y - 300),  # Top center-left
                            (center_x, center_y - 300),       # Top center
                            (center_x + 100, center_y - 300), # Top center-right
                        ]
                        
                        for tab_x, tab_y in notes_tab_attempts:
                            try:
                                pyautogui.click(tab_x, tab_y)
                                time.sleep(0.3)
                                pyautogui.hotkey('ctrl', 'a')
                                time.sleep(0.2)
                                pyautogui.hotkey('ctrl', 'c')
                                time.sleep(0.3)
                                
                                test_content = pyperclip.paste()
                                print(f"Notes tab attempt at ({tab_x}, {tab_y}): Got {len(test_content)} characters")
                                
                                if len(test_content) > len(new_content):
                                    new_content = test_content
                                    print(f"Better content found from Notes tab click: {len(test_content)} chars")
                                    break
                                    
                            except Exception as e:
                                print(f"Notes tab attempt at ({tab_x}, {tab_y}) failed: {e}")
                                continue
                                
                    except Exception as e:
                        print(f"Notes tab approach failed: {e}")
                
                if not new_content or len(new_content) < 20:
                    wx.CallAfter(lambda: wx.MessageBox(f'Failed to grab text for sexual health parsing. Got {len(new_content)} characters. Make sure EMR window is active and contains text. Try clicking into the Notes section first.', 'Error', wx.ICON_ERROR))
                    pyperclip.copy(original)
                    wx.CallAfter(lambda: frame.Show())
                    return

                # Clear selection by moving cursor to end of document once we have the content
                _clear_text_selection()

                # Parse sexual health-related fields
                lines = [ln.strip() for ln in new_content.splitlines() if ln.strip()]
                
                print(f"Sexual Health Parse Debug:")
                print(f"Total content length: {len(new_content)} characters")
                print(f"Total lines after filtering: {len(lines)}")
                print(f"First 10 lines: {lines[:10]}")
                print(f"Last 10 lines: {lines[-10:]}")
                
                med = ''
                effectiveness = ''
                bp_var = 'nr'  # Default to "nr" if no blood pressure found
                
                # Auto-detect diagnoses from most recent Sexual Health note
                detected_diagnoses = []
                
                # Look for Sexual Health notes (they contain "presents for Sexual Health")
                sexual_health_notes = []
                current_note = []
                collecting_note = False
                
                for i, line in enumerate(lines):
                    # More comprehensive note boundary detection
                    is_note_start = False
                    
                    # Date patterns at start of line
                    if re.search(r'^\d{1,2}/\d{1,2}/\d{4}', line):
                        is_note_start = True
                    # Doctor names
                    elif re.search(r'^Dr\.|^Matthew Tomcik|^Hi [A-Z][a-z]+,', line):
                        is_note_start = True
                    # Common note headers
                    elif re.search(r'^(Provider|Physician|Note|Visit|Assessment|Plan):', line, re.IGNORECASE):
                        is_note_start = True
                    # Time stamps
                    elif re.search(r'^\d{1,2}:\d{2}\s*(AM|PM)', line, re.IGNORECASE):
                        is_note_start = True
                    # Multiple consecutive line breaks might indicate note separation
                    elif line == "" and i > 0 and i < len(lines) - 1:
                        # Check if next non-empty line looks like a note start
                        for j in range(i+1, min(i+5, len(lines))):
                            if lines[j].strip():
                                next_line = lines[j].strip()
                                if (re.search(r'^\d{1,2}/\d{1,2}/\d{4}|^Dr\.|^Matthew Tomcik|^Hi [A-Z]', next_line) or
                                    re.search(r'presents for.*sexual health', next_line, re.IGNORECASE)):
                                    is_note_start = True
                                break
                    
                    if is_note_start:
                        # Save previous note if it was a sexual health note
                        if collecting_note and current_note:
                            note_text = ' '.join(current_note)
                            if re.search(r'presents for sexual health|sexual health.*visit|sexual.*dysfunction', note_text, re.IGNORECASE):
                                sexual_health_notes.append(note_text)
                                print(f"Found Sexual Health note: {note_text[:100]}...")
                        
                        # Start new note
                        current_note = [line]
                        collecting_note = True
                    elif collecting_note and line.strip():  # Only add non-empty lines
                        current_note.append(line)
                
                # Don't forget the last note
                if collecting_note and current_note:
                    note_text = ' '.join(current_note)
                    if re.search(r'presents for sexual health|sexual health.*visit|sexual.*dysfunction', note_text, re.IGNORECASE):
                        sexual_health_notes.append(note_text)
                        print(f"Found Sexual Health note (last): {note_text[:100]}...")
                
                print(f"Total Sexual Health notes found: {len(sexual_health_notes)}")
                
                # Check sexual health notes for diagnoses (most recent first)
                for i, note in enumerate(sexual_health_notes):
                    print(f"Checking note {i+1} for diagnoses...")
                    
                    # Look for ED diagnoses - be more flexible with patterns
                    if re.search(r'\bED\b|erectile dysfunction|E\.D\.|erection.*dysfunction', note, re.IGNORECASE):
                        if "ED" not in detected_diagnoses:
                            detected_diagnoses.append("ED")
                            print(f"Found ED diagnosis in note {i+1}")
                    
                    # Look for PE diagnoses - be more flexible with patterns
                    if re.search(r'\bPE\b|premature ejaculation|P\.E\.|early ejaculation|rapid ejaculation', note, re.IGNORECASE):
                        if "PE" not in detected_diagnoses:
                            detected_diagnoses.append("PE")
                            print(f"Found PE diagnosis in note {i+1}")
                    
                    # Look for PE-like ejaculatory dysfunction
                    if re.search(r'PE-like ejaculatory dysfunction|ejaculatory dysfunction|climax.*dysfunction', note, re.IGNORECASE):
                        if "PE-like ejaculatory dysfunction" not in detected_diagnoses:
                            detected_diagnoses.append("PE-like ejaculatory dysfunction")
                            print(f"Found PE-like dysfunction in note {i+1}")
                    
                    # If we found diagnoses in this note, stop looking
                    if detected_diagnoses:
                        print(f"Found diagnoses: {detected_diagnoses}, stopping search")
                        break
                
                # Parse medication - look for Current Dose or Treatment section
                med_keywords_re = re.compile(r'sildenafil|viagra|tadalafil|cialis|generic viagra|generic cialis', re.IGNORECASE)
                
                # Look for Current Dose or Treatment section
                for i, ln in enumerate(lines):
                    if re.search(r'^(current dose|treatment)\b', ln, re.IGNORECASE):
                        # Look ahead for medication line
                        for j in range(i+1, min(i+6, len(lines))):
                            candidate = lines[j].strip()
                            if len(candidate.split()) >= 2:
                                # Look for medication with dosage
                                if med_keywords_re.search(candidate) and re.search(r'\d+\s*mg', candidate, re.IGNORECASE):
                                    med = candidate
                                    break
                        if med:
                            break
                
                # Append "daily" if not present and doesn't already have frequency
                if med and not re.search(r'\b(daily|as needed|prn|weekly|monthly|every|per|doses)\b', med, re.IGNORECASE):
                    med = med.rstrip(' .') + ' daily'
                
                # Parse effectiveness - look for treatment satisfaction question
                for i, ln in enumerate(lines):
                    if re.search(r'are you happy with the way your treatment is working', ln, re.IGNORECASE):
                        # Take the next non-empty line as response
                        for j in range(i+1, min(i+4, len(lines))):
                            candidate = lines[j].strip()
                            if candidate and len(candidate) > 0:
                                if re.search(r'^(yes|no)$', candidate, re.IGNORECASE):
                                    effectiveness = candidate.capitalize()
                                    break
                        break
                
                # Parse blood pressure - look specifically for "What was your last blood pressure reading?" section
                for i, ln in enumerate(lines):
                    if re.search(r'what was your last blood pressure reading\?', ln, re.IGNORECASE):
                        # Look for BP reading in the next few lines after this specific question
                        for j in range(i+1, min(i+4, len(lines))):
                            candidate = lines[j].strip()
                            if candidate:
                                # Look for BP ranges like "90-139/50-80" or simple readings like "120/80"
                                bp_match = re.search(r'(\d{2,3}[-]\d{2,3}/\d{2,3}[-]\d{2,3}|\d{2,3}/\d{2,3})', candidate)
                                if bp_match:
                                    bp_var = bp_match.group(1)
                                    break
                        if bp_var != 'nr':
                            break

                bp_var = self._normalize_sexual_health_bp(bp_var)
                
                print(f"Parsed sexual health data: Med='{med}', Effectiveness='{effectiveness}', BP='{bp_var}'")
                print(f"Auto-detected diagnoses: {detected_diagnoses}")
                
                # Update fields on main thread
                wx.CallAfter(self.sexual_health_med_text.SetValue, med)
                wx.CallAfter(self.sexual_health_effectiveness_text.SetValue, effectiveness)
                wx.CallAfter(self.sexual_health_bp_text.SetValue, bp_var)

                # Auto-set response/plan dropdowns from effectiveness
                self._auto_set_response_and_plan_from_effectiveness(effectiveness)

                # Auto-check diagnosis checkboxes based on detected diagnoses
                self._apply_sexual_health_diagnoses(detected_diagnoses)

                # Restore original clipboard
                pyperclip.copy(original)
                wx.CallAfter(lambda: frame.Show())

            except Exception as e:
                pyperclip.copy(original)
                wx.CallAfter(lambda: wx.MessageBox(f'Error during sexual health grab: {e}', 'Error', wx.ICON_ERROR))
                wx.CallAfter(lambda: frame.Show())

        threading.Thread(target=do_grab, daemon=True).start()

    def grab_sexual_health(self):
        """Sexual Health data extraction. Respects USE_CDP_FOR_GRAB toggle."""
        def do_grab():
            try:
                original = ""
                used_clipboard = False
                clip_text = None

                # --- CDP path ---
                if USE_CDP_FOR_GRAB:
                    grabber = None
                    t0 = time.perf_counter()
                    try:
                        if self._browser_grabber_cache is None:
                            self._browser_grabber_cache = BrowserEMRGrabber()
                            self._browser_grabber_cache.connect_to_chrome()
                        grabber = self._browser_grabber_cache
                    except Exception:
                        try:
                            grabber = BrowserEMRGrabber()
                            grabber.connect_to_chrome()
                        except Exception:
                            grabber = None

                    if grabber and grabber.driver:
                        data = grabber.grab_sexual_health_data()
                        if data:
                            med = data.get('medication', '')
                            effectiveness = data.get('effectiveness', '')
                            bp_var = self._normalize_sexual_health_bp(data.get('blood_pressure'))
                            detected_diagnoses = data.get('diagnoses', [])
                            hair_loss_location = data.get('hair_loss_location', '')
                            hair_loss_additional_sxx = data.get('hair_loss_additional_sxx', '')

                            print(f"Sexual Health Data (CDP):")
                            print(f"   Medication: '{med}'")
                            print(f"   Effectiveness: '{effectiveness}'")
                            print(f"   Blood Pressure: '{bp_var}'")
                            print(f"   Auto-detected diagnoses: {detected_diagnoses}")
                            print(f"   Hair loss location: '{hair_loss_location}'")
                            print(f"   Hair loss additional sxx: '{hair_loss_additional_sxx}'")

                            patient_age = data.get('patient_age', '')
                            visit_type = data.get('visit_type', '')
                            ed_onset = data.get('rapidity_of_onset', '')
                            ed_frequency = data.get('frequency', '')
                            ed_description = data.get('ed_description', '')
                            ed_characterization = data.get('ed_characterization', '')
                            ehs = data.get('ehs', '')
                            pep = data.get('pep_score', '')
                            past_treatments = data.get('past_ed_treatments', '')
                            ros_pos = data.get('ros_positives', '')
                            ros_neg = data.get('ros_negatives', '')

                            wx.CallAfter(self.sexual_health_med_text.SetValue, med)
                            wx.CallAfter(self.sexual_health_effectiveness_text.SetValue, effectiveness)
                            wx.CallAfter(self.sexual_health_bp_text.SetValue, bp_var)
                            wx.CallAfter(self.sexual_health_hair_location_text.SetValue, hair_loss_location)
                            wx.CallAfter(self.sexual_health_hair_sxx_text.SetValue, hair_loss_additional_sxx)

                            wx.CallAfter(self.sexual_health_age_text.SetValue, patient_age)
                            wx.CallAfter(self.sexual_health_visit_type_text.SetValue, visit_type)
                            wx.CallAfter(self.sexual_health_onset_text.SetValue, ed_onset)
                            wx.CallAfter(self.sexual_health_frequency_text.SetValue, ed_frequency)
                            wx.CallAfter(self.sexual_health_ed_description_text.SetValue, ed_description)
                            wx.CallAfter(self.sexual_health_ed_characterization_text.SetValue, ed_characterization)
                            wx.CallAfter(self.sexual_health_ehs_text.SetValue, ehs)
                            wx.CallAfter(self.sexual_health_pep_text.SetValue, pep)
                            wx.CallAfter(self.sexual_health_past_treatments_text.SetValue, past_treatments)
                            wx.CallAfter(self.sexual_health_ros_pos_text.SetValue, ros_pos)
                            wx.CallAfter(self.sexual_health_ros_neg_text.SetValue, ros_neg)

                            self._auto_set_response_and_plan_from_effectiveness(effectiveness)

                            grabbed_vars['hair_loss_location'] = hair_loss_location
                            grabbed_vars['hair_loss_additional_sxx'] = hair_loss_additional_sxx

                            if med and ('finasteride' in med.lower() or 'minoxidil' in med.lower()):
                                if 'Hair Loss' not in detected_diagnoses:
                                    detected_diagnoses.append('Hair Loss')

                            self._apply_sexual_health_diagnoses(detected_diagnoses)

                            emit_emr_bridge({
                                "context": "sexual_health",
                                "sexual_health_med": med,
                                "sexual_health_effectiveness": effectiveness,
                                "sexual_health_bp": bp_var,
                                "sexual_health_diagnoses": detected_diagnoses,
                                "hair_loss_location": hair_loss_location,
                                "hair_loss_additional_sxx": hair_loss_additional_sxx,
                            })

                            self.refresh_patient_location_async()
                            t1 = time.perf_counter()
                            dprint(f"Sexual Health grab (CDP) total time: {(t1 - t0)*1000:.0f} ms")
                            return
                        else:
                            print("CDP grab returned no data for SH, falling through to clipboard...")
                    else:
                        print("CDP grab failed for SH, falling through to clipboard...")

                # --- Clipboard fallback path ---
                used_clipboard = True
                try:
                    original = pyperclip.paste()
                except Exception:
                    original = ""

                frame.Hide()
                time.sleep(0.1)
                try:
                    screen_width, screen_height = pyautogui.size()
                    center_x, center_y = screen_width // 2, screen_height // 2
                    pyautogui.click(center_x, center_y)
                    time.sleep(0.25)
                    pyautogui.hotkey('ctrl', 'a'); time.sleep(0.15)
                    pyautogui.hotkey('ctrl', 'c'); time.sleep(0.35)
                    clip_text = pyperclip.paste() or ''
                    _clear_text_selection()
                finally:
                    pyperclip.copy(original)
                    wx.CallAfter(lambda: frame.Show())

                if not clip_text or len(clip_text) < 30:
                    wx.CallAfter(lambda: wx.MessageBox(
                        'Clipboard grab failed. Ensure EMR window is focused and has visible text.',
                        'Clipboard Error', wx.ICON_WARNING))
                    return

                # Parse from clipboard text using disconnected grabber extractors
                grabber = BrowserEMRGrabber()
                grabber._cache_body_text = clip_text
                grabber._cache_all_text = clip_text
                grabber._cache_latest_segment = clip_text

                med = grabber._extract_medication_from_text(clip_text)
                effectiveness = grabber._extract_effectiveness(full_text=clip_text) or ''
                bp_var = self._normalize_sexual_health_bp(grabber._extract_blood_pressure())
                detected_diagnoses = grabber._extract_diagnoses() or []

                patient_age = grabber._extract_patient_age(full_text=clip_text)
                visit_type = 'sexual health'
                ed_onset = grabber._extract_ed_onset(full_text=clip_text)
                ed_frequency = grabber._extract_ed_frequency(full_text=clip_text)
                ed_description = grabber._extract_ed_description(full_text=clip_text)
                ed_characterization = grabber._extract_ed_characterization(full_text=clip_text)
                ehs = grabber._extract_ehs_scores(full_text=clip_text)
                pep = grabber._extract_pep_score(full_text=clip_text)
                past_treatments = grabber._extract_past_ed_treatments(full_text=clip_text)
                ros_pos = grabber._extract_ros_positives(full_text=clip_text)
                ros_neg = grabber._extract_ros_negatives(ros_pos)

                wx.CallAfter(self.sexual_health_med_text.SetValue, med)
                wx.CallAfter(self.sexual_health_effectiveness_text.SetValue, effectiveness)
                wx.CallAfter(self.sexual_health_bp_text.SetValue, bp_var)
                self._auto_set_response_and_plan_from_effectiveness(effectiveness)
                self._apply_sexual_health_diagnoses(detected_diagnoses)

                wx.CallAfter(self.sexual_health_age_text.SetValue, patient_age)
                wx.CallAfter(self.sexual_health_visit_type_text.SetValue, visit_type)
                wx.CallAfter(self.sexual_health_onset_text.SetValue, ed_onset)
                wx.CallAfter(self.sexual_health_frequency_text.SetValue, ed_frequency)
                wx.CallAfter(self.sexual_health_ed_description_text.SetValue, ed_description)
                wx.CallAfter(self.sexual_health_ed_characterization_text.SetValue, ed_characterization)
                wx.CallAfter(self.sexual_health_ehs_text.SetValue, ehs)
                wx.CallAfter(self.sexual_health_pep_text.SetValue, pep)
                wx.CallAfter(self.sexual_health_past_treatments_text.SetValue, past_treatments)
                wx.CallAfter(self.sexual_health_ros_pos_text.SetValue, ros_pos)
                wx.CallAfter(self.sexual_health_ros_neg_text.SetValue, ros_neg)

                emit_emr_bridge({
                    "context": "sexual_health",
                    "sexual_health_med": med,
                    "sexual_health_effectiveness": effectiveness,
                    "sexual_health_bp": bp_var,
                    "sexual_health_diagnoses": detected_diagnoses,
                })

                self.refresh_patient_location_async()

            except Exception as e:
                try:
                    pyperclip.copy(original)
                except Exception:
                    pass
                wx.CallAfter(lambda: wx.MessageBox(f'Error during sexual health grab: {e}', 'Error', wx.ICON_ERROR))
                wx.CallAfter(lambda: frame.Show())

        threading.Thread(target=do_grab, daemon=True).start()

    def _normalize_sexual_health_bp(self, bp_value: Optional[str]) -> str:
        text = (bp_value or '').strip()
        if not text:
            return 'not required'
        lowered = text.lower()
        if lowered in ('nr', 'n/r', 'not required'):
            return 'not required'
        return text

    def _auto_set_response_and_plan_from_effectiveness(self, effectiveness: str):
        """Auto-set response_choice and plan_choice based on grabbed effectiveness value."""
        def _apply():
            try:
                eff_lower = (effectiveness or "").strip().lower()
                if eff_lower == "yes":
                    self.sexual_health_response_choice.SetStringSelection("a good response")
                    self.sexual_health_plan_choice.SetStringSelection("Continue present treatment")
                elif eff_lower == "no":
                    self.sexual_health_response_choice.SetStringSelection("a poor response")
                    self.sexual_health_plan_choice.SetStringSelection("Change treatment to")
                else:
                    self.sexual_health_response_choice.SetStringSelection("a satisfactory response")
                    self.sexual_health_plan_choice.SetStringSelection("Continue present treatment")
            except Exception as e:
                print(f"Auto-set response/plan error: {e}")
        wx.CallAfter(_apply)

    def _apply_sexual_health_diagnoses(self, detected: Optional[List[str]]):
        canonical_order = ["ED", "PE", "PE-like ejaculatory dysfunction", "Hair Loss"]
        normalized: List[str] = []
        for item in detected or []:
            if not item:
                continue
            lowered = item.strip().lower()
            for canonical in canonical_order:
                if lowered == canonical.lower() and canonical not in normalized:
                    normalized.append(canonical)
                    break

        def _apply():
            try:
                self._suppress_sexual_health_dx_event = True
                if self._should_autoselect_hair_loss_from_medication() and "Hair Loss" not in normalized:
                    normalized.append("Hair Loss")
                self.sexual_health_dx_ed.SetValue("ED" in normalized)
                self.sexual_health_dx_pe.SetValue("PE" in normalized)
                self.sexual_health_dx_pe_like.SetValue("PE-like ejaculatory dysfunction" in normalized)
                self.sexual_health_dx_hair_loss.SetValue("Hair Loss" in normalized)
            finally:
                self._suppress_sexual_health_dx_event = False
            diagnoses[0] = ", ".join(normalized)
            print(f"Sexual Health diagnoses applied: {diagnoses[0] or 'none'}")

        wx.CallAfter(_apply)

    def _should_autoselect_hair_loss_from_medication(self) -> bool:
        keywords = ("finasteride", "minoxidil")
        candidates = []
        try:
            candidates.append(self.sexual_health_med_text.GetValue())
        except Exception:
            pass
        candidates.append(medication_value[0])
        for text in candidates:
            if not text:
                continue
            lower = text.lower()
            if any(keyword in lower for keyword in keywords):
                return True
        return False

    def detect_visit_type_and_switch_tab(self):
        """Detect what type of visit we're in and switch to the appropriate tab"""
        def do_detection():
            grabber = None
            created = False
            try:
                grabber = getattr(self, "_browser_grabber_cache", None)
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                    created = True

                if not grabber.connect_to_chrome():
                    print("❌ Visit type detection error: could not attach to Chrome")
                    wx.CallAfter(self.grab_status_text.SetLabel, "Detection failed")
                    if created:
                        self._browser_grabber_cache = None
                    return None

                visit_raw = grabber.detect_visit_type()
                canonical = self._canonicalize_visit_type(visit_raw)
                visit_display = canonical or (visit_raw if visit_raw else "Unknown")

                print(f"🎯 Detected visit type (header): {visit_display}")

                wx.CallAfter(self.visit_type_text.SetLabel, f"Visit type: {visit_display}")
                switched = self._schedule_tab_switch(canonical or visit_display, f"Detected: {visit_display}")
                if not switched:
                    wx.CallAfter(self.grab_status_text.SetLabel, "Visit type unclear - staying on current tab")

                self.refresh_patient_location_async()

                return canonical
            except Exception as e:
                print(f"❌ Visit type detection error: {e}")
                wx.CallAfter(self.grab_status_text.SetLabel, f"Detection failed")
                if grabber and grabber is getattr(self, "_browser_grabber_cache", None):
                    self._browser_grabber_cache = None
                return None
        
        return threading.Thread(target=do_detection, daemon=True).start()

    def universal_grab(self):
        """Universal grab that detects visit type and grabs appropriate data"""
        def do_universal_grab():
            try:
                wx.CallAfter(self.grab_status_text.SetLabel, "Detecting visit type...")
                
                # First detect and switch to appropriate tab
                grabber = getattr(self, "_browser_grabber_cache", None)
                created = False
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                    created = True

                if not grabber.connect_to_chrome():
                    wx.CallAfter(self.grab_status_text.SetLabel, "Chrome connection failed")
                    if created:
                        self._browser_grabber_cache = None
                    return

                visit_raw = grabber.detect_visit_type()
                canonical = self._canonicalize_visit_type(visit_raw)
                visit_label = canonical or (visit_raw if visit_raw else "Unknown")

                wx.CallAfter(self.visit_type_text.SetLabel, f"Visit type: {visit_label}")

                self.refresh_patient_location_async()

                if canonical and self._schedule_tab_switch(canonical, f"Grabbing {visit_label} data..."):
                    time.sleep(0.2)
                    if canonical == 'Sexual Health':
                        self.grab_sexual_health()
                    elif canonical == 'Hair Loss':
                        self.grab_hair()
                    elif canonical == 'Photoaging':
                        self.grab_photoaging()
                    elif canonical == 'Performance Anxiety':
                        self.grab_performance_anxiety()
                    else:  # TD/ED Labs / Testosterone
                        grab_all_labs()
                    print(f"🎯 Universal grab completed for: {visit_label}")
                else:
                    # Fallback to current tab's grab function
                    current_tab = self.notebook.GetSelection()
                    if current_tab == 0:
                        wx.CallAfter(self.grab_status_text.SetLabel, "Grabbing TD/ED Labs data...")
                        grab_all_labs()
                    elif current_tab == 1:
                        wx.CallAfter(self.grab_status_text.SetLabel, "Grabbing Hair Loss data...")
                        self.grab_hair()
                    elif current_tab == 2:
                        wx.CallAfter(self.grab_status_text.SetLabel, "Grabbing Photoaging data...")
                        self.grab_photoaging()
                    elif current_tab == 3:
                        wx.CallAfter(self.grab_status_text.SetLabel, "Grabbing Sexual Health data...")
                        self.grab_sexual_health()
                    else:
                        wx.CallAfter(self.grab_status_text.SetLabel, "No grab function for this tab")
                    print("🎯 Universal grab completed for: Current tab (fallback)")

            except Exception as e:
                print(f"❌ Universal grab error: {e}")
                wx.CallAfter(self.grab_status_text.SetLabel, f"Grab failed: {str(e)[:30]}")
        
        threading.Thread(target=do_universal_grab, daemon=True).start()

    def toggle_auto_refresh(self):
        """Toggle auto-refresh functionality - now calls the same auto-clicker as the tab"""
        # Switch to Auto Clicker tab
        self.notebook.SetSelection(4)  # Auto Clicker tab index
        
        # Call the same toggle method as the Auto Clicker tab button
        self.toggle_auto_clicker()
        
        # Update the universal button appearance to match the tab button
        if auto_clicker_enabled[0]:
            self.auto_refresh_btn.SetLabel("🛑 Stop Auto-Refresh")
            self.auto_refresh_btn.SetBackgroundColour(wx.Colour(200, 50, 50))  # Red
            self.grab_status_text.SetLabel("Auto-clicker ON")
        else:
            self.auto_refresh_btn.SetLabel("🔄 Auto-Refresh")
            self.auto_refresh_btn.SetBackgroundColour(wx.Colour(0, 100, 200))  # Blue
            self.grab_status_text.SetLabel("Auto-clicker OFF")

    def auto_refresh_loop(self):
        """Auto-refresh loop that grabs data every 10 seconds without hiding GUI"""
        refresh_interval = 10  # seconds
        
        while self.auto_refresh_enabled:
            try:
                print(f"🔄 Auto-refresh monitoring EMR...")
                wx.CallAfter(self.grab_status_text.SetLabel, "Monitoring EMR...")
                
                # Background grab without hiding GUI
                self.background_emr_grab()
                
                # Wait for the specified interval
                for i in range(refresh_interval):
                    if not self.auto_refresh_enabled:
                        break
                    time.sleep(1)
                
            except Exception as e:
                print(f"❌ Auto-refresh error: {e}")
                wx.CallAfter(self.grab_status_text.SetLabel, f"Auto-refresh error")
                time.sleep(5)  # Wait 5 seconds on error
        
        print("🔄 Auto-refresh loop ended")

    def start_url_monitoring(self):
        """Start URL monitoring for automatic tab switching"""
        if self.url_monitor_thread is None or not self.url_monitor_thread.is_alive():
            self.url_monitor_thread = threading.Thread(target=self.url_monitor_loop, daemon=True)
            self.url_monitor_thread.start()
            print("🌐 URL monitoring started for automatic tab switching")

    def url_monitor_loop(self):
        """Monitor URL changes and automatically switch tabs"""
        while self.url_monitoring_enabled:
            grabber = None
            try:
                grabber = getattr(self, "_browser_grabber_cache", None)
                created = False
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                    created = True

                if not grabber.connect_to_chrome():
                    if created:
                        self._browser_grabber_cache = None
                    time.sleep(3)
                    continue

                current_url = grabber.driver.current_url

                # Check if URL has changed
                if current_url != self.current_url:
                    self.current_url = current_url
                    print(f"🌐 URL changed to: {current_url}")
                    wx.CallAfter(self.refresh_patient_location_async)

                    # Get page content to determine visit type
                    page_text = grabber._get_page_text()
                    if page_text:
                        page_lower = page_text.lower()

                        # Detect visit type and switch tab
                        if any(keyword in page_lower for keyword in ['sexual health', 'erectile dysfunction', 'ed visit', 'viagra', 'cialis', 'sildenafil', 'tadalafil']):
                            visit_type = "Sexual Health"
                            target_tab = 3
                            wx.CallAfter(self.notebook.SetSelection, target_tab)
                            wx.CallAfter(self.grab_status_text.SetLabel, f"Auto-switched to: {visit_type}")
                            print(f"🎯 Auto-switched to {visit_type} tab")

                        elif any(keyword in page_lower for keyword in ['hair loss', 'alopecia', 'finasteride', 'minoxidil', 'hair thinning']):
                            visit_type = "Hair Loss"
                            target_tab = 1
                            wx.CallAfter(self.notebook.SetSelection, target_tab)
                            wx.CallAfter(self.grab_status_text.SetLabel, f"Auto-switched to: {visit_type}")
                            print(f"🎯 Auto-switched to {visit_type} tab")

                        elif any(keyword in page_lower for keyword in ['photoaging', 'tretinoin', 'retinoid', 'aging', 'wrinkles', 'skin care']):
                            visit_type = "Photoaging"
                            target_tab = 2
                            wx.CallAfter(self.notebook.SetSelection, target_tab)
                            wx.CallAfter(self.grab_status_text.SetLabel, f"Auto-switched to: {visit_type}")
                            print(f"🎯 Auto-switched to {visit_type} tab")

                        elif any(keyword in page_lower for keyword in ['testosterone', 'td', 'enclomiphene', 'hormone']):
                            visit_type = "TD/ED Labs"
                            target_tab = 0
                            wx.CallAfter(self.notebook.SetSelection, target_tab)
                            wx.CallAfter(self.grab_status_text.SetLabel, f"Auto-switched to: {visit_type}")
                            print(f"🎯 Auto-switched to {visit_type} tab")

                # Wait 3 seconds before checking again
                time.sleep(3)

            except Exception as e:
                print(f"❌ URL monitoring error: {e}")
                if grabber and grabber is getattr(self, "_browser_grabber_cache", None):
                    self._browser_grabber_cache = None
                time.sleep(5)
        
        print("🌐 URL monitoring ended")

    def background_emr_grab(self):
        """Background EMR grab that doesn't hide the GUI window"""
        def do_background_grab():
            try:
                # Use the browser grabber to detect visit type and grab data
                grabber = getattr(self, "_browser_grabber_cache", None)
                created = False
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                    created = True

                if not grabber.connect_to_chrome():
                    wx.CallAfter(self.grab_status_text.SetLabel, "Chrome connection failed")
                    if created:
                        self._browser_grabber_cache = None
                    return

                visit = grabber.detect_visit_type()
                if visit == 'Sexual Health':
                    data = grabber.grab_sexual_health_data()
                    if data:
                        wx.CallAfter(self.update_sexual_health_fields, data)
                        wx.CallAfter(self.grab_status_text.SetLabel, f"Updated: {visit}")
                elif visit in ('Hair Loss', 'Photoaging', 'TD/ED Labs', 'Testosterone'):
                    # Placeholders for future background grabs
                    wx.CallAfter(self.grab_status_text.SetLabel, f"Detected: {visit} (no auto-grab yet)")
                else:
                    wx.CallAfter(self.grab_status_text.SetLabel, "Monitoring - visit type unclear")
                print(f"🔄 Background grab completed for: {visit if visit else 'Unknown'}")

                self.refresh_patient_location_async()
                
            except Exception as e:
                print(f"❌ Background grab error: {e}")
                wx.CallAfter(self.grab_status_text.SetLabel, f"Background grab failed")
                if 'grabber' in locals() and grabber is getattr(self, "_browser_grabber_cache", None):
                    self._browser_grabber_cache = None
        
        threading.Thread(target=do_background_grab, daemon=True).start()

    def update_sexual_health_fields(self, data):
        """Update Sexual Health tab fields with grabbed data"""
        try:
            if 'medication' in data and data['medication']:
                self.sexual_health_med_text.SetValue(data['medication'])
            if 'effectiveness' in data and data['effectiveness']:
                self.sexual_health_effectiveness_text.SetValue(data['effectiveness'])
                self._auto_set_response_and_plan_from_effectiveness(data['effectiveness'])
            if 'blood_pressure' in data:
                self.sexual_health_bp_text.SetValue(self._normalize_sexual_health_bp(data.get('blood_pressure')))
            # Always update diagnosis checkboxes when key present, even if list is empty
            if 'diagnoses' in data:
                dx_list = data.get('diagnoses') or []
                self.sexual_health_dx_ed.SetValue("ED" in dx_list)
                self.sexual_health_dx_pe.SetValue("PE" in dx_list)
                self.sexual_health_dx_pe_like.SetValue("PE-like ejaculatory dysfunction" in dx_list)
            if 'intake_med_detail' in data and data['intake_med_detail']:
                # Pre-fill change detail only if currently blank; final value decided by auto-populate logic
                if not self.sexual_health_change_detail.GetValue():
                    self.sexual_health_change_detail.SetValue(data['intake_med_detail'])

            # SH Initial visit fields
            if data.get('patient_age'):
                self.sexual_health_age_text.SetValue(data['patient_age'])
            if data.get('visit_type'):
                self.sexual_health_visit_type_text.SetValue(data['visit_type'])
            if data.get('rapidity_of_onset'):
                self.sexual_health_onset_text.SetValue(data['rapidity_of_onset'])
            if data.get('frequency'):
                self.sexual_health_frequency_text.SetValue(data['frequency'])
            if data.get('ed_description'):
                self.sexual_health_ed_description_text.SetValue(data['ed_description'])
            if data.get('ed_characterization'):
                self.sexual_health_ed_characterization_text.SetValue(data['ed_characterization'])
            if data.get('ehs'):
                self.sexual_health_ehs_text.SetValue(data['ehs'])
            if data.get('pep_score'):
                self.sexual_health_pep_text.SetValue(data['pep_score'])
            if data.get('past_ed_treatments'):
                self.sexual_health_past_treatments_text.SetValue(data['past_ed_treatments'])
            if 'ros_positives' in data:
                self.sexual_health_ros_pos_text.SetValue(data.get('ros_positives', ''))
            if 'ros_negatives' in data:
                self.sexual_health_ros_neg_text.SetValue(data.get('ros_negatives', ''))

            print(f"✅ Sexual Health fields updated via background grab")
        except Exception as e:
            print(f"❌ Error updating Sexual Health fields: {e}")

        try:
            self._auto_populate_sexual_health_change_fields(
                data.get('intake_med_name') or data.get('medication'),
                data.get('intake_med_detail', ''),
                data.get('current_med_name'),
                data.get('current_med_detail', '')
            )
        except Exception as e:
            print(f"❌ Auto change comparison error: {e}")

    def insert_sexual_health_note(self):
        """Insert a sexual health follow-up note based on captured variables."""
        med = self.sexual_health_med_text.GetValue().strip()
        effectiveness = self.sexual_health_effectiveness_text.GetValue().strip()
        bp_var = self._normalize_sexual_health_bp(self.sexual_health_bp_text.GetValue())

        if not med:
            wx.MessageBox('No medication captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
            return

        # Collect sexual health diagnoses
        sexual_health_diagnoses = []
        if self.sexual_health_dx_ed.GetValue():
            sexual_health_diagnoses.append("ED")
        if self.sexual_health_dx_pe.GetValue():
            sexual_health_diagnoses.append("PE")
        if self.sexual_health_dx_pe_like.GetValue():
            sexual_health_diagnoses.append("PE-like ejaculatory dysfunction")
        if self.sexual_health_dx_hair_loss.GetValue():
            sexual_health_diagnoses.append("Hair Loss")
        
        # Use selected diagnoses or empty if none selected
        if sexual_health_diagnoses:
            diagnosis_text = ", ".join(sexual_health_diagnoses)
        else:
            diagnosis_text = ""

        # Determine response type and action based on effectiveness
        if effectiveness.lower() == "yes":
            response_text = "good response"
            plan_line = f"P: Continue treatment with {med}.\n"
        else:
            response_text = "inadequate response"
            plan_line = f"P: Change treatment to {med}.\n"

        # Build the note for sexual health follow-up
        note = (
            f"S: Reports {response_text} to treatment without side effects.\n"
            f"O: BP {bp_var}\n"
            f"A: {diagnosis_text}\n"
            + plan_line
            + "Prescription written, follow-up per routine."
        )

        # Hide and paste/type note
        frame.Hide()
        time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f'Error inserting sexual health note: {e}', 'Insert Error', wx.ICON_ERROR)
        frame.Show()
        frame.Raise()

    def insert_sexual_health_brief_template(self):
        """Insert a brief Sexual Health plan note using external template if available."""
        try:
            # Gather diagnoses from the Sexual Health tab checkboxes
            dx_list = []
            if self.sexual_health_dx_ed.GetValue():
                dx_list.append("ED")
            if self.sexual_health_dx_pe.GetValue():
                dx_list.append("PE")
            if self.sexual_health_dx_pe_like.GetValue():
                dx_list.append("PE-like ejaculatory dysfunction")
            diagnosis_text = ", ".join(dx_list)

            # Medication from Sexual Health tab field
            med = (self.sexual_health_med_text.GetValue() or "").strip()

            # Prefer external template
            tpl = templates.get("Sexual Health - Plan")
            if tpl:
                brief = tpl.format(diagnoses=diagnosis_text, medication=med)
            else:
                # Fallback to previous inline formatting
                brief = (
                    f" {diagnosis_text}\n\n"
                    f"P: Start treatment with {med}\n"
                    f"Prescription written, follow-up per routine."
                )

            # Type at cursor (plan note intentionally skips leading newline)
            frame.Hide()
            time.sleep(0.2)
            _type_template_text(brief, prepend_enter=False)
        except Exception as e:
            wx.MessageBox(f"Error inserting Sexual Health brief template: {e}", "Insert Error", wx.ICON_ERROR)
        finally:
            frame.Show()
            frame.Raise()

    def insert_sexual_health_change_template(self):
        """Insert a Sexual Health change template reflecting cadence, dose, or medication updates."""
        change_focus = (self.sexual_health_change_choice.GetStringSelection() or "").strip()
        change_detail = (self.sexual_health_change_detail.GetValue() or "").strip()
        med = (self.sexual_health_med_text.GetValue() or "").strip()
        bp_var = (self.sexual_health_bp_text.GetValue() or "").strip()
        if not bp_var or bp_var == 'nr':
            bp_var = "not required"
            
        if not med:
            wx.MessageBox('No medication captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
            return

        phrase_parts: List[str] = []
        if change_focus:
            phrase_parts.append(change_focus.lower())
        if change_detail:
            if phrase_parts:
                phrase_parts[-1] = f"{phrase_parts[-1]} {change_detail}" if change_focus.lower() != "medication" else f"{phrase_parts[-1]} to {change_detail}"
            else:
                phrase_parts.append(change_detail)
        change_phrase = " ".join(phrase_parts).strip()
        if not change_phrase:
            wx.MessageBox('Enter change details before inserting.', 'Nothing to Insert', wx.ICON_WARNING)
            return

        # Collect diagnoses for assessment line
        dx_list = []
        if self.sexual_health_dx_ed.GetValue():
            dx_list.append("ED")
        if self.sexual_health_dx_pe.GetValue():
            dx_list.append("PE")
        if self.sexual_health_dx_pe_like.GetValue():
            dx_list.append("PE-like ejaculatory dysfunction")
        if self.sexual_health_dx_hair_loss.GetValue():
            dx_list.append("Hair Loss")
        diagnosis_text = ", ".join(dx_list)

        plan_action = (self.sexual_health_plan_choice.GetStringSelection() or "").strip()
        plan_line = plan_action if not med else f"{plan_action} with {med}"

        focus_lower = change_focus.lower()
        if focus_lower in ("cadence", "dose number") and change_detail:
            plan_line += f" (amended {focus_lower} {change_detail})"
        elif focus_lower == "medication" and change_detail:
            plan_line += f" ({change_detail})"

        note = (
            f"S: The patient wishes to change {change_phrase}. No health changes (or side effects) are reported.\n"
            f"O: BP {bp_var}\n"
            f"A: {diagnosis_text}\n"
            f"P: {plan_line}\n"
            "Prescription written, follow-up per routine."
        )

        frame.Hide()
        time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f"Error inserting Sexual Health change template: {e}", "Insert Error", wx.ICON_ERROR)
        finally:
            frame.Show()
            frame.Raise()

    def _get_sh_dx_text(self) -> str:
        dx_list = []
        try:
            if self.sexual_health_dx_ed.GetValue():
                dx_list.append("ED")
            if self.sexual_health_dx_pe.GetValue():
                dx_list.append("PE")
            if self.sexual_health_dx_pe_like.GetValue():
                dx_list.append("PE-like ejaculatory dysfunction")
        except Exception:
            pass
        return ", ".join(dx_list)

    def insert_hair_info(self):
        """Insert hair loss information template in Sexual Health tab."""
        hair_location = grabbed_vars.get('hair_loss_location', '').strip()
        hair_sxx = grabbed_vars.get('hair_loss_additional_sxx', '').strip()
        
        # Also check the text fields directly in case they were manually edited
        if not hair_location:
            try:
                hair_location = self.sexual_health_hair_location_text.GetValue().strip()
            except Exception:
                pass
        
        if not hair_sxx:
            try:
                hair_sxx = self.sexual_health_hair_sxx_text.GetValue().strip()
            except Exception:
                pass
        
        if not hair_location and not hair_sxx:
            wx.MessageBox('No hair loss information captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
            return
        
        # Build the template text
        note = f"Reports hair loss described as '{hair_location}'. With regard to additional symptoms he affirms: '{hair_sxx}'."
        
        # Hide and paste/type note
        frame.Hide()
        time.sleep(0.2)
        try:
            _type_template_text(note, prepend_enter=False)
        except Exception as e:
            wx.MessageBox(f'Error inserting hair info: {e}', 'Insert Error', wx.ICON_ERROR)
        frame.Show()
        frame.Raise()

    def insert_sh_change_cadence(self):
        """Insert the 'change cadence' Sexual Health template."""
        try:
            bp_val = (self.sexual_health_bp_text.GetValue() or "").strip()
            if not bp_val or bp_val == 'nr':
                bp_val = "not required"
            dx_text = self._get_sh_dx_text()
            med = (self.sexual_health_med_text.GetValue() or "").strip()
            tpl = templates.get("Sexual Health - Change Cadence")
            if tpl:
                note = tpl.format(bp=bp_val, diagnoses=dx_text, medication=(med or medication_value[0]))
            else:
                note = (
                    f"S: The patient wishes to change the cadence of their prescription. No health changes or side effects are reported.\n"
                    f"O: BP {bp_val}\n"
                    f"A: {dx_text}\n"
                    f"P: Continue present treatment with {medication_value[0] if not med else med} and amended cadence\n"
                    f"Prescription written, follow-up per routine."
                )
            frame.Hide(); time.sleep(0.2)
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f"Error inserting change cadence template: {e}", "Insert Error", wx.ICON_ERROR)
        finally:
            frame.Show(); frame.Raise()

    def insert_sh_change_number(self):
        """Insert the 'change number' Sexual Health template."""
        try:
            bp_val = (self.sexual_health_bp_text.GetValue() or "").strip()
            if not bp_val or bp_val.lower() == "nr":
                bp_val = "not required"
            dx_text = self._get_sh_dx_text()
            med = (self.sexual_health_med_text.GetValue() or "").strip()
            tpl = templates.get("Sexual Health - Change Number")
            if tpl:
                note = tpl.format(bp=bp_val, diagnoses=dx_text, medication=(med or medication_value[0]))
            else:
                note = (
                    f"S: The patient wishes to change the dose number of their prescription. No health changes or side effects are reported.\n"
                    f"O: BP {bp_val}\n"
                    f"A: {dx_text}\n"
                    f"P: Continue present treatment with {medication_value[0] if not med else med}. and amended amount\n"
                    f"Prescription written, follow-up per routine."
                )
            frame.Hide(); time.sleep(0.2)
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f"Error inserting change number template: {e}", "Insert Error", wx.ICON_ERROR)
        finally:
            frame.Show(); frame.Raise()

    def insert_sh_change_medication(self):
        """Insert the 'change medication' Sexual Health template."""
        try:
            bp_val = (self.sexual_health_bp_text.GetValue() or "").strip()
            if not bp_val or bp_val == 'nr':
                bp_val = "not required"

            dx_text = self._get_sh_dx_text()
            med = (self.sexual_health_med_text.GetValue() or "").strip()
            tpl = templates.get("Sexual Health - Change Medication")
            if tpl:
                note = tpl.format(bp=bp_val, diagnoses=dx_text, medication=(med or medication_value[0]))
            else:
                note = (
                    f"S: The patient requests to change the medication they are taking to try to get a stronger effect. No health changes or side effects are reported.\n"
                    f"O: BP {bp_val}\n"
                    f"A: {dx_text}\n"
                    f"P: Change treatment to {medication_value[0] if not med else med} as-needed.\n"
                    f"Prescription written, follow-up per routine."
                )
            frame.Hide(); time.sleep(0.2)
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f"Error inserting change medication template: {e}", "Insert Error", wx.ICON_ERROR)
        finally:
            frame.Show(); frame.Raise()

    def insert_photoaging_note(self):
        """Insert a photoaging note based on captured variables using external template if available."""
        med = self.photoaging_med_text.GetValue().strip()
        goals = self.photoaging_goals_text.GetValue().strip()
        retinoid_history = self.photoaging_retinoid_text.GetValue().strip()

        # Collect photoaging exam findings
        photoaging_exam_findings = []
        if self.photoaging_exam_fine_lines.GetValue():
            photoaging_exam_findings.append("fine lines")
        if self.photoaging_exam_wrinkles.GetValue():
            photoaging_exam_findings.append("wrinkles")
        if self.photoaging_exam_crows_feet.GetValue():
            photoaging_exam_findings.append("crow's feet")
        if self.photoaging_exam_pigmentation.GetValue():
            photoaging_exam_findings.append("pigmentation changes")
        if self.photoaging_exam_age_spots.GetValue():
            photoaging_exam_findings.append("age spots")
        if self.photoaging_exam_melasma.GetValue():
            photoaging_exam_findings.append("melasma")
        if self.photoaging_exam_texture_changes.GetValue():
            photoaging_exam_findings.append("texture changes")
        if self.photoaging_exam_enlarged_pores.GetValue():
            photoaging_exam_findings.append("enlarged pores")
        if self.photoaging_exam_loss_elasticity.GetValue():
            photoaging_exam_findings.append("loss of elasticity")
        if self.photoaging_exam_inflammation.GetValue():
            photoaging_exam_findings.append("signs of inflammation")
        if self.photoaging_exam_scarring.GetValue():
            photoaging_exam_findings.append("acne scarring")
        if self.photoaging_exam_normal.GetValue():
            photoaging_exam_findings.append("no significant findings")

        if not med:
            wx.MessageBox('No medication captured. Use Grab or enter values manually.', 'Nothing to Insert', wx.ICON_WARNING)
            return

        # Build exam findings text
        if photoaging_exam_findings:
            exam_text = f"Images reviewed and there is present {' as well as '.join(photoaging_exam_findings)} in the facial skin"
            if not any([self.photoaging_exam_inflammation.GetValue(), self.photoaging_exam_scarring.GetValue()]):
                exam_text += " without sign of inflammation or scarring"
        else:
            exam_text = "Images reviewed and there is present some fine lines as well as pigmentation changes in the facial skin without sign of inflammation or scarring"

        # Build the note via template
        tpl = templates.get("Photoaging - Note")
        if tpl:
            note = tpl.format(retinoid_history=retinoid_history, exam_text=exam_text, medication=med)
        else:
            note = (
                f"S: Reports concern about aging, with presence of photoaging. {retinoid_history}. No serious health conditions.\n"
                f"O: {exam_text}\n"
                "A: Photoaging.\n"
                f"P: Start treatment with {med}.\n"
                "Prescription written, follow-up per routine."
            )

        # Hide and paste/type note
        frame.Hide()
        time.sleep(0.2)
        try:
            _type_template_text(note)
        except Exception as e:
            wx.MessageBox(f'Error inserting photoaging note: {e}', 'Insert Error', wx.ICON_ERROR)
        frame.Show()
        frame.Raise()

    def _strip_frequency_suffix(self, value: Optional[str]) -> str:
        if not value:
            return ""
        trimmed = value.strip()
        lowered = trimmed.lower()
        for suffix in (', daily', ', as-needed'):
            if lowered.endswith(suffix):
                return trimmed[: -len(suffix)]
        return trimmed

    def _normalize_phrase(self, value: Optional[str]) -> str:
        if not value:
            return ""
        return re.sub(r"\s+", " ", value).strip().lower()

    def _strings_differ(self, first: Optional[str], second: Optional[str]) -> bool:
        return self._normalize_phrase(first) != self._normalize_phrase(second)

    def _parse_dose_and_cadence(self, detail: Optional[str]) -> Tuple[str, str]:
        if not detail:
            return "", ""
        parts = [part.strip() for part in detail.split(',') if part.strip()]
        dose = ""
        cadence = ""
        for part in parts:
            low = part.lower()
            if not dose and any(token in low for token in ('dose', 'doses', 'per ', 'tablet', 'capsule')):
                dose = part
            if not cadence and any(token in low for token in ('ship', 'delivery', 'every', 'monthly', 'quarterly', 'cadence', 'supply')):
                cadence = part
        if not dose and parts:
            dose = parts[0]
        if not cadence and len(parts) > 1:
            cadence = parts[1]
        return dose, cadence

    def _auto_populate_sexual_health_change_fields(self, intake_name: Optional[str], intake_detail: Optional[str], current_name: Optional[str], current_detail: Optional[str]) -> bool:
        try:
            change_var = None
            change_value = ""

            intake_name = self._strip_frequency_suffix(intake_name)
            current_name = self._strip_frequency_suffix(current_name)

            if intake_name and current_name and self._strings_differ(intake_name, current_name):
                change_var = "Medication"
                change_value = intake_name
            else:
                dose_new, cadence_new = self._parse_dose_and_cadence(intake_detail)
                dose_cur, cadence_cur = self._parse_dose_and_cadence(current_detail)
                if self._strings_differ(dose_new, dose_cur):
                    change_var = "Dose number"
                    change_value = dose_new or dose_cur
                elif self._strings_differ(cadence_new, cadence_cur):
                    change_var = "Cadence"
                    change_value = cadence_new or cadence_cur

            if change_var:
                self.sexual_health_change_choice.SetStringSelection(change_var)
                self.sexual_health_change_detail.SetValue(change_value or "")
                self.sexual_health_plan_choice.SetStringSelection("Change treatment to")
                return True

            # No change detected; default fields only if empty
            if not self.sexual_health_change_detail.GetValue():
                self.sexual_health_change_choice.SetStringSelection("Cadence")
                self.sexual_health_change_detail.SetValue("")
            self.sexual_health_plan_choice.SetStringSelection("Continue present treatment")
            return False
        except Exception as e:
            print(f"❌ Auto-populate change fields error: {e}")
            return False

    def insert_specific_template(self, template_name):
        """Insert a specific template by name"""
        selected_template[0] = template_name
        insert_template_at_cursor()

    def create_template_dropdown_row_inline(self, parent, tab_sizer, tab_name: str) -> wx.Choice:
        """Create a template dropdown row for inside tabs (no separator, compact layout).
        
        Layout: 
          Row 1: Template: [dropdown][...][refresh]
          Row 2: [Insert Template] (wider button)
        
        Args:
            parent: The parent wx.Panel (the tab panel)
            tab_sizer: The sizer to add the row to
            tab_name: Name of the tab (must match keys in TEMPLATE_CONFIG)
        
        Returns:
            The wx.Choice dropdown (in case caller needs to refresh it later)
        """
        # Load dropdown choices from config
        template_list = load_tab_template_list(tab_name)
        
        # Create horizontal sizer for the dropdown row
        dropdown_row = wx.BoxSizer(wx.HORIZONTAL)
        
        # Label
        label = wx.StaticText(parent, label="Template:")
        dropdown_row.Add(label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        
        # Dropdown
        dropdown = wx.Choice(parent, choices=template_list)
        if template_list:
            dropdown.SetSelection(0)
        dropdown_row.Add(dropdown, 1, wx.ALL | wx.EXPAND, 5)
        
        # "..." button to edit dropdown config (JSON)
        config_btn = wx.Button(parent, label="...", size=(30, -1))
        config_btn.SetToolTip("Edit dropdown list (opens JSON config in Notepad)")
        
        def on_edit_config(event):
            config_path = get_template_config_path(tab_name)
            if os.path.exists(config_path):
                open_in_notepad(config_path)
            else:
                wx.MessageBox(f"Config file not found:\n{config_path}", "File Not Found", wx.OK | wx.ICON_WARNING)
        
        config_btn.Bind(wx.EVT_BUTTON, on_edit_config)
        dropdown_row.Add(config_btn, 0, wx.ALL, 5)
        
        # Refresh button to reload dropdown after editing
        refresh_btn = wx.Button(parent, label="↻", size=(30, -1))
        refresh_btn.SetToolTip("Refresh dropdown list after editing config")
        
        def on_refresh(event):
            new_list = load_tab_template_list(tab_name)
            dropdown.Clear()
            dropdown.AppendItems(new_list)
            if new_list:
                dropdown.SetSelection(0)
            print(f"Refreshed {tab_name} template dropdown: {len(new_list)} templates")
        
        refresh_btn.Bind(wx.EVT_BUTTON, on_refresh)
        dropdown_row.Add(refresh_btn, 0, wx.ALL, 5)
        
        tab_sizer.Add(dropdown_row, 0, wx.EXPAND | wx.ALL, 2)
        
        # Second row: Insert Template button (wider, centered)
        insert_row = wx.BoxSizer(wx.HORIZONTAL)
        
        insert_btn = wx.Button(parent, label="Insert Template", size=(200, -1))
        insert_btn.SetBackgroundColour(wx.Colour(200, 230, 200))  # Light green tint
        insert_btn.SetToolTip("Insert selected template at cursor (Ctrl+V paste)")
        
        def on_insert(event):
            selection = dropdown.GetStringSelection()
            if selection:
                self.insert_specific_template(selection)
            else:
                print("No template selected")
        
        insert_btn.Bind(wx.EVT_BUTTON, on_insert)
        insert_row.Add(insert_btn, 0, wx.ALL | wx.CENTER, 5)
        
        tab_sizer.Add(insert_row, 0, wx.ALIGN_CENTER | wx.ALL, 2)
        
        return dropdown

    def create_template_dropdown_row(self, parent, tab_sizer, tab_name: str) -> wx.Choice:
        """Create a template dropdown row with Insert, Edit Config, and Edit Templates buttons.
        
        Args:
            parent: The parent wx.Panel (the tab panel)
            tab_sizer: The sizer to add the row to
            tab_name: Name of the tab (must match keys in TEMPLATE_CONFIG)
        
        Returns:
            The wx.Choice dropdown (in case caller needs to refresh it later)
        """
        # Create separator line above dropdown
        separator = wx.StaticLine(parent, style=wx.LI_HORIZONTAL)
        tab_sizer.Add(separator, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)
        
        # Load dropdown choices from config
        template_list = load_tab_template_list(tab_name)
        
        # Create horizontal sizer for the dropdown row
        dropdown_row = wx.BoxSizer(wx.HORIZONTAL)
        
        # Label
        label = wx.StaticText(parent, label="Template:")
        dropdown_row.Add(label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        
        # Dropdown
        dropdown = wx.Choice(parent, choices=template_list)
        if template_list:
            dropdown.SetSelection(0)
        dropdown_row.Add(dropdown, 1, wx.ALL | wx.EXPAND, 5)
        
        # Insert button
        insert_btn = wx.Button(parent, label="Insert", size=(60, -1))
        insert_btn.SetBackgroundColour(wx.Colour(200, 230, 200))  # Light green tint
        insert_btn.SetToolTip("Insert selected template at cursor (Ctrl+V paste)")
        
        def on_insert(event):
            selection = dropdown.GetStringSelection()
            if selection:
                self.insert_specific_template(selection)
            else:
                print("No template selected")
        
        insert_btn.Bind(wx.EVT_BUTTON, on_insert)
        dropdown_row.Add(insert_btn, 0, wx.ALL, 5)
        
        # "..." button to edit dropdown config (JSON)
        config_btn = wx.Button(parent, label="...", size=(30, -1))
        config_btn.SetToolTip("Edit dropdown list (opens JSON config in Notepad)")
        
        def on_edit_config(event):
            config_path = get_template_config_path(tab_name)
            if os.path.exists(config_path):
                open_in_notepad(config_path)
            else:
                wx.MessageBox(f"Config file not found:\n{config_path}", "File Not Found", wx.OK | wx.ICON_WARNING)
        
        config_btn.Bind(wx.EVT_BUTTON, on_edit_config)
        dropdown_row.Add(config_btn, 0, wx.ALL, 5)
        
        # "Edit Templates" button to edit template file
        edit_templates_btn = wx.Button(parent, label="Edit Templates", size=(100, -1))
        edit_templates_btn.SetToolTip("Edit template text file (opens in Notepad)")
        
        def on_edit_templates(event):
            template_path = get_template_file_path(tab_name)
            if os.path.exists(template_path):
                open_in_notepad(template_path)
            else:
                wx.MessageBox(f"Template file not found:\n{template_path}", "File Not Found", wx.OK | wx.ICON_WARNING)
        
        edit_templates_btn.Bind(wx.EVT_BUTTON, on_edit_templates)
        dropdown_row.Add(edit_templates_btn, 0, wx.ALL, 5)
        
        # Refresh button to reload dropdown after editing
        refresh_btn = wx.Button(parent, label="↻", size=(30, -1))
        refresh_btn.SetToolTip("Refresh dropdown list after editing config")
        
        def on_refresh(event):
            new_list = load_tab_template_list(tab_name)
            dropdown.Clear()
            dropdown.AppendItems(new_list)
            if new_list:
                dropdown.SetSelection(0)
            print(f"Refreshed {tab_name} template dropdown: {len(new_list)} templates")
        
        refresh_btn.Bind(wx.EVT_BUTTON, on_refresh)
        dropdown_row.Add(refresh_btn, 0, wx.ALL, 5)
        
        tab_sizer.Add(dropdown_row, 0, wx.EXPAND | wx.ALL, 2)
        
        return dropdown

    # ================================================================
    # Template Tab Tracking (for Edit Templates button in header)
    # ================================================================
    
    # Map notebook tab indices to TEMPLATE_CONFIG keys
    TAB_INDEX_TO_TEMPLATE_KEY = {
        0: "T Deficiency",
        1: "Hair Loss",
        2: "Photoaging",
        3: "Sexual Health",
        # 4: "Auto Clicker" - no templates
        5: "Performance Anxiety",
        6: "Birth Control",
    }
    
    def on_notebook_page_changed(self, event):
        """Track current tab for Edit Templates button."""
        tab_index = event.GetSelection()
        tab_name = self.TAB_INDEX_TO_TEMPLATE_KEY.get(tab_index)
        
        if tab_name:
            self._current_template_tab = tab_name
            self.edit_templates_btn.Enable(True)
        else:
            # Auto Clicker tab or unknown - disable Edit Templates button
            self.edit_templates_btn.Enable(False)
        
        event.Skip()  # Allow default processing
    
    def on_edit_templates_global(self, event):
        """Open the template file for current tab in Notepad."""
        template_path = get_template_file_path(self._current_template_tab)
        if os.path.exists(template_path):
            open_in_notepad(template_path)
        else:
            wx.MessageBox(f"Template file not found:\n{template_path}", "File Not Found", wx.OK | wx.ICON_WARNING)

    def on_dx_checkbox(self, event):
        selected = []
        if self.dx_td_cb.GetValue():
            selected.append("Testosterone Deficiency")
        if self.dx_ed_cb.GetValue():
            selected.append("ED")
        diagnoses[0] = ", ".join(selected)
        print(f"Diagnoses set to: {diagnoses[0]}")

    def on_sexual_health_dx_checkbox(self, event):
        if getattr(self, "_suppress_sexual_health_dx_event", False):
            event.Skip()
            return
        selected: List[str] = []
        if self.sexual_health_dx_ed.GetValue():
            selected.append("ED")
        if self.sexual_health_dx_pe.GetValue():
            selected.append("PE")
        if self.sexual_health_dx_pe_like.GetValue():
            selected.append("PE-like ejaculatory dysfunction")
        if self.sexual_health_dx_hair_loss.GetValue():
            selected.append("Hair Loss")
        diagnoses[0] = ", ".join(selected)
        print(f"Sexual Health diagnoses set to: {diagnoses[0] or 'none'}")
        event.Skip()

    def open_pmh_dialog(self, event=None):
        """Open a modal dialog with a checklist for PMH multi-select."""
        try:
            dlg = wx.Dialog(self, title="Select PMH Diagnoses", style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
            vbox = wx.BoxSizer(wx.VERTICAL)
            info = wx.StaticText(dlg, label="Select all that apply; leave all unchecked for noncontributory:")
            vbox.Add(info, 0, wx.ALL, 8)
            clb = wx.CheckListBox(dlg, choices=PMH_OPTIONS)
            # Pre-check any already selected
            prechecked = [i for i, opt in enumerate(PMH_OPTIONS) if opt in pmh_selected]
            for idx in prechecked:
                clb.Check(idx, True)
            vbox.Add(clb, 1, wx.ALL | wx.EXPAND, 8)
            btn_sizer = dlg.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
            vbox.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
            dlg.SetSizerAndFit(vbox)
            if dlg.ShowModal() == wx.ID_OK:
                # Update selection
                pmh_selected.clear()
                for i in range(clb.GetCount()):
                    if clb.IsChecked(i):
                        pmh_selected.add(PMH_OPTIONS[i])
                self.update_pmh_summary()
            dlg.Destroy()
        except Exception as e:
            wx.MessageBox(f"PMH dialog error: {e}", "Error", wx.ICON_ERROR)

    def update_pmh_summary(self):
        """Update the PMH summary label from current selection."""
        try:
            phrase = build_pmh_text()
            if self.pmh_summary:
                if phrase == "is noncontributory":
                    self.pmh_summary.SetLabel("Current: none (noncontributory)")
                else:
                    self.pmh_summary.SetLabel(f"Current: {phrase.replace('significant for ', '')}")
        except Exception:
            pass

    def toggle_invisit_clicker(self):
        """Toggle auto-clicker that runs quick_next_task repeatedly at interval until URL changes"""
        if self.invisit_running:
            self.stop_invisit_clicker()
        else:
            self.start_invisit_clicker()
        self._push_overlay_invisit_autoclicker_state()
    
    def stop_invisit_clicker(self):
        """Stop the in-visit auto-clicker"""
        self.invisit_running = False
        self.clicker_invisit_btn.SetLabel("Start Autoclick: In-Visit")
        self.clicker_status_text.SetLabel("Status: STOPPED")
        self.clicker_status_text.SetForegroundColour(wx.Colour(255, 0, 0))  # Red
        print("[In-Visit Autoclick] Stopped by user")
        self._push_overlay_invisit_autoclicker_state()
    
    def start_invisit_clicker(self):
        """Start the in-visit auto-clicker (runs quick_next_task repeatedly)"""
        if self.invisit_running:
            return
        
        self.invisit_running = True
        self.clicker_invisit_btn.SetLabel("Stop Autoclick: In-Visit")
        self.clicker_status_text.SetLabel("Status: RUNNING (In-Visit)")
        self.clicker_status_text.SetForegroundColour(wx.Colour(0, 200, 0))  # Green
        
        # Get interval from settings
        try:
            interval = float(self.clicker_interval_text.GetValue())
        except ValueError:
            interval = 3.0
        
        # Start monitoring thread
        def monitor_and_click():
            grabber = BrowserEMRGrabber()
            if not grabber.connect_to_chrome():
                wx.CallAfter(lambda: print("❌ Could not connect to browser for in-visit autoclick"))
                wx.CallAfter(self.stop_invisit_clicker)
                return
            
            def normalize_url(u: str) -> str:
                try:
                    if not u:
                        return ""
                    parts = urlsplit(u)
                    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))
                except Exception:
                    return u or ""

            def resolve_url() -> str:
                active_url = self.get_active_page_url()
                if active_url:
                    return active_url
                try:
                    return grabber.driver.current_url or ""
                except Exception:
                    return ""

            initial_url = resolve_url()
            initial_norm = normalize_url(initial_url)
            if not initial_norm:
                for _ in range(5):
                    time.sleep(0.2)
                    initial_url = resolve_url()
                    initial_norm = normalize_url(initial_url)
                    if initial_norm:
                        break
            if not initial_norm:
                print("[In-Visit Autoclick] Could not determine baseline URL, stopping...")
                wx.CallAfter(lambda: (
                    self.clicker_invisit_btn.SetLabel("Start Autoclick: In-Visit"),
                    self.clicker_status_text.SetLabel("Status: STOPPED (URL Unknown)"),
                    self.clicker_status_text.SetForegroundColour(wx.Colour(255, 165, 0))
                ))
                wx.CallAfter(self.beep_sound)
                self.invisit_running = False
                return
            print(f"[In-Visit Autoclick] Starting on URL: {initial_url}")
            unresolved_url_checks = 0
            
            # Helper to perform the click sequence via Playwright simulated clicks
            def do_click_sequence():
                page = grabber.driver.page if grabber.driver else None
                if not page:
                    print("⚠️ In-Visit: No Playwright page available")
                    return False

                # Step 1: Click floating action button
                fab_selector = '[data-testid="floatingActionButton"]'
                fab_retries = 2
                fab_clicked = False
                for attempt in range(fab_retries):
                    try:
                        locator = page.locator(fab_selector)
                        if locator.count() > 0:
                            locator.first.click(timeout=3000)
                            fab_clicked = True
                            break
                    except Exception as e:
                        if attempt < fab_retries - 1:
                            time.sleep(0.05)
                        else:
                            print(f"⚠️ In-Visit: FAB click failed after {fab_retries} retries: {e}")
                if not fab_clicked:
                    print("⚠️ In-Visit: Could not click floating action button")
                    return False

                # Wait 120ms after FAB click for menu to appear
                time.sleep(0.120)

                # Step 2: Click "Get Next Task" menu item
                menu_selector = '[data-testid="menu-item-Get Next Task"]'
                menu_retries = 2
                menu_clicked = False
                for attempt in range(menu_retries):
                    try:
                        locator = page.locator(menu_selector)
                        if locator.count() > 0:
                            locator.first.click(timeout=3000)
                            menu_clicked = True
                            break
                    except Exception as e:
                        if attempt < menu_retries - 1:
                            time.sleep(0.05)
                        else:
                            print(f"⚠️ In-Visit: Menu item click failed after {menu_retries} retries: {e}")
                if not menu_clicked:
                    print("⚠️ In-Visit: Could not click 'Get Next Task' menu item")
                    return False

                # Wait 20ms after menu click
                time.sleep(0.020)
                print("✅ In-Visit: Clicked 'Get Next Task' (Playwright)")
                return True
            
            while self.invisit_running:
                try:
                    current_url = resolve_url()
                    current_norm = normalize_url(current_url)
                    if not current_norm:
                        unresolved_url_checks += 1
                        if unresolved_url_checks >= 5:
                            print("[In-Visit Autoclick] Could not resolve URL reliably, stopping...")
                            wx.CallAfter(lambda: (
                                self.clicker_invisit_btn.SetLabel("Start Autoclick: In-Visit"),
                                self.clicker_status_text.SetLabel("Status: STOPPED (URL Unknown)"),
                                self.clicker_status_text.SetForegroundColour(wx.Colour(255, 165, 0))
                            ))
                            wx.CallAfter(self.beep_sound)
                            self.invisit_running = False
                            break
                    else:
                        unresolved_url_checks = 0
                    
                    # Stop if URL changed
                    if initial_norm and current_norm != initial_norm:
                        print(f"[In-Visit Autoclick] URL changed, stopping...")
                        display_url = current_url or current_norm
                        wx.CallAfter(lambda: (
                            self.clicker_invisit_btn.SetLabel("Start Autoclick: In-Visit"),
                            self.clicker_status_text.SetLabel("Status: STOPPED (URL Changed)"),
                            self.clicker_status_text.SetForegroundColour(wx.Colour(255, 165, 0))
                        ))
                        wx.CallAfter(lambda url=display_url: self.update_url_display(url))
                        wx.CallAfter(self.beep_sound)
                        self.invisit_running = False
                        break
                    
                    # Perform click sequence synchronously (no additional threads)
                    do_click_sequence()
                    
                    # Wait for interval before next click
                    time.sleep(interval)
                    
                except Exception as e:
                    print(f"⚠️ In-Visit monitor error: {e}")
                    time.sleep(interval)
            
            print("[In-Visit Autoclick] Stopped")
        
        self.invisit_thread = threading.Thread(target=monitor_and_click, daemon=True)
        self.invisit_thread.start()
    
    def toggle_page_refresh(self):
        """Toggle page refresh that refreshes the page every 3 minutes ±30%"""
        if self.page_refresh_running:
            self.stop_page_refresh()
        else:
            self.start_page_refresh()
    
    def stop_page_refresh(self):
        """Stop the page refresh"""
        self.page_refresh_running = False
        self.page_refresh_btn.SetLabel("Start Auto-Refresh Page")
        print("[Page Refresh] Stopped by user")
    
    def start_page_refresh(self):
        """Start page refresh that refreshes page every 3 minutes ±30%"""
        if self.page_refresh_running:
            return
        
        self.page_refresh_running = True
        self.page_refresh_btn.SetLabel("Stop Auto-Refresh Page")
        
        def refresh_loop():
            import random
            
            grabber = BrowserEMRGrabber()
            if not grabber.connect_to_chrome():
                wx.CallAfter(lambda: print("❌ Could not connect to browser for page refresh"))
                wx.CallAfter(self.stop_page_refresh)
                return
            
            print("[Page Refresh] Started - refreshing every 3 minutes ±30%")
            
            while self.page_refresh_running:
                try:
                    # Calculate random interval: 3 minutes ± 30% = 126-234 seconds
                    base_interval = 180  # 3 minutes in seconds
                    variance = base_interval * 0.3  # 30% variance
                    interval = random.uniform(base_interval - variance, base_interval + variance)
                    
                    print(f"[Page Refresh] Next refresh in {interval:.1f} seconds ({interval/60:.1f} minutes)")
                    
                    # Wait for the interval
                    time.sleep(interval)
                    
                    if not self.page_refresh_running:
                        break
                    
                    # Refresh the page via CDP
                    refresh_script = """
                        window.location.reload();
                        return { success: true };
                    """
                    
                    result = grabber.driver.execute_script(refresh_script)
                    if result and result.get('success'):
                        print(f"✅ [Page Refresh] Page refreshed at {time.strftime('%H:%M:%S')}")
                    else:
                        print("⚠️ [Page Refresh] Refresh command sent but no confirmation")
                    
                except Exception as e:
                    print(f"⚠️ [Page Refresh] Error: {e}")
                    if self.page_refresh_running:
                        time.sleep(60)  # Wait 1 minute before retrying on error
            
            print("[Page Refresh] Stopped")
        
        self.page_refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
        self.page_refresh_thread.start()
    
    def quick_next_task(self):
        """Click floating action button, then click 'Get Next Task' menu item using CDP (with Playwright fallback)"""
        def _click_via_cdp(grabber):
            """Try to click using CDP JavaScript injection"""
            try:
                # First click: Floating action button via CDP
                floating_script = """
                    const selectors = [
                        '[data-testid="floatingActionButton"]',
                        'button[aria-label="Floating Action Button"]',
                        'button.rounded-full.bg-black.drop-shadow-md'
                    ];
                    
                    for (const sel of selectors) {
                        const elem = document.querySelector(sel);
                        if (elem) {
                            elem.click();
                            return { success: true, selector: sel };
                        }
                    }
                    return { success: false };
                """
                
                result = grabber.driver.execute_script(floating_script)
                
                if not result or not isinstance(result, dict) or not result.get('success'):
                    print("⚠️ CDP: Could not find floating button")
                    return False
                
                print(f"✅ CDP: Clicked floating button with: {result.get('selector')}")
                time.sleep(0.8)  # Wait longer for menu animation to complete
                
                # Second click: Get Next Task menu item via CDP
                menu_script = """
                    try {
                        const selectors = [
                            '[data-testid="menu-item-Get Next Task"]',
                            'div.css-1hj5o6h[data-testid="menu-item-Get Next Task"]',
                            'div[role="button"][data-testid="menu-item-Get Next Task"]'
                        ];
                        
                        // First check if any selector finds the element
                        for (const sel of selectors) {
                            const elem = document.querySelector(sel);
                            if (elem) {
                                const isVisible = elem.offsetWidth > 0 && elem.offsetHeight > 0;
                                console.log('Found element with selector:', sel, 'Visible:', isVisible);
                                if (isVisible) {
                                    elem.click();
                                    return { success: true, selector: sel };
                                }
                            }
                        }
                        
                        // Try text-based search as fallback
                        const allDivs = document.querySelectorAll('div[role="button"]');
                        console.log('Searching through', allDivs.length, 'role=button divs');
                        for (const div of allDivs) {
                            const text = (div.textContent || '').trim();
                            if (text === 'Get Next Task') {
                                console.log('Found via text search');
                                div.click();
                                return { success: true, selector: 'text search: ' + text };
                            }
                        }
                        
                        return { success: false, error: 'Element not found or not visible' };
                    } catch (e) {
                        return { success: false, error: e.message };
                    }
                """
                
                result = grabber.driver.execute_script(menu_script)
                
                if not result or not isinstance(result, dict):
                    print(f"⚠️ CDP: Menu script returned invalid result: {result}")
                    return False
                
                if not result.get('success'):
                    error = result.get('error', 'unknown')
                    print(f"⚠️ CDP: Could not find 'Get Next Task' menu item: {error}")
                    return False
                
                print(f"✅ CDP: Clicked 'Get Next Task' with: {result.get('selector')}")
                return True
                
            except Exception as e:
                print(f"⚠️ CDP click failed: {e}")
                return False
        
        def _click_via_playwright(grabber):
            """Fallback to Playwright if CDP fails"""
            try:
                print("🔄 Falling back to Playwright method...")
                
                # First click: Floating action button
                floating_btn_selectors = [
                    '[data-testid="floatingActionButton"]',
                    'button[aria-label="Floating Action Button"]',
                    'button.rounded-full.bg-black.drop-shadow-md'
                ]
                
                clicked_floating = False
                for selector in floating_btn_selectors:
                    try:
                        locator = grabber.page.locator(selector)
                        if locator.count() > 0:
                            locator.first.click()
                            clicked_floating = True
                            print(f"✅ Playwright: Clicked floating button with: {selector}")
                            time.sleep(0.3)
                            break
                    except Exception:
                        continue
                
                if not clicked_floating:
                    return False
                
                # Second click: Get Next Task menu item
                menu_item_selectors = [
                    '[data-testid="menu-item-Get Next Task"]',
                    'div[role="button"]:has-text("Get Next Task")',
                    'div.css-1hj5o6h[data-testid="menu-item-Get Next Task"]'
                ]
                
                clicked_menu = False
                for selector in menu_item_selectors:
                    try:
                        locator = grabber.page.locator(selector)
                        if locator.count() > 0:
                            locator.first.click()
                            clicked_menu = True
                            print(f"✅ Playwright: Clicked 'Get Next Task' with: {selector}")
                            break
                    except Exception:
                        continue
                
                return clicked_menu
                
            except Exception as e:
                print(f"⚠️ Playwright click failed: {e}")
                return False
        
        def _click_sequence():
            try:
                grabber = BrowserEMRGrabber()
                if not grabber or not grabber.connect_to_chrome():
                    wx.CallAfter(lambda: wx.MessageBox(
                        'Could not connect to browser. Make sure Chrome/Thorium is running with debug mode.',
                        'Connection Error',
                        wx.ICON_WARNING
                    ))
                    return
                
                # Try CDP first (faster and more reliable)
                success = _click_via_cdp(grabber)
                
                # Fall back to Playwright if CDP failed
                if not success:
                    success = _click_via_playwright(grabber)
                
                if success:
                    wx.CallAfter(lambda: print("✅ Quick Next Task completed successfully"))
                else:
                    wx.CallAfter(lambda: wx.MessageBox(
                        'Could not find floating button or "Get Next Task" menu item using either method.',
                        'Element Not Found',
                        wx.ICON_WARNING
                    ))
                
            except Exception as e:
                wx.CallAfter(lambda: wx.MessageBox(
                    f'Error during Quick Next Task: {e}',
                    'Error',
                    wx.ICON_ERROR
                ))
        
        # Run in thread to avoid blocking GUI
        threading.Thread(target=_click_sequence, daemon=True).start()

    def toggle_auto_clicker(self):
        """Toggle the auto clicker on/off"""
        auto_clicker_enabled[0] = not auto_clicker_enabled[0]
        
        if auto_clicker_enabled[0]:
            # Update settings from UI
            try:
                auto_clicker_x[0] = int(self.clicker_x_text.GetValue())
                auto_clicker_y[0] = int(self.clicker_y_text.GetValue())
                auto_clicker_interval[0] = float(self.clicker_interval_text.GetValue())
            except ValueError:
                wx.MessageBox("Invalid coordinates or interval values", "Error", wx.ICON_ERROR)
                auto_clicker_enabled[0] = False
                return
            
            # Start clicker
            self.clicker_start_btn.SetLabel("Stop Clicking")
            self.clicker_status_text.SetLabel("Status: RUNNING")
            self.clicker_status_text.SetForegroundColour(wx.Colour(0, 128, 0))  # Green
            
            # Start the clicker thread
            if auto_clicker_thread[0] is None or not auto_clicker_thread[0].is_alive():
                auto_clicker_thread[0] = threading.Thread(target=self.auto_clicker_loop, daemon=True)
                auto_clicker_thread[0].start()
            
            print(f"Auto clicker started: ({auto_clicker_x[0]}, {auto_clicker_y[0]}) every {auto_clicker_interval[0]}s")
        else:
            # Stop clicker
            self.clicker_start_btn.SetLabel("Start Autoclick: Dashboard")
            self.clicker_status_text.SetLabel("Status: STOPPED")
            self.clicker_status_text.SetForegroundColour(wx.Colour(255, 0, 0))  # Red
            print("Auto clicker stopped")
        self._push_overlay_autoclicker_state()

    def get_active_page_url(self):
        """Query Chrome/Thorium remote debugging API for active page URL"""
        try:
            debug_url = f"http://localhost:{CDP_DEBUG_PORT}/json"
            if getattr(self, 'requests_session', None) is not None:
                resp = self.requests_session.get(debug_url, timeout=1.2)
            else:
                resp = requests.get(debug_url, timeout=1.2)
            resp.raise_for_status()
            tabs = resp.json()
            
            if not isinstance(tabs, list):
                return ""
            
            # Debug: print first few tabs
            if len(tabs) > 0:
                print(f"Found {len(tabs)} browser tabs")
                for i, tab in enumerate(tabs[:3]):  # Show first 3 tabs
                    if isinstance(tab, dict):
                        url = tab.get("url", "")
                        tab_type = tab.get("type", "")
                        title = tab.get("title", "")[:50]
                        print(f"  Tab {i}: type='{tab_type}', url='{url[:60]}...', title='{title}'")
            
            # Build candidate list of real pages
            candidates = []
            for tab in tabs:
                if not isinstance(tab, dict):
                    continue
                ttype = tab.get("type", "")
                url = (tab.get("url", "") or "").strip()
                if ttype != "page":
                    continue
                if not (url.startswith("http://") or url.startswith("https://")):
                    continue
                if url.startswith(("devtools://", "chrome://", "extension://")):
                    continue
                candidates.append(url)

            # Prefer EMR domain to stabilize selection
            for url in candidates:
                if "emr.forhims.com" in url:
                    print(f"Selected active URL (EMR): {url}")
                    return url

            # Fallback to first candidate
            if candidates:
                print(f"Selected active URL (first candidate): {candidates[0]}")
                return candidates[0]
            
            # Fallback: first tab's URL if it's http/https
            if tabs:
                url = tabs[0].get("url", "") or ""
                if url.startswith(("http://", "https://")):
                    print(f"Fallback URL: {url}")
                    return url
                    
        except requests.exceptions.ConnectionError:
            print(f"Cannot connect to browser debugging API. Make sure Chrome or Thorium is running with --remote-debugging-port={CDP_DEBUG_PORT}")
            wx.CallAfter(self.show_chrome_error)
        except Exception as e:
            print(f"Chrome API error: {e}")
        
        return ""

    def show_chrome_error(self):
        """Show error dialog for Chrome debugging connection"""
        if hasattr(self, '_chrome_error_shown'):
            return  # Don't spam error dialogs
        self._chrome_error_shown = True
        
        msg = ("Cannot connect to Chrome Remote Debugging API.\n\n"
               "To enable URL monitoring:\n"
               "1. Close Chrome completely\n"
               f"2. Start Chrome/Thorium with: --remote-debugging-port={CDP_DEBUG_PORT}\n"
               f"3. Or add --remote-debugging-port={CDP_DEBUG_PORT} to browser shortcut\n\n"
               "Auto clicker will continue without URL monitoring.")
        wx.MessageBox(msg, "Chrome Debugging Not Available", wx.ICON_WARNING)

    def beep_sound(self):
        """Play a beep sound"""
        try:
            winsound.Beep(1000, 700)
        except Exception:
            try:
                winsound.MessageBeep()
            except:
                print("🔔 URL Changed!")

    def auto_clicker_loop(self):
        """Main auto clicker loop"""
        click_count = 0
        print("Auto clicker loop started")

        # Helper to normalize URLs (ignore query and fragment to avoid false positives)
        def normalize_url(u: str) -> str:
            try:
                if not u:
                    return ""
                parts = urlsplit(u)
                # Rebuild without query/fragment
                return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))
            except Exception:
                return u or ""

        # Establish a baseline URL before the very first click
        baseline_url = self.get_active_page_url()
        if baseline_url:
            auto_clicker_last_url[0] = normalize_url(baseline_url)
            wx.CallAfter(self.update_url_display, baseline_url)
            print(f"🔗 Initial baseline URL: {baseline_url}")

        # Thread-local BrowserEMRGrabber for CDP/browser clicks
        grabber: Optional[BrowserEMRGrabber] = None

        try:
            while auto_clicker_enabled[0]:
                try:
                    loop_start = time.perf_counter()

                    # Capture URL right before the click as pre-click baseline
                    pre_url = self.get_active_page_url()
                    pre_norm = normalize_url(pre_url)

                    desired_method = getattr(self, "clicker_method_mode", "cdp")
                    if desired_method in ("browser", "cdp") and grabber is None:
                        try:
                            grabber = BrowserEMRGrabber()
                        except Exception as ge:
                            print(f"Failed to initialize browser grabber for {desired_method.upper()} clicks: {ge}")
                            grabber = None

                    used_method: Optional[str] = None
                    if desired_method == "browser" and grabber is not None:
                        try:
                            if grabber.click_get_next_task():
                                used_method = "browser"
                        except Exception as se:
                            print(f"Browser click attempt failed: {se}")
                    elif desired_method == "cdp" and grabber is not None:
                        try:
                            if grabber.click_get_next_task_cdp():
                                used_method = "cdp"
                        except Exception as ce:
                            print(f"CDP click attempt failed: {ce}")

                    if used_method is None:
                        if desired_method != "xy":
                            if grabber is None and desired_method in ("browser", "cdp"):
                                print(f"{desired_method.upper()} clicker unavailable; using X/Y screen fallback")
                            else:
                                print(f"{desired_method.upper()} click failed; using X/Y screen fallback")
                        pyautogui.click(auto_clicker_x[0], auto_clicker_y[0])
                        used_method = "xy"

                    if used_method == "browser":
                        self.browser_click_count += 1
                    elif used_method == "cdp":
                        self.cdp_click_count += 1
                    else:
                        self.xy_click_count += 1

                    click_count += 1
                    wx.CallAfter(self.update_click_method, used_method)

                    # Give the browser a brief moment to navigate if the click triggers a change
                    time.sleep(0.15)

                    # Poll briefly for up to ~0.6-0.7s to detect fast navigation after click
                    post_url = self.get_active_page_url()
                    post_norm = normalize_url(post_url)
                    poll_attempts = 0
                    while poll_attempts < 3:
                        if post_norm and pre_norm and post_norm != pre_norm:
                            break
                        time.sleep(0.2)
                        post_url = self.get_active_page_url()
                        post_norm = normalize_url(post_url)
                        poll_attempts += 1

                    # If URL changed as a result of this click (including the very first click), stop
                    if post_norm and pre_norm and post_norm != pre_norm:
                        print(f"🔔 URL CHANGED on click #{click_count}!")
                        print(f"   From: {pre_url}")
                        print(f"   To:   {post_url}")
                        auto_clicker_enabled[0] = False
                        if self.notify_with_popup:
                            wx.CallAfter(self.show_new_task_popup)
                        wx.CallAfter(self.beep_sound)
                        wx.CallAfter(self.update_clicker_stopped)
                        wx.CallAfter(self.update_url_display, post_url)
                        wx.CallAfter(self.refresh_patient_location_async)
                        auto_clicker_last_url[0] = post_norm
                        break

                    # Otherwise, if we have a last_url and the new URL differs, also stop
                    if post_norm and auto_clicker_last_url[0] and post_norm != auto_clicker_last_url[0]:
                        print(f"🔔 URL CHANGED on click #{click_count} (vs. stored baseline)!")
                        print(f"   From: {auto_clicker_last_url[0]}")
                        print(f"   To:   {post_url}")
                        auto_clicker_enabled[0] = False
                        if self.notify_with_popup:
                            wx.CallAfter(self.show_new_task_popup)
                        wx.CallAfter(self.beep_sound)
                        wx.CallAfter(self.update_clicker_stopped)
                        wx.CallAfter(self.update_url_display, post_url)
                        wx.CallAfter(self.refresh_patient_location_async)
                        auto_clicker_last_url[0] = post_norm
                        break

                    # Update stored baseline when stable
                    if post_norm:
                        auto_clicker_last_url[0] = post_norm

                    # Periodic status updates
                    if post_url:
                        if click_count % 10 == 0:
                            print(f"✓ Clicked {click_count} times, URL unchanged")
                            wx.CallAfter(self.update_url_display, post_url)
                    else:
                        if click_count % 5 == 0:
                            print(f"⚠ No URL detected after {click_count} clicks")
                            wx.CallAfter(self.update_url_display, "No active page detected - check Chrome debugging")

                    # Update last-click UI string
                    wx.CallAfter(self.update_last_click_time)

                    # Honor the configured interval precisely accounting for elapsed work time
                    elapsed = time.perf_counter() - loop_start
                    remaining = max(0.0, float(auto_clicker_interval[0]) - elapsed)
                    if remaining > 0:
                        time.sleep(remaining)

                except Exception as e:
                    print(f"❌ Auto clicker error: {e}")
                    time.sleep(1)

            print(f"Auto clicker loop ended after {click_count} clicks")
        finally:
            if grabber is not None:
                try:
                    grabber.shutdown_playwright()
                except Exception:
                    pass

    def update_clicker_stopped(self):
        """Update UI when clicker is stopped due to URL change"""
        self.clicker_start_btn.SetLabel("Start Autoclick: Dashboard")
        if hasattr(self, 'clicker_invisit_btn'):
            self.clicker_invisit_btn.SetLabel("Start Autoclick: In-Visit")
        self.clicker_status_text.SetLabel("Status: STOPPED (URL Changed)")
        self.clicker_status_text.SetForegroundColour(wx.Colour(255, 165, 0))  # Orange
        self._push_overlay_autoclicker_state()
        self._push_overlay_invisit_autoclicker_state()

    def update_click_method(self, method: str):
        """Show which method performed the last click and colorize label."""
        method = (method or "").lower()
        if method in ("browser", "selenium"):
            self.clicker_method_text.SetLabel(f"Click method: Browser ({self.browser_click_count})")
            self.clicker_method_text.SetForegroundColour(wx.Colour(0, 128, 0))  # Green
        elif method == "cdp":
            self.clicker_method_text.SetLabel(f"Click method: CDP ({self.cdp_click_count})")
            self.clicker_method_text.SetForegroundColour(wx.Colour(30, 144, 255))  # DodgerBlue
        else:
            self.clicker_method_text.SetLabel(f"Click method: X/Y screen ({self.xy_click_count})")
            self.clicker_method_text.SetForegroundColour(wx.Colour(255, 140, 0))  # Orange

    def update_last_click_time(self):
        ts = time.strftime('%H:%M:%S')
        self.clicker_last_text.SetLabel(f"Last click: {ts}")

    def update_url_display(self, url):
        """Update the URL display in the UI"""
        display_url = url if len(url) <= 80 else url[:77] + "..."
        self.clicker_url_text.SetLabel(f"Current URL: {display_url}")

    def on_click_method_changed(self, event=None):
        """Persist selected click method and refresh the label color."""
        previous = getattr(self, "clicker_method_mode", "cdp")
        if getattr(self, "method_browser_rb", None) and self.method_browser_rb.GetValue():
            self.clicker_method_mode = "browser"
        elif getattr(self, "method_xy_rb", None) and self.method_xy_rb.GetValue():
            self.clicker_method_mode = "xy"
        elif getattr(self, "method_cdp_rb", None) and self.method_cdp_rb.GetValue():
            self.clicker_method_mode = "cdp"
        else:
            self.clicker_method_mode = "browser"
        if self.clicker_method_mode != previous:
            print(f"Auto clicker method set to {self.clicker_method_mode.upper()}")
        self.update_click_method(self.clicker_method_mode)

    def on_popup_toggle(self, event):
        """Handle toggle for popup vs beep notification"""
        try:
            self.notify_with_popup = self.popup_toggle.GetValue()
        except Exception:
            self.notify_with_popup = False

    def show_new_task_popup(self):
        """Show a small always-on-top popup with a button to open EMR in Chrome."""
        try:
            # If already open, just raise it
            if self.new_task_popup and self.new_task_popup.IsShown():
                try:
                    self.new_task_popup.Raise()
                    self.new_task_popup.RequestUserAttention(wx.NOTIFY)
                except Exception:
                    pass
                return

            dlg = wx.Dialog(
                self,
                title="New task detected",
                style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP | wx.FRAME_NO_TASKBAR | wx.FRAME_TOOL_WINDOW,
            )
            sizer = wx.BoxSizer(wx.VERTICAL)
            msg = wx.StaticText(dlg, label="New task detected")
            btn_row = wx.BoxSizer(wx.HORIZONTAL)
            open_btn = wx.Button(dlg, label="Open EMR in Chrome")
            close_btn = wx.Button(dlg, label="Close")

            open_btn.Bind(wx.EVT_BUTTON, lambda evt: self._on_popup_open_emr(dlg))
            close_btn.Bind(wx.EVT_BUTTON, lambda evt: dlg.Destroy())

            sizer.Add(msg, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 10)
            btn_row.Add(open_btn, 0, wx.ALL, 5)
            btn_row.Add(close_btn, 0, wx.ALL, 5)
            sizer.Add(btn_row, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 5)

            dlg.SetSizerAndFit(sizer)
            # Position centered on the visible screen (same display as the app window)
            try:
                display_index = wx.Display.GetFromWindow(self)
                if display_index == wx.NOT_FOUND:
                    display_index = 0
                rect = wx.Display(display_index).GetClientArea()  # excludes taskbar
                size = dlg.GetSize()
                x = rect.GetX() + (rect.GetWidth() - size.GetWidth()) // 2
                y = rect.GetY() + (rect.GetHeight() - size.GetHeight()) // 2
                dlg.SetPosition((x, y))
            except Exception:
                try:
                    dlg.CentreOnScreen()
                except Exception:
                    pass
            self.new_task_popup = dlg
            dlg.Show()  # modeless
            dlg.Raise()
        except Exception as e:
            print(f"Error showing popup: {e}")

    def _on_popup_open_emr(self, dlg):
        # Close popup first to avoid Z-order/focus flicker, then bring Chrome forward
        try:
            dlg.Destroy()
        except Exception:
            pass
        self.new_task_popup = None
        self.open_emr_in_chrome()

    def open_emr_in_chrome(self):
        """Activate the EMR tab in Chrome and bring the Chrome window to foreground."""
        try:
            # Reuse a cached browser grabber to avoid repeated attach overhead/flicker
            try:
                grabber = getattr(self, "_browser_grabber_cache", None)
                if grabber is None:
                    grabber = BrowserEMRGrabber()
                    self._browser_grabber_cache = grabber
                if grabber.connect_to_chrome():
                    try:
                        grabber._ensure_emr_tab()
                    except Exception:
                        pass
                elif grabber is getattr(self, "_browser_grabber_cache", None):
                    self._browser_grabber_cache = None
            except Exception as e:
                print(f"Browser attach error: {e}")

            # Bring Chrome to front using pygetwindow
            try:
                wins = gw.getWindowsWithTitle('Chrome')
                if not wins:
                    wins = gw.getWindowsWithTitle('Google Chrome')
                # Prefer a normal, visible window
                target = None
                for w in wins:
                    try:
                        if w.isMinimized:
                            continue
                        target = w
                        break
                    except Exception:
                        continue
                if not target and wins:
                    target = wins[0]
                if target:
                    try:
                        if target.isMinimized:
                            target.restore()
                            time.sleep(0.12)
                    except Exception:
                        pass
                    try:
                        target.activate()
                    except Exception:
                        pass
            except Exception as we:
                print(f"Window activation error: {we}")
        except Exception as e:
            print(f"open_emr_in_chrome error: {e}")

    def test_chrome_connection(self):
        """Test connection to Chrome/Thorium debugging API"""
        print("Testing browser connection...")
        url = self.get_active_page_url()
        
        if url:
            wx.MessageBox(f"✅ Browser connection successful!\n\nActive URL:\n{url}", 
                         "Browser Test Result", wx.ICON_INFORMATION)
            self.update_url_display(url)
        else:
            msg = (f"❌ Cannot connect to browser or no active pages found.\n\n"
                   f"Make sure:\n"
                   f"1. Chrome or Thorium is running\n"
                   f"2. Started with --remote-debugging-port={CDP_DEBUG_PORT}\n"
                   f"3. At least one tab is open with a website")
            wx.MessageBox(msg, "Browser Test Failed", wx.ICON_WARNING)

    def setup_global_hotkeys(self):
        """Setup global hotkeys that work regardless of window focus"""
        try:
            # Register F4 as a global hotkey
            keyboard.add_hotkey('f4', self.trigger_appropriate_grab)
            print("Global F4 hotkey registered successfully")

            # Register Ctrl+Alt+Enter for Quick Next Task (optional)
            if ENABLE_QUICK_NEXT_TASK_HOTKEY:
                keyboard.add_hotkey(QUICK_NEXT_TASK_HOTKEY, lambda: wx.CallAfter(self.quick_next_task))
                print(f"Quick Next Task hotkey registered ({QUICK_NEXT_TASK_HOTKEY})")
            else:
                print("Quick Next Task hotkey disabled")
            
            # Register Ctrl+Alt+I for In-Visit Autoclick
            keyboard.add_hotkey('ctrl+alt+i', lambda: wx.CallAfter(self.toggle_invisit_clicker))
            print("In-Visit Autoclick hotkey registered (Ctrl+Alt+I)")
            
            # Register Ctrl+Shift+R for Page Refresh
            keyboard.add_hotkey('ctrl+shift+r', lambda: wx.CallAfter(self.toggle_page_refresh))
            print("Page Refresh hotkey registered (Ctrl+Shift+R)")

            # Prefer OS-level registration for the GUI toggle
            self._gui_toggle_hotkey_method = None
            self._gui_toggle_hotkey_handle = None
            try:
                if self._gui_toggle_hotkey_id is None:
                    try:
                        self._gui_toggle_hotkey_id = wx.Window.NewControlId()
                    except Exception:
                        self._gui_toggle_hotkey_id = 9301
                if self.RegisterHotKey(self._gui_toggle_hotkey_id, wx.MOD_CONTROL | wx.MOD_ALT, ord('H')):
                    try:
                        self.Unbind(wx.EVT_HOTKEY, id=self._gui_toggle_hotkey_id)
                    except Exception:
                        pass
                    self.Bind(wx.EVT_HOTKEY, lambda evt: self.toggle_gui_visibility(), id=self._gui_toggle_hotkey_id)
                    self._gui_toggle_hotkey_method = 'wx'
                    print("GUI toggle OS-level hotkey registered (Ctrl+Alt+H)")
                else:
                    raise RuntimeError("RegisterHotKey returned False")
            except Exception as exc:
                try:
                    self._gui_toggle_hotkey_handle = keyboard.add_hotkey(
                        'ctrl+alt+h',
                        lambda: wx.CallAfter(self.toggle_gui_visibility)
                    )
                    self._gui_toggle_hotkey_method = 'keyboard'
                    print("GUI toggle hotkey registered via keyboard module (Ctrl+Alt+H)")
                except Exception as fallback_exc:
                    self._gui_toggle_hotkey_handle = None
                    self._gui_toggle_hotkey_method = None
                    print(f"Failed to register GUI toggle hotkey: {exc} | Fallback error: {fallback_exc}")
            # Prepare dynamic registration for Tab 1 (T Deficiency), Tab 2 (Hair), and Tab 4 (Sexual Health)
            # hotkeys (active only when the respective tab is selected)
            self._tab4_hotkeys_registered = False
            # Where to change key combos for each tab: update the lists below
            self._tab1_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n', 'ctrl+alt+c']  # T Deficiency: F=Lab msg, N=Rx note, C=Referral
            self._tab2_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n', 'ctrl+alt+c']  # Hair Loss: F=Follow-up, N=Initial, C=Limited
            self._tab4_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n', 'ctrl+alt+c']  # Sexual Health: F=Follow-up, N=Plan, C=Change
            self._tab7_hotkey_names = ['ctrl+alt+f', 'ctrl+alt+n']               # Birth Control: F=Follow-up note, N=Initial note
            self._tab4_hotkeys_method = None  # 'wx' or 'keyboard'
            self._tab4_keyboard_handles = []  # fallback handles for keyboard lib
            self._hotkeys_target = None       # 'td', 'hair', or 'sh'
            # Pre-create unique IDs for OS-level global hotkeys
            try:
                self._hk_id_followup = wx.Window.NewControlId()
                self._hk_id_plan = wx.Window.NewControlId()
                self._hk_id_change = wx.Window.NewControlId()
            except Exception:
                # Reasonable static fallback
                self._hk_id_followup = 9101
                self._hk_id_plan = 9102
                self._hk_id_change = 9103
            # Bind to notebook page change so we can toggle hotkeys based on selected tab
            try:
                self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab_changed)
            except Exception:
                pass
            # Set initial state based on current selection
            self._refresh_tab4_global_hotkeys()
        except Exception as e:
            print(f"Failed to register global F4 hotkey: {e}")
            wx.MessageBox(f"Failed to register global F4 hotkey: {e}", "Hotkey Registration Failed", wx.ICON_WARNING)

    def toggle_gui_visibility(self):
        if getattr(self, "_js_overlay_active", False):
            self._queue_overlay_cmd("toggle_minimize")
            return
        if not self._is_gui_foreground():
            self._bring_gui_to_foreground()
            return
        if getattr(self, "_gui_hidden", False):
            self._show_gui_window()
        else:
            self._hide_gui_window()

    def _is_gui_foreground(self) -> bool:
        try:
            if self.IsActive():
                return True
        except Exception:
            pass
        try:
            active = gw.getActiveWindow()
            if not active:
                return False
            try:
                active_handle = getattr(active, "_hWnd", None)
                if active_handle is not None and hasattr(self, "GetHandle"):
                    return int(active_handle) == int(self.GetHandle())
            except Exception:
                pass
            title = (active.title or "").strip()
            if not title:
                return False
            return panel_title.lower() in title.lower()
        except Exception:
            return False

    def _bring_gui_to_foreground(self) -> None:
        try:
            if getattr(self, "_gui_hidden", False):
                self._show_gui_window()
                return

            try:
                self.Show()
            except Exception:
                pass
            try:
                style = self.GetWindowStyleFlag()
                if not (style & wx.STAY_ON_TOP):
                    self.SetWindowStyleFlag(style | wx.STAY_ON_TOP)
            except Exception:
                pass

            activated = False
            try:
                title = (self.GetTitle() or panel_title).strip()
                wins = gw.getWindowsWithTitle(title) if title else []
                if not wins and panel_title:
                    wins = gw.getWindowsWithTitle(panel_title)
                target = wins[0] if wins else None
                if target:
                    try:
                        if getattr(target, "isMinimized", False):
                            target.restore()
                            time.sleep(0.05)
                    except Exception:
                        pass
                    try:
                        target.activate()
                        activated = True
                    except Exception:
                        activated = False
            except Exception:
                activated = False

            if not activated:
                self.Raise()
                try:
                    self.SetFocus()
                except Exception:
                    pass
                try:
                    self.RequestUserAttention(wx.USER_ATTENTION_INFO)
                except Exception:
                    pass
        except Exception as exc:
            print(f"Failed to bring GUI to foreground: {exc}")

    def _get_taskbar_top_y(self) -> int:
        try:
            work_rect = wx.GetClientDisplayRect()
            if work_rect:
                return int(work_rect.y + work_rect.height)
        except Exception:
            pass
        try:
            _, screen_h = wx.GetDisplaySize()
            return int(screen_h)
        except Exception:
            return 0

    def _hide_gui_window(self):
        try:
            visible = getattr(self, "_gui_visible_sliver", GUI_HIDDEN_VISIBLE_WIDTH)
            new_x = 0
            new_y = self._get_taskbar_top_y()
            self.Move((new_x, new_y))
            self._gui_hidden = True
            print(f"GUI hidden (leaving {visible}px visible)")
        except Exception as exc:
            print(f"Failed to hide GUI window: {exc}")

    def _show_gui_window(self):
        try:
            self.Move((0, 0))
            try:
                self.Show()
            except Exception:
                pass
            try:
                style = self.GetWindowStyleFlag()
                if not (style & wx.STAY_ON_TOP):
                    self.SetWindowStyleFlag(style | wx.STAY_ON_TOP)
            except Exception:
                pass
            self.Raise()
            try:
                self.SetFocus()
            except Exception:
                pass
            self._gui_hidden = False
            print("GUI restored to top-left corner")
        except Exception as exc:
            print(f"Failed to show GUI window: {exc}")

    # Inline CDP hotkeys stay close to the auto-hide helpers for easy discovery and editing.
    CUSTOM_CDP_HOTKEYS: List[Dict[str, Any]] = [
        {
            "hotkey": "ctrl+alt+g",
            "css": [
                "button.btn:has-text('Dashboard')",  # Most reliable - finds button with "Dashboard" text
                "button.btn.border-none.bg-transparent",  # Fallback - targets button classes
            ],
            "description": "Click dashboard",
            "auto_hide": True,
        },
        {
            "hotkey": "ctrl+alt+d",
            "css": [
                "button.btn.border-none.bg-transparent.hover\\:shadow-none > div.css-1rynq56.r-cqee49",
                "button.btn.border-none.bg-transparent.hover\\:shadow-none",
            ],
            "description": "Click Dashboard button",
        },
    ]

    def _clear_custom_cdp_hotkeys(self) -> None:
        removed = 0
        for handle in self._custom_cdp_hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
                removed += 1
            except Exception:
                pass
        if removed:
            print(f"[CDP Hotkeys] Cleared {removed} bindings")
        self._custom_cdp_hotkey_handles = []
        self._custom_cdp_hotkeys_active = []
        self._custom_cdp_hotkeys_registered = False

    def _register_custom_cdp_hotkeys(self) -> None:
        self._clear_custom_cdp_hotkeys()
        active_entries: List[Dict[str, Any]] = []
        for entry in self.CUSTOM_CDP_HOTKEYS:
            hotkey = (entry.get("hotkey") or "").strip().lower()
            if not hotkey:
                continue
            css_entries: List[str] = []
            xpath_entries: List[str] = []
            # Support explicit css/xpath lists or generic selectors
            for css in entry.get("css", []) or []:
                if css:
                    css_entries.append(css)
            for xpath in entry.get("xpath", []) or []:
                if xpath:
                    xpath_entries.append(xpath)
            for raw_selector in entry.get("selectors", []) or []:
                token = (raw_selector or "").strip()
                if not token:
                    continue
                low = token.lower()
                if low.startswith("css:"):
                    css_entries.append(token[4:].strip())
                elif low.startswith("xpath:"):
                    xpath_entries.append(token[6:].strip())
                elif token.startswith("//") or token.startswith("(//"):
                    xpath_entries.append(token)
                else:
                    css_entries.append(token)
            css_entries = [s for s in css_entries if s]
            xpath_entries = [s for s in xpath_entries if s]
            if not css_entries and not xpath_entries:
                print(f"[CDP Hotkeys] Skipped '{hotkey}': no selectors configured")
                continue
            description = entry.get("description") or ""
            auto_hide = bool(entry.get("auto_hide"))
            try:
                handle = keyboard.add_hotkey(
                    hotkey,
                    lambda hk=hotkey, css=tuple(css_entries), xp=tuple(xpath_entries), desc=description, hide=auto_hide: wx.CallAfter(
                        self._execute_custom_cdp_click,
                        hk,
                        css,
                        xp,
                        desc,
                        hide,
                    )
                )
            except Exception as exc:
                print(f"[CDP Hotkeys] Failed to register '{hotkey}': {exc}")
                continue
            self._custom_cdp_hotkey_handles.append(handle)
            active_entries.append({
                "hotkey": hotkey,
                "css": css_entries,
                "xpath": xpath_entries,
                "description": description,
                "auto_hide": auto_hide,
            })
        self._custom_cdp_hotkeys_active = active_entries
        self._custom_cdp_hotkeys_registered = bool(active_entries)
        self._log_custom_cdp_hotkeys()

    def _execute_custom_cdp_click(
        self,
        hotkey: str,
        css_entries: Tuple[str, ...],
        xpath_entries: Tuple[str, ...],
        description: str,
        auto_hide: bool,
    ) -> None:
        if getattr(self, "_is_closing", False):
            return

        if description:
            print(f"[CDP Hotkeys] {hotkey} triggered — {description}")
        else:
            print(f"[CDP Hotkeys] {hotkey} triggered")

        should_restore = False
        if auto_hide and not getattr(self, "_gui_hidden", False):
            try:
                self._hide_gui_window()
                should_restore = True
            except Exception as exc:
                print(f"[CDP Hotkeys] Failed to hide GUI before '{hotkey}': {exc}")

        grabber, _ = self._ensure_browser_grabber()
        if grabber is None:
            print(f"[CDP Hotkeys] '{hotkey}' aborted: browser grabber unavailable")
            if should_restore:
                wx.CallLater(250, self._show_gui_window)
            return

        if not grabber.connect_to_chrome():
            print(f"[CDP Hotkeys] '{hotkey}' failed: unable to connect to Chrome")
            if should_restore:
                wx.CallLater(250, self._show_gui_window)
            return

        driver = getattr(grabber, 'driver', None)
        if driver is None:
            print(f"[CDP Hotkeys] '{hotkey}' failed: no active driver")
            if should_restore:
                wx.CallLater(250, self._show_gui_window)
            return

        expression = BrowserEMRGrabber._build_cdp_click_expression(list(css_entries), list(xpath_entries))
        params = {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
            'userGesture': True,
        }

        try:
            result = driver.execute_cdp_cmd('Runtime.evaluate', params)
            success = bool(((result or {}).get('result', {}) or {}).get('value'))
            if success:
                print(f"[CDP Hotkeys] '{hotkey}' succeeded")
            else:
                print(f"[CDP Hotkeys] '{hotkey}' did not match any elements")
        except Exception as exc:
            print(f"[CDP Hotkeys] '{hotkey}' error: {exc}")
        finally:
            if should_restore:
                wx.CallLater(250, self._show_gui_window)

    def _log_custom_cdp_hotkeys(self) -> None:
        if not self._custom_cdp_hotkeys_registered:
            print("[CDP Hotkeys] None active")
            return
        print(f"[CDP Hotkeys] Active count: {len(self._custom_cdp_hotkeys_active)}")
        for entry in self._custom_cdp_hotkeys_active:
            desc = entry.get("description") or ""
            label = f"{entry.get('hotkey')}"
            if desc:
                label = f"{label} — {desc}"
            print(f"  - {label}")

    def trigger_appropriate_grab(self):
        """Trigger the grab function for the currently active tab"""
        try:
            if getattr(self, "_js_overlay_active", False):
                wx.CallAfter(self._do_overlay_grab)
                return
            # Use wx.CallAfter to ensure this runs on the main thread
            wx.CallAfter(self._do_appropriate_grab)
        except Exception as e:
            print(f"Error in trigger_appropriate_grab: {e}")

    def _do_appropriate_grab(self):
        """Internal method to execute grab on main thread"""
        try:
            active_tab = self.notebook.GetSelection()
            
            if active_tab == getattr(self, '_tab_index_t_def', 0):  # T Deficiency tab
                grab_all_labs()
            elif active_tab == getattr(self, '_tab_index_hair_loss', 1):  # Hair Loss tab
                self.grab_hair()
            elif active_tab == 2:  # Photoaging tab (fixed position)
                self.grab_photoaging()
            elif active_tab == getattr(self, '_tab_index_sexual_health', 3):  # Sexual Health tab
                self.grab_sexual_health()
            elif active_tab == 4:  # Auto Clicker tab
                # No grab function for auto clicker tab
                print("F4 pressed on Auto Clicker tab - no grab function available")
            elif active_tab == getattr(self, '_tab_index_performance_anxiety', 5):
                self.grab_performance_anxiety()
            elif active_tab == getattr(self, '_tab_index_birth_control', 6):
                self.grab_birth_control()
            
            print(f"F4 triggered grab for tab {active_tab}")
        except Exception as e:
            print(f"Error executing grab function: {e}")
            wx.MessageBox(f"Error executing grab function: {e}", "Grab Error", wx.ICON_ERROR)

    # --- Tab 4 global hotkeys lifecycle ---
    def _on_tab_changed(self, evt):
        try:
            self._refresh_tab4_global_hotkeys()
        finally:
            evt.Skip()

    def _refresh_tab4_global_hotkeys(self):
        try:
            sel = self.notebook.GetSelection()
        except Exception:
            sel = None
        td_idx = getattr(self, '_tab_index_t_def', 0)
        hair_idx = getattr(self, '_tab_index_hair_loss', 1)
        sh_idx = getattr(self, '_tab_index_sexual_health', 3)
        bc_idx = getattr(self, '_tab_index_birth_control', None)
        if sel is None:
            self._unregister_tab4_hotkeys()
            return
        if sel == td_idx:
            # T Deficiency tab selected -> map Ctrl+Alt+F/N/C to T Deficiency actions
            self._register_tab1_hotkeys()
        elif sel == hair_idx:
            # Hair Loss tab selected -> map Ctrl+Alt+F/N/C to Hair Loss actions
            self._register_tab2_hotkeys()
        elif sel == sh_idx:
            # Sexual Health tab selected -> map Ctrl+Alt+F/N/C to Sexual Health actions
            self._register_tab4_hotkeys()
        elif bc_idx is not None and sel == bc_idx:
            self._register_tab7_hotkeys()
        else:
            self._unregister_tab4_hotkeys()

    def _register_tab1_hotkeys(self):
        """Register T Deficiency (Tab 1) global hotkeys. Active only when Tab 1 is selected.
        Where to change: shortcuts are Ctrl+Alt+F/N/C below via modifiers and keycodes.
        """
        if getattr(self, '_tab4_hotkeys_registered', False):
            if getattr(self, '_hotkeys_target', None) == 'td':
                return
            self._unregister_tab4_hotkeys()
        # Try OS-level global hotkeys via wx.RegisterHotKey (works when app is not active)
        try:
            # Clear old handlers for these IDs before rebinding
            try:
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_followup)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_plan)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_change)
            except Exception:
                pass
            ok1 = self.RegisterHotKey(self._hk_id_followup, wx.MOD_CONTROL | wx.MOD_ALT, ord('F'))
            ok2 = self.RegisterHotKey(self._hk_id_plan,    wx.MOD_CONTROL | wx.MOD_ALT, ord('N'))
            ok3 = self.RegisterHotKey(self._hk_id_change,  wx.MOD_CONTROL | wx.MOD_ALT, ord('C'))
            if ok1 and ok2 and ok3:
                # Map to T Deficiency actions
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_td_lab_message(), id=self._hk_id_followup)
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_td_rx_note(), id=self._hk_id_plan)
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_td_referral_note(), id=self._hk_id_change)
                self._tab4_hotkeys_method = 'wx'
                self._tab4_hotkeys_registered = True
                self._hotkeys_target = 'td'
                print("Tab 1 (T Deficiency) OS-level hotkeys registered (Ctrl+Alt+F/N/C)")
                return
        except Exception as e:
            print(f"wx.RegisterHotKey failed for Tab 1 (T Deficiency), will try keyboard fallback: {e}")

        # Fallback: keyboard module global hooks for T Deficiency
        try:
            # Where to change: update _tab1_hotkey_names above for T Deficiency keys
            h1 = keyboard.add_hotkey(self._tab1_hotkey_names[0], self._trigger_td_lab_message)
            h2 = keyboard.add_hotkey(self._tab1_hotkey_names[1], self._trigger_td_rx_note)
            h3 = keyboard.add_hotkey(self._tab1_hotkey_names[2], self._trigger_td_referral_note)
            self._tab4_keyboard_handles = [h1, h2, h3]
            self._tab4_hotkeys_method = 'keyboard'
            self._tab4_hotkeys_registered = True
            self._hotkeys_target = 'td'
            print("Tab 1 (T Deficiency) global hotkeys registered via keyboard module (Ctrl+Alt+F/N/C)")
        except Exception as e:
            print(f"Failed to register Tab 1 (T Deficiency) hotkeys (fallback): {e}")

    def _register_tab4_hotkeys(self):
        if getattr(self, '_tab4_hotkeys_registered', False):
            # If already registered for SH, nothing to do; if registered for Hair, switch
            if getattr(self, '_hotkeys_target', None) == 'sh':
                return
            self._unregister_tab4_hotkeys()
        # Where to change: shortcuts are Ctrl+Alt+F/N/C below via modifiers and keycodes
        # Try OS-level global hotkeys via wx.RegisterHotKey (works when app is not active)
        try:
            # Ensure old handlers are cleared for these IDs before rebinding
            try:
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_followup)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_plan)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_change)
            except Exception:
                pass
            ok1 = self.RegisterHotKey(self._hk_id_followup, wx.MOD_CONTROL | wx.MOD_ALT, ord('F'))
            ok2 = self.RegisterHotKey(self._hk_id_plan,    wx.MOD_CONTROL | wx.MOD_ALT, ord('N'))
            ok3 = self.RegisterHotKey(self._hk_id_change,  wx.MOD_CONTROL | wx.MOD_ALT, ord('C'))
            if ok1 and ok2 and ok3:
                # Bind handlers once for each id
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_sh_followup(), id=self._hk_id_followup)
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_sh_plan(), id=self._hk_id_plan)
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_sh_change(), id=self._hk_id_change)
                self._tab4_hotkeys_method = 'wx'
                self._tab4_hotkeys_registered = True
                self._hotkeys_target = 'sh'
                print("Tab 4 OS-level hotkeys registered (Ctrl+Alt+F/N/C)")
                return
        except Exception as e:
            print(f"wx.RegisterHotKey failed, will try keyboard fallback: {e}")

        # Fallback: keyboard module global hooks
        try:
            # Where to change: update _tab4_hotkey_names above for SH keys
            h1 = keyboard.add_hotkey(self._tab4_hotkey_names[0], self._trigger_sh_followup)
            h2 = keyboard.add_hotkey(self._tab4_hotkey_names[1], self._trigger_sh_plan)
            h3 = keyboard.add_hotkey(self._tab4_hotkey_names[2], self._trigger_sh_change)
            self._tab4_keyboard_handles = [h1, h2, h3]
            self._tab4_hotkeys_method = 'keyboard'
            self._tab4_hotkeys_registered = True
            self._hotkeys_target = 'sh'
            print("Tab 4 global hotkeys registered via keyboard module (Ctrl+Alt+F/N/C)")
        except Exception as e:
            print(f"Failed to register Tab 4 hotkeys (fallback): {e}")

    def _register_tab2_hotkeys(self):
        """Register Hair Loss (Tab 2) global hotkeys. Active only when Tab 2 is selected.
        Where to change: shortcuts are Ctrl+Alt+F/N/C below via modifiers and keycodes.
        """
        if getattr(self, '_tab4_hotkeys_registered', False):
            if getattr(self, '_hotkeys_target', None) == 'hair':
                return
            self._unregister_tab4_hotkeys()
        try:
            # Clear old handlers for these IDs before rebinding
            try:
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_followup)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_plan)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_change)
            except Exception:
                pass
            ok1 = self.RegisterHotKey(self._hk_id_followup, wx.MOD_CONTROL | wx.MOD_ALT, ord('F'))
            ok2 = self.RegisterHotKey(self._hk_id_plan,    wx.MOD_CONTROL | wx.MOD_ALT, ord('N'))
            ok3 = self.RegisterHotKey(self._hk_id_change,  wx.MOD_CONTROL | wx.MOD_ALT, ord('C'))
            if ok1 and ok2 and ok3:
                # Map to Hair Loss actions
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_hair_followup(), id=self._hk_id_followup)
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_hair_initial(), id=self._hk_id_plan)
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_hair_limited(), id=self._hk_id_change)
                self._tab4_hotkeys_method = 'wx'
                self._tab4_hotkeys_registered = True
                self._hotkeys_target = 'hair'
                print("Tab 2 OS-level hotkeys registered (Ctrl+Alt+F/N/C)")
                return
        except Exception as e:
            print(f"wx.RegisterHotKey failed for Tab 2, will try keyboard fallback: {e}")

        # Fallback: keyboard module global hooks for Hair Loss
        try:
            # Where to change: update _tab2_hotkey_names above for Hair keys
            h1 = keyboard.add_hotkey(self._tab2_hotkey_names[0], self._trigger_hair_followup)
            h2 = keyboard.add_hotkey(self._tab2_hotkey_names[1], self._trigger_hair_initial)
            h3 = keyboard.add_hotkey(self._tab2_hotkey_names[2], self._trigger_hair_limited)
            self._tab4_keyboard_handles = [h1, h2, h3]
            self._tab4_hotkeys_method = 'keyboard'
            self._tab4_hotkeys_registered = True
            self._hotkeys_target = 'hair'
            print("Tab 2 global hotkeys registered via keyboard module (Ctrl+Alt+F/N/C)")
        except Exception as e:
            print(f"Failed to register Tab 2 hotkeys (fallback): {e}")

    def _register_tab7_hotkeys(self):
        """Register Birth Control tab hotkeys (Ctrl+Alt+F/N)."""
        if getattr(self, '_tab4_hotkeys_registered', False):
            if getattr(self, '_hotkeys_target', None) == 'bc':
                return
            self._unregister_tab4_hotkeys()
        try:
            try:
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_followup)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_plan)
                self.Unbind(wx.EVT_HOTKEY, id=self._hk_id_change)
            except Exception:
                pass
            ok1 = self.RegisterHotKey(self._hk_id_followup, wx.MOD_CONTROL | wx.MOD_ALT, ord('F'))
            ok2 = self.RegisterHotKey(self._hk_id_plan,    wx.MOD_CONTROL | wx.MOD_ALT, ord('N'))
            if ok1 and ok2:
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_bc_followup(), id=self._hk_id_followup)
                self.Bind(wx.EVT_HOTKEY, lambda e: self._trigger_bc_initial(), id=self._hk_id_plan)
                self._tab4_hotkeys_method = 'wx'
                self._tab4_hotkeys_registered = True
                self._hotkeys_target = 'bc'
                print("Birth Control OS-level hotkeys registered (Ctrl+Alt+F/N)")
                return
        except Exception as e:
            print(f"wx.RegisterHotKey failed for Birth Control, will try keyboard fallback: {e}")

        try:
            handles = []
            if self._tab7_hotkey_names:
                handles.append(keyboard.add_hotkey(self._tab7_hotkey_names[0], self._trigger_bc_followup))
                if len(self._tab7_hotkey_names) > 1:
                    handles.append(keyboard.add_hotkey(self._tab7_hotkey_names[1], self._trigger_bc_initial))
            self._tab4_keyboard_handles = handles
            self._tab4_hotkeys_method = 'keyboard'
            self._tab4_hotkeys_registered = True
            self._hotkeys_target = 'bc'
            print("Birth Control hotkeys registered via keyboard module (Ctrl+Alt+F/N)")
        except Exception as e:
            print(f"Failed to register Birth Control hotkeys (fallback): {e}")

    def _unregister_tab4_hotkeys(self):
        if not getattr(self, '_tab4_hotkeys_registered', False):
            return
        try:
            if getattr(self, '_tab4_hotkeys_method', None) == 'wx':
                try:
                    self.UnregisterHotKey(self._hk_id_followup)
                except Exception:
                    pass
                try:
                    self.UnregisterHotKey(self._hk_id_plan)
                except Exception:
                    pass
                try:
                    self.UnregisterHotKey(self._hk_id_change)
                except Exception:
                    pass
            elif self._tab4_hotkeys_method == 'keyboard':
                for handle in getattr(self, '_tab4_keyboard_handles', []):
                    try:
                        keyboard.remove_hotkey(handle)
                    except Exception:
                        pass
                self._tab4_keyboard_handles = []
            self._tab4_hotkeys_registered = False
            self._tab4_hotkeys_method = None
            self._hotkeys_target = None
            print("Context hotkeys unregistered (because a different tab is selected)")
        except Exception as e:
            print(f"Failed to unregister Tab 4 hotkeys: {e}")

    # --- Global Sexual Health (Tab 4) hotkey triggers ---
    def _trigger_sh_followup(self):
        try:
            # Slight delay helps the active field stabilize before paste/insert
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, self.insert_sexual_health_note)
        except Exception as e:
            print(f"Error in _trigger_sh_followup: {e}")

    def _trigger_sh_plan(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, self.insert_sexual_health_brief_template)
        except Exception as e:
            print(f"Error in _trigger_sh_plan: {e}")

    def _trigger_sh_change(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, self.insert_sexual_health_change_template)
        except Exception as e:
            print(f"Error in _trigger_sh_change: {e}")

    # --- Global Hair Loss (Tab 2) hotkey triggers ---
    def _trigger_hair_followup(self):
        try:
            # Slight delay helps active field stabilize in Chrome before paste
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, lambda: self.insert_hair_note(followup=True))
        except Exception as e:
            print(f"Error in _trigger_hair_followup: {e}")

    def _trigger_hair_initial(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, lambda: self.insert_hair_note(initial=True))
        except Exception as e:
            print(f"Error in _trigger_hair_initial: {e}")

    def _trigger_hair_limited(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, self.insert_hair_limited_checkin_note)
        except Exception as e:
            print(f"Error in _trigger_hair_limited: {e}")

    # --- Global Birth Control (Tab 7) hotkey triggers ---
    def _trigger_bc_followup(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, self.insert_birth_control_followup_note)
        except Exception as e:
            print(f"Error in _trigger_bc_followup: {e}")

    def _trigger_bc_initial(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            wx.CallLater(delay, self.insert_birth_control_initial_note)
        except Exception as e:
            print(f"Error in _trigger_bc_initial: {e}")

    # --- Global T Deficiency (Tab 1) hotkey triggers ---
    def _trigger_td_lab_message(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            # Lab message for T Deficiency
            # Prefer the templates.txt entry named "Lab Message" if available; otherwise fallback to legacy builder
            def do_insert():
                try:
                    if 'Lab Message' in templates:
                        self.insert_specific_template('Lab Message')
                    else:
                        insert_template(lab_message=True)
                except Exception:
                    # Last-resort fallback
                    insert_template(lab_message=True)
            wx.CallLater(delay, do_insert)
        except Exception as e:
            print(f"Error in _trigger_td_lab_message: {e}")

    def _trigger_td_rx_note(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            # Rx note for T Deficiency
            # Prefer a template named some variant of "Rx Note" if available; otherwise fallback to legacy builder
            def do_insert():
                try:
                    # Build a case-insensitive lookup over template names
                    candidates = ['Rx Note', 'Rx note', 'RxNote', 'rx note', 'rxnote']
                    match_key = None
                    try:
                        for k in templates.keys():
                            if any(k.lower() == c.lower() for c in candidates):
                                match_key = k
                                break
                    except Exception:
                        match_key = None

                    if match_key:
                        self.insert_specific_template(match_key)
                    else:
                        insert_template(rx_note=True)
                except Exception:
                    # Last-resort fallback
                    insert_template(rx_note=True)
            wx.CallLater(delay, do_insert)
        except Exception as e:
            print(f"Error in _trigger_td_rx_note: {e}")

    def _trigger_td_referral_note(self):
        try:
            delay = getattr(self, "global_insert_delay_ms", 200)
            # Referral note template (must exist in templates.txt as "Referral note")
            wx.CallLater(delay, lambda: self.insert_specific_template("Referral note"))
        except Exception as e:
            print(f"Error in _trigger_td_referral_note: {e}")

    def _queue_overlay_cmd(self, cmd, data=None):
        try:
            self._js_overlay_cmd_queue.put_nowait((cmd, data))
        except Exception:
            pass

    def inject_js_overlay(self, evt=None, show_error_on_fail=True):
        if self._js_overlay_active:
            return
        if sync_playwright is None:
            if show_error_on_fail:
                wx.MessageBox(
                    "Playwright is not available, so the JS overlay cannot start.",
                    "JS Overlay Error",
                    wx.ICON_ERROR,
                )
            return

        self._js_overlay_active = True
        self._js_overlay_show_error_on_fail = bool(show_error_on_fail)
        self._orig_wx_Show = self.Show
        self.Show = lambda show=True: None if self._js_overlay_active else self._orig_wx_Show(show)

        global _emr_bridge_hook
        _emr_bridge_hook = lambda payload=None: self._queue_overlay_cmd("push_vars", payload or {})

        while not self._js_overlay_cmd_queue.empty():
            try:
                self._js_overlay_cmd_queue.get_nowait()
            except queue.Empty:
                break

        self._js_overlay_thread = threading.Thread(
            target=self._overlay_thread_run,
            daemon=True,
            name="JSOverlay",
        )
        self._js_overlay_thread.start()

    def _overlay_thread_run(self):
        pw = None
        browser = None
        page = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_DEBUG_PORT}")

            for ctx in browser.contexts:
                for candidate in ctx.pages:
                    url_lower = (candidate.url or "").lower()
                    if "emr" in url_lower or "hims" in url_lower:
                        page = candidate
                        break
                if page is not None:
                    break
            if page is None:
                for ctx in browser.contexts:
                    if ctx.pages:
                        page = ctx.pages[0]
                        break
            if page is None:
                wx.CallAfter(self._on_overlay_start_failed, "No browser page found")
                return

            self._js_overlay_page = page
            self._register_overlay_exposed_functions(page)
            page.evaluate(OVERLAY_JS)
            self._overlay_eval_push_visit(page, self._get_overlay_visit_display())
            self._overlay_eval_push_vars(page)
            self._overlay_eval_push_autoclicker_state(page)
            self._overlay_eval_push_invisit_autoclicker_state(page)
            self._overlay_eval_push_dark_mode(page)
            wx.CallAfter(self.Hide)

            while self._js_overlay_active:
                try:
                    page.wait_for_timeout(120)
                except Exception:
                    break

                while True:
                    try:
                        cmd, data = self._js_overlay_cmd_queue.get_nowait()
                    except queue.Empty:
                        break

                    if cmd == "stop":
                        return
                    if cmd == "push_vars":
                        self._overlay_eval_push_vars(page, data)
                    elif cmd == "push_visit":
                        self._overlay_eval_push_visit(page, data)
                    elif cmd == "push_status":
                        self._overlay_eval_push_status(page, data)
                    elif cmd == "push_autoclicker_state":
                        self._overlay_eval_push_autoclicker_state(page, data)
                    elif cmd == "push_invisit_autoclicker_state":
                        self._overlay_eval_push_invisit_autoclicker_state(page, data)
                    elif cmd == "push_dark_mode":
                        self._overlay_eval_push_dark_mode(page, data)
                    elif cmd == "reinject":
                        self._overlay_eval_reinject(page)
                    elif cmd == "toggle_minimize":
                        try:
                            page.evaluate("window.__emrToggleMinimize()")
                        except Exception:
                            pass
        except Exception as exc:
            wx.CallAfter(self._on_overlay_start_failed, str(exc))
        finally:
            self._js_overlay_page = None
            try:
                if page is not None:
                    page.evaluate(OVERLAY_REMOVE_JS)
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                pass

    def _on_overlay_start_failed(self, msg):
        self._js_overlay_active = False
        global _emr_bridge_hook
        _emr_bridge_hook = None
        if hasattr(self, "_orig_wx_Show"):
            self.Show = self._orig_wx_Show
        if self._js_overlay_show_error_on_fail:
            wx.MessageBox(
                f"Could not start JS overlay:\n{msg}\n\nMake sure Chrome is running with --remote-debugging-port={CDP_DEBUG_PORT}.",
                "JS Overlay Error",
                wx.ICON_ERROR,
            )

    def _register_overlay_exposed_functions(self, page):
        def on_grab():
            wx.CallAfter(self._do_overlay_grab)

        def on_detect_visit():
            wx.CallAfter(self._do_overlay_detect_visit)

        def on_insert_template(template_name, insert_source="overlay"):
            wx.CallAfter(self.insert_specific_template, template_name, insert_source)

        def on_switch_to_python():
            wx.CallAfter(self._remove_js_overlay)

        def on_close():
            wx.CallAfter(self.Close)

        def on_toggle_autoclicker():
            wx.CallAfter(self._handle_overlay_toggle_autoclicker)

        def on_toggle_invisit_autoclicker():
            wx.CallAfter(self._handle_overlay_toggle_invisit_clicker)

        def on_toggle_dark_mode():
            next_state = not bool(getattr(self, "_js_overlay_dark_mode", False))
            self._js_overlay_dark_mode = next_state
            self._queue_overlay_cmd("push_dark_mode", next_state)

        funcs = {
            "__emr_grab": on_grab,
            "__emr_detect_visit": on_detect_visit,
            "__emr_insert_template": on_insert_template,
            "__emr_toggle_autoclicker": on_toggle_autoclicker,
            "__emr_toggle_invisit_autoclicker": on_toggle_invisit_autoclicker,
            "__emr_toggle_dark_mode": on_toggle_dark_mode,
            "__emr_switch_to_python": on_switch_to_python,
            "__emr_close": on_close,
        }
        for name, fn in funcs.items():
            try:
                page.expose_function(name, fn)
            except Exception as exc:
                if "already registered" not in str(exc).lower():
                    print(f"[JS OVERLAY] expose_function({name}) error: {exc}")

    def _remove_js_overlay(self):
        self._js_overlay_active = False
        global _emr_bridge_hook
        _emr_bridge_hook = None
        self._queue_overlay_cmd("stop")
        if hasattr(self, "_orig_wx_Show"):
            self.Show = self._orig_wx_Show
        self.Show()
        self.Raise()

    def _get_overlay_visit_display(self) -> str:
        candidates = [
            getattr(self, "_js_overlay_visit_type", None),
            getattr(self, "_cdp_last_display", None),
        ]
        try:
            label_text = str(self.visit_type_text.GetLabel() or "").strip()
            if ":" in label_text:
                candidates.append(label_text.split(":", 1)[1].strip())
        except Exception:
            pass
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return "Unknown"

    def _overlay_eval_push_vars(self, page, payload=None):
        try:
            vars_dict = dict(grabbed_vars)
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key in {"timestamp", "grabbed_vars", "context"}:
                        continue
                    if isinstance(value, (dict, list)):
                        continue
                    vars_dict[str(key)] = "" if value is None else str(value)
            page.evaluate("(payload) => window.__emrUpdateVariables(payload)", vars_dict)
        except Exception as exc:
            print(f"[JS OVERLAY] Push vars error: {exc}")

    def _overlay_eval_push_visit(self, page, visit_type):
        self._js_overlay_visit_type = visit_type
        try:
            templates_for_visit = load_tab_template_list(visit_type)
            page.evaluate(
                "([visitType, templates]) => window.__emrUpdateVisitType(visitType, templates)",
                [str(visit_type or "Unknown"), templates_for_visit],
            )
        except Exception as exc:
            print(f"[JS OVERLAY] Push visit error: {exc}")

    def _overlay_eval_push_status(self, page, msg):
        try:
            page.evaluate("(message) => window.__emrSetStatus(message)", str(msg or ""))
        except Exception:
            pass

    def _overlay_eval_push_autoclicker_state(self, page, enabled=None):
        try:
            state = bool(auto_clicker_enabled[0] if enabled is None else enabled)
            page.evaluate("(enabled) => window.__emrSetAutoclickerState(enabled)", state)
        except Exception:
            pass

    def _overlay_eval_push_invisit_autoclicker_state(self, page, enabled=None):
        try:
            state = bool(self.invisit_running if enabled is None else enabled)
            page.evaluate("(enabled) => window.__emrSetInvisitAutoclickerState(enabled)", state)
        except Exception:
            pass

    def _overlay_eval_push_dark_mode(self, page, enabled=None):
        try:
            state = bool(self._js_overlay_dark_mode if enabled is None else enabled)
            self._js_overlay_dark_mode = state
            page.evaluate("(enabled) => window.__emrSetDarkMode(enabled)", state)
        except Exception:
            pass

    def _overlay_eval_reinject(self, page):
        try:
            still_there = page.evaluate("!!document.getElementById('emr-assist-overlay')")
            if still_there:
                return
            self._register_overlay_exposed_functions(page)
            page.evaluate(OVERLAY_JS)
            self._overlay_eval_push_visit(page, self._get_overlay_visit_display())
            self._overlay_eval_push_vars(page)
            self._overlay_eval_push_autoclicker_state(page)
            self._overlay_eval_push_invisit_autoclicker_state(page)
            self._overlay_eval_push_dark_mode(page)
        except Exception as exc:
            print(f"[JS OVERLAY] Reinject error: {exc}")

    def _do_overlay_grab(self):
        visit_type = self._canonicalize_visit_type(self._get_overlay_visit_display())
        try:
            if visit_type == "T Deficiency":
                grab_all_labs()
            elif visit_type == "Hair Loss":
                self.grab_hair()
            elif visit_type == "Photoaging":
                self.grab_photoaging()
            elif visit_type == "Sexual Health":
                self.grab_sexual_health()
            elif visit_type == "Performance Anxiety":
                self.grab_performance_anxiety()
            elif visit_type == "Birth Control":
                self.grab_birth_control()
            else:
                self._push_overlay_status(f"No grab handler for {visit_type or 'Unknown'}")
                return
            self._push_overlay_status(f"Grab completed for {visit_type}")
        except Exception as exc:
            self._push_overlay_status(f"Grab error: {exc}")

    def _do_overlay_detect_visit(self):
        try:
            self.detect_visit_type_and_switch_tab()
            visit_type = self._get_overlay_visit_display()
            self._queue_overlay_cmd("push_visit", visit_type)
            self._queue_overlay_cmd("push_status", f"Detected visit: {visit_type}")
        except Exception as exc:
            self._push_overlay_status(f"Detect error: {exc}")

    def _push_overlay_autoclicker_state(self):
        if getattr(self, "_js_overlay_active", False):
            self._queue_overlay_cmd("push_autoclicker_state", bool(auto_clicker_enabled[0]))

    def _push_overlay_invisit_autoclicker_state(self):
        if getattr(self, "_js_overlay_active", False):
            self._queue_overlay_cmd("push_invisit_autoclicker_state", bool(self.invisit_running))

    def _push_overlay_status(self, message):
        if getattr(self, "_js_overlay_active", False):
            self._queue_overlay_cmd("push_status", str(message or ""))

    def _handle_overlay_toggle_autoclicker(self):
        try:
            self.toggle_auto_clicker()
            self._push_overlay_autoclicker_state()
        except Exception as exc:
            self._push_overlay_status(f"Autoclicker error: {exc}")

    def _handle_overlay_toggle_invisit_clicker(self):
        try:
            self.toggle_invisit_clicker()
            self._push_overlay_invisit_autoclicker_state()
        except Exception as exc:
            self._push_overlay_status(f"In-visit error: {exc}")

    def on_close(self, event):
        """Handle application close event"""
        try:
            # Mark app as closing to short-circuit any late callbacks
            setattr(self, "_is_closing", True)

            # Stop auto clicker loops ASAP
            try:
                auto_clicker_enabled[0] = False
            except Exception:
                pass

            # Cleanup global hotkeys
            self._clear_custom_cdp_hotkeys()
            if getattr(self, "_gui_toggle_hotkey_method", None) == 'wx' and self._gui_toggle_hotkey_id is not None:
                try:
                    self.Unbind(wx.EVT_HOTKEY, id=self._gui_toggle_hotkey_id)
                except Exception:
                    pass
                try:
                    self.UnregisterHotKey(self._gui_toggle_hotkey_id)
                except Exception:
                    pass
            elif getattr(self, "_gui_toggle_hotkey_handle", None) is not None:
                try:
                    keyboard.remove_hotkey(self._gui_toggle_hotkey_handle)
                except Exception:
                    pass
            self._gui_toggle_hotkey_handle = None
            self._gui_toggle_hotkey_method = None
            keyboard.unhook_all_hotkeys()
            print("Global hotkeys cleaned up")
        except Exception as e:
            print(f"Error cleaning up hotkeys: {e}")
        # Also unregister OS-level hotkeys if any are active
        try:
            self._unregister_tab4_hotkeys()
        except Exception:
            pass
        # Stop CDP monitor
        try:
            self._cdp_monitor_running = False
        except Exception:
            pass
        try:
            self._js_overlay_active = False
            global _emr_bridge_hook
            _emr_bridge_hook = None
            self._queue_overlay_cmd("stop")
            thread = getattr(self, "_js_overlay_thread", None)
            if thread and thread.is_alive():
                thread.join(timeout=1.5)
        except Exception:
            pass
        # Wait briefly for CDP monitor thread to exit before tearing down Playwright
        try:
            if getattr(self, "_cdp_monitor_thread", None) and self._cdp_monitor_thread.is_alive():
                self._cdp_monitor_thread.join(timeout=1.0)
        except Exception:
            pass
        # Do NOT call quit() on the Chrome debugging driver here; it can block on close.
        # Just drop the handle; Chrome remains open for the user.
        try:
            self._cdp_driver = None
            self._cdp_driver_thread_id = None
        except Exception:
            pass

        # Dismiss any modeless popup to prevent stray events
        try:
            if getattr(self, "new_task_popup", None):
                try:
                    self.new_task_popup.Destroy()
                except Exception:
                    pass
                self.new_task_popup = None
        except Exception:
            pass

        # Best-effort: disconnect cached browser grabber without blocking the UI
        try:
            grabber = getattr(self, "_browser_grabber_cache", None)
            if grabber:
                def _cleanup_grabber():
                    try:
                        grabber.disconnect()
                    finally:
                        try:
                            grabber.shutdown_playwright()
                        except Exception:
                            pass

                threading.Thread(target=_cleanup_grabber, daemon=True).start()
        except Exception:
            pass
        
        # Continue with normal close
        try:
            self.Destroy()
        except Exception:
            # As a fallback, request main loop exit
            try:
                wx.GetApp().ExitMainLoop()
            except Exception:
                pass

app = wx.App(False)
frame = MyFrame()
frame.Show()
if AUTO_START_JS_OVERLAY:
    wx.CallAfter(frame.inject_js_overlay, None, False)
threading.Thread(target=window_tracker, daemon=True).start()

# ================================================================
# Ctrl+Right-click Mouse Listener for Template Popup
# ================================================================

def start_template_popup_listener():
    """Start a mouse listener for Ctrl+Right-click template popup using mouse library."""
    
    if mouse_lib is None:
        print("[STARTUP] mouse library not available, using keyboard shortcut only...")
        return
    
    print("[STARTUP] Setting up mouse library right-click hook...")
    
    def on_right_click():
        """Handle right-click - show template popup if Ctrl is held."""
        try:
            # Check if Ctrl is held using ctypes (Windows)
            VK_CONTROL = 0x11
            ctrl_state = ctypes.windll.user32.GetAsyncKeyState(VK_CONTROL)
            ctrl_pressed = ctrl_state & 0x8000
            
            if not ctrl_pressed:
                return  # Ctrl not held, ignore
            
            # Get current mouse position
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            
            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            x, y = pt.x, pt.y
            
            print(f"[MOUSE] Ctrl+Right-click detected at ({x}, {y})")
            
            # Show template popup via wx.CallAfter (thread-safe)
            def show_popup():
                try:
                    if not frame or not frame.IsShown():
                        return
                    
                    # Get current tab name
                    tab_name = getattr(frame, '_current_template_tab', 'T Deficiency')
                    
                    # Skip if Auto Clicker tab (no templates)
                    if tab_name is None:
                        return
                    
                    # Create and show popup
                    popup = TemplatePopup(frame, tab_name, frame.insert_specific_template)
                    popup.position_on_screen(x, y)
                    popup.Popup()
                    print(f"[MOUSE] Popup shown for tab: {tab_name}")
                    
                except Exception as e:
                    print(f"Template popup error: {e}")
                    import traceback
                    traceback.print_exc()
            
            wx.CallAfter(show_popup)
            
        except Exception as e:
            print(f"[MOUSE] Error in right-click handler: {e}")
    
    # Hook right-click using mouse library
    try:
        mouse_lib.on_right_click(on_right_click)
        print("[STARTUP] Mouse right-click hook registered successfully")
        print("Template popup listener started (Ctrl+Right-click)")
    except Exception as e:
        print(f"Failed to start template popup listener: {e}")
        import traceback
        traceback.print_exc()


def start_keyboard_template_popup():
    """Alternative: Use Ctrl+Alt+T keyboard shortcut to show template popup at mouse position."""
    print("[STARTUP] Setting up keyboard shortcut Ctrl+Alt+T for template popup...")
    
    def show_template_popup_at_mouse():
        """Show template popup at current mouse position."""
        try:
            # Get mouse position using ctypes
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            
            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            x, y = pt.x, pt.y
            
            print(f"[KEYBOARD] Ctrl+Alt+T pressed, showing popup at ({x}, {y})")
            
            def show_popup():
                try:
                    if not frame or not frame.IsShown():
                        return
                    
                    tab_name = getattr(frame, '_current_template_tab', 'T Deficiency')
                    if tab_name is None:
                        return
                    
                    popup = TemplatePopup(frame, tab_name, frame.insert_specific_template)
                    popup.position_on_screen(x, y)
                    popup.Popup()
                    print("[KEYBOARD] Popup shown")
                    
                except Exception as e:
                    print(f"Template popup error: {e}")
                    import traceback
                    traceback.print_exc()
            
            wx.CallAfter(show_popup)
            
        except Exception as e:
            print(f"[KEYBOARD] Error getting mouse position: {e}")
    
    try:
        keyboard.add_hotkey('ctrl+alt+t', show_template_popup_at_mouse, suppress=False)
        print("Template popup keyboard shortcut registered: Ctrl+Alt+T")
    except Exception as e:
        print(f"Failed to register template popup hotkey: {e}")


# Start only keyboard shortcut listener for template popup
print("[STARTUP] Ctrl+Right-click popup listener is disabled by configuration")
print("[STARTUP] About to start keyboard template popup listener...")
try:
    start_keyboard_template_popup()
    print("[STARTUP] Keyboard template popup listener function completed")
except Exception as e:
    print(f"[STARTUP] Error starting keyboard template popup listener: {e}")
    import traceback
    traceback.print_exc()

app.MainLoop()
