(() => {
  // Update both constants if this spike is hosted anywhere other than the default GitHub Pages URL.
  const DEFAULT_OVERLAY_URL = 'https://gigigugit.github.io/vibes-ipad/ipad_bookmarklet/overlay.js';
  const LOADER_BOOKMARKLET = "javascript:(()=>{const d=document;const old=d.getElementById('vibes-ipad-phase0-loader');if(old)old.remove();const s=d.createElement('script');s.id='vibes-ipad-phase0-loader';s.src='https://gigigugit.github.io/vibes-ipad/ipad_bookmarklet/overlay.js?t='+Date.now();(d.head||d.documentElement).appendChild(s);})();";
  // Keep this fallback in sync with overlay.js if selector or copy behavior changes.
  const INLINE_FALLBACK_SOURCE = String.raw`(()=>{const R='vibes-ipad-phase0-inline';const old=document.getElementById(R);if(old)old.remove();const clean=v=>String(v||'').replace(/\s+/g,' ').trim();const first=s=>{for(const selector of s){const node=document.querySelector(selector);if(!node)continue;const value=clean(typeof node.value==='string'&&clean(node.value)?node.value:(node.innerText||node.textContent||''));if(value)return{selector,value};}return{selector:'',value:''};};const read=()=>{const title=first(['[data-testid="medication-title"]','[data-testid="treatment-0"] [data-testid="medication-title"]']);const detail=first(['[data-testid="medication-text"]','[data-testid="treatmentPlan"]','[data-testid="proposedTreatmentPlan"]']);const medication=clean(title.value&&detail.value&&!detail.value.includes(title.value)?title.value+' — '+detail.value:(title.value||detail.value||''));return{medication,found:Boolean(medication),selectors:[title.selector||'none',detail.selector||'none']};};const root=document.createElement('div');root.id=R;root.style.cssText='position:fixed;top:12px;right:12px;z-index:2147483647;max-width:min(320px,calc(100vw - 24px));padding:12px;border:1px solid rgba(59,211,255,.45);border-radius:14px;background:rgba(7,20,39,.96);color:#e5f0ff;font:13px/1.4 -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;box-shadow:0 12px 28px rgba(0,0,0,.35)';root.innerHTML='<div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><strong style="color:#93c0ff">Helper loaded</strong><button type="button" data-action="close" style="border:0;background:transparent;color:#cfe7ff;font-size:18px;line-height:1">×</button></div><div style="margin-top:10px;font-size:11px;color:#94a9c6">Inline fallback bookmarklet</div><div data-role="value" style="margin-top:10px;padding:10px;border-radius:10px;background:rgba(15,34,51,.92);border:1px solid rgba(59,211,255,.16)"></div><div data-role="meta" style="margin-top:8px;padding:10px;border-radius:10px;background:rgba(15,34,51,.92);border:1px solid rgba(59,211,255,.16);font-size:11px;color:#94a9c6;white-space:pre-wrap"></div><div style="display:flex;gap:8px;margin-top:10px"><button type="button" data-action="refresh" style="flex:1;min-height:40px;border:1px solid rgba(59,211,255,.28);border-radius:10px;background:#11293a;color:#e5f0ff">Refresh</button><button type="button" data-action="copy" style="flex:1;min-height:40px;border:1px solid rgba(59,211,255,.28);border-radius:10px;background:#11293a;color:#e5f0ff">Copy note</button></div><div data-role="status" style="margin-top:10px;font-size:12px;color:#a9c5e8"></div>';const valueNode=root.querySelector('[data-role="value"]');const metaNode=root.querySelector('[data-role="meta"]');const statusNode=root.querySelector('[data-role="status"]');const copyButton=root.querySelector('[data-action="copy"]');let current=read();const render=()=>{valueNode.textContent=current.found?current.medication:'Medication not found yet.';metaNode.textContent=current.found?'selectors: '+current.selectors.join(' / '):'Tried medication-title, medication-text, treatmentPlan, proposedTreatmentPlan.';statusNode.textContent=current.found?'Medication read succeeded. Copy is ready.':'Injection passed, but medication has not been read from the current page state.';copyButton.disabled=!current.found;copyButton.style.opacity=current.found?'1':'0.45';};const refresh=()=>{current=read();render();};root.querySelector('[data-action="refresh"]').addEventListener('click',refresh);root.querySelector('[data-action="copy"]').addEventListener('click',async()=>{if(!current.found)return;const text='the medication is: '+current.medication;try{if(navigator.clipboard&&typeof navigator.clipboard.writeText==='function'){await navigator.clipboard.writeText(text);}else{const t=document.createElement('textarea');t.value=text;t.setAttribute('readonly','readonly');t.style.position='fixed';t.style.opacity='0';document.body.appendChild(t);t.focus();t.select();document.execCommand('copy');t.remove();}statusNode.textContent='Copied: '+text;}catch(error){statusNode.textContent='Copy failed: '+(error&&error.message?error.message:error);}});root.querySelector('[data-action="close"]').addEventListener('click',()=>root.remove());document.documentElement.appendChild(root);render();})();`;
  const loaderLink = document.querySelector('[data-bookmarklet="loader"]');
  const loaderCode = document.querySelector('[data-output="loader"]');
  const inlineLink = document.querySelector('[data-bookmarklet="inline"]');
  const inlineCode = document.querySelector('[data-output="inline"]');
  const overlayUrlNode = document.querySelector('[data-output="overlay-url"]');
  const statusNode = document.querySelector('[data-output="status"]');

  const attachCopyHandler = (button, getValue) => {
    if (!button) {
      return;
    }
    button.addEventListener('click', async () => {
      const value = getValue();
      if (!value) {
        return;
      }
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = 'Copied';
        window.setTimeout(() => {
          button.textContent = 'Copy';
        }, 1200);
      } catch (error) {
        statusNode.textContent = `Copy failed: ${error instanceof Error ? error.message : String(error || 'Unknown error')}`;
      }
    });
  };

  overlayUrlNode.textContent = DEFAULT_OVERLAY_URL;
  loaderLink.href = LOADER_BOOKMARKLET;
  loaderCode.value = LOADER_BOOKMARKLET;

  attachCopyHandler(document.querySelector('[data-copy="loader"]'), () => loaderCode.value);

  const inlineBookmarklet = `javascript:${INLINE_FALLBACK_SOURCE}`;
  inlineLink.href = inlineBookmarklet;
  inlineCode.value = inlineBookmarklet;
  statusNode.textContent = 'Primary and fallback bookmarklets are ready.';
  attachCopyHandler(document.querySelector('[data-copy="inline"]'), () => inlineCode.value);
})();
