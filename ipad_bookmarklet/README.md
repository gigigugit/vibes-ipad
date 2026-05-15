# iPad bookmarklet Phase 0 spike

This folder is an isolated browser experiment for the iPad Chrome feasibility gate.

## Files
- `index.html` — small installer page for the bookmarklet links.
- `bookmarklet.js` — generates the hosted loader bookmarklet and one inline fallback bookmarklet.
- `overlay.js` — injected overlay for the three micro-tests.

## Manual install
1. Host `ipad_bookmarklet/` as static files.
2. If you are not using `https://gigigugit.github.io/vibes-ipad/ipad_bookmarklet/`, update `DEFAULT_OVERLAY_URL` in `bookmarklet.js` before publishing.
3. Open `index.html` on the iPad.
4. Save the primary bookmarklet from the installer page.
5. If the hosted script is blocked on the EMR page, retry once with the inline fallback bookmarklet.

## Manual verification
1. **Injection pass**: run the bookmarklet on the live EMR in iPad Chrome and confirm the panel appears, can be closed, and can be relaunched.
2. **Read pass**: confirm the overlay reads the medication field on the current visit and on at least one different page state in the same visit flow.
3. **Action pass**: tap **Copy note** and confirm the clipboard contains `the medication is: <medication value>`.

## Scope limits
- This spike reads medication only.
- It does not depend on the Python runtime.
- It does not attempt broad parsing, template libraries, or direct insertion.
