---
applyTo: '**/{input_GUI_w_fixed_autoclick_closed_loop.pyw,grab_points.py,emr_assist/core/parsers.py}'
description: "Use when changing EMR data grabs, selector ranking, ad hoc selector persistence, parser normalization, or visit-specific variable extraction."
---

# EMR Data-Grab Pathway Instructions

Use this file when changing any part of the EMR grab pipeline, including user-facing Grab actions, selector ranking, ad hoc selector acceptance, parser normalization, or selector-spec publication.

## Scope and Ownership

- The data-grab pipeline is shared across multiple visit types and multiple trigger paths. Treat it as one system, not a set of isolated one-off handlers.
- The primary orchestration lives in `input_GUI_w_fixed_autoclick_closed_loop.pyw`.
- Ranked selector catalog, selector persistence, selector workbench decoration, and selector-spec generation live in `grab_points.py`.
- Low-level extraction helpers and normalization helpers also live in `emr_assist/core/parsers.py`.
- Changes to any one of these files can break the others even when the edited code looks local.

## Primary Pathways

The current grab architecture has five linked stages. Preserve the boundaries between them.

### 1. User-facing grab entry points

- `grab_sexual_health()`
- `grab_hair()`
- `grab_photoaging()`
- `grab_performance_anxiety()`
- `grab_birth_control()`
- Their browser-driven counterparts such as `grab_sexual_health_data()`, `grab_hair_loss_data()`, `grab_performance_anxiety_data()`, and `grab_birth_control_data()`

Rules:

- User-facing grab actions should continue to route through shared helpers rather than introducing one-off extraction code inside button handlers.
- Before a new browser-driven grab, preserve the existing pattern of verifying the EMR tab and resetting text caches.
- When the overlay is active, preserve the Playwright-first text-grab behavior to avoid clipboard focus side effects.

### 2. Text acquisition

- `_grab_emr_text_for_parsing()` is the user-facing text source selector.
- `_get_ranked_playwright_raw_texts()` is the narrow selector text collector.
- `_get_all_text_across_frames()` and `_get_sh_wide_fallback_text()` are the wide fallback collectors.
- `_get_cdp_emr_page()` owns cached Playwright page reuse.

Rules:

- Preserve the current order: narrow selector lookup first, wide text fallback later.
- Do not remove the wide fallback path just because ranked selectors exist.
- Keep frame and page access defensive. Stale frames and dead Playwright pages must fail soft and continue to the next fallback.
- Do not assume selector text will always include all needed context. Some normalizers need nearby or wide page text to infer cadence or meaning.

### 3. Ranked selector resolution

- `get_ranked_playwright_selectors(group, key)` defines the effective selector order.
- `_resolve_ranked_playwright_value()` executes the selector chain.
- `_parse_ranked_selector_candidate()` determines parser acceptance.

Rules:

- Preserve the selector merge order: persisted overrides first, eligible ad hoc log history second, built-in defaults last.
- Never let a partial override wipe out built-in fallback selectors.
- Preserve de-duplication without changing ranking order.
- A parser rejection must not abort the whole variable. The next candidate or the wide fallback must still run.

### 4. Parsing and normalization

- Per-variable parsing is split between shared parsers and monolith-local normalization helpers.
- Medication parsing is especially sensitive because it trims the raw line and may append cadence such as `daily` or `as-needed`.
- Normalized variable values must still pass through the parser and per-variable normalization path even when a ranked selector hit already found text.

Rules:

- Do not return raw selector text directly for variables that already have normalization logic.
- Preserve the distinction between raw selector text, parser output, fallback normalization, and final emitted field value.
- If a selector only yields the short medication title, preserve the use of wider page context so cadence inference still works.
- If you touch medication parsing, test both cases where the selector text contains cadence and where cadence must be inferred from nearby or wide text.
- Keep Yes/No, blood pressure, multi-select options, and medication normalization centralized instead of duplicating slightly different versions in each handler.

### 5. Selector persistence and overlay publication

