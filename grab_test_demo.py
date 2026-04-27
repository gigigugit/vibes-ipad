"""
Grab Test Demo — Compare Playwright text-grab methods vs clipboard copy.

Connects to Chrome via CDP on port 9222. Make sure Chrome is launched with:
  chrome.exe --remote-debugging-port=9222

Each grab method writes a labelled section to grab_tests.txt so results
can be compared side-by-side.

Requires: PyQt6, playwright, pyperclip, pyautogui
"""

from __future__ import annotations

import os
import sys
import time
import threading
from datetime import datetime
from typing import Optional

import pyperclip
import pyautogui

from emr_assist.browser.grabber import BrowserEMRGrabber
from emr_assist.core.parsers import extract_hair_medication_from_text

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QFrame, QCheckBox, QMessageBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CDP_PORT = 9222
EMR_URL_PREFIXES = ["https://emr.forhims.com", "http://emr.forhims.com"]
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grab_tests.txt")
APP_PLAYWRIGHT_TEXT_GRAB_METHOD = "inner_text_body"

# ---------------------------------------------------------------------------
# Palette (from dark-ui-theme spec)
# ---------------------------------------------------------------------------
PALETTE = {
    "window_bg":    "#0f1724",
    "panel_bg":     "#0f2233",
    "input_bg":     "#071427",
    "panel_border": "#233447",
    "input_border": "#213244",
    "text_primary": "#cbd7e6",
    "text_heading": "#93c0ff",
    "text_input":   "#ddeefb",
    "text_output":  "#bcd3ea",
    "text_label":   "#c7d8ea",
    "accent_start": "#3bd3ff",
    "accent_end":   "#12aee6",
    "accent_text":  "#002233",
    "btn_bg":       "#11293a",
    "btn_text":     "#cfe7ff",
    "btn_border":   "#243948",
    "btn_pressed":  "#0d1f2b",
    "disabled_text":"#7f93a4",
    "status_ok":    "#2ecc71",
    "status_err":   "#ff5555",
}

def build_qss() -> str:
    p = PALETTE
    return f"""
    QWidget {{
        background: {p['window_bg']};
        color: {p['text_primary']};
        font-family: Segoe UI, Arial, sans-serif;
    }}
    QFrame[objectName^="panel_"] {{
        background: {p['panel_bg']};
        border: 1px solid {p['panel_border']};
        border-radius: 8px;
        padding: 8px;
    }}
    QLabel#panelTitle {{
        color: {p['text_heading']};
        font-weight: 600;
    }}
    QPushButton {{
        background: {p['btn_bg']};
        color: {p['btn_text']};
        border: 1px solid {p['btn_border']};
        padding: 6px 10px;
        border-radius: 6px;
    }}
    QPushButton:pressed {{ background: {p['btn_pressed']}; }}
    QPushButton:disabled {{ color: {p['disabled_text']}; }}
    QPushButton#ctaButton {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {p['accent_start']}, stop:1 {p['accent_end']});
        color: {p['accent_text']};
        font-weight: 700;
        border-radius: 6px;
        padding: 8px 14px;
    }}
    QTextEdit#outputBox {{
        background: {p['input_bg']};
        border: 1px solid {p['input_border']};
        color: {p['text_output']};
        border-radius: 6px;
        padding: 6px 8px;
        font-family: Consolas, monospace;
        font-size: 12px;
    }}
    QCheckBox {{
        color: {p['text_heading']};
        font-weight: 600;
        spacing: 6px;
        padding: 2px 4px;
    }}
    QCheckBox::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {p['btn_border']};
        border-radius: 3px;
        background: {p['input_bg']};
    }}
    QCheckBox::indicator:checked {{
        background: {p['accent_start']};
        border-color: {p['accent_start']};
    }}
    """


# ---------------------------------------------------------------------------
# Signal bridge (thread → UI)
# ---------------------------------------------------------------------------
class _Signals(QObject):
    log = pyqtSignal(str)
    status = pyqtSignal(str, str)  # message, color


