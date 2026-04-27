---
applyTo: '**/{input_GUI_w_fixed_autoclick_closed_loop.pyw,grab_points.py,emr_assist/core/parsers.py}'
description: "Use when resuming restoration of the richer JS overlay from the external salvage copy, including the selector workbench, context menu, payroll/notepad panels, keep-awake controls, and their selector/parser support code."
---

# JS Overlay Restore Handoff Instructions

Use this file when resuming the paused effort to restore the older, richer JS overlay into the current workspace after the selector/ad-hoc rollback.

## Goal

- Restore the old rich JS overlay that the user preferred.
- Do it by selectively merging from the external salvage copy, not by blindly replacing the entire current monolith.
- Restore enough backend support that the rich overlay is functional again, especially the selector workbench and overlay-driven quick actions.

## Authoritative Sources

Treat these files as the main source of truth for the paused restore:

- Current workspace monolith: `input_GUI_w_fixed_autoclick_closed_loop.pyw`
- Current workspace selector backend: `grab_points.py`
- Current workspace parser helpers: `emr_assist/core/parsers.py`
- External salvage monolith: `C:\Users\mattt\Documents\vibecoding\emr-assist-ad-hoc-selector-salvage-2026-04-24\input_GUI_w_fixed_autoclick_closed_loop.pyw`
- External salvage selector backend: `C:\Users\mattt\Documents\vibecoding\emr-assist-ad-hoc-selector-salvage-2026-04-24\grab_points.py`
- External salvage parser helpers: `C:\Users\mattt\Documents\vibecoding\emr-assist-ad-hoc-selector-salvage-2026-04-24\emr_assist\core\parsers.py`

## Verified Current State

- The current workspace does not contain the old rich overlay anymore.
- The current monolith contains only the compact restored overlay shell and its plumbing.
- The compact overlay currently has:
  - visit display
  - template dropdown and insert button
  - status line
  - grabbed-vars panel
  - buttons for grab, detect visit, dashboard autoclicker, in-visit autoclicker, dark mode, minimize, Python UI, and close
- The current monolith still has the overlay lifecycle and queue scaffolding:
  - `OVERLAY_JS`
  - `_js_overlay_cmd_queue`
  - `inject_js_overlay`
  - `_overlay_thread_run`
  - `_register_overlay_exposed_functions`
  - `_remove_js_overlay`
  - `_emr_bridge_hook`
- The current root-level `grab_points.py` is a stripped placeholder and is not sufficient for the old selector workbench UI.
- The current shared parser module is missing later salvage helpers that the selector-preview and normalization paths relied on.
- During the paused restore investigation, no new restore patch had been applied yet. The work stopped before replacing the selector backend or porting the rich overlay blocks.

## What Exists Only In The External Salvage Copy

The old rich overlay features still exist in the external salvage copy and should be recovered from there rather than re-created from scratch.

Confirmed salvage-only overlay features include:

- floating mini-bar
- vars collapse/expand behavior beyond the compact grid
- richer template context menu
- selector panel / selector workbench
- notepad panel
- keep-awake controls and click rotation UI
- dashboard payroll panel
- richer dark-mode site styling layer
- additional overlay queue commands and helper methods

## Verified Dependency Gaps

The rich overlay was coupled to backend code that is no longer present in the current workspace baseline.

### Selector backend gap

- The rich overlay expected the root-level `grab_points.py` to provide selector/workbench functions such as:
  - `get_variable_selector_specs`
  - `get_variable_selector_spec`
  - selector ranking and persistence helpers
  - selector workbench decoration and parser metadata helpers
- The current placeholder `grab_points.py` does not provide that API surface.
- The salvage `grab_points.py` was inspected and appeared self-contained enough to restore as a file rather than mocking the API in the monolith.

### Parser helper gap

- The rich overlay and selector preview path expected later parser/normalization helpers that are missing from the current `emr_assist/core/parsers.py`.
- The current parser module did not already contain those salvage-era helper tails.
- The planned restore direction was to port only the missing shared parser helpers into `emr_assist/core/parsers.py`, not to duplicate parser logic inside the monolith.

### Monolith compatibility gap

- The current monolith is missing many of the richer overlay support methods and constants.
- The restore likely needs additional frame state for:
  - keep-awake
  - payroll panel state
  - selector/workbench overlay state
  - richer quick-template / context-menu flows

## Important Compatibility Notes Already Discovered

### Template insertion nuance

- Overlay template insertion needs special handling for context-menu insertions.
- Preserve the known rule:
  - `insert_source == "overlay_ctx_menu"` suppresses the synthetic Enter before paste.
  - Proactive refill quick templates are the exception and should still prepend Enter so they land on the next line.
- This behavior was already captured in repo memory and should not be lost during the restore.

### Overlay-thread rule