- `_save_selector_override_from_overlay()` manages ranked selector CRUD from the overlay workbench.
- `_handle_ad_hoc_selector_grab_accept()` manages ad hoc selector acceptance, optional persistence, logging, and bridge emission.
- `_overlay_eval_push_selector_specs()` republishes selector specs into the JS overlay.
- `get_variable_selector_specs()` and `_decorate_selector_spec()` define what the overlay sees.

Rules:

- Preserve the semantic difference between `Use Once` and `Use + Save`.
- Entries with `persist_selector=false` must not affect future ranked lookup.
- Legacy ad hoc log entries without an explicit persistence flag remain eligible as learned selectors.
- After changing selector persistence state, continue to republish selector specs so the overlay reflects the current ranked list.
- Keep selector-spec decoration intact: ranked selectors, configured selectors, parser rules, labels, keywords, and anchors must all survive decoration.

## High-Risk Regression Seams

Future edits should assume these seams are fragile.

- Changing `get_ranked_playwright_selectors()` can silently break all saved selector reuse.
- Changing `_resolve_ranked_playwright_value()` can make selectors appear to work while dropping values after parser rejection.
- Changing medication normalization can reintroduce raw untrimmed values or lose cadence suffixes.
- Changing visit labels, canonicalization, or selector-spec publication can make saved selectors exist but never apply for the active visit.
- Changing ad hoc logging or persistence flags can corrupt the boundary between one-time use and future reuse.
- Changing field maps or `var_id` / `group` / `key` relationships can make accepted values save successfully but never populate the expected control.

## Required Invariants

Preserve these invariants unless the change explicitly re-architects the system.

- A ranked selector hit is not the final output. The value must still pass through the correct parser and normalization path.
- A parser failure on one selector candidate must not stop later selector candidates or the wide-text fallback.
- Built-in selectors remain part of the ranked list even when overrides or logged selectors exist.
- Selector history loaded from `ad_hoc_selector_log.jsonl` must ignore entries that explicitly opted out of persistence.
- `get_variable_selector_specs()` must continue to decorate specs with ranked selectors and parser metadata for the overlay.
- Ad hoc acceptance must continue to validate `var_id` against `get_variable_selector_spec()` before saving or emitting.
- Overlay page mutations stay on the overlay thread. wx-thread code should queue overlay updates instead of calling page APIs directly.

## Required Files To Review Before Editing

When touching the grab pipeline, read the neighboring control points first.

- `input_GUI_w_fixed_autoclick_closed_loop.pyw`
- `grab_points.py`
- `emr_assist/core/parsers.py`
- `selector_overrides.json` when debugging persistence behavior
- `selector_workbench.json` when debugging custom variable or parser-rule metadata
- `ad_hoc_selector_log.jsonl` when debugging learned selector reuse or one-time vs persistent behavior

## Change Checklist

Before editing:

- Identify whether the change affects entry points, text acquisition, selector ranking, parsing, persistence, or overlay publication.
- Find the shared helper that owns that behavior before changing a visit-specific handler.
- If medication, BP, Yes/No, or multi-select normalization is involved, compare the ranked-selector path and the ad hoc accept path.

After editing:

- Run a focused syntax check on touched Python files.
- Do a live validation on at least one affected visit type.
- Confirm from logs whether the value was accepted from a ranked selector, a combined selector candidate, or the wide fallback.
- If selector persistence changed, verify the overlay spec list refreshes and the saved selector appears in the ranked list.
- If ad hoc behavior changed, verify both `Use Once` and `Use + Save` semantics still behave differently.

## Preferred Validation Signals

- A ranked-selector success log should show both the raw candidate and the normalized parsed value.
- Medication variables should show the trimmed medication line with cadence suffix when appropriate.
- The overlay variable push should include the expected non-empty variable ids after a successful grab.
- A missing narrow-selector hit is acceptable only if the wide fallback still fills the variable correctly.

## Anti-Patterns To Avoid

- Do not add a new visit-specific parser path when an existing shared helper already owns that normalization.
- Do not replace the ranked selector merge with override-only behavior.
- Do not treat the ad hoc selector log as disposable if you still rely on it for learned-selector reuse.
- Do not make overlay UI updates without republishing selector specs after persistence changes.
- Do not trust a live selector hit unless the final normalized field value is also correct.