# ---------------------------------------------------------------------------
# Playwright helpers (each runs in a worker thread)
# ---------------------------------------------------------------------------
def _find_emr_page(browser):
    """Return the first page whose URL matches the EMR prefix, or None."""
    for ctx in browser.contexts:
        for page in ctx.pages:
            url = page.url or ""
            if any(url.startswith(pfx) for pfx in EMR_URL_PREFIXES):
                return page
    return None


def _pw_connect_and_find():
    """Start Playwright, connect CDP, find EMR page. Returns (pw, browser, page) or raises."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    page = _find_emr_page(browser)
    if page is None:
        browser.close()
        pw.stop()
        raise RuntimeError("No EMR page found in Chrome tabs")
    return pw, browser, page


def grab_pw_inner_text(signals: _Signals) -> tuple[str, str]:
    """page.inner_text('body') — visible rendered text only."""
    label = "playwright- page.inner_text('body')"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    pw, browser, page = _pw_connect_and_find()
    try:
        t0 = time.perf_counter()
        text = page.inner_text("body", timeout=8000)
        elapsed = time.perf_counter() - t0
        signals.log.emit(f"[{label}] Got {len(text)} chars in {elapsed:.2f}s")
        return label, text
    finally:
        browser.close()
        pw.stop()


def grab_pw_text_content(signals: _Signals) -> tuple[str, str]:
    """page.text_content('body') — all text including hidden elements."""
    label = "playwright- page.text_content('body')"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    pw, browser, page = _pw_connect_and_find()
    try:
        t0 = time.perf_counter()
        text = page.text_content("body", timeout=8000)
        elapsed = time.perf_counter() - t0
        signals.log.emit(f"[{label}] Got {len(text or '')} chars in {elapsed:.2f}s")
        return label, text or ""
    finally:
        browser.close()
        pw.stop()


def grab_pw_evaluate_innerText(signals: _Signals) -> tuple[str, str]:
    """page.evaluate('document.body.innerText') — JS innerText (visible)."""
    label = "playwright- evaluate('document.body.innerText')"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    pw, browser, page = _pw_connect_and_find()
    try:
        t0 = time.perf_counter()
        text = page.evaluate("() => document.body.innerText")
        elapsed = time.perf_counter() - t0
        signals.log.emit(f"[{label}] Got {len(text)} chars in {elapsed:.2f}s")
        return label, text
    finally:
        browser.close()
        pw.stop()


def grab_pw_evaluate_textContent(signals: _Signals) -> tuple[str, str]:
    """page.evaluate('document.body.textContent') — JS textContent (all)."""
    label = "playwright- evaluate('document.body.textContent')"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    pw, browser, page = _pw_connect_and_find()
    try:
        t0 = time.perf_counter()
        text = page.evaluate("() => document.body.textContent")
        elapsed = time.perf_counter() - t0
        signals.log.emit(f"[{label}] Got {len(text)} chars in {elapsed:.2f}s")
        return label, text
    finally:
        browser.close()
        pw.stop()


def grab_pw_inner_text_plus_notes(signals: _Signals) -> tuple[str, str]:
    """page.inner_text('body') + hidden notes panel via targeted selector."""
    label = "playwright- inner_text('body') + notes panel"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    pw, browser, page = _pw_connect_and_find()
    try:
        t0 = time.perf_counter()
        body_text = page.inner_text("body", timeout=8000)

        # Grab notes panel elements (may be hidden in DOM)
        notes_js = """() => {
            const els = document.querySelectorAll('[data-testid^="note-content-"]');
            if (!els.length) return '';
            return Array.from(els).map((el, i) => {
                const parent = el.closest('[data-testid^="note-item-"]');
                let header = '';
                if (parent) {
                    const nameEl = parent.querySelector('div[dir="auto"]');
                    const dateEl = parent.querySelectorAll('div[dir="auto"]')[1];
                    if (nameEl) header += nameEl.textContent.trim();
                    if (dateEl) header += ' ' + dateEl.textContent.trim();
                }
                return (header ? header + '\\n' : '') + el.textContent.trim();
            }).join('\\n\\n---\\n\\n');
        }"""
        notes_text = page.evaluate(notes_js)

        elapsed = time.perf_counter() - t0
        combined = body_text
        if notes_text:
            combined += "\n\n=== Previous Notes (from DOM) ===\n\n" + notes_text
            signals.log.emit(f"[{label}] Got {len(body_text)} body + {len(notes_text)} notes chars in {elapsed:.2f}s")
        else:
            signals.log.emit(f"[{label}] Got {len(body_text)} body chars, no notes found, in {elapsed:.2f}s")
        return label, combined
    finally:
        browser.close()
        pw.stop()


def grab_pw_all_frames(signals: _Signals) -> tuple[str, str]:
    """inner_text from main frame + all child frames."""
    label = "playwright- all frames inner_text"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    pw, browser, page = _pw_connect_and_find()
    try:
        t0 = time.perf_counter()
        parts = []
        for i, frame in enumerate(page.frames):
            try:
                ft = frame.inner_text("body", timeout=3000)
                if ft and ft.strip():
                    frame_label = frame.url or f"frame-{i}"
                    parts.append(f"--- Frame: {frame_label} ---\n{ft}")
            except Exception:
                pass
        text = "\n\n".join(parts)
        elapsed = time.perf_counter() - t0
        signals.log.emit(f"[{label}] Got {len(text)} chars from {len(parts)} frames in {elapsed:.2f}s")
        return label, text
    finally:
        browser.close()
        pw.stop()


def grab_clipboard(signals: _Signals) -> tuple[str, str]:
    """Classic Ctrl+A → Ctrl+C → pyperclip.paste() clipboard grab."""
    label = "clipboard copy"
    signals.log.emit(f"[{label}] Saving clipboard, performing Ctrl+A/Ctrl+C …")

    original = ""
    try:
        original = pyperclip.paste()
    except Exception:
        pass

    try:
        t0 = time.perf_counter()

        # Click center of screen to ensure EMR page has focus
        cx, cy = pyautogui.size()
        pyautogui.click(cx // 2, cy // 2)
        time.sleep(0.25)

        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.4)

        text = pyperclip.paste()

        # Clear selection
        pyautogui.press("right")
        time.sleep(0.05)
        pyautogui.press("left")

        elapsed = time.perf_counter() - t0
        signals.log.emit(f"[{label}] Got {len(text)} chars in {elapsed:.2f}s")
        return label, text
    finally:
        # Restore original clipboard
        try:
            pyperclip.copy(original)
        except Exception:
            pass


def _app_like_playwright_text(signals: _Signals, method: str = APP_PLAYWRIGHT_TEXT_GRAB_METHOD) -> tuple[str, str]:
    """Mirror the app's _grab_text_with_playwright implementation."""
    selected_method = (method or APP_PLAYWRIGHT_TEXT_GRAB_METHOD).strip().lower()
    label = f"app-like playwright:{selected_method}"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    pw, browser, page = _pw_connect_and_find()
    try:
        t0 = time.perf_counter()
        if selected_method == "inner_text_body":
            text = page.inner_text("body", timeout=4000)
        elif selected_method == "evaluate_inner_text":
            text = page.evaluate("() => document.body.innerText")
        elif selected_method == "text_content_body":
            text = page.text_content("body", timeout=4000) or ""
        elif selected_method == "evaluate_text_content":
            text = page.evaluate("() => document.body.textContent || ''")
        elif selected_method == "inner_text_with_notes":
            body_text = page.inner_text("body", timeout=4000)
            notes_text = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid^="note-content-"]'))
                    .map((el) => (el.textContent || '').trim())
                    .filter(Boolean)
                    .join('\\n\\n---\\n\\n')"""
            )
            text = body_text if not notes_text else f"{body_text}\n\n=== Previous Notes ===\n\n{notes_text}"
        else:
            raise ValueError(f"Unsupported app-like method: {selected_method}")
        cleaned = (text or "").strip()
        elapsed = time.perf_counter() - t0
        signals.log.emit(f"[{label}] Got {len(cleaned)} chars in {elapsed:.2f}s")
        return label, cleaned
    finally:
        browser.close()
        pw.stop()


def grab_app_like_hair_text_and_parse(signals: _Signals) -> tuple[str, str]:
    """Show the raw Playwright text and parsed hair medication using the app's parser."""
    label, text = _app_like_playwright_text(signals, APP_PLAYWRIGHT_TEXT_GRAB_METHOD)
    med = extract_hair_medication_from_text(text)
    summary = (
        f"Configured method: {APP_PLAYWRIGHT_TEXT_GRAB_METHOD}\n"
        f"Parsed hair_medication: {med or '<empty>'}\n"
        f"{'=' * 72}\n"
        f"{text}"
    )
    return f"{label} + hair parser", summary


