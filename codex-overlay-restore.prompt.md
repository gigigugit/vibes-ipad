## Codex Prompt: Rich Overlay Restore Continuation

You are continuing a paused restore of the older rich JS overlay in this Windows repo:

`c:\Users\mattt\Documents\vibecoding\vibes-assist__copy with JS overlay`

Read these workspace files first and treat them as authoritative:
1. `c:\Users\mattt\Documents\vibecoding\vibes-assist__copy with JS overlay\.github\instructions\overlay-restore-handoff.instructions.md`
2. `c:\Users\mattt\Documents\vibecoding\vibes-assist__copy with JS overlay\.github\instructions\emr-data-grab.instructions.md`
3. `c:\Users\mattt\Documents\vibecoding\vibes-assist__copy with JS overlay\.github\instructions\emr-js-overlay.instructions.md`
4. `c:\Users\mattt\Documents\vibecoding\vibes-assist__copy with JS overlay\input_GUI_w_fixed_autoclick_closed_loop.pyw`
5. `c:\Users\mattt\Documents\vibecoding\vibes-assist__copy with JS overlay\grab_points.py`
6. `c:\Users\mattt\Documents\vibecoding\vibes-assist__copy with JS overlay\emr_assist\core\parsers.py`

The external salvage copy is the source of truth for the old richer overlay:
- `c:\Users\mattt\Documents\vibecoding\emr-assist-ad-hoc-selector-salvage-2026-04-24\input_GUI_w_fixed_autoclick_closed_loop.pyw`
- `c:\Users\mattt\Documents\vibecoding\emr-assist-ad-hoc-selector-salvage-2026-04-24\grab_points.py`
- `c:\Users\mattt\Documents\vibecoding\emr-assist-ad-hoc-selector-salvage-2026-04-24\emr_assist\core\parsers.py`

Current verified state:
- The current workspace only has the compact restored overlay, not the rich old UI.
- The compact overlay already works as a temporary baseline and includes overlay thread/queue plumbing.
- No actual rich-overlay restore patch has been applied yet.
- The current root-level `grab_points.py` is too stripped down for the old selector workbench UI.
- The current shared `parsers.py` is missing salvage-era parser/helper functions needed by selector preview and normalization paths.
- The repo worktree is dirty with many unrelated changes. Do not revert unrelated work and do not broaden scope into unrelated files.

Your task:
Continue the rich-overlay restore safely and incrementally from the salvage copy.

Hard constraints:
- Do NOT blindly replace the whole monolith with the salvage monolith.
- Do NOT emulate missing selector backend APIs inside the monolith if the salvage `grab_points.py` can be restored instead.
- Do NOT duplicate shared parser logic in multiple overlay callbacks.
- Do NOT touch unrelated dirty files unless absolutely required by the overlay restore.
- Preserve the overlay-thread rule: only the overlay-owned thread may call `page.evaluate()` or mutate the EMR DOM/CSS.
- Preserve the template insertion nuance:
  - `insert_source == "overlay_ctx_menu"` suppresses the synthetic Enter before paste
  - proactive refill quick templates remain the exception and should still prepend Enter
- Preserve the selector/normalization rule:
  - a selector hit is not the final value
  - selector save/preview paths must still run through the appropriate parser/normalization logic

Required restore order:
1. Compare current `grab_points.py` against the salvage version and restore the selector backend first.
2. Run focused syntax validation after that change before touching the monolith.
3. Port the missing shared parser helpers from the salvage `parsers.py` into `emr_assist/core/parsers.py`.
4. Validate again.
5. Only then port the richer overlay blocks and support methods from the salvage monolith into `input_GUI_w_fixed_autoclick_closed_loop.pyw`.
6. Reconcile compatibility seams:
   - selector spec publication / refresh
   - payroll state and helper methods if needed
   - keep-awake controls if present in salvage overlay
   - template insertion paths
   - callback signatures and bridge plumbing
7. Validate again, then do a focused runtime smoke test.

Functional target:
Restore the richer old overlay UI from salvage, including as much of the following as is still supportable:
- floating mini-bar
- richer vars panel behavior
- richer template context menu
- selector panel / selector workbench
- notepad panel
- keep-awake controls
- dashboard payroll panel
- richer dark-mode site styling layer
- additional overlay queue commands and helper methods

Validation sequence:
1. Syntax:
   `& ".venv/Scripts/python.exe" -m py_compile "input_GUI_w_fixed_autoclick_closed_loop.pyw" "grab_points.py" "emr_assist/core/parsers.py"`
2. Runtime smoke:
   `& ".venv/Scripts/python.exe" "input_GUI_w_fixed_autoclick_closed_loop.pyw"`

After the rich overlay is back, verify at minimum:
- the richer overlay renders instead of the compact-only toolbar
- selector specs populate in the selector panel
- saving a selector refreshes the overlay selector list
- ad hoc acceptance still distinguishes `Use Once` vs `Use + Save`
- overlay template insertion still respects the `overlay_ctx_menu` enter-suppression rule

When you respond, give:
1. A concise summary of what you changed
2. Any remaining gaps or blockers
3. Exact validation results
4. Any risks of regression that still need live testing

Do not waste time restating the problem broadly. Start from the current local files and salvage sources, make the smallest targeted restore slice that can be validated, and proceed in that dependency-first order.