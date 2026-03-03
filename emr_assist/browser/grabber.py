"""Playwright-based EMR data extraction (zero wx dependencies)."""

from __future__ import annotations

import json
import re
import time
import threading
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

# Playwright
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Frame, ElementHandle
except Exception:
    sync_playwright = None

from .exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    By,
)
from .adapters import PlaywrightDriverAdapter, WebDriverWait, EC

# grab_points utilities
import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
from grab_points import get_selector, get_selector_list

from ..core.config import (
    CDP_DEBUG_PORT,
    EFFECTIVENESS_QUESTION_ALIASES,
    FAST_MODE_BP,
    USE_CDP_FOR_TEXT,
    USE_PLAYWRIGHT_FOR_SH,
    USE_PLAYWRIGHT_FOR_MED,
    INTAKE_FORM_DATE_SELECTOR,
    VISIT_TYPE_FALLBACK_KEYWORDS,
    EMR_DASHBOARD_BASE_URLS,
    EMR_DASHBOARD_WELCOME_SELECTORS,
    PA_SITUATION_OPTIONS,
    PA_SYMPTOM_OPTIONS,
    PA_SYMPTOM_KEYWORDS,
    BIRTH_CONTROL_PMH_KEYWORDS,
    GEOCODER_USER_AGENT,
    dprint,
)
from ..core.geocoding import geocode_address, get_chippewa_falls_coords, Nominatim, geodesic


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
            
            # Get all text content as fallback for parsing
            full_text = self._get_page_text()
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



# Legacy alias
SeleniumEMRGrabber = BrowserEMRGrabber