def grab_browser_grabber_page_text(signals: _Signals) -> tuple[str, str]:
    """Use the same BrowserEMRGrabber helper the app uses elsewhere."""
    label = "BrowserEMRGrabber._get_page_text()"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    grabber = BrowserEMRGrabber()
    try:
        t0 = time.perf_counter()
        if not grabber.connect_to_chrome():
            raise RuntimeError("connect_to_chrome() failed")
        text = grabber._get_page_text() or ""
        elapsed = time.perf_counter() - t0
        med = extract_hair_medication_from_text(text)
        signals.log.emit(f"[{label}] Got {len(text)} chars in {elapsed:.2f}s")
        payload = (
            f"Parsed hair_medication: {med or '<empty>'}\n"
            f"{'=' * 72}\n"
            f"{text}"
        )
        return label, payload
    finally:
        try:
            grabber.shutdown()
        except Exception:
            pass


def grab_browser_grabber_all_frames(signals: _Signals) -> tuple[str, str]:
    """Use the frame-aware BrowserEMRGrabber text collection path."""
    label = "BrowserEMRGrabber._get_all_text_across_frames()"
    signals.log.emit(f"[{label}] Connecting to Chrome …")
    grabber = BrowserEMRGrabber()
    try:
        t0 = time.perf_counter()
        if not grabber.connect_to_chrome():
            raise RuntimeError("connect_to_chrome() failed")
        text = grabber._get_all_text_across_frames() or ""
        elapsed = time.perf_counter() - t0
        med = extract_hair_medication_from_text(text)
        signals.log.emit(f"[{label}] Got {len(text)} chars in {elapsed:.2f}s")
        payload = (
            f"Parsed hair_medication: {med or '<empty>'}\n"
            f"{'=' * 72}\n"
            f"{text}"
        )
        return label, payload
    finally:
        try:
            grabber.shutdown()
        except Exception:
            pass


