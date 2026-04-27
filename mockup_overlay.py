"""
Mockup overlay — injects a non-functional visual mockup into the EMR page
so you can evaluate positioning, sizing, and layout before building the real thing.

Usage:
  1. Make sure Chrome is running with --remote-debugging-port=9222
  2. Navigate to the EMR page
  3. Run: python mockup_overlay.py
  4. The overlay will appear at the top of the page
  5. Press Enter in this terminal to remove the overlay and exit
"""

from playwright.sync_api import sync_playwright

CDP_PORT = 9222

MOCKUP_JS = r"""
(() => {
    // Remove existing mockup if re-running
    const existing = document.getElementById('emr-assist-overlay');
    if (existing) existing.remove();

    // --- Overlay container ---
    const overlay = document.createElement('div');
    overlay.id = 'emr-assist-overlay';
    overlay.innerHTML = `
    <style>
        #emr-assist-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            z-index: 100000;
            font-family: 'Segoe UI', Arial, sans-serif;
            font-size: 12px;
            pointer-events: auto;
        }
        #emr-assist-overlay * {
            box-sizing: border-box;
        }

        /* --- Main bar --- */
        .emr-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 5px 12px;
            background: rgba(10, 30, 60, 0.88);
            border-bottom: 2px solid #3bd3ff;
            backdrop-filter: blur(6px);
            -webkit-backdrop-filter: blur(6px);
        }

        /* --- Sections --- */
        .emr-section {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .emr-section-divider {
            width: 1px;
            height: 24px;
            background: rgba(59, 211, 255, 0.3);
            margin: 0 4px;
        }

        /* --- Visit type label --- */
        .emr-visit-label {
            color: #93c0ff;
            font-weight: 600;
            font-size: 12px;
            white-space: nowrap;
        }

        /* --- Buttons --- */
        .emr-btn {
            background: #0f2233;
            color: #cfe7ff;
            border: 1px solid #243948;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 11px;
            cursor: pointer;
            white-space: nowrap;
            font-family: inherit;
        }
        .emr-btn:hover {
            background: #1a3a52;
            border-color: #3bd3ff;
            color: #fff;
        }
        .emr-btn-accent {
            background: linear-gradient(180deg, #3bd3ff 0%, #12aee6 100%);
            color: #002233;
            font-weight: 700;
            border: 1px solid #3bd3ff;
        }
        .emr-btn-accent:hover {
            background: linear-gradient(180deg, #5de0ff 0%, #1ec4ff 100%);
        }

        /* --- Dropdown --- */
        .emr-select {
            background: #071427;
            color: #ddeefb;
            border: 1px solid #213244;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            min-width: 160px;
            font-family: inherit;
        }

        /* --- Close button --- */
        .emr-close {
            background: transparent;
            color: #7f93a4;
            border: none;
            font-size: 16px;
            cursor: pointer;
            padding: 2px 6px;
            line-height: 1;
            margin-left: auto;
        }
        .emr-close:hover {
            color: #ff5555;
        }

        /* --- Variables panel --- */
        .emr-vars-toggle {
            background: transparent;
            color: #7f93a4;
            border: none;
            font-size: 10px;
            cursor: pointer;
            padding: 2px 4px;
        }
        .emr-vars-toggle:hover {
            color: #3bd3ff;
        }
        .emr-vars-panel {
            background: rgba(7, 20, 39, 0.92);
            border-bottom: 1px solid rgba(59, 211, 255, 0.25);
            padding: 6px 14px;
            display: none;
            flex-wrap: wrap;
            gap: 4px 16px;
            font-size: 11px;
            max-height: 120px;
            overflow-y: auto;
        }
        .emr-vars-panel.emr-vars-open {
            display: flex;
        }
        .emr-var-item {
            color: #8ab4d8;
            white-space: nowrap;
        }
        .emr-var-item .emr-var-name {
            color: #5a8ab0;
        }
        .emr-var-item .emr-var-value {
            color: #cfe7ff;
            background: #0f2233;
            padding: 1px 5px;
            border-radius: 3px;
            border: 1px solid #1a3a52;
            font-family: Consolas, monospace;
            font-size: 10px;
        }

        /* --- Shortcut hints --- */
        .emr-shortcut {
            color: #5a7a94;
            font-size: 9px;
            margin-left: 2px;
        }
    </style>

    <!-- Main bar -->
    <div class="emr-bar">
        <!-- Visit type -->
        <div class="emr-section">
            <span class="emr-visit-label">Visit: Hair Loss</span>
            <button class="emr-btn" title="Detect visit type from EMR">Detect</button>
        </div>

        <div class="emr-section-divider"></div>

        <!-- Templates -->
        <div class="emr-section">
            <select class="emr-select">
                <option>Follow-up Note</option>
                <option>Initial Note</option>
                <option>Limited Check-in</option>
            </select>
            <button class="emr-btn emr-btn-accent">Insert</button>
        </div>

        <div class="emr-section-divider"></div>

        <!-- Grab -->
        <div class="emr-section">
            <button class="emr-btn emr-btn-accent">⟳ Grab<span class="emr-shortcut">F4</span></button>
        </div>

        <div class="emr-section-divider"></div>

        <!-- Variables toggle -->
        <div class="emr-section">
            <button class="emr-vars-toggle" id="emr-vars-toggle-btn" title="Show/hide grabbed variables">▼ Vars</button>
        </div>

        <!-- Spacer -->
        <div style="flex:1"></div>

        <!-- Right side -->
        <div class="emr-section">
            <button class="emr-btn" title="Return to Python UI">⮐ Python UI</button>
            <button class="emr-close" title="Close EMR Assist entirely">✕</button>
        </div>
    </div>

    <!-- Variables panel (collapsed by default) -->
    <div class="emr-vars-panel" id="emr-vars-panel">
        <div class="emr-var-item"><span class="emr-var-name">medication:</span> <span class="emr-var-value">finasteride 1mg daily</span></div>
        <div class="emr-var-item"><span class="emr-var-name">hair_response:</span> <span class="emr-var-value">improved</span></div>
        <div class="emr-var-item"><span class="emr-var-name">hair_symptoms:</span> <span class="emr-var-value">thinning at crown</span></div>
        <div class="emr-var-item"><span class="emr-var-name">hair_loss_location:</span> <span class="emr-var-value">vertex</span></div>
        <div class="emr-var-item"><span class="emr-var-name">diagnoses:</span> <span class="emr-var-value">Androgenetic alopecia</span></div>
    </div>
    `;

    document.body.prepend(overlay);

    // Recalculate page offset so overlay never covers content
    function updateBodyPadding() {
        document.body.style.paddingTop = overlay.offsetHeight + 'px';
    }

    // Toggle variables panel
    const toggleBtn = document.getElementById('emr-vars-toggle-btn');
    const varsPanel = document.getElementById('emr-vars-panel');
    if (toggleBtn && varsPanel) {
        toggleBtn.addEventListener('click', () => {
            varsPanel.classList.toggle('emr-vars-open');
            toggleBtn.textContent = varsPanel.classList.contains('emr-vars-open') ? '▲ Vars' : '▼ Vars';
            // Wait one frame for layout to recalculate, then update padding
            requestAnimationFrame(updateBodyPadding);
        });
    }

    // Initial push
    updateBodyPadding();

    return 'Mockup overlay injected';
})();
"""

REMOVE_JS = """
(() => {
    const el = document.getElementById('emr-assist-overlay');
    if (el) {
        el.remove();
        document.body.style.paddingTop = '';
        return 'Overlay removed';
    }
    return 'No overlay found';
})();
"""


def main():
    print(f"Connecting to Chrome on port {CDP_PORT}...")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        # Find the first visible page
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                page = p
                break
            if page:
                break

        if not page:
            print("No browser pages found. Is Chrome open?")
            return

        print(f"Connected to: {page.url[:80]}...")
        result = page.evaluate(MOCKUP_JS)
        print(f"Result: {result}")
        print()
        print("Overlay is now visible in the browser.")
        print("  - Click '▼ Vars' to expand the variables panel")
        print("  - Nothing is functional — this is layout/positioning only")
        print()
        input("Press Enter to remove the overlay and exit...")

        result = page.evaluate(REMOVE_JS)
        print(f"Cleanup: {result}")

    finally:
        pw.stop()


if __name__ == "__main__":
    main()