- Only the overlay thread may call `page.evaluate()` or perform Playwright DOM/CSS mutations.
- wx-thread and grab-thread code must continue to communicate through `_js_overlay_cmd_queue`.
- Do not reintroduce direct Playwright page mutations from wx callbacks while restoring richer UI features.

### Selector/normalization rule

- A selector hit is not the final value.
- The rich overlay preview and selector-save paths must continue to run through the appropriate parser and normalization logic.
- Do not restore the selector UI in a way that bypasses parser rules, medication cadence normalization, Yes/No normalization, or option mapping.

## Recommended Resume Order

Resume in this order unless there is a concrete reason to deviate.

### 1. Restore the selector backend first

- Compare current `grab_points.py` against the salvage version.
- Prefer restoring the salvage root-level selector backend as a file or near-file transplant.
- Validate imports and syntax before touching the monolith.

### 2. Restore the missing shared parser helpers

- Port the missing salvage helpers into `emr_assist/core/parsers.py`.
- Keep parser logic centralized in the shared parser module where possible.
- Do not paper over missing parser helpers by hard-coding alternate logic inside the overlay methods.

### 3. Restore the richer overlay blocks in the monolith

- Port the richer `OVERLAY_JS` block from the salvage monolith.
- Port the richer overlay helper methods, queue handlers, and exposed callbacks that the old UI needs.
- Restore missing frame state initialization needed by those helpers.

### 4. Reconcile monolith-side compatibility seams

- Check overlay template insertion paths.
- Check payroll save/load helpers.
- Check keep-awake start/stop helpers.
- Check selector-spec publication and refresh after persistence changes.
- Check any callback signatures that changed between the rollback baseline and the salvage copy.

### 5. Only then do live validation

- Start with syntax/compile validation.
- Then perform a focused runtime smoke test.
- Then verify one selector-workbench path and one overlay quick-template path.

## Preferred Validation Sequence

Run these checks after each meaningful restore slice.

### Syntax / compile

Use the workspace virtual environment:

```powershell
& ".venv/Scripts/python.exe" -m py_compile "input_GUI_w_fixed_autoclick_closed_loop.pyw" "grab_points.py" "emr_assist/core/parsers.py"
```

### Runtime smoke check

```powershell
& ".venv/Scripts/python.exe" "input_GUI_w_fixed_autoclick_closed_loop.pyw"
```

Look for:

- no immediate overlay startup exception
- overlay injection still succeeds
- no missing-method crash on frame initialization

### Functional checks after the rich overlay is back

- Verify the richer overlay renders instead of the compact-only toolbar.
- Verify selector specs populate in the selector panel.
- Verify saving a selector refreshes the overlay selector list.
- Verify ad hoc acceptance still distinguishes `Use Once` vs `Use + Save`.
- Verify a template inserted from the overlay context menu respects the special enter-suppression rule.

## High-Risk Regression Areas

- Replacing the entire monolith with the salvage monolith may wipe out post-rollback fixes that were intentionally kept.
- Restoring only the overlay HTML/CSS/JS without the selector backend will produce a UI shell with dead controls.
- Restoring selector UI without parser helpers will make preview/save behavior look functional while silently normalizing incorrectly.
- Reintroducing overlay features by calling Playwright directly from wx code will break the thread ownership model.
- Restoring quick-template features without the context-menu insertion rule will regress template placement.

## Anti-Patterns To Avoid

- Do not blindly overwrite `input_GUI_w_fixed_autoclick_closed_loop.pyw` with the salvage copy.
- Do not leave the placeholder `grab_points.py` in place and try to emulate the workbench API from inside the monolith.
- Do not duplicate salvage parser logic in multiple overlay callbacks.
- Do not treat the compact overlay as the target state; it is only the temporary restored baseline.
- Do not skip compile validation between restoring support files and restoring the monolith UI layer.

## Paused Session Stop Point

When this handoff file was written, the restore had reached this point:

- The external salvage files had been identified and inspected.
- The current workspace had been confirmed to contain only the compact overlay.
- The root-level selector backend had been identified as missing and likely restorable from the salvage file.
- The shared parser helper tail had been identified as missing and likely restorable from the salvage parser file.
- The next intended action was to restore the selector backend first, then validate, then continue with parser helpers and the monolith overlay blocks.

## Required Files To Review Before Resuming

- `input_GUI_w_fixed_autoclick_closed_loop.pyw`
- `grab_points.py`
- `emr_assist/core/parsers.py`
- `selector_overrides.json`
- `selector_workbench.json`
- `ad_hoc_selector_log.jsonl`
- the external salvage copies of those same source files

## Resume Principle

The correct strategy is targeted reconstruction from the salvage copy, not a panic rollback and not a cosmetic-only overlay swap. Restore the backend surfaces first, then the richer UI, and validate each slice before widening scope.