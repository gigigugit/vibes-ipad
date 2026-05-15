(() => {
  const loaderLink = document.querySelector('[data-bookmarklet="loader"]');
  const loaderCode = document.querySelector('[data-output="loader"]');
  const inlineLink = document.querySelector('[data-bookmarklet="inline"]');
  const inlineCode = document.querySelector('[data-output="inline"]');
  const overlayUrlNode = document.querySelector('[data-output="overlay-url"]');
  const statusNode = document.querySelector('[data-output="status"]');

  const setCopyButton = (button, getValue) => {
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
        statusNode.textContent = `Copy failed: ${error && error.message ? error.message : error}`;
      }
    });
  };

  const overlayUrl = new URL('./overlay.js', window.location.href).href;
  const loaderBookmarklet = `javascript:(()=>{const d=document;const old=d.getElementById('vibes-ipad-phase0-loader');if(old)old.remove();const s=d.createElement('script');s.id='vibes-ipad-phase0-loader';s.src=${JSON.stringify(overlayUrl)}+'?t='+Date.now();(d.head||d.documentElement).appendChild(s);})();`;

  overlayUrlNode.textContent = overlayUrl;
  loaderLink.href = loaderBookmarklet;
  loaderCode.value = loaderBookmarklet;

  setCopyButton(document.querySelector('[data-copy="loader"]'), () => loaderCode.value);

  fetch('./overlay.js', { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) {
        throw new Error(`overlay fetch failed: ${response.status}`);
      }
      return response.text();
    })
    .then((overlaySource) => {
      const inlineBookmarklet = `javascript:(()=>{const d=document;const old=d.getElementById('vibes-ipad-phase0-loader');if(old)old.remove();const s=d.createElement('script');s.id='vibes-ipad-phase0-loader';s.textContent=${JSON.stringify(overlaySource)};(d.head||d.documentElement).appendChild(s);s.remove();})();`;
      inlineLink.href = inlineBookmarklet;
      inlineCode.value = inlineBookmarklet;
      statusNode.textContent = 'Primary and fallback bookmarklets are ready.';
      setCopyButton(document.querySelector('[data-copy="inline"]'), () => inlineCode.value);
    })
    .catch((error) => {
      inlineLink.removeAttribute('href');
      inlineCode.value = '';
      statusNode.textContent = `Fallback bookmarklet unavailable: ${error && error.message ? error.message : error}`;
    });
})();