# Registry of all grab methods
GRAB_METHODS = [
    ("app-like playwright text + hair parser", grab_app_like_hair_text_and_parse),
    ("BrowserEMRGrabber page text + hair parser", grab_browser_grabber_page_text),
    ("BrowserEMRGrabber all frames + hair parser", grab_browser_grabber_all_frames),
    ("page.inner_text('body')",            grab_pw_inner_text),
    ("page.text_content('body')",          grab_pw_text_content),
    ("evaluate innerText",                 grab_pw_evaluate_innerText),
    ("evaluate textContent",               grab_pw_evaluate_textContent),
    ("inner_text + notes panel",           grab_pw_inner_text_plus_notes),
    ("all frames inner_text",              grab_pw_all_frames),
    ("clipboard Ctrl+A/Ctrl+C",            grab_clipboard),
]


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class GrabTestWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Grab Test Demo — Playwright vs Clipboard")
        self.setMinimumSize(720, 560)
        self.setObjectName("mainWindow")
        self.signals = _Signals()
        self.signals.log.connect(self._append_log)
        self.signals.status.connect(self._set_status)
        self._busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # --- Title ---
        title = QLabel("Grab Test Demo")
        title.setObjectName("panelTitle")
        title.setStyleSheet(f"font-size: 16px; color: {PALETTE['text_heading']};")
        root.addWidget(title)

        # --- Methods panel ---
        methods_panel = QFrame()
        methods_panel.setObjectName("panel_methods")
        methods_panel.setFrameShape(QFrame.Shape.StyledPanel)
        mp_layout = QVBoxLayout(methods_panel)
        mp_layout.setSpacing(4)

        mp_title = QLabel("Select methods to test:")
        mp_title.setObjectName("panelTitle")
        mp_layout.addWidget(mp_title)

        self.checkboxes: list[tuple[QCheckBox, callable]] = []
        for name, func in GRAB_METHODS:
            cb = QCheckBox(name)
            cb.setChecked(True)
            mp_layout.addWidget(cb)
            self.checkboxes.append((cb, func))

        root.addWidget(methods_panel)

        # --- Buttons ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.run_btn = QPushButton("Run Selected Grabs")
        self.run_btn.setObjectName("ctaButton")
        self.run_btn.clicked.connect(self._on_run)
        btn_row.addWidget(self.run_btn)

        self.run_all_btn = QPushButton("Run All")
        self.run_all_btn.clicked.connect(self._on_run_all)
        btn_row.addWidget(self.run_all_btn)

        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.clicked.connect(lambda: self.log_box.clear())
        btn_row.addWidget(self.clear_btn)

        self.open_btn = QPushButton("Open Output File")
        self.open_btn.clicked.connect(self._open_output)
        btn_row.addWidget(self.open_btn)

        root.addLayout(btn_row)

        # --- Status ---
        status_row = QHBoxLayout()
        self.status_dot = QLabel("\u2B24")
        self.status_dot.setFixedWidth(18)
        self.status_dot.setStyleSheet(f"font-size: 10px; color: {PALETTE['text_label']};")
        status_row.addWidget(self.status_dot)
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(f"color: {PALETTE['text_label']};")
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        root.addLayout(status_row)

        # --- Log output ---
        self.log_box = QTextEdit()
        self.log_box.setObjectName("outputBox")
        self.log_box.setReadOnly(True)
        root.addWidget(self.log_box, stretch=1)

    # ----- actions -----
    def _on_run_all(self):
        for cb, _ in self.checkboxes:
            cb.setChecked(True)
        self._on_run()

    def _on_run(self):
        if self._busy:
            return
        selected = [(cb.text(), func) for cb, func in self.checkboxes if cb.isChecked()]
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Check at least one grab method.")
            return

        self._busy = True
        self.run_btn.setEnabled(False)
        self.run_all_btn.setEnabled(False)
        self.signals.status.emit("Running …", PALETTE["accent_start"])

        # Run grabs sequentially on a background thread
        def worker():
            results: list[tuple[str, str]] = []
            for name, func in selected:
                try:
                    label, text = func(self.signals)
                    results.append((label, text))
                    self.signals.log.emit(f"  ✓ {name} complete\n")
                except Exception as exc:
                    self.signals.log.emit(f"  ✗ {name} FAILED: {exc}\n")
                    results.append((f"{name} (FAILED)", f"ERROR: {exc}"))

            # Write output file
            self._write_results(results)
            self.signals.status.emit("Done — results written to grab_tests.txt", PALETTE["status_ok"])
            self.signals.log.emit(f"Results written to: {OUTPUT_FILE}\n")
            self._busy = False
            self.run_btn.setEnabled(True)
            self.run_all_btn.setEnabled(True)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _write_results(self, results: list[tuple[str, str]]):
        """Append labelled sections to grab_tests.txt."""
        separator = "=" * 72
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{separator}\n")
            f.write(f"  Grab Test Run — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{separator}\n\n")
            for label, text in results:
                f.write(f"[{label}]\n")
                f.write(f"--- {len(text)} characters ---\n")
                f.write(text)
                f.write(f"\n\n{'─' * 72}\n\n")

    def _open_output(self):
        if os.path.exists(OUTPUT_FILE):
            os.startfile(OUTPUT_FILE)
        else:
            QMessageBox.information(self, "No file yet", "Run a grab first to create the output file.")

    # ----- UI helpers -----
    def _append_log(self, msg: str):
        self.log_box.append(msg)
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _set_status(self, msg: str, color: str):
        self.status_label.setText(msg)
        self.status_dot.setStyleSheet(f"font-size: 10px; color: {color};")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(build_qss())
    win = GrabTestWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
