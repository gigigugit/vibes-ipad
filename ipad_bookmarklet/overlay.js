(() => {
  const ROOT_ID = 'vibes-ipad-phase0-spike';
  const GLOBAL_KEY = '__vibesIpadPhase0Spike';
  const LOADER_ID = 'vibes-ipad-phase0-loader';
  const TITLE_SELECTORS = [
    '[data-testid="medication-title"]',
    '[data-testid="treatment-0"] [data-testid="medication-title"]',
  ];
  const DETAIL_SELECTORS = [
    '[data-testid="medication-text"]',
    '[data-testid="treatmentPlan"]',
    '[data-testid="proposedTreatmentPlan"]',
  ];

  const existingState = window[GLOBAL_KEY];
  if (existingState && typeof existingState.destroy === 'function') {
    try {
      existingState.destroy();
    } catch (error) {
      console.warn('Phase 0 spike cleanup failed:', error);
    }
  }

  const existingRoot = document.getElementById(ROOT_ID);
  if (existingRoot) {
    existingRoot.remove();
  }

  const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();

  const extractTextFromSelector = (selector) => {
    const node = document.querySelector(selector);
    if (!node) {
      return '';
    }
    if (typeof node.value === 'string' && normalizeText(node.value)) {
      return normalizeText(node.value);
    }
    return normalizeText(node.innerText || node.textContent || '');
  };

  const findFirstMatchingSelector = (selectors) => {
    for (const selector of selectors) {
      const value = extractTextFromSelector(selector);
      if (value) {
        return { selector, value };
      }
    }
    return { selector: '', value: '' };
  };

  const buildMedicationValue = (titleValue, detailValue) => {
    if (titleValue && detailValue && !detailValue.includes(titleValue)) {
      return `${titleValue} — ${detailValue}`;
    }
    return titleValue || detailValue || '';
  };

  const extractMedication = () => {
    const title = findFirstMatchingSelector(TITLE_SELECTORS);
    const detail = findFirstMatchingSelector(DETAIL_SELECTORS);
    const medication = normalizeText(buildMedicationValue(title.value, detail.value));
    return {
      medication,
      found: Boolean(medication),
      titleSelector: title.selector,
      detailSelector: detail.selector,
      copiedText: medication ? `the medication is: ${medication}` : '',
    };
  };

  const root = document.createElement('div');
  root.id = ROOT_ID;
  root.innerHTML = `
    <style>
      #${ROOT_ID} {
        position: fixed;
        top: 12px;
        right: 12px;
        z-index: 2147483647;
        width: min(360px, calc(100vw - 24px));
        font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #e5f0ff;
      }
      #${ROOT_ID} * {
        box-sizing: border-box;
      }
      #${ROOT_ID} .phase0-card {
        background: rgba(7, 20, 39, 0.96);
        border: 1px solid rgba(59, 211, 255, 0.45);
        border-radius: 14px;
        box-shadow: 0 12px 28px rgba(0, 0, 0, 0.35);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        overflow: hidden;
      }
      #${ROOT_ID} .phase0-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 10px 12px;
        border-bottom: 1px solid rgba(59, 211, 255, 0.18);
      }
      #${ROOT_ID} .phase0-title {
        font-weight: 700;
        color: #93c0ff;
      }
      #${ROOT_ID} .phase0-subtitle {
        font-size: 11px;
        color: #94a9c6;
      }
      #${ROOT_ID} .phase0-close {
        border: 0;
        background: transparent;
        color: #cfe7ff;
        font-size: 18px;
        line-height: 1;
        cursor: pointer;
      }
      #${ROOT_ID} .phase0-body {
        padding: 12px;
        display: grid;
        gap: 10px;
      }
      #${ROOT_ID} .phase0-label {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #94a9c6;
      }
      #${ROOT_ID} .phase0-value,
      #${ROOT_ID} .phase0-meta {
        padding: 10px;
        border-radius: 10px;
        background: rgba(15, 34, 51, 0.92);
        border: 1px solid rgba(59, 211, 255, 0.16);
        word-break: break-word;
      }
      #${ROOT_ID} .phase0-meta {
        font-size: 11px;
        color: #94a9c6;
      }
      #${ROOT_ID} .phase0-actions {
        display: flex;
        gap: 8px;
      }
      #${ROOT_ID} .phase0-button {
        flex: 1;
        min-height: 40px;
        border: 1px solid rgba(59, 211, 255, 0.28);
        border-radius: 10px;
        background: #11293a;
        color: #e5f0ff;
        font: inherit;
        cursor: pointer;
      }
      #${ROOT_ID} .phase0-button:disabled {
        opacity: 0.45;
        cursor: default;
      }
      #${ROOT_ID} .phase0-status {
        font-size: 12px;
        color: #a9c5e8;
      }
    </style>
    <div class="phase0-card" role="dialog" aria-label="Vibes iPad Phase 0 spike">
      <div class="phase0-header">
        <div>
          <div class="phase0-title">Helper loaded</div>
          <div class="phase0-subtitle">Phase 0 iPad Chrome feasibility spike</div>
        </div>
        <button class="phase0-close" type="button" aria-label="Close spike overlay">×</button>
      </div>
      <div class="phase0-body">
        <div>
          <div class="phase0-label">Medication</div>
          <div class="phase0-value" data-role="medication-value">Checking page…</div>
        </div>
        <div>
          <div class="phase0-label">Selector status</div>
          <div class="phase0-meta" data-role="selector-meta">Waiting for first read…</div>
        </div>
        <div class="phase0-actions">
          <button class="phase0-button" type="button" data-action="refresh">Refresh</button>
          <button class="phase0-button" type="button" data-action="copy">Copy note</button>
        </div>
        <div class="phase0-status" data-role="status">Injected successfully.</div>
      </div>
    </div>
  `;

  const medicationValueNode = root.querySelector('[data-role="medication-value"]');
  const selectorMetaNode = root.querySelector('[data-role="selector-meta"]');
  const statusNode = root.querySelector('[data-role="status"]');
  const refreshButton = root.querySelector('[data-action="refresh"]');
  const copyButton = root.querySelector('[data-action="copy"]');
  const closeButton = root.querySelector('.phase0-close');

  let currentResult = extractMedication();
  let refreshTimer = null;
  let observer = null;

  const render = () => {
    medicationValueNode.textContent = currentResult.found ? currentResult.medication : 'Medication not found yet.';
    selectorMetaNode.textContent = currentResult.found
      ? `title: ${currentResult.titleSelector || 'none'}\ndetail: ${currentResult.detailSelector || 'none'}`
      : `No medication selector match yet.\nTried: ${[...TITLE_SELECTORS, ...DETAIL_SELECTORS].join(', ')}`;
    statusNode.textContent = currentResult.found
      ? 'Medication read succeeded. Copy is ready.'
      : 'Injection passed, but medication has not been read from the current page state.';
    copyButton.disabled = !currentResult.found;
  };

  const refresh = () => {
    currentResult = extractMedication();
    render();
  };

  const scheduleRefresh = () => {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(refresh, 150);
  };

  const copyToClipboard = async () => {
    if (!currentResult.found) {
      return;
    }
    const text = currentResult.copiedText;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', 'readonly');
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        document.execCommand('copy');
        textarea.remove();
      }
      statusNode.textContent = `Copied: ${text}`;
    } catch (error) {
      statusNode.textContent = `Copy failed: ${String(error?.message || error || 'Unknown error')}`;
    }
  };

  const destroy = () => {
    window.clearTimeout(refreshTimer);
    if (observer) {
      observer.disconnect();
    }
    const loader = document.getElementById(LOADER_ID);
    if (loader) {
      loader.remove();
    }
    root.remove();
    delete window[GLOBAL_KEY];
  };

  refreshButton.addEventListener('click', refresh);
  copyButton.addEventListener('click', () => {
    void copyToClipboard();
  });
  closeButton.addEventListener('click', destroy);

  document.documentElement.appendChild(root);
  render();

  if (document.body) {
    observer = new MutationObserver(scheduleRefresh);
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  window[GLOBAL_KEY] = { destroy, refresh };
})();
